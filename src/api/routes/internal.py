import json
import os
import time
from urllib.parse import urlparse, quote
from api.sqlmodels import WorkflowRunStatus
from api.middleware.auth import parse_jwt
from api.utils.storage_helper import get_s3_config
from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    BackgroundTasks,
    Response,
    Request,
)
import asyncio
import clickhouse_connect
from uuid import uuid4
from datetime import datetime, timezone
from fastapi.responses import RedirectResponse
from pydantic import UUID4, BaseModel, Field
from typing import Optional, Any, cast
from datetime import datetime
from enum import Enum
import boto3
from botocore.exceptions import ClientError
from clickhouse_connect.driver.asyncclient import AsyncClient
from botocore.config import Config
import aiohttp
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from pprint import pprint
import logfire

from .utils import (
    async_lru_cache,
    clean_up_outputs,
    generate_presigned_url,
    get_user_settings,
    post_process_outputs,
    retry_fetch,
    select,
)
from sqlalchemy import update, case, and_

from api.database import AsyncSessionLocal, get_clickhouse_client, get_db, get_db_context
from api.models import (
    GPUEvent,
    Machine,
    WorkflowRun,
    WorkflowRunOutput,
    User,
)

from fastapi import Query
from datetime import datetime, timezone
import datetime as dt

from fastapi import Depends
from api.utils.autumn import send_autumn_usage_event

router = APIRouter()

logger = logging.getLogger(__name__)


def json_safe_value(value):
    if callable(value):
        return str(value)
    return value

async def send_webhook(
    workflow_run,
    updated_at: datetime,
    run_id: str,
    type: str = "run.updated",
    client: AsyncClient = Depends(get_clickhouse_client),
):
    # try:
    url = workflow_run["webhook"]
    extra_headers = {}  # You may want to define this based on your requirements

    payload = {
        "event_type": type,
        "status": workflow_run["status"],
        "live_status": workflow_run["live_status"],
        "progress": workflow_run["progress"],
        "run_id": workflow_run["id"],
        "outputs": workflow_run["outputs"]
        if "outputs" in workflow_run
        else [],  # Assuming 'data' contains the outputs
    }

    logging.info("Webhook going to be sent to: " + url)

    headers = {"Content-Type": "application/json", **extra_headers}

    options = {"method": "POST", "headers": headers, "json": payload}

    start_time = time.time()
    response = await retry_fetch(url, options)
    latency_ms = (time.time() - start_time) * 1000

    webhook_clickhouse_data = [
        (
            uuid4(),
            workflow_run["id"],
            workflow_run["workflow_id"],
            workflow_run["machine_id"],
            datetime.fromtimestamp(start_time, tz=timezone.utc),
            "webhook",
            json.dumps({
                "status": json_safe_value(response.status),
                "latency_ms": latency_ms,
                "url": url,
                "message": json_safe_value(response.text) if not response.ok else None,
                "payload": json.dumps(payload),
            }),
        )
    ]

    asyncio.create_task(
        insert_to_clickhouse(client, "log_entries", webhook_clickhouse_data)
    )

    if response.ok:
        logfire.info(
            "Webhook sent successfully",
            workflow_run_id=workflow_run["id"],
            workflow_id=workflow_run["workflow_id"],
            url=url,
        )
        logger.info(
            f"POST webhook {response.status}",
            extra={
                "status_code": response.status,
                "route": "webhook",  # Use low-cardinality route path
                "full_route": "webhook",
                "function_name": "send_webhook",
                "method": "POST",
                # "user_id": workflow_run["user_id"],
                # "org_id": workflow_run["org_id"],
                "latency_ms": latency_ms,
            },
        )
        return {"status": "success", "message": "Webhook sent successfully"}
    else:
        logfire.error(
            f"Webhook failed with status {response.status}",
            workflow_run_id=workflow_run["id"],
            workflow_id=workflow_run["workflow_id"],
            url=url,
        )
        logger.error(
            f"POST webhook {response.status}",
            extra={
                "status_code": response.status,
                "route": "webhook",  # Use low-cardinality route path
                "full_route": "webhook",
                "function_name": "send_webhook",
                "method": "POST",
                # "user_id": workflow_run["user_id"],
                # "org_id": workflow_run["org_id"],
                "latency_ms": latency_ms,
            },
        )
        return {
            "status": "error",
            "message": f"Webhook failed with status {response.status}",
        }
    # except Exception as error:
    #     logging.info("Webhook failed with error: " + str(error))
    #     raise error
    #     return {"status": "error", "message": str(error)}


class UpdateRunBody(BaseModel):
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    machine_id: Optional[str] = None
    status: Optional[WorkflowRunStatus] = None
    time: Optional[datetime] = None
    output_data: Optional[Any] = None
    node_meta: Optional[Any] = None
    log_data: Optional[Any] = None
    logs: Optional[Any] = None
    ws_event: Optional[Any] = None
    live_status: Optional[str] = None
    progress: Optional[float] = None
    modal_function_call_id: Optional[str] = None
    gpu_event_id: Optional[str] = None


router = APIRouter()


async def insert_to_clickhouse(client: AsyncClient, table: str, data: list):
    await client.insert(table=table, data=data)


# async def insert_to_clickhouse_multi(client: AsyncClient, table: str, data: list):
#     # Prepare the data for batch insert
#     columns = list(data[0].keys())
#     values = [list(item.values()) for item in data]

#     # Perform batch insert
#     result = await client.insert(table=table, data=values, column_names=columns)
#     print("result", result)
endStatuses = ["success", "failed", "timeout", "cancelled"]


@async_lru_cache(maxsize=1000)
async def get_cached_workflow_run(run_id: str, db: AsyncSession):
    existing_run = await db.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))
    workflow_run = existing_run.scalar_one_or_none()
    workflow_run = cast(WorkflowRun, workflow_run)
    return workflow_run


@router.post("/update-run", include_in_schema=False)
async def update_run(
    request: Request,
    body: UpdateRunBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    client: AsyncClient = Depends(get_clickhouse_client),
):
    # updated_at = datetime.utcnow()
    updated_at = dt.datetime.now(dt.UTC)

    # Ensure request.time is timezone-aware
    # fixed_time = request.time.replace(tzinfo=timezone.utc) if request.time and request.time.tzinfo is None else request.time
    fixed_time = updated_at

    if body.ws_event is not None:
        # print("body.ws_event", body.ws_event)
        # Get the workflow run
        # print("body.run_id", body.run_id)
        with logfire.span("get_cached_workflow_run"):
            workflow_run = await get_cached_workflow_run(body.run_id, db)
        # print("workflow_run", workflow_run)

            log_data = [
                (
                    uuid4(),
                    body.run_id,
                    workflow_run.workflow_id,
                    workflow_run.machine_id,
                    updated_at,
                    "ws_event",
                    json.dumps(body.ws_event),
                )
            ]
            # Add ClickHouse insert to background tasks
            background_tasks.add_task(insert_to_clickhouse, client, "log_entries", log_data)
            return {"status": "success"}

    if body.logs is not None:
        # Get the workflow run
        workflow_run = await get_cached_workflow_run(body.run_id, db)

        # if not workflow_run:
        #     raise HTTPException(status_code=404, detail="WorkflowRun not found")

        # Prepare data for ClickHouse insert
        log_data = []
        for log_entry in body.logs:
            data = (
                uuid4(),
                body.run_id if body.session_id is None else body.session_id,
                workflow_run.workflow_id if workflow_run else None,
                body.machine_id,
                datetime.fromtimestamp(log_entry["timestamp"], tz=timezone.utc),
                "info",
                log_entry["logs"],
            )
            # print("data", log_entry["logs"])
            log_data.append(data)

        background_tasks.add_task(insert_to_clickhouse, client, "log_entries", log_data)

        return {"status": "success"}

    # Updating the progress
    if body.live_status is not None and body.progress is not None:
        # Updating the workflow run table with live_status, progress, and updated_at
        update_stmt = (
            update(WorkflowRun)
            .where(WorkflowRun.id == body.run_id)
            .values(
                live_status=body.live_status,
                progress=body.progress,
                updated_at=updated_at,
                gpu_event_id=body.gpu_event_id,
            )
            .returning(WorkflowRun)
        )
        result = await db.execute(update_stmt)
        await db.commit()

        workflow_run = result.scalar_one()
        workflow_run = cast(WorkflowRun, workflow_run)
        await db.refresh(workflow_run)

        # # Sending a real-time update to connected clients
        # background_tasks.add_task(
        #     send_workflow_update, str(workflow_run.workflow_id), workflow_run.to_dict()
        # )
        # background_tasks.add_task(
        #     send_realtime_update, str(workflow_run.id), workflow_run.to_dict()
        # )

        # Sending to clickhouse
        progress_data = [
            (
                workflow_run.user_id,
                workflow_run.org_id,
                workflow_run.machine_id,
                body.gpu_event_id,
                workflow_run.workflow_id,
                workflow_run.workflow_version_id,
                body.run_id,
                updated_at,
                "executing",  # NOTE: when body.progress and body.live_status are set, body.status is not sent so we patch this
                body.progress,
                body.live_status,
            )
        ]

        background_tasks.add_task(
            insert_to_clickhouse, client, "workflow_events", progress_data
        )

        if (
            workflow_run.webhook is not None
            and workflow_run.webhook_intermediate_status
        ):
            asyncio.create_task(
                send_webhook(
                    workflow_run=workflow_run.to_dict(),
                    updated_at=updated_at,
                    run_id=workflow_run.id,
                    client=client,
                )
            )
            # background_tasks.add_task(
            #     send_webhook, workflow_run, updated_at, workflow_run.id
            # )

        return {"status": "success"}

    if body.log_data is not None:
        # Cause all the logs will be sent to clickhouse now.
        return {"status": "success"}
        # update_stmt = (
        #     update(WorkflowRun)
        #     .where(WorkflowRun.id == request.run_id)
        #     .values(run_log=request.log_data, updated_at=updated_at)
        # )
        # await db.execute(update_stmt)
        # await db.commit()
        # return {"status": "success"}

    if body.status == "started" and fixed_time is not None:
        update_stmt = (
            update(WorkflowRun)
            .where(WorkflowRun.id == body.run_id)
            .values(started_at=fixed_time, updated_at=updated_at)
        )
        await db.execute(update_stmt)
        await db.commit()

    if body.status == "queued" and fixed_time is not None:
        # Ensure request.time is UTC-aware
        update_stmt = (
            update(WorkflowRun)
            .where(WorkflowRun.id == body.run_id)
            .values(queued_at=fixed_time, updated_at=updated_at)
        )
        await db.execute(update_stmt)
        await db.commit()
        # return {"status": "success"}

    ended = body.status in endStatuses
    if body.output_data is not None:
        # Sending to postgres
        newOutput = WorkflowRunOutput(
            id=uuid4(),  # Add this line to generate a new UUID for the primary key
            created_at=updated_at,
            updated_at=updated_at,
            run_id=body.run_id,
            data=body.output_data,
            node_meta=body.node_meta,
        )
        db.add(newOutput)
        await db.commit()
        await db.refresh(newOutput)

        existing_run = await db.execute(
            select(WorkflowRun).where(WorkflowRun.id == body.run_id)
        )
        workflow_run = existing_run.scalar_one_or_none()
        workflow_run = cast(WorkflowRun, workflow_run)

        if workflow_run.webhook is not None:
            try:
                # Parse the webhook URL and get query parameters
                parsed_url = urlparse(workflow_run.webhook)
                query_params = {}

                if parsed_url.query:
                    # Safely parse query parameters
                    for param in parsed_url.query.split("&"):
                        try:
                            if "=" in param:
                                key, value = param.split("=", 1)
                                query_params[key] = value
                        except Exception:
                            # Skip malformed query parameters
                            continue

                # Get target_events from query params and split into list
                target_events = query_params.get("target_events", "").split(",")
                target_events = [
                    event.strip() for event in target_events if event.strip()
                ]

                # Send webhook if no target_events specified or if the event type is in target_events
                if "run.output" in target_events:
                    workflow_run_data = workflow_run.to_dict()
                    outputs = clean_up_outputs([newOutput])
                    if len(outputs) > 0:
                        workflow_run_data["outputs"] = [output.to_dict() for output in outputs]
                        asyncio.create_task(
                            send_webhook(
                                workflow_run=workflow_run_data,
                                updated_at=updated_at,
                                run_id=workflow_run.id,
                                type="run.output",
                                client=client,
                            )
                        )
                    else:
                        logfire.info("No outputs to send", workflow_run_id=workflow_run.id)
            except Exception as e:
                # Log the error but don't send webhook
                logging.error(f"Error processing webhook URL parameters: {str(e)}")

        # TODO: Send to upload to clickhouse
        output_data = [
            (
                workflow_run.user_id,
                workflow_run.org_id,
                workflow_run.machine_id,
                body.gpu_event_id,
                workflow_run.workflow_id,
                workflow_run.workflow_version_id,
                body.run_id,
                updated_at,
                "output",
                body.progress if body.progress is not None else -1,
                json.dumps(body.output_data),
            )
        ]
        background_tasks.add_task(
            insert_to_clickhouse, client, "workflow_events", output_data
        )

    elif body.status is not None:
        # Get existing run
        existing_run = await db.execute(
            select(WorkflowRun).where(WorkflowRun.id == body.run_id)
        )
        workflow_run = existing_run.scalar_one_or_none()
        workflow_run = cast(WorkflowRun, workflow_run)

        if not workflow_run:
            raise HTTPException(status_code=404, detail="WorkflowRun not found")

        # If the run is already cancelled, don't update it
        if body.status == "cancelled":
            return {"status": "success"}

        # If the run is already timed out, don't update it
        if workflow_run.status == "timeout":
            return {"status": "success"}

        # Update the run status
        update_data = {"status": body.status, "updated_at": updated_at}
        if ended and fixed_time is not None:
            update_data["ended_at"] = fixed_time

        update_values = {
            "status": body.status,
            "ended_at": updated_at if ended else None,
            "updated_at": updated_at,
        }

        # Add modal_function_call_id if it's provided and the existing value is empty
        if body.modal_function_call_id:
            update_values["modal_function_call_id"] = body.modal_function_call_id

        if body.status == "success":
            logfire.info(
                "Workflow run success",
                workflow_run_id=workflow_run.id,
                workflow_id=workflow_run.workflow_id,
            )
            logger.info(
                "Workflow run success",
                extra={
                    "route": "run-status/success",  # Use low-cardinality route path
                    "full_route": "run-status",
                    "function_name": "update_run",
                    "method": "POST",
                    "status_code": 200,
                    "user_id": workflow_run.user_id,
                    "org_id": workflow_run.org_id,
                    "workflow_run_id": workflow_run.id,
                    "workflow_id": workflow_run.workflow_id,
                },
            )
        elif body.status == "failed":
            logfire.error(
                "Workflow run failed",
                workflow_run_id=workflow_run.id,
                workflow_id=workflow_run.workflow_id,
            )
            logger.error(
                "Workflow run failed",
                extra={
                    "route": "run-status/failed",  # Use low-cardinality route path
                    "full_route": "run-status",
                    "function_name": "update_run",
                    "method": "POST",
                    "status_code": 500,
                    "user_id": workflow_run.user_id,
                    "org_id": workflow_run.org_id,
                    "workflow_run_id": workflow_run.id,
                    "workflow_id": workflow_run.workflow_id,
                },
            )
        elif body.status == "timeout":
            logfire.error(
                "Workflow run timeout",
                workflow_run_id=workflow_run.id,
                workflow_id=workflow_run.workflow_id,
            )
            logger.warning(
                "Workflow run timeout",
                extra={
                    "route": "run-status/timeout",  # Use low-cardinality route path
                    "full_route": "run-status",
                    "function_name": "update_run",
                    "method": "POST",
                    "user_id": workflow_run.user_id,
                    "org_id": workflow_run.org_id,
                    "workflow_run_id": workflow_run.id,
                    "workflow_id": workflow_run.workflow_id,
                },
            )
        elif body.status == "cancelled":
            logfire.error(
                "Workflow run cancelled",
                workflow_run_id=workflow_run.id,
                workflow_id=workflow_run.workflow_id,
            )
            logger.warning(
                "Workflow run cancelled",
                extra={
                    "route": "run-status/cancelled",  # Use low-cardinality route path
                    "full_route": "run-status",
                    "function_name": "update_run",
                    "method": "POST",
                    "user_id": workflow_run.user_id,
                    "org_id": workflow_run.org_id,
                    "workflow_run_id": workflow_run.id,
                    "workflow_id": workflow_run.workflow_id,
                },
            )

        update_stmt = (
            update(WorkflowRun)
            .where(
                and_(
                    WorkflowRun.id == body.run_id, ~WorkflowRun.status.in_(endStatuses)
                )
            )
            .values(**update_values)
        )
        await db.execute(update_stmt)
        await db.commit()
        await db.refresh(workflow_run)

        # Get the updated workflow run
        existing_run = await db.execute(
            select(WorkflowRun).where(WorkflowRun.id == body.run_id)
        )
        workflow_run = existing_run.scalar_one_or_none()
        workflow_run = cast(WorkflowRun, workflow_run)

        # Sending to clickhouse
        progress_data = [
            (
                workflow_run.user_id,
                workflow_run.org_id,
                workflow_run.machine_id,
                body.gpu_event_id,
                workflow_run.workflow_id,
                workflow_run.workflow_version_id,
                body.run_id,
                updated_at,
                body.status,
                body.progress if body.progress is not None else -1,
                body.live_status if body.live_status is not None else "",
            )
        ]
        background_tasks.add_task(
            insert_to_clickhouse, client, "workflow_events", progress_data
        )

        # Get all outputs for the workflow run
        outputs_query = select(WorkflowRunOutput).where(
            WorkflowRunOutput.run_id == body.run_id
        )
        outputs_result = await db.execute(outputs_query)
        outputs = outputs_result.scalars().all()

        user_settings = await get_user_settings(request, db)
        if outputs:
            await post_process_outputs(outputs, user_settings)
        clean_up_outputs(outputs)
        # Instead of setting outputs directly, create a new dictionary with all the data
        workflow_run_data = workflow_run.to_dict()
        workflow_run_data["outputs"] = [output.to_dict() for output in outputs]
        # workflow_run_data["status"] = request.status.value

        if workflow_run.webhook is not None:
            asyncio.create_task(
                send_webhook(
                    workflow_run=workflow_run_data,
                    updated_at=updated_at,
                    run_id=workflow_run.id,
                    client=client,
                )
            )
            # background_tasks.add_task(
            #     send_webhook, workflow_run, updated_at, workflow_run.id
            # )

        return {"status": "success"}

    return {"status": "success"}


@router.post("/gpu_event", include_in_schema=False)
async def create_gpu_event(
    request: Request,
    data: Any = Body(...),
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    # Extract data from request body
    machine_id = data.get("machine_id")
    timestamp = data.get("timestamp")
    gpu_type = data.get("gpuType")
    ws_gpu_type = data.get("wsGpuType")
    event_type = data.get("eventType")
    gpu_provider = data.get("gpu_provider")
    event_id = data.get("event_id")
    user_id = data.get("user_id")
    org_id = data.get("org_id")
    session_id = data.get("session_id")
    modal_function_id = data.get("modal_function_id")
    environment = data.get("environment", None)

    # Get token data from request
    token = request.headers.get("authorization", "").replace("Bearer ", "")
    if not token:
        return {"error": "user_id required"}, 404

    token_data = await parse_jwt(token)

    if not token_data.get("user_id"):
        return {"error": "user_id required"}, 404

    final_user_id = user_id or token_data["user_id"]
    final_org_id = org_id if user_id else token_data["org_id"]

    try:
        if event_type == "gpu_start":
            if session_id is not None:
                # find the gpu event with the session_id and update the start time
                gpu_event = await db.execute(
                    select(GPUEvent).where(GPUEvent.session_id == session_id)
                )
                gpu_event = gpu_event.scalar_one_or_none()
                gpu_event.start_time = datetime.fromisoformat(timestamp)
                # gpu_event.modal_function_id = modal_function_id
                await db.commit()
            else:
                # Insert new GPU event
                if machine_id:
                    machine = await db.execute(
                        select(Machine).where(Machine.id == machine_id)
                    )
                    machine = machine.scalar_one_or_none()
                    if machine:
                        final_user_id = machine.user_id
                        final_org_id = machine.org_id

                gpu_event = GPUEvent(
                    id=uuid4(),
                    user_id=final_user_id,
                    org_id=final_org_id,
                    start_time=datetime.fromisoformat(timestamp),
                    machine_id=machine_id,
                    gpu=gpu_type,
                    ws_gpu=ws_gpu_type,
                    gpu_provider=gpu_provider,
                    session_id=session_id,
                    modal_function_id=modal_function_id,
                    environment=environment,
                )

                db.add(gpu_event)
                await db.commit()

            logging.info(f"gpu_event: {gpu_event.id}")
            return {"event_id": gpu_event.id}

        elif event_type == "gpu_end":
            if not event_id:
                raise HTTPException(status_code=404, detail="missing event_id")

            # Update existing GPU event with end time
            stmt = (
                update(GPUEvent)
                .where(GPUEvent.id == event_id)
                .values(end_time=datetime.fromisoformat(timestamp))
                .returning(GPUEvent)
            )

            result = await db.execute(stmt)
            event = result.scalar_one()
            await db.commit()

            # Send usage data to Autumn API
            await send_autumn_usage_event(
                customer_id=event.org_id or event.user_id,
                gpu_type=event.gpu,
                start_time=event.start_time,
                end_time=event.end_time,
                environment=event.environment,
                idempotency_key=str(event.id),
            )

            # if event.session_id is not None:
            #     background_tasks.add_task(cancel_executing_runs, event.session_id)

            # Get headers from the incoming request
            headers = dict(request.headers)
            # Remove host header as it will be set by aiohttp
            headers.pop("host", None)
            # Send a POST request to the legacy API to update the user spent usage.
            # async with aiohttp.ClientSession() as session:
            #     # Remove any existing encoding headers and set to just gzip
            #     headers["Accept-Encoding"] = "gzip, deflate"
            #     if "content-encoding" in headers:
            #         del headers["content-encoding"]

            #     async with session.post(new_url, json=data, headers=headers) as response:
            #         content = await response.read()
            #         return Response(
            #             content=content,
            #             status_code=response.status,
            #             headers={
            #                 k: v
            #                 for k, v in response.headers.items()
            #                 if k.lower() != "content-encoding"
            #             },
            #         )
            return {"event_id": event.id}

            # logging.info(f"end_time added to gpu_event: {event.id}")

    except Exception as e:
        logging.error(f"Error: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


async def cancel_executing_runs(session_id: str):
    # Create a new database session for this background task
    async with get_db_context() as db:
        updateExecutingRuns = (
            update(WorkflowRun)
            .where(
                and_(
                    WorkflowRun.gpu_event_id == session_id,
                    ~WorkflowRun.status.in_(endStatuses),
                )
            )
            .values(status="cancelled")
        )

        await db.execute(updateExecutingRuns)
        await db.commit()


@router.post("/fal-webhook", include_in_schema=False)
async def fal_webhook(request: Request, data: Any = Body(...)):
    legacy_api_url = os.getenv("LEGACY_API_URL", "").rstrip("/")
    new_url = f"{legacy_api_url}/api/fal-webhook"

    # Get headers from the incoming request
    headers = dict(request.headers)
    # Remove host header as it will be set by aiohttp
    headers.pop("host", None)

    async with aiohttp.ClientSession() as session:
        # Remove any existing encoding headers and set to just gzip
        headers["Accept-Encoding"] = "gzip, deflate"
        if "content-encoding" in headers:
            del headers["content-encoding"]

        async with session.post(new_url, json=data, headers=headers) as response:
            content = await response.read()
            return Response(
                content=content,
                status_code=response.status,
                headers={
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() != "content-encoding"
                },
            )


@router.get("/file-upload", include_in_schema=False)
async def get_file_upload_url(
    request: Request,
    file_name: str = Query(..., description="Name of the file to upload"),
    run_id: UUID4 = Query(..., description="UUID of the run"),
    size: Optional[int] = Query(None, description="Size of the file in bytes"),
    type: str = Query(..., description="Type of the file"),
    public: bool = Query(
        True, description="Whether to make the file publicly accessible"
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        s3_config = await get_s3_config(request, db)
        if not s3_config.public:
            public = False

        # Generate the object key
        object_key = f"outputs/runs/{run_id}/{file_name}"

        # Encode the object key for use in the URL
        encoded_object_key = quote(object_key)
        # SPACES_ENDPOINT_V2="https://comfy-deploy-output-dev.s3.amazonaws.com"
        composed_endpoint = f"https://{s3_config.bucket}.s3.{s3_config.region}.amazonaws.com"
        download_url = f"{composed_endpoint}/{encoded_object_key}"

        # Generate pre-signed S3 upload URL
        upload_url = generate_presigned_url(
            object_key=object_key,
            expiration=3600,  # URL expiration time in seconds
            http_method="PUT",
            size=size,
            content_type=type,
            public=public,
            bucket=s3_config.bucket,
            region=s3_config.region,
            access_key=s3_config.access_key,
            secret_key=s3_config.secret_key,
            session_token=s3_config.session_token,
        )

        # if public:
        #     # Set the object ACL to public-read after upload
        #     set_object_acl_public(os.getenv("SPACES_BUCKET_V2"), object_key)

        return {
            "url": upload_url,
            "download_url": download_url,
            "include_acl": public,
            "is_public": public,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class EmailAddress(BaseModel):
    email_address: str
    id: str
    verification: dict


class ClerkWebhookData(BaseModel):
    data: dict
    type: str
    timestamp: int


@router.post("/clerk/webhook", include_in_schema=False)
async def handle_clerk_webhook(
    webhook_data: ClerkWebhookData,
    db: AsyncSession = Depends(get_db),
):
    try:
        user_data = webhook_data.data

        if webhook_data.type == "user.created":
            # Check if user already exists
            existing_user = await db.execute(
                select(User).where(User.id == user_data["id"])
            )
            if existing_user.scalar_one_or_none():
                return {"status": "success", "message": "User already exists"}

            # Get username fallback (username or first_name + last_name)
            username_fallback = user_data.get("username") or (
                (user_data.get("first_name") or "") + (user_data.get("last_name") or "")
            )

            # Get name fallback
            name_fallback = (user_data.get("first_name") or "") + (
                user_data.get("last_name") or ""
            )
            if not name_fallback:
                name_fallback = username_fallback

            # Create new user
            new_user = User(
                id=user_data["id"],
                username=username_fallback,
                name=name_fallback,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

            db.add(new_user)
            await db.commit()
            await db.refresh(new_user)

            return {"status": "success", "message": "User created successfully"}

        elif webhook_data.type == "user.updated":
            # Get existing user
            existing_user = await db.execute(
                select(User).where(User.id == user_data["id"])
            )
            user = existing_user.scalar_one_or_none()

            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            # Update username and name with same fallback logic
            username_fallback = user_data.get("username") or (
                (user_data.get("first_name") or "") + (user_data.get("last_name") or "")
            )

            name_fallback = (user_data.get("first_name") or "") + (
                user_data.get("last_name") or ""
            )
            if not name_fallback:
                name_fallback = username_fallback

            user.username = username_fallback
            user.name = name_fallback
            user.updated_at = datetime.now(timezone.utc)

            await db.commit()
            await db.refresh(user)

            return {"status": "success", "message": "User updated successfully"}

        return {"status": "success", "message": "Event processed"}

    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
