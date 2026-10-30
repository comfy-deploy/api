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
from sqlalchemy import func, and_, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from fastapi import BackgroundTasks
# import fal_client
from .internal import send_webhook, publish_progress_update
from .utils import (
    apply_org_check_direct,
    clean_up_outputs,
    ensure_run_timeout,
    execute_with_org_check,
    generate_temporary_token,
    get_temporary_download_url,
    get_user_settings,
    is_private_s3_url,
    post_process_output_data,
    post_process_outputs,
    process_inputs_s3_urls,
    select,
)
from botocore.config import Config
import random
import aioboto3
from sqlalchemy.orm import defer
from api.utils.constants import blocking_log_streaming_user_id

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
from api.database import get_db, get_db_context
from typing import Optional, Union, cast, Dict, Any, List
from uuid import UUID, uuid4
import logging
import logfire
import json
import httpx
import base64
from api.utils.retrieve_s3_config_helper import retrieve_s3_config
from api.utils.autumn import autumn_client
from api.routes.platform import get_customer_plan_cached

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
async def get_run(request: Request, run_id: UUID, queue_position: bool = False, db: AsyncSession = Depends(get_db), background_tasks: BackgroundTasks = BackgroundTasks()):
    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user.get("org_id", None)

    query = (
        select(WorkflowRunWithExtra)
        .options(
            joinedload(WorkflowRun.outputs),
            defer(WorkflowRun.workflow_api),
            defer(WorkflowRun.run_log)
        )
        .where(WorkflowRun.id == run_id)
        # .apply_org_check(request)
    )

    result = await db.execute(query)
    run = result.unique().scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    
    if run.deployment_id is None:
        apply_org_check_direct(run, request)
    else:
        # 2 cases:
        # 1. if user is the creator of the run
        if org_id is not None and run.org_id == org_id:
            pass
        elif run.user_id == user_id:
            pass
        # 2. if user is the creator of the deployment
        else:
            workflow = await db.execute(
                select(Workflow).where(
                    Workflow.id == run.workflow_id,
                    Workflow.deleted.is_(False)
                )
            )
            workflow = workflow.unique().scalar_one_or_none()
            if workflow is not None:
                apply_org_check_direct(workflow, request)
            else:
                # Workflow deleted or not found, deny access
                raise HTTPException(status_code=403, detail="Access denied. ")

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
                
                status_event = {
                    "run_id": str(run.id),
                    "workflow_id": str(run.workflow_id),
                    "machine_id": str(run.machine_id),
                    "timestamp": dt.datetime.now(dt.UTC).isoformat(),
                    "progress": run.progress if run.progress is not None else -1,
                    "status": run.status,
                    "log": None,
                }
                background_tasks.add_task(
                    publish_progress_update,
                    str(run.id),
                    status_event,
                    user_id=str(run.user_id) if run.user_id else None,
                    org_id=str(run.org_id) if run.org_id else None,
                )

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
        ComfyDeployRunner = modal.Cls.from_name(target_app_name, class_name)
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
        ComfyDeployRunner = modal.Cls.from_name(target_app_name, class_name)
            
    return ComfyDeployRunner.with_options(gpu=gpu if gpu != "CPU" else None)(gpu=gpu)

async def redeploy_comfy_deploy_runner_if_exists(machine_id: str, gpu: str, deployment: Optional[Deployment] = None):
    # Only when this deployment is using latest modal_image
    target_app_name = str(deployment.id if deployment is not None and deployment.modal_image_id is not None else machine_id)
    try:
        ComfyDeployRunner = modal.Cls.from_name(target_app_name, "ComfyDeployRunner")
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
        
    ComfyDeployRunner = modal.Cls.from_name(target_app_name, "ComfyDeployRunner")
    return ComfyDeployRunner.with_options(gpu=gpu if gpu != "CPU" else None)(gpu=gpu)


async def perform_run_feature_gate_check(
    request: Request,
    db: AsyncSession,
    deployment: Optional[Deployment] = None,
    is_public_deployment: bool = False,
):
    """
    Perform feature gate checks for run creation based on deployment context.
    - For deployment runs: check deployment owner's plan/credits
    - For workflow runs: check current user's plan/credits  
    - For public deployments: skip checks
    """
    # Skip checks for public deployments
    if is_public_deployment:
        return

    current_user = request.state.current_user
    
    # Determine whose plan/credits to check
    if deployment and not is_public_deployment:
        # Check deployment owner's plan/credits
        check_user_id = deployment.user_id
        check_org_id = deployment.org_id
    else:
        # Check current user's plan/credits
        check_user_id = current_user["user_id"]
        check_org_id = current_user.get("org_id")
    
    # Get the customer ID (org_id takes precedence over user_id)
    customer_id = check_org_id or check_user_id
    
    # Get the plan to determine if we should apply the check
    try:
        plan_data = await get_customer_plan_cached(customer_id)
        if plan_data and plan_data.get("plans"):
            effective_plan = plan_data["plans"][0]
        else:
            effective_plan = "free"
    except Exception as e:
        logger.warning(f"Failed to get customer plan for {customer_id}, defaulting to free: {e}")
        effective_plan = "free"
    
    # Only apply the check for free plan users (matching the original rule)
    if effective_plan != "free":
        return
    
    # Perform the gpu-credit check (matching the original rule)
    try:
        res = await autumn_client.check(
            customer_id=customer_id,
            feature_id="gpu-credit-topup",
            required_balance=500,
        )
    except Exception as e:
        # If Autumn is misconfigured or unreachable, fail-open (allow).
        logger.error(f"Autumn check failed for gpu-credit-topup: {e}")
        return
    
    # If result is None (e.g., misconfiguration or network), allow request
    allowed = True if res is None else bool(res.get("allowed") is True)
    
    if not allowed:
        # Determine whose credits are insufficient for better error message
        owner_type = "deployment owner's" if deployment and not is_public_deployment else "your"
        raise HTTPException(
            status_code=403, 
            detail=f"Insufficient {owner_type} GPU credits to create run"
        )


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
):
    return await _create_run(request, data, background_tasks, db)


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
):
    return await _create_run(request, data, background_tasks, db)


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
):
    data.execution_mode = "sync"
    return await _create_run(request, data, background_tasks, db)


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
):
    data.execution_mode = "stream"
    return await _create_run(request, data, background_tasks, db)


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
):
    return await _create_run(request, data, background_tasks, db)


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
):
    data.execution_mode = "sync"
    return await _create_run(request, data, background_tasks, db)


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
):
    data.execution_mode = "stream"
    return await _create_run(request, data, background_tasks, db)


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
):
    return await _create_run(request, data, background_tasks, db)


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
):
    data.execution_mode = "sync"
    return await _create_run(request, data, background_tasks, db)


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
) -> StreamingResponse:
    data.execution_mode = "stream"
    return await _create_run(request, data, background_tasks, db)




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
                            # client=client,
                        )
                    )
                    
                status_event = {
                    "run_id": str(run_id),
                    "workflow_id": str(workflow_run.workflow_id),
                    "machine_id": str(workflow_run.machine_id),
                    "timestamp": dt.datetime.now(dt.UTC).isoformat(),
                }
                background_tasks.add_task(
                    publish_progress_update,
                    str(run_id),
                    status_event,
                    user_id=str(workflow_run.user_id) if workflow_run.user_id else None,
                    org_id=str(workflow_run.org_id) if workflow_run.org_id else None,
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

    # progress_data = [
    #     (
    #         workflow_run.user_id,
    #         workflow_run.org_id,
    #         workflow_run.machine_id,
    #         None,
    #         # body.gpu_event_id,
    #         workflow_run.workflow_id,
    #         workflow_run.workflow_version_id,
    #         workflow_run.run_id,
    #         updated_at,
    #         status,
    #         -1,
    #         "",
    #     )
    # ]
    # background_tasks.add_task(
    #     insert_to_clickhouse, client, "workflow_events", progress_data
    # )
    
    # Also publish to Redis for real-time streaming
    # Publish compact payload – we let the publisher shrink it further
    status_event = {
        "run_id": str(workflow_run.run_id),
        "workflow_id": str(workflow_run.workflow_id),
        "machine_id": str(workflow_run.machine_id),
        "status": status,
        "progress": -1,
        "timestamp": updated_at.isoformat(),
    }
    background_tasks.add_task(
        publish_progress_update,
        str(workflow_run.run_id),
        status_event,
        user_id=str(workflow_run.user_id) if workflow_run.user_id else None,
        org_id=str(workflow_run.org_id) if workflow_run.org_id else None,
    )


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
):
    # check if the user has reached the spend limit
    # exceed_spend_limit = await is_exceed_spend_limit(request, db)
    # if exceed_spend_limit:
    #     raise HTTPException(status_code=400, detail="Spend limit reached")

    # Get user settings once at the beginning to avoid multiple DB calls
    user_settings = await get_user_settings(request, db)

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
    
    is_blocking_log_update = request.state.current_user["user_id"] in blocking_log_streaming_user_id

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
        is_public_deployment = deployment.environment == "public-share" or deployment.environment == "community-share"

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

    # Process S3 URLs in inputs - check for private S3 URLs and replace with temporary access URLs
    if hasattr(data, 'inputs') and data.inputs:
        try:
            if user_settings:
                s3_config = await retrieve_s3_config(user_settings)
                if s3_config and not s3_config.public:
                    # Process the inputs to replace private S3 URLs with temporary access URLs
                    processed_inputs = await process_inputs_s3_urls(data.inputs, s3_config)
                    # Update the data object with processed inputs
                    data.inputs = processed_inputs
                    logger.info(f"Processed S3 URLs in inputs for private bucket: {s3_config.bucket}")
        except Exception as e:
            logger.warning(f"Failed to process S3 URLs in inputs: {str(e)}")
            # Continue without processing - don't fail the run
            pass

    # Perform feature gate checks based on deployment context
    await perform_run_feature_gate_check(
        request=request,
        db=db,
        deployment=deployment,
        is_public_deployment=is_public_deployment,
    )

    if not is_model_run:
        if not workflow_api_raw:
            raise HTTPException(status_code=404, detail="Workflow API not found")

        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

    # Convert base64 images to S3 URLs before database operations
    async def convert_base64_to_s3(inputs_dict, workflow_api_data):
        """Convert base64 image inputs to S3 URLs with early exit optimization"""
        base64_prefixes = ('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/jpg;base64,')
        
        # OPTIMIZATION 1: Early detection scan - exit fast if no base64 found
        base64_items = []
        
        # Quick scan of direct inputs
        if inputs_dict:
            for key, value in inputs_dict.items():
                if isinstance(value, str) and value.startswith(base64_prefixes):
                    base64_items.append(('input', key, value))
        
        # Quick scan of workflow API nodes
        if workflow_api_data:
            for node_id, node in workflow_api_data.items():
                if node.get("class_type") == "ComfyUIDeployExternalImage":
                    node_inputs = node.get("inputs", {})
                    for field in ["input_id", "default_value_url"]:
                        value = node_inputs.get(field)
                        if isinstance(value, str) and value.startswith(base64_prefixes):
                            base64_items.append(('node', node_id, field, value))
        
        # OPTIMIZATION 2: Early exit - no S3 operations if no base64 found
        if not base64_items:
            return  # Exit immediately, saves ~5-15ms S3 config fetch
        
        # Only fetch S3 config when base64 images are actually present
        s3_config = await retrieve_s3_config(user_settings)
        
        # OPTIMIZATION 3: Reusable upload function with format detection
        async def upload_base64_to_s3(base64_data: str) -> str:
            """Upload a base64 image to S3 and return the URL"""
            # Optimized format detection
            if base64_data.startswith('data:image/png;base64,'):
                image_format, content_type = 'png', 'image/png'
                base64_content = base64_data[22:]  # len('data:image/png;base64,')
            elif base64_data.startswith('data:image/jpeg;base64,'):
                image_format, content_type = 'jpeg', 'image/jpeg'
                base64_content = base64_data[23:]  # len('data:image/jpeg;base64,')
            elif base64_data.startswith('data:image/jpg;base64,'):
                image_format, content_type = 'jpeg', 'image/jpeg'
                base64_content = base64_data[22:]  # len('data:image/jpg;base64,')
            else:
                raise ValueError("Unsupported image format")
            
            # Decode base64
            image_data = base64.b64decode(base64_content)
            
            # Generate file ID and path
            from api.routes.files import new_id
            file_id = new_id("img")
            file_path = f"inputs/{file_id}.{image_format}"
            
            # Upload to S3
            async with aioboto3.Session().client(
                "s3",
                region_name=s3_config.region,
                aws_access_key_id=s3_config.access_key,
                aws_secret_access_key=s3_config.secret_key,
                aws_session_token=s3_config.session_token,
                config=Config(signature_version="s3v4"),
            ) as s3_client:
                await s3_client.put_object(
                    Bucket=s3_config.bucket,
                    Key=file_path,
                    Body=image_data,
                    ACL="public-read" if s3_config.public else "private",
                    ContentType=content_type,
                )
                
                file_url = f"https://{s3_config.bucket}.s3.{s3_config.region}.amazonaws.com/{file_path}"
                return file_url
        
        # OPTIMIZATION 4: Process all base64 items found in scan
        for item in base64_items:
            if item[0] == 'input':  # Direct input
                _, key, value = item
                inputs_dict[key] = await upload_base64_to_s3(value)
            elif item[0] == 'node':  # Workflow node
                _, node_id, field, value = item
                workflow_api_data[node_id]["inputs"][field] = await upload_base64_to_s3(value)

    async def run(inputs: Dict[str, Any] = None, batch_id: Optional[UUID] = None):
        # Make it an empty so it will not get stuck
        if inputs is None:
            inputs = {}

        # Convert base64 images to S3 URLs before processing
        await convert_base64_to_s3(inputs if inputs else data.inputs, workflow_api_raw)

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
            # This is pretty costly in the long run, ignore it for specific users
            # workflow_api=workflow_api_raw if not is_blocking_log_update else {},
            # Emptying this
            workflow_api={},
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

        # print("data", data)
        # print("GPU EVENT ID", data.gpu_event_id)
        
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

            # Process S3 URLs in ComfyUIDeployExternalImage nodes for both workflow_api_raw and workflow_api
            if workflow_api:
                try:
                    if user_settings:
                        s3_config = await retrieve_s3_config(user_settings)
                        if s3_config and not s3_config.public:
                            # Helper function to process S3 URLs in a workflow
                            def process_s3_urls_in_workflow(workflow_data):
                                for node_id, node in workflow_data.items():
                                    if node.get("class_type") == "ComfyUIDeployExternalImage":
                                        # Ensure inputs exists and get reference
                                        if "inputs" not in node:
                                            node["inputs"] = {}
                                        
                                        # Check default_value_url field
                                        if "default_value_url" in node["inputs"] and node["inputs"]["default_value_url"]:
                                            url = node["inputs"]["default_value_url"]
                                            if isinstance(url, str) and is_private_s3_url(url, s3_config):
                                                temp_url = get_temporary_download_url(
                                                    url=url,
                                                    region=s3_config.region,
                                                    access_key=s3_config.access_key,
                                                    secret_key=s3_config.secret_key,
                                                    session_token=s3_config.session_token or "",
                                                    expiration=3600  # 1 hour expiration
                                                )
                                                if temp_url:
                                                    node["inputs"]["default_value_url"] = temp_url
                                                    logger.info(f"Replaced S3 URL in ComfyUIDeployExternalImage node {node_id}")
                                        
                                        # Also check if the input_id value from inputs contains S3 URLs
                                        input_id = node["inputs"].get("input_id")
                                        if input_id and inputs and input_id in inputs:
                                            input_value = inputs[input_id]
                                            if isinstance(input_value, str) and is_private_s3_url(input_value, s3_config):
                                                temp_url = get_temporary_download_url(
                                                    url=input_value,
                                                    region=s3_config.region,
                                                    access_key=s3_config.access_key,
                                                    secret_key=s3_config.secret_key,
                                                    session_token=s3_config.session_token or "",
                                                    expiration=3600  # 1 hour expiration
                                                )
                                                if temp_url:
                                                    # Update the input_id field in the node directly
                                                    node["inputs"]["input_id"] = temp_url
                                                    logger.info(f"Replaced S3 URL in ComfyUIDeployExternalImage node {node_id} input_id")
                            
                            # Process both workflow_api_raw and workflow_api
                            print("Processing S3 URLs in workflow_api_raw")
                            process_s3_urls_in_workflow(workflow_api_raw)
                            print("Processing S3 URLs in workflow_api")
                            process_s3_urls_in_workflow(workflow_api)
                            
                except Exception as e:
                    logger.warning(f"Failed to process S3 URLs in ComfyUIDeployExternalImage nodes: {str(e)}")
                    # Continue without processing - don't fail the run
                    pass

            print("workflow_api", workflow_api)

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
            
            if is_blocking_log_update is False:
                # Also publish to Redis for real-time streaming
                input_event = {
                    "run_id": str(new_run.id),
                    "workflow_id": str(workflow_id),
                    "machine_id": str(machine_id),
                    "timestamp": dt.datetime.now(dt.UTC).isoformat(),
                    "progress": 0,
                    "status": "input",
                    # Avoid sending full inputs to pub/sub to save bandwidth
                    "log": "input",
                }
                background_tasks.add_task(
                    publish_progress_update,
                    str(new_run.id),
                    input_event,
                    user_id=str(user_id) if user_id else None,
                    org_id=str(org_id) if org_id else None,
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
                                # The new default is to use the v2 endpoint
                                
                                # if data.flags and "runpod_v2" in data.flags:
                                #     pass
                                # else:
                                #     # Proxy the update run back to v1 endpoints
                                #     params["file_upload_endpoint"] = (
                                #         os.environ.get("LEGACY_API_URL") + "/api/file-upload"
                                #     )
                                #     params["status_endpoint"] = (
                                #         os.environ.get("LEGACY_API_URL") + "/api/update-run"
                                #     )

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
                result = await run(data.inputs, batch_id)
                results.append(result)
            return results

        await batch_run()

        return {"status": "success", "batch_id": str(batch_id)}
    else:
        return await run(
            data.inputs,
        )
