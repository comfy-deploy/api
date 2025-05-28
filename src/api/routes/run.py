import asyncio
import datetime
import os
from pprint import pprint
from urllib.parse import quote, urljoin
import uuid

from api.routes.machines import redeploy_machine_deployment_internal, redeploy_machine_internal
from api.routes.models import AVAILABLE_MODELS
from api.sqlmodels import WorkflowRunWebhookResponse
from .types import (
    CreateRunBatchResponse,
    CreateRunRequest,
    CreateRunResponse,
    DeploymentRunRequest,
    ModelRunRequest,
    RunStream,
    WorkflowRequestShare,
    WorkflowRunModel,
    WorkflowRunNativeOutputModel,
    WorkflowRunOutputModel,
    WorkflowRunRequest,
    WorkflowRunVersionRequest,
    WorkflowRunWebhookBody,
    # WorkflowWebhookModel,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Body
from fastapi.responses import StreamingResponse
import modal
from sqlalchemy import func, and_, or_, text, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from fastapi import BackgroundTasks
import fal_client
from .internal import insert_to_clickhouse, send_webhook
from .utils import (
    apply_org_check_direct,
    clean_up_outputs,
    ensure_run_timeout,
    execute_with_org_check,
    generate_temporary_token,
    get_user_settings,
    post_process_output_data,
    post_process_outputs,
    select,
)
from botocore.config import Config
import random
import aioboto3
from clickhouse_connect.driver.asyncclient import AsyncClient

import datetime as dt

# from sqlalchemy import select
from api.models import (
    GPUEvent,
    WorkflowRun,
    Deployment,
    Machine,
    WorkflowRunOutput,
    WorkflowRunWithExtra,
    WorkflowVersion,
    Workflow,
)
from api.database import get_db, get_clickhouse_client, get_db_context
from typing import Optional, Union, cast
from typing import Dict, Any
from uuid import UUID, uuid4
import logging
import logfire
import json
import httpx
from typing import Optional, List
from uuid import UUID
import base64
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Run"])
webhook_router = APIRouter(tags=["Callbacks"])


@webhook_router.post(
    "{$request.body#/webhook}",
    response_model=WorkflowRunWebhookResponse,
    summary="Run Update Webhook",
    description="This endpoint is called by the workflow runner to update the status of a run.",
)
async def run_update(
    body: WorkflowRunWebhookBody = Body(description="The updated run information"),
):
    # Implement the webhook update logic here
    pass


@router.get(
    "/run/{run_id}",
    response_model=WorkflowRunModel,
    openapi_extra={
        "x-speakeasy-name-override": "get",
    },
)
@router.get("/run", response_model=WorkflowRunModel, include_in_schema=False)
async def get_run(request: Request, run_id: UUID, queue_position: bool = False, db: AsyncSession = Depends(get_db)):
    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user.get("org_id", None)

    query = (
        select(WorkflowRunWithExtra)
        .options(joinedload(WorkflowRun.outputs))
        .where(WorkflowRun.id == run_id)
        # .apply_org_check(request)
    )

    result = await db.execute(query)
    run = result.unique().scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    
    deployment = None
    if run.deployment_id is not None:
        deployment = await db.execute(select(Deployment).where(Deployment.id == run.deployment_id))
        deployment = deployment.unique().scalar_one_or_none()

    # Permission check
    if deployment is not None and deployment.environment == "public-share":
        # Public share, no permission check
        if run.user_id == user_id:
            pass
        elif org_id is not None and run.org_id == org_id:
            pass
        else:
            apply_org_check_direct(deployment, request)
    else:
        apply_org_check_direct(run, request)

    run = cast(WorkflowRun, run)
    
    # Calculate queue position if requested and run has a machine_id
    if queue_position and run.machine_id is not None and run.status in ["not-started", "queued"]:
        # Count runs that are queued or not-started, created before this run, and for the same machine
        # Use a simple approach - just count runs that are in the queue and created before this one
        queue_position_query = select(func.count()).where(
            and_(
                or_(WorkflowRun.status == "not-started", WorkflowRun.status == "queued"),
                WorkflowRun.machine_id == run.machine_id,
                WorkflowRun.id != run.id,
                func.timezone('UTC', WorkflowRun.created_at) < func.timezone('UTC', run.created_at),
                func.timezone('UTC', WorkflowRun.created_at) >= func.timezone('UTC', datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24))
            )
        )
        queue_position_result = await db.execute(queue_position_query)
        queue_position = queue_position_result.scalar_one()
        
        # Add queue position to the run object
        setattr(run, "queue_position", queue_position)

    # If the run is not in a terminal state, check if the GPU event ended
    # logfire.info(str(run.status))
    if run.status not in ["success", "failed", "timeout", "cancelled"]:
        if run.gpu_event_id is not None:
            # Right now we are tracking session_id
            gpu_event = await db.execute(select(GPUEvent).where(GPUEvent.session_id == str(run.gpu_event_id)))
            gpu_event = gpu_event.unique().scalar_one_or_none()
            # logfire.info(run.gpu_event_id)
            # logfire.info(gpu_event)
            # GPU event ended, this run should have been ended
            if gpu_event and gpu_event.end_time is not None:
                run.status = "cancelled"
                await db.commit()

    user_settings = await get_user_settings(request, db)
    ensure_run_timeout(run)
    clean_up_outputs(run.outputs)
    await post_process_outputs(run.outputs, user_settings)
    # Convert the run to a dictionary and remove the run_log
    # run_dict = {k: v for k, v in vars(run).items() if k != "run_log"}

    return run.to_dict()


async def get_comfy_deploy_runner(machine_id: str, gpu: str, deployment: Optional[Deployment] = None, optimized_runner: bool = False):
    # Only when this deployment is using latest modal_image
    target_app_name = str(deployment.id if deployment is not None and deployment.modal_image_id is not None else machine_id)
    class_name = "ComfyDeployRunner" if not optimized_runner else "ComfyDeployRunnerOptimizedImports"
    try:
        ComfyDeployRunner = await modal.Cls.lookup.aio(target_app_name, class_name)
    except modal.exception.NotFoundError as e:
        logger.info(f"App not found for machine {target_app_name}, redeploying...")
        if deployment is not None:
            if deployment.modal_image_id is not None:
                await redeploy_machine_deployment_internal(deployment)
            else:
                logfire.error(f"App not found for machine {target_app_name}, and no modal_image_id found")
        else:
            # We are deploying the modal app as machine
            await redeploy_machine_internal(target_app_name)
        ComfyDeployRunner = await modal.Cls.lookup.aio(target_app_name, class_name)
            
    return ComfyDeployRunner.with_options(gpu=gpu if gpu != "CPU" else None)(gpu=gpu)

async def redeploy_comfy_deploy_runner_if_exists(machine_id: str, gpu: str, deployment: Optional[Deployment] = None):
    # Only when this deployment is using latest modal_image
    target_app_name = str(deployment.id if deployment is not None and deployment.modal_image_id is not None else machine_id)
    try:
        ComfyDeployRunner = await modal.Cls.lookup.aio(target_app_name, "ComfyDeployRunner")
    except modal.exception.NotFoundError as e:
        return
    
    logger.info(f"App found for machine {target_app_name}, redeploying...")
    if deployment is not None:
        if deployment.modal_image_id is not None:
            await redeploy_machine_deployment_internal(deployment)
        else:
            logfire.error(f"App found for machine {target_app_name}, and no modal_image_id found")
    else:
        await redeploy_machine_internal(target_app_name)
        
    ComfyDeployRunner = await modal.Cls.lookup.aio(target_app_name, "ComfyDeployRunner")
    return ComfyDeployRunner.with_options(gpu=gpu if gpu != "CPU" else None)(gpu=gpu)

@router.post(
    "/run",
    response_model=Union[
        CreateRunResponse,
        CreateRunBatchResponse,
        WorkflowRunOutputModel,
        WorkflowRunNativeOutputModel,
    ],
    summary="Run a workflow",
    description="Create a new workflow run with the given parameters. This function sets up the run and initiates the execution process. For callback information, see [Callbacks](#tag/callbacks/POST/\{callback_url\}).",
    callbacks=webhook_router.routes,
    include_in_schema=False,
    # openapi_extra={
    #     "x-speakeasy-name-override": "create",
    # },
)
async def create_run_all(
    request: Request,
    data: CreateRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    return await _create_run(request, data, background_tasks, db, client)


@router.post(
    "/run/deployment/queue",
    response_model=CreateRunResponse,
    summary="Queue Run",
    description="Create a new deployment run with the given parameters.",
    openapi_extra={
        "x-speakeasy-group": "run.deployment",
        "x-speakeasy-name-override": "queue",
    },
)
async def queue_deployment_run(
    request: Request,
    data: DeploymentRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    return await _create_run(request, data, background_tasks, db, client)


@router.post(
    "/run/deployment/sync",
    response_model=List[WorkflowRunOutputModel],
    summary="Deployment - Sync",
    description="Create a new deployment run with the given parameters.",
    openapi_extra={
        "x-speakeasy-group": "run.deployment",
        "x-speakeasy-name-override": "sync",
    },
)
async def sync_deployment_run(
    request: Request,
    data: DeploymentRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    data.execution_mode = "sync"
    return await _create_run(request, data, background_tasks, db, client)


@router.post(
    "/run/deployment/stream",
    response_model=RunStream,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Stream of workflow run events",
            "content": {
                "text/event-stream": {
                    "schema": {
                        "$ref": "#/components/schemas/RunStream",
                    },
                },
            },
        }
    },
    summary="Deployment - Stream",
    description="Create a new deployment run with the given parameters. This function sets up the run and initiates the execution process. For callback information, see [Callbacks](#tag/callbacks/POST/\{callback_url\}).",
    callbacks=webhook_router.routes,
    openapi_extra={
        "x-speakeasy-group": "run.deployment",
        "x-speakeasy-name-override": "stream",
    },
)
async def create_run_deployment_stream(
    request: Request,
    data: DeploymentRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    data.execution_mode = "stream"
    return await _create_run(request, data, background_tasks, db, client)


@router.post(
    "/run/workflow/queue",
    response_model=CreateRunResponse,
    summary="Workflow - Queue",
    description="Create a new workflow run with the given parameters.",
    openapi_extra={
        "x-speakeasy-group": "run.workflow",
        "x-speakeasy-name-override": "queue",
    },
)
async def queue_workflow_run(
    request: Request,
    data: WorkflowRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    return await _create_run(request, data, background_tasks, db, client)


@router.post(
    "/run/workflow/sync",
    response_model=List[WorkflowRunOutputModel],
    summary="Workflow - Sync",
    description="Create a new workflow run with the given parameters.",
    openapi_extra={
        "x-speakeasy-group": "run.workflow",
        "x-speakeasy-name-override": "sync",
    },
)
async def sync_workflow_run(
    request: Request,
    data: WorkflowRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    data.execution_mode = "sync"
    return await _create_run(request, data, background_tasks, db, client)


@router.post(
    "/run/workflow/stream",
    response_model=RunStream,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Stream of workflow run events",
            "content": {
                "text/event-stream": {
                    "schema": {
                        "$ref": "#/components/schemas/RunStream",
                    },
                },
            },
        }
    },
    summary="Workflow - Stream",
    description="Create a new workflow run with the given parameters. This function sets up the run and initiates the execution process. For callback information, see [Callbacks](#tag/callbacks/POST/\{callback_url\}).",
    callbacks=webhook_router.routes,
    openapi_extra={
        "x-speakeasy-group": "run.workflow",
        "x-speakeasy-name-override": "stream",
    },
)
async def create_run_workflow_stream(
    request: Request,
    data: WorkflowRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    data.execution_mode = "stream"
    return await _create_run(request, data, background_tasks, db, client)


@router.post(
    "/run/queue",
    deprecated=True,
    response_model=CreateRunResponse,
    summary="Queue a workflow",
    description="Create a new workflow run with the given parameters. This function sets up the run and initiates the execution process. For callback information, see [Callbacks](#tag/callbacks/POST/\{callback_url\}).",
    callbacks=webhook_router.routes,
    include_in_schema=False,
    openapi_extra={
        "x-speakeasy-name-override": "queue",
    },
)
async def create_run_queue(
    request: Request,
    data: CreateRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    return await _create_run(request, data, background_tasks, db, client)


@router.post(
    "/run/sync",
    response_model=List[WorkflowRunOutputModel],
    deprecated=True,
    summary="Run a workflow in sync",
    description="Create a new workflow run with the given parameters. This function sets up the run and initiates the execution process. For callback information, see [Callbacks](#tag/callbacks/POST/\{callback_url\}).",
    callbacks=webhook_router.routes,
    include_in_schema=False,
    openapi_extra={
        "x-speakeasy-name-override": "sync",
    },
)
async def create_run_sync(
    request: Request,
    data: CreateRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    data.execution_mode = "sync"
    return await _create_run(request, data, background_tasks, db, client)


@router.post(
    "/run/stream",
    response_model=RunStream,
    response_class=StreamingResponse,
    deprecated=True,
    include_in_schema=False,
    responses={
        200: {
            "description": "Stream of workflow run events",
            "content": {
                "text/event-stream": {
                    "schema": {
                        "$ref": "#/components/schemas/RunStream",
                    },
                },
            },
        }
    },
    summary="Run a workflow in stream",
    description="Create a new workflow run with the given parameters. This function sets up the run and initiates the execution process. For callback information, see [Callbacks](#tag/callbacks/POST/\{callback_url\}).",
    callbacks=webhook_router.routes,
    # include_in_schema=False,
    openapi_extra={
        "x-speakeasy-name-override": "stream",
    },
)
async def create_run_stream(
    request: Request,
    data: CreateRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
) -> StreamingResponse:
    data.execution_mode = "stream"
    return await _create_run(request, data, background_tasks, db, client)




@router.post(
    "/run/{run_id}/cancel",
    openapi_extra={
        "x-speakeasy-name-override": "cancel",
    },
)
async def cancel_run(
    request: Request,
    run_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client)
):
    try:
        # Cancel the modal function
        if run_id:
            query = """
                SELECT modal_function_call_id, status, id
                FROM "comfyui_deploy"."workflow_runs"
                WHERE id = :run_id
            """
            result = await execute_with_org_check(
                db,
                query,
                request,
                WorkflowRun,
                {"run_id": run_id}
            )
            existing_run = result.mappings().first()

            print(existing_run)

            if existing_run and existing_run.modal_function_call_id:
                a = modal.functions.FunctionCall.from_id(existing_run.modal_function_call_id)
                await a.cancel.aio()
            else:
                raise HTTPException(status_code=404, detail="Run not found or has no modal function call ID")

            if existing_run and existing_run.status not in [
                "success",
                "failed",
                "timeout",
                "cancelled",
            ]:
                now = dt.datetime.now(dt.UTC)
                # Update the run status
                stmt = (
                    update(WorkflowRun)
                    .where(WorkflowRun.id == run_id)  # Use run_id instead of function_id
                    .values(status="cancelled", updated_at=now, ended_at=now)
                )
                await db.execute(stmt)
                await db.commit()
                existing_run = await db.execute(
                    select(WorkflowRun).where(WorkflowRun.id == run_id)
                )
                workflow_run = existing_run.scalar_one_or_none()
                
                outputs_query = select(WorkflowRunOutput).where(
                    WorkflowRunOutput.run_id == run_id
                )
                outputs_result = await db.execute(outputs_query)
                outputs = outputs_result.scalars().all()
                
                workflow_run_data = workflow_run.to_dict()
                workflow_run_data["outputs"] = [output.to_dict() for output in outputs]
                
                if workflow_run.webhook is not None:
                    asyncio.create_task(
                        send_webhook(
                            workflow_run=workflow_run_data,
                            updated_at=now,
                            run_id=workflow_run.id,
                            client=client,
                        )
                    )

        return {"status": "success", "message": "Function cancelled"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error cancel function {str(e)}")


# @router.post(
#     "/run/workflow",
#     response_model=Union[
#         CreateRunResponse,
#         CreateRunBatchResponse,
#         WorkflowRunOutputModel,
#         WorkflowRunNativeOutputModel,
#     ],
#     summary="Run comfyui workflow",
#     description="Create a new workflow run with the given parameters. This function sets up the run and initiates the execution process. For callback information, see [Callbacks](#tag/callbacks/POST/\{callback_url\}).",
#     callbacks=webhook_router.routes,
# )
# async def create_run_workflow(
#     request: Request,
#     data: WorkflowRunRequest,
#     background_tasks: BackgroundTasks,
#     db: AsyncSession = Depends(get_db),
#     client: AsyncClient = Depends(get_clickhouse_client),
# ):
#     return await _create_run(request, data, background_tasks, db, client)


# @router.post(
#     "/run",
#     response_model=Union[
#         CreateRunResponse,
#         CreateRunBatchResponse,
#         WorkflowRunOutputModel,
#         WorkflowRunNativeOutputModel,
#     ],
#     summary="Run workflow",
#     description="Create a new workflow run with the given parameters. This function sets up the run and initiates the execution process. For callback information, see [Callbacks](#tag/callbacks/POST/\{callback_url\}).",
#     callbacks=webhook_router.routes,
#     openapi_extra={
#         "x-speakeasy-name-override": "create",
#     },
# )
# async def create_run(
#     request: Request,
#     data: WorkflowRequestShare,
#     background_tasks: BackgroundTasks,
#     db: AsyncSession = Depends(get_db),
#     client: AsyncClient = Depends(get_clickhouse_client),
# ):
#     data = DeploymentRunRequest(deployment_id=deployment_id, **data.model_dump())
#     return await _create_run(request, data, background_tasks, db, client)


async def update_status(
    run_id: str,
    status: str,
    background_tasks: BackgroundTasks,
    workflow_run,
    client: AsyncClient,
):
    async with get_db_context() as db:
        updated_at = dt.datetime.now(dt.UTC)
        update_data = {"status": status, "updated_at": updated_at}
        update_stmt = (
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .values(**update_data)
            .returning(WorkflowRun)
        )
        await db.execute(update_stmt)

    progress_data = [
        (
            workflow_run.user_id,
            workflow_run.org_id,
            workflow_run.machine_id,
            None,
            # body.gpu_event_id,
            workflow_run.workflow_id,
            workflow_run.workflow_version_id,
            workflow_run.run_id,
            updated_at,
            status,
            -1,
            "",
        )
    ]
    background_tasks.add_task(
        insert_to_clickhouse, client, "workflow_events", progress_data
    )


async def run_model(
    request: Request,
    data: ModelRunRequest,
    params: dict,
    workflow_run,
    background_tasks,
    client: AsyncClient,
):
    run_id = params.get("prompt_id")
    model = next((m for m in AVAILABLE_MODELS if m.id == data.model_id), None)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {data.model_id} not found")

    # def on_queue_update(update):
    #     if isinstance(update, fal_client.InProgress):
    #         for log in update.logs:
    #             print(log["message"])

    if model.fal_id is not None:
        start_time = dt.datetime.now(dt.UTC)
        result = await fal_client.subscribe_async(
            model.fal_id,
            arguments={
                # "seed": 6252023,
                # "image_size": "landscape_4_3",
                "num_images": 1,
                **params.get("inputs", {}),
            },
            # on_queue_update=on_queue_update,
        )
        end_time = dt.datetime.now(dt.UTC)

        is_image = result.get("images", [])
        is_video = result.get("video", [])
        if model.cost_per_megapixel is not None:
            total_cost = 0
            if is_image:
                for image in result.get("images", []):
                    # Calculate megapixels from width and height
                    width = image.get("width", 0)
                    height = image.get("height", 0)
                    megapixels = (width * height) / 1_000_000
                    total_cost += megapixels * model.cost_per_megapixel
                cost = total_cost
            elif is_video:
                cost = model.cost_per_megapixel

        print(result)
        print(cost)

        async with get_db_context() as db:
            output_data = {}
            if is_image:
                output_data["images"] = [
                    {
                        "url": image.get(
                            "url", ""
                        ),  # Assuming result.images contains objects with a url attribute
                        "type": "output",
                        "filename": f"image_{idx}.jpeg",
                        "subfolder": "",
                        "is_public": True,  # You may want to make this configurable
                        "upload_duration": 0,  # You may want to calculate this if needed
                    }
                    for idx, image in enumerate(result.get("images", []))
                ]
            elif is_video:
                print("video", result)
                output_data["video"] = [
                    {
                        "url": result.get("video", {}).get("url", ""),
                        "type": "output",
                        "filename": "output.mp4",
                        "subfolder": "",
                        "is_public": True,
                        "upload_duration": 0,
                    }
                ]

            updated_at = dt.datetime.now(dt.UTC)
            newOutput = WorkflowRunOutput(
                id=uuid4(),  # Add this line to generate a new UUID for the primary key
                created_at=updated_at,
                updated_at=updated_at,
                run_id=run_id,
                data=output_data,
                # node_meta=body.node_meta,
            )
            db.add(newOutput)
            await db.commit()
            await db.refresh(newOutput)
            output_dict = newOutput.to_dict()

            if cost is not None:
                gpu_event = GPUEvent(
                    id=uuid4(),
                    user_id=workflow_run.user_id,
                    org_id=workflow_run.org_id,
                    cost=cost,
                    cost_item_title=model.name,
                    start_time=start_time,
                    end_time=end_time,
                    updated_at=updated_at,
                    gpu_provider="fal",
                )
                db.add(gpu_event)
                await db.commit()
                await db.refresh(gpu_event)

            return [output_dict]

    print(result)

    return []

    # ComfyDeployRunner = modal.Cls.lookup(data.model_id, "ComfyDeployRunner")

    # if not model.is_comfyui:
    #     update_status(run_id, "queued", background_tasks, client, workflow_run)

    # result = await ComfyDeployRunner().run.remote.aio(params)

    # if not model.is_comfyui:
    #     update_status(run_id, "uploading", background_tasks, client, workflow_run)

    async with get_db_context() as db:
        user_settings = await get_user_settings(request, db)

        # if model.is_comfyui:
    # output_query = (
    #     select(WorkflowRunOutput)
    #     .where(WorkflowRunOutput.run_id == run_id)
    #     .order_by(WorkflowRunOutput.created_at.desc())
    # )

    # result = await db.execute(output_query)
    # outputs = result.scalars().all()

    # user_settings = await get_user_settings(request, db)
    # post_process_outputs(outputs, user_settings)

    # return [output.to_dict() for output in outputs]

    bucket = os.getenv("SPACES_BUCKET_V2")
    region = os.getenv("SPACES_REGION_V2")
    access_key = os.getenv("SPACES_KEY_V2")
    secret_key = os.getenv("SPACES_SECRET_V2")
    public = True

    if user_settings is not None:
        if user_settings.output_visibility == "private":
            public = False

        if user_settings.custom_output_bucket:
            bucket = user_settings.s3_bucket_name
            region = user_settings.s3_region
            access_key = user_settings.s3_access_key_id
            secret_key = user_settings.s3_secret_access_key

    for output in model.outputs:
        if output.class_type == "ComfyDeployStdOutputImage":
            # Generate the object key
            file_name = f"{output.output_id}.jpeg"
            object_key = f"outputs/runs/{run_id}/{file_name}"
            encoded_object_key = quote(object_key)
            download_url = f"{os.getenv('SPACES_ENDPOINT_V2')}/{encoded_object_key}"

            async with aioboto3.Session().client(
                "s3",
                region_name=region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(signature_version="s3v4"),
            ) as s3_client:
                try:
                    start_time = dt.datetime.now()
                    file_content = result
                    await s3_client.put_object(
                        Bucket=bucket,
                        Key=object_key,
                        Body=file_content,
                        ACL="public-read" if public else "private",
                        ContentType="image/jpeg",
                    )
                    upload_duration = (dt.datetime.now() - start_time).total_seconds()

                    file_url = (
                        f"https://{bucket}.s3.{region}.amazonaws.com/{object_key}"
                    )

                except Exception as e:
                    logger.error(f"Error uploading file: {str(e)}")
                    raise HTTPException(status_code=500, detail="Error uploading file")

            output_data = {
                "images": [
                    {
                        "url": download_url,
                        "type": "output",
                        "filename": file_name,
                        "subfolder": "",
                        "is_public": public,
                        "upload_duration": upload_duration,
                    }
                ]
            }

            updated_at = dt.datetime.now(dt.UTC)

            newOutput = WorkflowRunOutput(
                id=uuid4(),  # Add this line to generate a new UUID for the primary key
                created_at=updated_at,
                updated_at=updated_at,
                run_id=run_id,
                data=output_data,
                # node_meta=body.node_meta,
            )
            db.add(newOutput)
            await db.commit()
            await db.refresh(newOutput)

            output_dict = newOutput.to_dict()
            await post_process_output_data(output_dict["data"], user_settings)

            update_status(run_id, "success", background_tasks, client, workflow_run)

            return [output_dict]


async def run_model_async(
    request: Request,
    data: ModelRunRequest,
    params: dict,
    workflow_run,
    background_tasks,
    client: AsyncClient,
):
    run_id = params.get("prompt_id")
    model = next((m for m in AVAILABLE_MODELS if m.id == data.model_id), None)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {data.model_id} not found")

    ComfyDeployRunner = modal.Cls.lookup(data.model_id, "ComfyDeployRunner")
    result = await ComfyDeployRunner().run.spawn.aio(params)

    return {
        "run_id": run_id,
    }


async def retry_post_request(
    client: httpx.AsyncClient,
    url: str,
    json: Dict,
    headers: Dict,
    max_retries: int = 6,
    initial_delay: float = 1.0,
    max_delay: float = 10.0,
    exponential_base: float = 2.0,
    jitter: float = 0.1,
) -> httpx.Response:
    """
    Retry POST requests with exponential backoff and jitter.

    Args:
        initial_delay: Starting delay in seconds.
        max_delay: Maximum delay between retries in seconds.
        exponential_base: Base for exponential backoff (typically 2).
        jitter: Random jitter factor to add/subtract from delay (0.1 = ±10%).
    """
    for attempt in range(max_retries):
        try:
            response = await client.post(
                url,
                json=json,
                headers=headers,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 500 and attempt < max_retries - 1:

                delay = min(initial_delay * (exponential_base ** attempt), max_delay)

                jitter_range = delay * jitter
                actual_delay = delay + random.uniform(-jitter_range, jitter_range)

                logger.info(
                    f"Attempt {attempt + 1} failed with status 500, retrying in {actual_delay:.2f} seconds..."
                )
                await asyncio.sleep(actual_delay)
                continue
            raise
        except httpx.ReadTimeout as e:
            if attempt < max_retries - 1:
                delay = min(initial_delay * (exponential_base ** attempt), max_delay)
                jitter_range = delay * jitter
                actual_delay = delay + random.uniform(-jitter_range, jitter_range)
                logger.info(
                    f"Attempt {attempt + 1} timed out, retrying in {actual_delay:.2f} seconds..."
                )
                await asyncio.sleep(actual_delay)
                continue
            raise
        except Exception as e:
            # For any other exceptions, just raise immediately.
            raise


async def _create_run(
    request: Request,
    data: CreateRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    # check if the user has reached the spend limit
    # exceed_spend_limit = await is_exceed_spend_limit(request, db)
    # if exceed_spend_limit:
    #     raise HTTPException(status_code=400, detail="Spend limit reached")

    if (
        data.batch_number is not None
        and data.batch_number > 1
        and data.execution_mode == "sync_first_result"
    ):
        raise HTTPException(
            status_code=400,
            detail="Batch number is not supported for sync_first_result execution mode",
        )

    machine = None
    machine_id = None

    workflow = None

    workflow_version_version = None
    workflow_version_id = None

    workflow_id = None
    workflow_api_raw = None
    deployment = None

    # Ensure GPU is always a string
    gpu = data.gpu.value if data.gpu is not None else None

    # print("MyGPU", gpu)

    org_id = (
        request.state.current_user["org_id"]
        if "org_id" in request.state.current_user
        else None
    )

    is_native_run = data.is_native_run
    is_public_deployment = False

    if isinstance(data, WorkflowRunVersionRequest):
        workflow_version_id = data.workflow_version_id
        machine_id = data.machine_id
    elif isinstance(data, WorkflowRunRequest):
        workflow_api_raw = data.workflow_api_json
        workflow_id = data.workflow_id
        machine_id = data.machine_id
        workflow = data.workflow
    elif isinstance(data, DeploymentRunRequest):
        # Retrieve the deployment and its associated workflow version
        deployment_query = select(Deployment).where(Deployment.id == data.deployment_id)

        # First get deployment without org check to check environment
        deployment_result = await db.execute(deployment_query)
        deployment = deployment_result.scalar_one_or_none()
        deployment = cast(Optional[Deployment], deployment)

        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

        # Set public deployment flag
        is_public_deployment = deployment.environment == "public-share"

        # If not public deployment, verify org access
        if not is_public_deployment:
            has_access = (
                (org_id and deployment.org_id == org_id) or
                (not org_id and deployment.user_id == request.state.current_user["user_id"])
            )
            if not has_access:
                raise HTTPException(status_code=404, detail="Deployment not found")

        workflow_version_id = deployment.workflow_version_id
        machine_id = deployment.machine_id

        if deployment.gpu is not None:
            gpu = str(deployment.gpu)

    # Get the workflow version associated with the deployment
    if workflow_version_id is not None:
        workflow_version_query = select(WorkflowVersion).where(
            WorkflowVersion.id == workflow_version_id
        )
        workflow_version_result = await db.execute(workflow_version_query)
        workflow_version = workflow_version_result.scalar_one_or_none()
        workflow_version = cast(Optional[WorkflowVersion], workflow_version)

        if not workflow_version:
            raise HTTPException(
                status_code=404, detail="Workflow version not found for this deployment"
            )

        workflow_api_raw = workflow_version.workflow_api
        workflow_id = workflow_version.workflow_id
        workflow_version_version = workflow_version.version
        workflow = workflow_version.workflow

    is_model_run = isinstance(data, ModelRunRequest)

    # check if the actual workflow exists if it is not a model run
    if not is_model_run:
        if workflow_id is None:
            raise HTTPException(status_code=404, detail="Workflow not found")

        # To account for the new soft delete workflow
        workflow_query = (
            select(Workflow)
            .where(Workflow.id == workflow_id)
            .where(Workflow.deleted == False)
        )
        if not is_public_deployment:
            workflow_query = workflow_query.apply_org_check(request)
        test_workflow = await db.execute(workflow_query)
        test_workflow = test_workflow.scalar_one_or_none()
        test_workflow = cast(Optional[Workflow], test_workflow)

        if not test_workflow:
            raise HTTPException(status_code=404, detail="Workflow not found")

    if machine_id is not None:
        # Get the machine associated with the deployment
        machine_query = (
            select(Machine)
            .where(Machine.id == machine_id)
            .where(~Machine.deleted)
        )
        if not is_public_deployment:
            machine_query = machine_query.apply_org_check(request)
        machine_result = await db.execute(machine_query)
        machine = machine_result.scalar_one_or_none()
        machine = cast(Optional[Machine], machine)

        if not machine:
            raise HTTPException(
                status_code=404, detail="Machine not found for this deployment"
            )

        # Ensure GPU is always a string
        gpu = str(machine.gpu) if machine.gpu is not None and gpu is None else gpu

    if not is_model_run:
        if not workflow_api_raw:
            raise HTTPException(status_code=404, detail="Workflow API not found")

        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

    async def run(inputs: Dict[str, Any] = None, batch_id: Optional[UUID] = None):
        # Make it an empty so it will not get stuck
        if inputs is None:
            inputs = {}

        prompt_id = uuid.uuid4()
        user_id = request.state.current_user["user_id"]

        # Check if this is a ModelRunRequest
        model_id = None
        if isinstance(data, ModelRunRequest):
            model_id = data.model_id
            print(f"Debug - Found ModelRunRequest with model_id: {model_id}")
            
        safe_deployment_id = getattr(data, "deployment_id", None)

        # Create a new run
        new_run = WorkflowRun(
            id=prompt_id,
            workflow_id=workflow_id,
            workflow_version_id=workflow_version_id,
            workflow_inputs=inputs if inputs is not None else data.inputs,
            workflow_api=workflow_api_raw,
            # User
            user_id=user_id,
            org_id=org_id,
            origin=data.origin,
            # Machine
            machine_id=machine_id,
            machine_type=machine.type if machine else None,
            gpu=gpu,
            # Webhook
            webhook=data.webhook,
            webhook_intermediate_status=data.webhook_intermediate_status,
            batch_id=batch_id,
            model_id=model_id,  # Use the extracted model_id
            deployment_id=safe_deployment_id,
        )

        if is_native_run:
            new_run.queued_at = dt.datetime.now(dt.UTC)
            new_run.started_at = dt.datetime.now(dt.UTC)
            new_run.gpu_event_id = data.gpu_event_id

        print(
            f"Debug - model_id being saved: {new_run.model_id}"
        )  # Add this debug line

        db.add(new_run)
        await db.commit()
        await db.refresh(new_run)

        print("data", data)
        print("GPU EVENT ID", data.gpu_event_id)
        
        try:
            # backward compatibility for old comfyui custom nodes
            # Clone workflow_api_raw to modify inputs without affecting original
            workflow_api = json.loads(json.dumps(workflow_api_raw))

            if inputs and workflow_api:
                for key in inputs:
                    for node in workflow_api.values():
                        if node.get("inputs", {}).get("input_id") == key:
                            node["inputs"]["input_id"] = inputs[key]
                            # Fix for external text default value
                            if node.get("class_type") == "ComfyUIDeployExternalText":
                                node["inputs"]["default_value"] = inputs[key]

            # print("workflow_api", workflow_api)

            params = {
                "prompt_id": str(new_run.id),
                "workflow_api_raw": workflow_api_raw,
                "workflow_api": workflow_api,
                "inputs": inputs,
                "status_endpoint": os.environ.get("CURRENT_API_URL") + "/api/update-run",
                "file_upload_endpoint": os.environ.get("CURRENT_API_URL")
                + "/api/file-upload",
                "workflow": workflow,
                "gpu_event_id": data.gpu_event_id
                if data.gpu_event_id is not None
                else None,
            }

            # Get the count of runs for this workflow
            # run_count_query = select(func.count(WorkflowRun.id)).where(
            #     WorkflowRun.workflow_id == workflow_id
            # )
            # result = await db.execute(run_count_query)
            # run_count = result.scalar_one()

            new_run_data = new_run.to_dict()
            new_run_data["version"] = {
                "version": workflow_version_version,
            }
            new_run_data["machine"] = {
                "name": machine.name if machine else None,
            }
            # new_run_data["number"] = run_count
            # background_tasks.add_task(
            #     send_workflow_update, str(new_run.workflow_id), new_run_data
            # )
            # background_tasks.add_task(
            #     send_realtime_update, str(new_run.id), new_run.to_dict()
            # )

            # Sending to clickhouse
            progress_data = [
                (
                    user_id,
                    org_id,
                    machine_id,
                    data.gpu_event_id if data.gpu_event_id is not None else None,
                    workflow_id,
                    workflow_version_id,
                    new_run.id,
                    dt.datetime.now(dt.UTC),
                    "input",
                    0,
                    json.dumps(inputs),
                )
            ]

            print("INPUT")
            print("progress_data", progress_data)

            background_tasks.add_task(
                insert_to_clickhouse, client, "workflow_events", progress_data
            )

            token = generate_temporary_token(
                request.state.current_user["user_id"], org_id, expires_in="12h"
            )
            # logger.info(token)
            # logger.info("machine type " + machine.type)

            # return the params for the native run
            if is_native_run:
                return {
                    **params,
                    "cd_token": token,
                }

            if is_model_run:
                if data.execution_mode == "async":
                    params = {
                        **params,
                        "auth_token": token,
                    }
                    return await run_model_async(
                        request,
                        data,
                        params=params,
                        workflow_run=new_run,
                        background_tasks=background_tasks,
                        client=client,
                    )
                # if data.execution_mode == "async":
                #     ComfyDeployRunner = modal.Cls.lookup(data.model_id, "ComfyDeployRunner")
                #     await ComfyDeployRunner().run.spawn.aio(params)
                #     return {"run_id": str(new_run.id)}
                if data.execution_mode == "sync":
                    params = {
                        **params,
                        "auth_token": token,
                    }
                    return await run_model(
                        request,
                        data,
                        params=params,
                        workflow_run=new_run,
                        background_tasks=background_tasks,
                        client=client,
                    )

            if data.execution_mode == "async":
                match machine.type:
                    case "comfy-deploy-serverless":
                        # print("shit", str(machine_id))
                        ComfyDeployRunner = await get_comfy_deploy_runner(machine_id, gpu, deployment, machine.optimized_runner if machine else False)
                        with logfire.span("spawn-run"):
                            result = ComfyDeployRunner.run._experimental_spawn(params)
                            new_run.modal_function_call_id = result.object_id
                    # For runpod there will be a problem with the auth token cause v2 endpoint requires a token
                    case "runpod-serverless":
                        if not machine.auth_token:
                            raise HTTPException(
                                status_code=400, detail="Machine auth token not found"
                            )

                        async with httpx.AsyncClient() as _client:
                            try:
                                if data.flags and "runpod_v2" in data.flags:
                                    pass
                                else:
                                    # Proxy the update run back to v1 endpoints
                                    params["file_upload_endpoint"] = (
                                        os.environ.get("LEGACY_API_URL") + "/api/file-upload"
                                    )
                                    params["status_endpoint"] = (
                                        os.environ.get("LEGACY_API_URL") + "/api/update-run"
                                    )

                                params["cd_token"] = token
                                payload = {"input": params}

                                # Use the retry function instead of direct post
                                response = await retry_post_request(
                                    _client,
                                    f"{machine.endpoint}/run",
                                    json=payload,
                                    headers={
                                        "Content-Type": "application/json",
                                        "Authorization": f"Bearer {machine.auth_token}",
                                    }
                                )

                            except httpx.HTTPStatusError as e:
                                raise HTTPException(
                                    status_code=e.response.status_code,
                                    detail=f"Error creating run: {e.response.text}",
                                )

                        # Update the run with the RunPod job ID if available
                        runpod_response = response.json()
                    case "classic":
                        # comfyui_endpoint = f"{machine.endpoint}/comfyui-deploy/run"
                        comfyui_endpoint = urljoin(machine.endpoint, "comfyui-deploy/run")

                        headers = {"Content-Type": "application/json"}
                        # if machine.auth_token:
                        # headers["Authorization"] = f"Bearer {machine.auth_token}"
                        if machine.auth_token:
                            # Use Basic Authentication
                            credentials = base64.b64encode(
                                machine.auth_token.encode()
                            ).decode()
                            headers["Authorization"] = f"Basic {credentials}"
                            headers["authorizationheader"] = machine.auth_token

                        # print(headers)

                        async with httpx.AsyncClient() as _client:
                            response = None  # Define response outside try block
                            try:
                                response = await retry_post_request(
                                    _client,
                                    comfyui_endpoint,
                                    json={
                                        **params,
                                        "cd_token": token,
                                    },
                                    headers=headers,
                                )
                                response.raise_for_status()
                            except httpx.HTTPStatusError as e:
                                error_message = f"Error creating run: {e.response.status_code} {e.response.reason_phrase}"
                                try:
                                    if response:  # Check if response exists
                                        result = response.json()
                                        if "node_errors" in result:
                                            error_message += f" {result['node_errors']}"
                                except json.JSONDecodeError:
                                    pass
                                raise HTTPException(
                                    status_code=e.response.status_code, detail=error_message
                                )
                    case _:
                        raise HTTPException(status_code=400, detail="Invalid machine type")

                await db.commit()
                await db.refresh(new_run)

                return {"run_id": new_run.id}
            elif data.execution_mode in ["sync", "sync_first_result"]:
                with logfire.span("run-sync"):
                    ComfyDeployRunner = await get_comfy_deploy_runner(machine_id, gpu, deployment, machine.optimized_runner if machine else False)
                    result = await ComfyDeployRunner.run.remote.aio(params)

                if data.execution_mode == "sync_first_result":
                    first_output_query = (
                        select(WorkflowRunOutput)
                        .where(WorkflowRunOutput.run_id == new_run.id)
                        .order_by(WorkflowRunOutput.created_at.desc())
                        .limit(1)
                    )

                    result = await db.execute(first_output_query)
                    output = result.scalar_one_or_none()

                    user_settings = await get_user_settings(request, db)

                    await post_process_outputs([output], user_settings)

                    if data.execution_mode == "sync_first_result":
                        if output and output.data and isinstance(output.data, dict):
                            images = output.data.get("images", [])
                            for image in images:
                                if isinstance(image, dict):
                                    if "url" in image:
                                        # Fetch the image/video data
                                        async with httpx.AsyncClient() as _client:
                                            response = await _client.get(image["url"])
                                            if response.status_code == 200:
                                                content_type = response.headers.get(
                                                    "content-type"
                                                )
                                                if content_type:
                                                    return Response(
                                                        content=response.content,
                                                        media_type=content_type,
                                                    )
                        else:
                            raise HTTPException(
                                status_code=400,
                                detail="No output found is matching, please check the workflow or disable output_first_result",
                            )
                    else:
                        return output
                else:
                    output_query = (
                        select(WorkflowRunOutput)
                        .where(WorkflowRunOutput.run_id == new_run.id)
                        .order_by(WorkflowRunOutput.created_at.desc())
                    )

                    result = await db.execute(output_query)
                    outputs = result.scalars().all()

                    user_settings = await get_user_settings(request, db)
                    clean_up_outputs(outputs)
                    await post_process_outputs(outputs, user_settings)

                    return [output.to_dict() for output in outputs]
            elif data.execution_mode == "stream":
                ComfyDeployRunner = await get_comfy_deploy_runner(machine_id, gpu, deployment, machine.optimized_runner if machine else False)
                user_settings = await get_user_settings(request, db)

                async def wrapped_generator():
                    yield f"event: event_update\ndata: {json.dumps({'event': 'queuing'})}\n\n"
                    try:
                        with logfire.span("stream-run"):
                            async for event in ComfyDeployRunner.streaming.remote_gen.aio(
                                input=params
                            ):
                                if isinstance(event, (str, bytes)):
                                    # Convert bytes to string if necessary
                                    event_str = (
                                        event.decode("utf-8")
                                        if isinstance(event, bytes)
                                        else event
                                    )
                                    lines = event_str.strip().split("\n")
                                    event_type = None
                                    event_data = None
                                    for line in lines:
                                        if line.startswith("event:"):
                                            event_type = line.split(":", 1)[1].strip()
                                        elif line.startswith("data:"):
                                            event_data = line.split(":", 1)[1].strip()

                                    # logger.info(event_type)
                                    # logger.info(lines)

                                    if event_type == "event_update" and event_data:
                                        try:
                                            data = json.loads(event_data)
                                            if data.get("event") == "function_call_id":
                                                new_run.modal_function_call_id = data.get(
                                                    "data"
                                                )
                                                await db.commit()
                                            if data.get("event") == "executed":
                                                logger.info(
                                                    data.get("data", {}).get("output")
                                                )
                                                await post_process_output_data(
                                                    data.get("data", {}).get("output"),
                                                    user_settings,
                                                )
                                                new_event_data = {
                                                    "event": "executed",
                                                    "data": data.get("data", {}),
                                                }
                                                new_event = f"event: event_update\ndata: {json.dumps(new_event_data)}\n\n"
                                                yield new_event
                                                continue

                                        except json.JSONDecodeError:
                                            pass  # Invalid JSON, ignore

                                # logger.info(event)
                                yield event

                    except Exception as e:
                        print(e)

                return StreamingResponse(
                    wrapped_generator(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            else:
                raise HTTPException(status_code=400, detail="Invalid execution_mode")
        except HTTPException as e:
            new_run.status = "failed"
            await db.commit()
            await db.refresh(new_run)
            raise e
        except Exception as e:
            new_run.status = "failed"
            await db.commit()
            await db.refresh(new_run)
            raise e
            # raise HTTPException(status_code=500, detail="Internal server error: " + str(e))

    if data.batch_input_params is not None:
        batch_id = uuid.uuid4()

        # Generate a grid of all combinations of input parameters
        import itertools

        # Get all parameter names and their corresponding values
        param_names = list(data.batch_input_params.keys())
        param_values = list(data.batch_input_params.values())

        # Generate all combinations
        combinations = list(itertools.product(*param_values))

        # print(combinations)

        async def batch_run():
            results = []
            for combination in combinations:
                # Create a new input dictionary for each combination
                new_inputs = data.inputs.copy()
                for name, value in zip(param_names, combination):
                    new_inputs[name] = value

                print(new_inputs)

                # # Create a new request object with the updated inputs
                new_data = data.model_copy(update={"inputs": new_inputs})

                # Run the workflow with the new inputs
                result = await run(new_data.inputs, batch_id)
                results.append(result)
            return results

        results = await batch_run()

        return {"status": "success", "batch_id": str(batch_id)}


    elif data.batch_number is not None and data.batch_number > 1:
        batch_id = uuid.uuid4()

        async def batch_run():
            results = []
            for _ in range(data.batch_number):
                result = await run(None, batch_id)
                results.append(result)
            return results

        await batch_run()

        return {"status": "success", "batch_id": str(batch_id)}
    else:
        return await run(
            data.inputs,
        )
