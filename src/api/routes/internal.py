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
from uuid import uuid4
from datetime import datetime, timezone
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import UUID4, BaseModel, Field
from typing import Optional, Any, cast
from datetime import datetime
from enum import Enum
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
import aiohttp
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from pprint import pprint
import logfire
from sqlalchemy.orm import defer
from api.utils.constants import blocking_log_streaming_user_id

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

from api.database import get_db, get_db_context
from api.models import (
    GPUEvent,
    Machine,
    WorkflowRun,
    WorkflowRunOutput,
    User,
)

from fastapi import Query
from datetime import datetime, timedelta, timezone
import datetime as dt

from fastapi import Depends
from api.utils.autumn import send_autumn_usage_event, autumn_client

from upstash_redis.asyncio import Redis
from cachetools import TTLCache
from .log import cancel_active_streams, delayed_archive_logs_for_run, is_terminal_status, signal_stream_end

router = APIRouter()

logger = logging.getLogger(__name__)


async def track_gpu_concurrency_change(
    customer_id: str,
    increment: int,
    gpu_event_id: str
):
    """
    Track GPU concurrency change by increment (+1 for start, -1 for end).
    
    Args:
        customer_id: The customer ID (org_id or user_id)
        increment: +1 for gpu_start, -1 for gpu_end
        gpu_event_id: The GPU event ID to use for idempotency
    """
    try:
        # Create idempotency key with appropriate prefix
        prefix = "start_" if increment > 0 else "end_"
        idempotency_key = f"{prefix}{gpu_event_id}"
        
        # Track feature usage increment/decrement in Autumn
        await autumn_client.track_feature_usage(
            customer_id=customer_id,
            feature_id="gpu_concurrency_limit",
            value=increment,
            idempotency_key=idempotency_key
        )
        
        action = "incremented" if increment > 0 else "decremented"
        logger.info(f"GPU concurrency count {action} by {abs(increment)} for customer {customer_id} (event: {gpu_event_id})")
        
    except Exception as e:
        logger.error(f"Failed to track GPU concurrency change for customer {customer_id}: {str(e)}")
        # Don't fail the request if tracking fails


def json_safe_value(value):
    if callable(value):
        return str(value)
    return value

async def send_webhook(
    workflow_run,
    updated_at: datetime,
    run_id: str,
    type: str = "run.updated",
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

    # Prepare webhook log entry for Redis
    webhook_log_entry = {
        "timestamp": start_time,
        "level": "webhook",  # Set webhook level for proper filtering
        "logs": json.dumps({
            "type": "webhook",
            "status": json_safe_value(response.status),
            "latency_ms": latency_ms,
            "url": url,
            "message": json_safe_value(response.text) if not response.ok else None,
            "payload": json.dumps(payload),
        })
    }

    asyncio.create_task(
        insert_log_entry_to_redis(workflow_run["id"], [webhook_log_entry])
    )

    if response.ok:
        # logfire.info(
        #     "Webhook sent successfully",
        #     workflow_run_id=workflow_run["id"],
        #     workflow_id=workflow_run["workflow_id"],
        #     url=url,
        # )
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


# Deprecated: ClickHouse inserts are no longer used. Keep stub for compatibility if imported elsewhere.
# Removed: ClickHouse insert function is no longer used. All write paths now go through Redis streams.

# Initialize Redis clients only when env vars are present; else set to None
_redis_url_log = os.getenv("UPSTASH_REDIS_REST_URL_LOG")
_redis_token_log = os.getenv("UPSTASH_REDIS_REST_TOKEN_LOG")
redis = (
    Redis(url=_redis_url_log, token=_redis_token_log)
    if _redis_url_log and _redis_token_log
    else None
)

_redis_url_realtime = os.getenv("UPSTASH_REDIS_REST_URL_REALTIME")
_redis_token_realtime = os.getenv("UPSTASH_REDIS_REST_TOKEN_REALTIME")
redis_realtime = (
    Redis(url=_redis_url_realtime, token=_redis_token_realtime)
    if _redis_url_realtime and _redis_token_realtime
    else None
)

async def ensure_redis_stream_expires(run_id: str, ttl: int = 43200):
    # Set TTL (this will fail silently if key already has TTL, but that's fine)
    try:
        if redis is None:
            return
        await redis.execute(["EXPIRE", run_id, ttl])
    except:
        # Key already has TTL or other non-critical error, safe to ignore
        pass

async def insert_log_entry_to_redis(run_id: str, value: Any):
    try:
        if redis is None:
            return
        # Serialize the value to JSON if it's not already a string
        if isinstance(value, str):
            serialized_value = value
        else:
            serialized_value = json.dumps(value)
        
        # Use XADD directly - Redis streams handle auto-creation
        # Set maxlen to prevent unlimited growth (approximate trimming with ~)
        await redis.execute([
            "XADD", run_id, "MAXLEN", "~", "1000", "*", "message", serialized_value
        ])
    except:
        # Key already has TTL or other non-critical error, safe to ignore
        pass
    
async def clear_redis_log(run_id: str):
    try:
        if redis is None:
            return
        await redis.delete(run_id)
    except:
        # Key already has TTL or other non-critical error, safe to ignore
        pass

async def publish_progress_update(
    run_id: str,
    progress_data: dict,
    user_id: str | None = None,
    org_id: str | None = None,
):
    """
    Publish progress updates to Redis pub/sub channels for real-time streaming.
    This replaces the need for continuous ClickHouse polling in stream_progress.
    
    Args:
        run_id: The workflow run ID
        progress_data: Dictionary containing progress information
    """
    try:
        if redis_realtime is None:
            return

        # Build a compact payload to reduce bandwidth. We intentionally keep only
        # identifiers needed for filtering on the subscriber and minimal metadata.
        # Fallback to long-form keys when callers provide the legacy structure.
        compact_payload = {
            # Required identifiers
            "r": progress_data.get("run_id") or progress_data.get("r"),
            "w": progress_data.get("workflow_id") or progress_data.get("w"),
            "m": progress_data.get("machine_id") or progress_data.get("m"),
            # Minimal state
            "s": progress_data.get("status") or progress_data.get("s"),
            "p": progress_data.get("progress") if progress_data.get("progress") is not None else progress_data.get("p", 0),
            # Optional, small extras
            "t": progress_data.get("timestamp") or progress_data.get("t"),
        }

        # Optional short field for node/log class name
        node_class = progress_data.get("log") or progress_data.get("n")
        if isinstance(node_class, str) and len(node_class) <= 120:
            compact_payload["n"] = node_class

        serialized_data = json.dumps(compact_payload)

        # Publish to both global and run-scoped channels so run streams don't
        # receive unrelated traffic while dashboards can still aggregate.
        # Prefer scoped channels only. Publish to org or user. If neither is
        # available, skip publishing (saves bandwidth and avoids hot global channel).
        channels_to_publish = []
        if org_id:
            channels_to_publish.append(f"progress:org:{org_id}")
        elif user_id:
            channels_to_publish.append(f"progress:user:{user_id}")
        else:
            logger.debug(
                "Skipping publish: no org_id or user_id for run %s", run_id
            )
            return

        for channel in channels_to_publish:
            await redis_realtime.execute(["PUBLISH", channel, serialized_data])

        logger.debug(f"Published compact update for run {run_id} to {channels_to_publish}")

    except Exception as e:
        logger.error(f"Failed to publish progress update for run {run_id}: {e}")
        # Don't raise - this is a best-effort notification system


endStatuses = ["success", "failed", "timeout", "cancelled"]


# Session containers can take a little while to shut down after their deadline.
SESSION_BILLING_GRACE = timedelta(minutes=5)


def _as_utc(value):
    # A naive vs aware comparison raises, which would turn into a lost charge.
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def billable_end(event):
    """End time to store and bill: never before start, and for a session never past
    start + session_timeout + grace (containers can report a late end).

    Returns event.end_time itself when nothing changes, and otherwise keeps its
    naive/aware style: the DB hands these back naive, and the Autumn charge
    subtracts start from end, so mixing styles there raises and drops the charge."""
    end = event.end_time
    if event.start_time is None or end is None:
        return end
    start_utc, end_utc = _as_utc(event.start_time), _as_utc(end)
    billed = max(end_utc, start_utc)
    if getattr(event, "session_id", None) and getattr(event, "session_timeout", None):
        billed = min(billed, start_utc + timedelta(minutes=event.session_timeout) + SESSION_BILLING_GRACE)
    if billed == end_utc:
        return end
    return billed if end.tzinfo else billed.replace(tzinfo=None)


_cross_tenant_warned: TTLCache = TTLCache(maxsize=5000, ttl=600)


def _cross_tenant(flag: str, what: str, row_user_id, row_org_id, caller: dict, **ids) -> bool:
    """Tenant rule of get_org_or_user_condition, applied to an already-loaded row.

    Logs a WARNING on mismatch. Returns True (caller must reject) only when the
    `flag` env var is "true"; otherwise the request carries on unchanged (shadow mode).
    """
    caller_org_id = caller.get("org_id")
    if caller_org_id:
        if row_org_id == caller_org_id:
            return False
    elif row_user_id == caller.get("user_id") and row_org_id is None:
        return False

    enforce = os.getenv(flag, "false").lower() == "true"
    # update-run fires per log line; warn once per target per 10 minutes, not per call
    dedupe_key = (what, enforce, tuple(sorted((k, str(v)) for k, v in ids.items())))
    if dedupe_key in _cross_tenant_warned:
        return enforce
    _cross_tenant_warned[dedupe_key] = True
    context = {
        "caller_user_id": caller.get("user_id"),
        "caller_org_id": caller_org_id,
        "caller_token_type": caller.get("token_type"),
        "target_user_id": row_user_id,
        "target_org_id": row_org_id,
        **{k: str(v) if v is not None else None for k, v in ids.items()},
    }
    logger.warning(
        f"{what} tenant mismatch ({'rejected' if enforce else f'allowed, {flag} is off'}): "
        + " ".join(f"{k}={v}" for k, v in context.items()),
        extra=context,
    )
    return enforce


def _forbidden():
    return JSONResponse(
        status_code=403,
        content={"detail": "You are not authorized to access this resource"},
    )


@async_lru_cache(maxsize=1000)
async def get_cached_workflow_run(run_id: str, db: AsyncSession):
    return await get_workflow_run(run_id, db)

async def get_workflow_run(run_id: str, db: AsyncSession):
    existing_run = await db.execute(select(WorkflowRun)
        .options(defer(WorkflowRun.workflow_api), defer(WorkflowRun.run_log))
        .where(WorkflowRun.id == run_id))
    workflow_run = existing_run.scalar_one_or_none()
    workflow_run = cast(WorkflowRun, workflow_run)
    return workflow_run

@router.post("/update-run", include_in_schema=False)
async def update_run(
    request: Request,
    body: UpdateRunBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    # Use suppress_instrumentation for successful calls
    is_blocking_log_update = False
        
    try:
        with logfire.suppress_instrumentation():
            # updated_at = datetime.utcnow()
            updated_at = dt.datetime.now(dt.UTC)

            # Ensure request.time is timezone-aware
            # fixed_time = request.time.replace(tzinfo=timezone.utc) if request.time and request.time.tzinfo is None else request.time
            fixed_time = updated_at

            workflow_run = await get_cached_workflow_run(body.run_id, db)

            # O(1) ownership check on the run already fetched above (no extra query).
            if workflow_run is not None and _cross_tenant(
                "UPDATE_RUN_ENFORCE_OWNERSHIP",
                "update-run",
                workflow_run.user_id,
                workflow_run.org_id,
                getattr(request.state, "current_user", None) or {},
                run_id=body.run_id,
                machine_id=workflow_run.machine_id,
                deployment_id=workflow_run.deployment_id,
                status=body.status,
            ):
                raise HTTPException(
                    status_code=403,
                    detail="You are not authorized to access this resource",
                )

            if workflow_run is not None and workflow_run.user_id in blocking_log_streaming_user_id:
                is_blocking_log_update = True
                
            if body.status == WorkflowRunStatus.STARTED:
                await ensure_redis_stream_expires(body.run_id)
            
            if body.ws_event is not None:
                # print("body.ws_event", body.ws_event)
                # Get the workflow run
                # print("body.run_id", body.run_id)
                # with logfire.span("get_cached_workflow_run"):
                # print("workflow_run", workflow_run)
                
                # Disable sending ws event
                # if is_blocking_log_update is False:
                #     log_data = [
                #         (
                #             uuid4(),
                #             body.run_id,
                #             workflow_run.workflow_id,
                #             workflow_run.machine_id,
                #             updated_at,
                #             "ws_event",
                #             json.dumps(body.ws_event),
                #         )
                #     ]
                #     # Add ClickHouse insert to background tasks
                #     # background_tasks.add_task(insert_to_clickhouse, client, "log_entries", log_data)
                #     background_tasks.add_task(insert_log_entry_to_redis, body.run_id, body.ws_event)
                return {"status": "success"}

            if body.logs is not None:
                # Get the workflow run
                # workflow_run = await get_cached_workflow_run(body.run_id, db)

                # if not workflow_run:
                #     raise HTTPException(status_code=404, detail="WorkflowRun not found")

                target_id = body.run_id if body.session_id is None else body.session_id

                # Prepare data for ClickHouse insert
                if is_blocking_log_update is False:
                    log_data = []
                    for log_entry in body.logs:
                        data = (
                            uuid4(),
                            target_id,
                            workflow_run.workflow_id if workflow_run else None,
                            body.machine_id,
                            datetime.fromtimestamp(log_entry["timestamp"], tz=timezone.utc),
                            "info",
                            log_entry["logs"],
                        )
                        # print("data", log_entry["logs"])
                        log_data.append(data)

                    # background_tasks.add_task(insert_to_clickhouse, client, "log_entries", log_data)
                    background_tasks.add_task(insert_log_entry_to_redis, target_id, body.logs)
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
                    # Skip returning the workflow run to avoid re-fetching the data
                    # .returning(WorkflowRun)
                )
                await db.execute(update_stmt)
                await db.commit()

                # workflow_run = result.scalar_one()
                # workflow_run = cast(WorkflowRun, workflow_run)
                # await db.refresh(workflow_run)
                
                # Getting the fresh data, excluding the workflow_api and run_log
                workflow_run = await get_workflow_run(body.run_id, db)
                
                # Update the workflow run in the cache
                workflow_run.live_status = body.live_status
                workflow_run.progress = body.progress
                workflow_run.updated_at = updated_at
                workflow_run.gpu_event_id = body.gpu_event_id

                if (
                    workflow_run.webhook is not None
                    and workflow_run.webhook_intermediate_status
                ):
                    asyncio.create_task(
                        send_webhook(
                            workflow_run=workflow_run.to_dict(),
                            updated_at=updated_at,
                            run_id=workflow_run.id,
                        )
                    )

                return {"status": "success"}

            if body.log_data is not None:
                return {"status": "success"}

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
                                        # client=client,
                                    )
                                )
                                # logfire.info("No outputs to send", workflow_run_id=workflow_run.id)
                    except Exception as e:
                        # Log the error but don't send webhook
                        logging.error(f"Error processing webhook URL parameters: {str(e)}")
                
                if is_blocking_log_update is False:
                    # background_tasks.add_task(
                    #     insert_to_clickhouse, client, "workflow_events", output_data
                    # )
                    
                    # Also publish to Redis for real-time streaming
                    output_event = {
                        "run_id": str(body.run_id),
                        "workflow_id": str(workflow_run.workflow_id),
                        "machine_id": str(workflow_run.machine_id),
                        "timestamp": updated_at.isoformat(),
                        "progress": body.progress if body.progress is not None else -1,
                        "status": "output",
                        # We avoid sending large payloads; keep a small marker only
                        "log": "output",
                    }
                    background_tasks.add_task(
                        publish_progress_update,
                        str(body.run_id),
                        output_event,
                        user_id=str(workflow_run.user_id) if workflow_run.user_id else None,
                        org_id=str(workflow_run.org_id) if workflow_run.org_id else None,
                    )

            elif body.status is not None:
                # Get existing run
                
                # if body.status is not None:
                    # print("update_run", body.status, body.run_id)
                
                existing_run = await db.execute(
                    select(WorkflowRun).where(WorkflowRun.id == body.run_id)
                )
                workflow_run = existing_run.scalar_one_or_none()
                workflow_run = cast(WorkflowRun, workflow_run)

                if not workflow_run:
                    raise HTTPException(status_code=404, detail="WorkflowRun not found")

                # If the run is already cancelled, don't update it
                if workflow_run.status == "cancelled":
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
                    # logfire.info(
                    #     "Workflow run success",
                    #     workflow_run_id=workflow_run.id,
                    #     workflow_id=workflow_run.workflow_id,
                    # )
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
                # progress_data = [
                #     (
                #         workflow_run.user_id,
                #         workflow_run.org_id,
                #         workflow_run.machine_id,
                #         body.gpu_event_id,
                #         workflow_run.workflow_id,
                #         workflow_run.workflow_version_id,
                #         body.run_id,
                #         updated_at,
                #         body.status,
                #         body.progress if body.progress is not None else -1,
                #         body.live_status if body.live_status is not None else "",
                #     )
                # ]
                if is_blocking_log_update is False:
                    # background_tasks.add_task(
                    #     insert_to_clickhouse, client, "workflow_events", progress_data
                    # )
                    
                    # Also publish to Redis for real-time streaming
                    status_event = {
                        "run_id": str(body.run_id),
                        "workflow_id": str(workflow_run.workflow_id),
                        "machine_id": str(workflow_run.machine_id),
                        "timestamp": updated_at.isoformat(),
                        "progress": body.progress if body.progress is not None else -1,
                        "status": body.status,
                        "log": body.live_status if body.live_status is not None else "",
                    }
                    background_tasks.add_task(
                        publish_progress_update,
                        str(body.run_id),
                        status_event,
                        user_id=str(workflow_run.user_id) if workflow_run.user_id else None,
                        org_id=str(workflow_run.org_id) if workflow_run.org_id else None,
                    )

                # Archive logs if run reached terminal state
                if is_terminal_status(body.status):
                    logger.info(f"Run {body.run_id} reached terminal state {body.status}, triggering log archival")
                    # Signal live listeners to end immediately
                    background_tasks.add_task(signal_stream_end, body.run_id)
                    background_tasks.add_task(cancel_active_streams, body.run_id)
                    # Add delay before archiving to allow final logs to arrive
                    background_tasks.add_task(delayed_archive_logs_for_run, body.run_id)

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
                            # client=client,
                        )
                    )
                    # background_tasks.add_task(
                    #     send_webhook, workflow_run, updated_at, workflow_run.id
                    # )

                return {"status": "success"}

            return {"status": "success"}
            
    except Exception as e:
        # Always log exceptions
        logfire.error(
            "Exception in update_run",
            error=str(e),
            run_id=body.run_id,
            exc_info=True
        )
        raise


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
    token_data = await parse_jwt(token) if token else None
    if not token_data or not token_data.get("user_id"):
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    final_user_id = user_id or token_data["user_id"]
    final_org_id = org_id if org_id else token_data.get("org_id", None)

    try:
        if event_type == "gpu_start":
            if session_id is not None:
                # find the gpu event with the session_id and update the start time
                gpu_event = await db.execute(
                    select(GPUEvent).where(GPUEvent.session_id == session_id)
                )
                gpu_event = gpu_event.scalar_one_or_none()
                if gpu_event:
                    if _cross_tenant(
                        "GPU_EVENT_ENFORCE_OWNERSHIP",
                        "gpu_event gpu_start (session)",
                        gpu_event.user_id,
                        gpu_event.org_id,
                        token_data,
                        event_id=gpu_event.id,
                        session_id=session_id,
                        machine_id=machine_id,
                    ):
                        return _forbidden()
                    gpu_event.start_time = datetime.fromisoformat(timestamp)
                    # gpu_event.modal_function_id = modal_function_id
                    await db.commit()
                    
                    # Track GPU concurrency change (+1 for gpu_start)
                    await track_gpu_concurrency_change(
                        customer_id=gpu_event.org_id or gpu_event.user_id,
                        increment=1,
                        gpu_event_id=str(gpu_event.id)
                    )
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

                # The tenant that would be billed must be the token's tenant.
                if _cross_tenant(
                    "GPU_EVENT_ENFORCE_OWNERSHIP",
                    "gpu_event gpu_start",
                    final_user_id,
                    final_org_id,
                    token_data,
                    machine_id=machine_id,
                    body_user_id=user_id,
                    body_org_id=org_id,
                ):
                    return _forbidden()

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
                
                # Track GPU concurrency change (+1 for gpu_start)
                await track_gpu_concurrency_change(
                    customer_id=final_org_id or final_user_id,
                    increment=1,
                    gpu_event_id=str(gpu_event.id)
                )

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
            # Checked before commit: in enforce mode the UPDATE is rolled back.
            if _cross_tenant(
                "GPU_EVENT_ENFORCE_OWNERSHIP",
                "gpu_event gpu_end",
                event.user_id,
                event.org_id,
                token_data,
                event_id=event.id,
                session_id=event.session_id,
                machine_id=machine_id,
            ):
                await db.rollback()
                return _forbidden()
            end_time = billable_end(event)
            if end_time != event.end_time:
                logger.warning(
                    f"gpu_end {event.id}: billed end adjusted from {event.end_time} to {end_time}"
                )
                await db.execute(
                    update(GPUEvent).where(GPUEvent.id == event.id).values(end_time=end_time)
                )
                event.end_time = end_time
            await db.commit()
            
            # Track GPU concurrency change (-1 for gpu_end)
            await track_gpu_concurrency_change(
                customer_id=event.org_id or event.user_id,
                increment=-1,
                gpu_event_id=str(event.id)
            )

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