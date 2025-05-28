import asyncio
import datetime
import os
import uuid
from .types import (
    CreateRunRequest,
    CreateRunResponse,
    DeploymentRunRequest,
    WorkflowRunModel,
    WorkflowRunOutputModel,
    WorkflowRunRequest,
    WorkflowRunVersionRequest,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
import modal
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from .utils import ensure_run_timeout, get_user_settings, post_process_outputs, select

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
from uuid import UUID
import logging
import logfire
import json
import httpx
from typing import Optional, List
from uuid import UUID
import datetime as dt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Log"])


@router.get("/stream-logs")
async def stream_logs_endpoint(
    request: Request,
    run_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    session_id: Optional[str] = None,
    machine_id: Optional[str] = None,
    log_level: Optional[str] = None,
    # db: AsyncSession = Depends(get_db),
    client=Depends(get_clickhouse_client),
):
    if sum(bool(x) for x in [run_id, workflow_id, machine_id, session_id]) != 1:
        raise HTTPException(
            status_code=400, detail="Exactly one ID type must be provided"
        )

    id_type = "run" if run_id else "workflow" if workflow_id else "machine"
    id_value = run_id or workflow_id or machine_id or session_id
    
    if session_id:
        id_type = "session"
        
    print(f"id_type: {id_type}, id_value: {id_value}")

    return StreamingResponse(
        stream_logs(id_type, id_value, request, client, log_level),
        media_type="text/event-stream",
    )


async def stream_logs(
    id_type: str, id_value: str, request: Request, client, log_level: Optional[str]
):
    try:
        # Get the current user from the request state
        current_user = request.state.current_user

        async with get_db_context() as db:
            # Verify the entity exists and check permissions
            if id_type == "session":
                event = await db.execute(
                select(GPUEvent)
                .where(GPUEvent.session_id == id_value)
                .apply_org_check(request)
            )
                event = event.scalars().first()
                if not event:
                    raise HTTPException(status_code=404, detail="Session not found")
                entity = event
                id_type = "run"
            else:
                model = {"run": WorkflowRun, "workflow": Workflow, "machine": Machine}.get(
                    id_type
                )
                if not model:
                    raise HTTPException(status_code=400, detail="Invalid ID type")

                entity_query = select(model).where(model.id == id_value)
                result = await db.execute(entity_query)
                entity = result.scalar_one_or_none()

                if not entity:
                    raise HTTPException(
                        status_code=404, detail=f"{id_type.capitalize()} not found"
                    )

        # Check permissions based on org_id or user_id
        has_permission = False
        if hasattr(entity, "org_id") and entity.org_id is not None:
            has_permission = entity.org_id == current_user.get("org_id")
        elif hasattr(entity, "user_id") and entity.user_id is not None:
            has_permission = (
                entity.user_id == current_user.get("user_id")
                and current_user.get("org_id") is None
            )

        if not has_permission:
            raise HTTPException(
                status_code=403, detail="Not authorized to access these logs"
            )

        # Stream logs
        last_timestamp = None
        while True:
            query = f"""
            SELECT timestamp, log_level, message
            FROM log_entries
            WHERE {id_type}_id = '{id_value}'
            {f"AND timestamp > '{last_timestamp}'" if last_timestamp else ""}
            {f"AND log_level = '{log_level}'" if log_level else ""}
            ORDER BY timestamp ASC
            LIMIT 100
            """
            result = await client.query(query)
            for row in result.result_rows:
                timestamp, level, message = row
                last_timestamp = timestamp
                yield f"data: {json.dumps({'message': message, 'level': level, 'timestamp': timestamp.isoformat()[:-3] + 'Z'})}\n\n"

            if not result.result_rows:
                yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

            await asyncio.sleep(1)  # Wait for 1 second before next query

    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        raise
    # finally:
    #     await client.close()  # Ensure the client is closed


@router.get("/stream-progress")
async def stream_progress_endpoint(
    request: Request,
    run_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    machine_id: Optional[str] = None,
    return_run: Optional[bool] = False,
    from_start: Optional[bool] = False,

    # filter params
    status: Optional[str] = None,
    deployment_id: Optional[str] = None,

    client=Depends(get_clickhouse_client),
):
    if sum(bool(x) for x in [run_id, workflow_id, machine_id]) != 1:
        raise HTTPException(
            status_code=400, detail="Exactly one ID type must be provided"
        )

    id_type = "run" if run_id else "workflow" if workflow_id else "machine"
    id_value = run_id or workflow_id or machine_id

    return StreamingResponse(
        stream_progress(id_type, id_value, request, client, return_run, from_start, status, deployment_id),
        media_type="text/event-stream",
    )


async def stream_progress(
    id_type: str,
    id_value: str,
    request: Request,
    client,
    return_run: Optional[bool] = False,
    from_start: Optional[bool] = False,

    # filter params
    status: Optional[str] = None,
    deployment_id: Optional[str] = None,
):
    async with get_db_context() as db:
        user_settings = await get_user_settings(request, db)

    try:
        # Get the current user from the request state
        current_user = request.state.current_user

        # Verify the entity exists and check permissions
        model = {"run": WorkflowRun, "workflow": Workflow, "machine": Machine}.get(
            id_type
        )
        if not model:
            raise HTTPException(status_code=400, detail="Invalid ID type")

        async with get_db_context() as db:
            entity_query = select(model).where(model.id == id_value)
            result = await db.execute(entity_query)
            entity = result.scalar_one_or_none()

            if not entity:
                raise HTTPException(
                    status_code=404, detail=f"{id_type.capitalize()} not found"
                )

        # Check permissions based on org_id or user_id
        has_permission = False
        if hasattr(entity, "org_id") and entity.org_id is not None:
            has_permission = entity.org_id == current_user.get("org_id")
        elif hasattr(entity, "user_id") and entity.user_id is not None:
            has_permission = (
                entity.user_id == current_user.get("user_id")
                and current_user.get("org_id") is None
            )

        if not has_permission:
            raise HTTPException(
                status_code=403, detail="Not auxhorized to access this progress"
            )

        # Stream progress updates from ClickHouse
        last_update_time = None if from_start else dt.datetime.now(dt.timezone.utc)
        while True:
            # Convert last_update_time to the format expected by ClickHouse
            formatted_time = (
                last_update_time.strftime("%Y-%m-%d %H:%M:%S.%f")
                if last_update_time is not None
                else None
            )

            query = f"""
            SELECT run_id, workflow_id, workflow_version_id, machine_id, timestamp, progress, log, log_type
            FROM workflow_events
            WHERE {id_type}_id = '{id_value}'
            {f"AND timestamp > toDateTime64('{formatted_time}', 6)" if formatted_time is not None else ""}
            ORDER BY timestamp ASC
            LIMIT 100
            """
            result = await client.query(query)

            if result.result_rows and return_run:
                async with get_db_context() as db:
                    # get a set of unique run ids
                    run_ids = set()
                    for row in result.result_rows:
                        run_ids.add(row[0])

                    # update the last_update_time to the timestamp of the last row
                    if result.result_rows:
                        last_update_time = result.result_rows[-1][4]

                    for run_id in run_ids:
                        query = (
                            select(WorkflowRunWithExtra)
                            .options(joinedload(WorkflowRun.outputs))
                            .where(WorkflowRun.id == run_id)
                            # .apply_org_check(request)
                        )

                        if status:
                            query = query.where(WorkflowRun.status == status)

                        if deployment_id:
                            query = query.where(WorkflowRun.deployment_id == deployment_id)

                        result = await db.execute(query)
                        run = result.unique().scalar_one_or_none()

                        if not run:
                            # raise HTTPException(status_code=404, detail="Run not found")
                            continue

                        run = cast(WorkflowRun, run)
                        ensure_run_timeout(run)
                        await post_process_outputs(run.outputs, user_settings)
                        # Convert the run to a dictionary and remove the run_log
                        # run_dict = {k: v for k, v in vars(run).items() if k != "run_log"}
                        run_dict = run.to_dict()
                        run_dict.pop("run_log", None)
                        yield f"data: {json.dumps(run_dict)}\n\n"
            elif result.result_rows:
                for row in result.result_rows:
                    (
                        run_id,
                        workflow_id,
                        workflow_version_id,
                        machine_id,
                        timestamp,
                        progress,
                        log,
                        status,
                    ) = row
                    last_update_time = timestamp
                    progress_data = {
                        "run_id": str(run_id),
                        "workflow_id": str(workflow_id),
                        "machine_id": str(machine_id),
                        "progress": progress,
                        "status": status,
                        "node_class": log,
                        "timestamp": timestamp.isoformat()[:-3] + "Z",
                    }
                    yield f"data: {json.dumps(progress_data)}\n\n"

                    if status in ["success", "failed", "timeout", "cancelled"]:
                        return  # Exit the function if the final status is reached
            else:
                yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

            await asyncio.sleep(1)  # Wait for 1 second before next query

    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        raise

@router.get("/clickhouse-run-logs/{run_id}")
async def get_clickhouse_run_logs(
    run_id: UUID,
    request: Request,
    client=Depends(get_clickhouse_client),
):
    org_id = request.state.current_user.get("org_id", None)
    user_id = request.state.current_user.get("user_id")

    query = """
        SELECT run_id, timestamp, log, user_id, org_id, log_type
        FROM workflow_events
        WHERE run_id = %(run_id)s
        AND log_type != 'input'
        AND log != ''
        AND (
            (%(org_id)s IS NOT NULL AND org_id = %(org_id)s)
            OR (%(org_id)s IS NULL AND org_id IS NULL AND user_id = %(user_id)s)
        )
        ORDER BY timestamp ASC
    """

    result = await client.query(
        query,
        parameters={
            "run_id": str(run_id),
            "org_id": str(org_id) if org_id else None,
            "user_id": str(user_id)
        }
    )

    # Convert rows to list of dicts with properly formatted timestamps
    formatted_rows = [
        {
            "run_id": str(row[0]),
            "timestamp": row[1].isoformat(),
            "log": row[2],
            "user_id": str(row[3]) if row[3] else None,
            "org_id": str(row[4]) if row[4] else None,
            "log_type": row[5]
        }
        for row in result.result_rows
    ]

    return formatted_rows
