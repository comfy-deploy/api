import logging
from typing import Any, Dict, List, Optional
from .types import (
    WorkflowRunModel, 
    MachineGPU,
    WorkflowRunOrigin,
    WorkflowRunStatus,
)
from fastapi import APIRouter, Request, Query
from api.database import AsyncSessionLocal
from .utils import select
from api.models import WorkflowRun
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, case
from collections import defaultdict
import asyncio
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Runs"])

# Define valid filters that can be used in the count endpoint
VALID_FILTERS = {
    "gpu": str,  # GPU type/name
    "status": str,  # run status (success, failed, etc)
    "origin": str,  # origin of the run
    "workflow_id": str,  # workflow identifier
    "machine_id": str,  # machine identifier
    "deployment_id": str,  # deployment identifier
}

# @router.get("/runs", response_model=List[WorkflowRunModel])
# async def get_runs(
#     request: Request,
#     limit: int = 60,
#     offset: int = 0,
#     gpu: Optional[str] = None,
#     status: Optional[str] = None,
#     origin: Optional[str] = None,
#     workflow_id: Optional[str] = None,
#     duration: Optional[str] = None,
#     created_at: Optional[str] = None,
#     machine_id: Optional[str] = None,
#     deployment_id: Optional[str] = None,
# ):
#     # Process filter lists upfront
#     gpu_list = [g.strip() for g in gpu.split(",")] if gpu else []
#     status_list = [s.strip().lower() for s in status.split(",")] if status else []
#     origin_list = [o.strip() for o in origin.split(",")] if origin else []

#     # Process datetime variables upfront
#     start_datetime = None
#     end_datetime = None
#     if created_at:
#         try:
#             # Validate format
#             if "-" not in created_at:
#                 raise ValueError("Time range must be in format 'start-end'")

#             start_time, end_time = created_at.split("-")

#             # Convert to timestamps
#             start_datetime = datetime.fromtimestamp(int(start_time) / 1000)
#             end_datetime = datetime.fromtimestamp(int(end_time) / 1000)

#             # Validate time range
#             if start_datetime > end_datetime:
#                 raise ValueError("Start time cannot be later than end time")

#         except (ValueError, TypeError) as e:
#             # Return error response instead of just logging
#             return JSONResponse(
#                 status_code=400,
#                 content={"error": f"Invalid time range format: {str(e)}"},
#             )

#     # Get total count first (unfiltered)
#     total_count_query = (
#         select(func.count(WorkflowRun.id))
#         .select_from(WorkflowRun)
#         .filter(WorkflowRun.workflow_id.isnot(None))
#         .apply_org_check(request)
#     )

#     # Create base query with joins
#     base_query = (
#         select(
#             WorkflowRun.id,
#             WorkflowRun.status,
#             WorkflowRun.created_at,
#             WorkflowRun.started_at,
#             WorkflowRun.ended_at,
#             WorkflowRun.workflow_id,
#             WorkflowRun.workflow_version_id,
#             WorkflowRun.machine_id,
#             WorkflowRun.gpu,
#             WorkflowRun.origin,
#             WorkflowRun.user_id,
#             WorkflowRun.deployment_id,  # Add deployment_id to selected fields
#             case(
#                 (
#                     WorkflowRun.ended_at.isnot(None)
#                     & WorkflowRun.started_at.isnot(None),
#                     func.extract(
#                         "epoch", WorkflowRun.ended_at - WorkflowRun.started_at
#                     ),
#                 ),
#                 else_=None,
#             ).label("duration"),
#         )
#         .select_from(WorkflowRun)
#         .filter(WorkflowRun.workflow_id.isnot(None))
#         .apply_org_check(request)
#     )

#     # Handle filters
#     if gpu_list:
#         base_query = base_query.filter(WorkflowRun.gpu.in_(gpu_list))

#     if status_list:
#         base_query = base_query.filter(WorkflowRun.status.in_(status_list))

#     if origin_list:
#         base_query = base_query.filter(WorkflowRun.origin.in_(origin_list))

#     if workflow_id:
#         base_query = base_query.filter(WorkflowRun.workflow_id == workflow_id)

#     if machine_id:
#         base_query = base_query.filter(WorkflowRun.machine_id == machine_id)

#     if deployment_id:  # Add deployment_id filter
#         base_query = base_query.filter(WorkflowRun.deployment_id == deployment_id)

#     if start_datetime and end_datetime:
#         base_query = base_query.filter(
#             WorkflowRun.created_at.between(start_datetime, end_datetime)
#         )

#     # Add duration filter
#     if duration:
#         try:
#             start_duration, end_duration = map(float, duration.split("-"))
#             base_query = base_query.filter(
#                 WorkflowRun.ended_at.isnot(None),
#                 WorkflowRun.started_at.isnot(None),
#                 func.extract(
#                     "epoch", WorkflowRun.ended_at - WorkflowRun.started_at
#                 ).between(start_duration, end_duration),
#             )
#         except (ValueError, TypeError) as e:
#             logger.error(f"Invalid duration range format: {e}")

#     # Check if any filters are applied
#     has_filters = any(
#         [
#             gpu_list,
#             status_list,
#             origin_list,
#             workflow_id,
#             machine_id,
#             deployment_id,  # Add deployment_id to filter check
#             start_datetime and end_datetime,  # Only count if both dates are present
#             duration,
#         ]
#     )

#     async def fetch_total_count():
#         async with AsyncSessionLocal() as count_db:
#             return await count_db.scalar(total_count_query)

#     async def fetch_filter_count():
#         if not has_filters:
#             return None  # Will use total_count instead if no filters
#         async with AsyncSessionLocal() as count_db:
#             filter_count_query = select(func.count()).select_from(base_query.subquery())
#             return await count_db.scalar(filter_count_query)

#     async def fetch_runs_data():
#         async with AsyncSessionLocal() as runs_db:
#             # Get paginated data
#             query = base_query.order_by(WorkflowRun.created_at.desc()).paginate(
#                 limit, offset
#             )
#             result = await runs_db.execute(query)
#             runs = result.unique().all()

#             if not runs:
#                 return []

#             runs_data = []
#             for (
#                 id,
#                 status,
#                 created_at,
#                 started_at,
#                 ended_at,
#                 workflow_id,
#                 workflow_version_id,
#                 machine_id,
#                 gpu,
#                 origin,
#                 user_id,
#                 deployment_id,  # Add deployment_id to unpacking
#                 duration,
#             ) in runs:
#                 run_dict = {
#                     "id": str(id),
#                     "status": status,
#                     "created_at": created_at.isoformat() if created_at else None,
#                     "started_at": started_at.isoformat() if started_at else None,
#                     "ended_at": ended_at.isoformat() if ended_at else None,
#                     "workflow_id": str(workflow_id),
#                     "workflow_version_id": str(workflow_version_id)
#                     if workflow_version_id
#                     else None,
#                     "machine_id": str(machine_id) if machine_id else None,
#                     "gpu": gpu,
#                     "origin": origin,
#                     "user_id": str(user_id) if user_id else None,
#                     "deployment_id": str(deployment_id) if deployment_id else None,  # Add deployment_id to response
#                     "duration": str(duration) if duration else None,
#                 }
#                 runs_data.append(run_dict)
#             return runs_data

#     async def fetch_chart_data():
#         async with AsyncSessionLocal() as chart_db:
#             chart_query = (
#                 select(
#                     func.date_trunc("hour", WorkflowRun.created_at).label("hour"),
#                     WorkflowRun.status,
#                     func.count().label("count"),
#                 )
#                 .select_from(WorkflowRun)
#                 .apply_org_check(request)
#             )

#             # Apply all filters to chart query
#             if gpu_list:
#                 chart_query = chart_query.filter(WorkflowRun.gpu.in_(gpu_list))
#             if status_list:
#                 chart_query = chart_query.filter(WorkflowRun.status.in_(status_list))
#             if origin_list:
#                 chart_query = chart_query.filter(WorkflowRun.origin.in_(origin_list))
#             if workflow_id:
#                 chart_query = chart_query.filter(WorkflowRun.workflow_id == workflow_id)
#             if machine_id:
#                 chart_query = chart_query.filter(WorkflowRun.machine_id == machine_id)
#             if deployment_id:  # Add deployment_id filter to chart query
#                 chart_query = chart_query.filter(WorkflowRun.deployment_id == deployment_id)
#             if start_datetime and end_datetime:
#                 chart_query = chart_query.filter(
#                     WorkflowRun.created_at.between(start_datetime, end_datetime)
#                 )
#             if duration:
#                 start_duration, end_duration = map(float, duration.split("-"))
#                 chart_query = chart_query.filter(
#                     WorkflowRun.ended_at.isnot(None),
#                     WorkflowRun.started_at.isnot(None),
#                     func.extract(
#                         "epoch", WorkflowRun.ended_at - WorkflowRun.started_at
#                     ).between(start_duration, end_duration),
#                 )

#             chart_query = chart_query.group_by("hour", WorkflowRun.status).order_by(
#                 "hour"
#             )
#             chart_result = await chart_db.execute(chart_query)
#             chart_rows = chart_result.all()

#             # Process chart data using defaultdict
#             chart_data = defaultdict(
#                 lambda: {"success": 0, "failed": 0, "others": 0, "timestamp": None}
#             )

#             for hour, status, count in chart_rows:
#                 timestamp = int(hour.timestamp() * 1000)
#                 chart_data[timestamp]["timestamp"] = timestamp

#                 if status.lower() == "success":
#                     chart_data[timestamp]["success"] += count
#                 elif status.lower() == "failed":
#                     chart_data[timestamp]["failed"] += count
#                 else:
#                     chart_data[timestamp]["others"] += count

#             return list(chart_data.values())

#     # Run all four queries in parallel
#     # total_count, filter_count, runs_data, chart_data = await asyncio.gather(
#     #     fetch_total_count(), fetch_filter_count(), fetch_runs_data(), fetch_chart_data()
#     # )

#     # dont run total count first
#     filter_count, runs_data, chart_data = await asyncio.gather(
#         fetch_filter_count(), fetch_runs_data(), fetch_chart_data()
#     )

#     return JSONResponse(
#         content={
#             "data": runs_data,
#             "meta": {
#                 "totalRowCount": 0,
#                 "filterRowCount": filter_count or 0,  # Use total_count as fallback
#                 "chartData": chart_data,
#             },
#         }
#     )
    
async def get_runs_count(
    request: Request,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    filters: Optional[Dict[str, Any]] = None
) -> int:
    """
    Get the total number of runs within the specified time range and filters.
    This is an optimized version that only returns the count without fetching full run data.
    
    Args:
        start_time (datetime, optional): Start time to filter runs
        end_time (datetime, optional): End time to filter runs
        filters (Dict, optional): Additional filters to apply. Valid filters are:
            - gpu (str): GPU type/name
            - status (str): Run status (success, failed, etc)
            - origin (str): Origin of the run
            - workflow_id (str): Workflow identifier
            - machine_id (str): Machine identifier
            - deployment_id (str): Deployment identifier
    
    Returns:
        int: Total number of runs matching the criteria
    """
    async with AsyncSessionLocal() as db:
        query = select(func.count(WorkflowRun.id)).apply_org_check_by_type(WorkflowRun, request)
        
        if start_time:
            query = query.filter(WorkflowRun.created_at >= start_time)
        if end_time:
            query = query.filter(WorkflowRun.created_at <= end_time)
            
        if filters:
            # Apply only whitelisted filters
            for key, value in filters.items():
                if key in VALID_FILTERS:
                    # Convert value to expected type
                    try:
                        typed_value = VALID_FILTERS[key](value)
                        query = query.filter(getattr(WorkflowRun, key) == typed_value)
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Invalid value for filter {key}: {value}")
                        continue
        
        return await db.scalar(query)

class RunFilter(BaseModel):
    """Filter model for run counts"""
    gpu: Optional[MachineGPU] = Field(None, description="GPU type to filter by")
    status: Optional[WorkflowRunStatus] = Field(None, description="Run status (e.g. success, failed)")
    origin: Optional[WorkflowRunOrigin] = Field(None, description="Origin of the run (e.g. api, manual)")
    workflow_id: Optional[str] = Field(None, description="Workflow identifier")
    machine_id: Optional[str] = Field(None, description="Machine identifier")
    deployment_id: Optional[str] = Field(None, description="Deployment identifier")

    class Config:
        json_schema_extra = {
            "example": {
                "status": "success",
                "gpu": "A100",
                "workflow_id": "123e4567-e89b-12d3-a456-426614174000"
            }
        }
        use_enum_values = True  # This ensures enum values are used in the JSON schema

class RunCountResponse(BaseModel):
    """Response model for the run count endpoint"""
    count: int = Field(description="Number of runs matching the criteria")
    start_time: Optional[str] = Field(None, description="ISO formatted start time if provided")
    end_time: Optional[str] = Field(None, description="ISO formatted end time if provided")
    filters: Optional[RunFilter] = Field(None, description="Applied filters")

    class Config:
        json_schema_extra = {
            "example": {
                "count": 42,
                "start_time": "2024-03-15T10:00:00",
                "end_time": "2024-03-15T11:00:00",
                "filters": {
                    "status": "success",
                    "gpu": "A100"
                }
            }
        }

@router.get("/runs/count", response_model=RunCountResponse)
async def count_runs(
    request: Request,
    start_time: Optional[str] = Query(
        None, 
        description="UTC ISO format datetime string (e.g. '2024-03-15T10:00:00Z')"
    ),
    end_time: Optional[str] = Query(
        None, 
        description="UTC ISO format datetime string (e.g. '2024-03-15T10:00:00Z')"
    ),
    gpu: Optional[MachineGPU] = Query(
        None, 
        description="GPU type to filter by"
    ),
    status: Optional[WorkflowRunStatus] = Query(
        None, 
        description="Run status to filter by (e.g. 'success', 'failed')"
    ),
    origin: Optional[WorkflowRunOrigin] = Query(
        None, 
        description="Origin of the run to filter by (e.g. 'api', 'manual')"
    ),
    workflow_id: Optional[str] = Query(
        None, 
        description="Workflow identifier to filter by"
    ),
    machine_id: Optional[str] = Query(
        None, 
        description="Machine identifier to filter by"
    ),
    deployment_id: Optional[str] = Query(
        None, 
        description="Deployment identifier to filter by"
    ),
):
    """
    REST endpoint to get run counts.

    Examples
    --------
    ```
    # Get counts within a time range (UTC)
    GET /runs/count?start_time=2024-03-15T10:00:00Z&end_time=2024-03-15T11:00:00Z

    # Get counts with status and GPU filters
    GET /runs/count?status=success&gpu=A100

    # Get counts for a specific workflow
    GET /runs/count?workflow_id=123e4567-e89b-12d3-a456-426614174000

    # Get counts for a specific deployment with status
    GET /runs/count?deployment_id=456e7890-f12d-34e5-b678-426614174000&status=success

    # Combine multiple filters
    GET /runs/count?gpu=A100&status=success&origin=api
    ```
    """
    try:
        if start_time:
            # Ensure UTC timezone
            start_time = datetime.fromisoformat(start_time.rstrip('Z')).replace(tzinfo=timezone.utc)
            
        if end_time:
            # Ensure UTC timezone
            end_time = datetime.fromisoformat(end_time.rstrip('Z')).replace(tzinfo=timezone.utc)
            
        # Build filters dict from query parameters
        filters = {}
        for key in VALID_FILTERS:
            value = locals().get(key)
            if value is not None:
                filters[key] = value
            
        count = await get_runs_count(
            request,
            start_time=start_time,
            end_time=end_time,
            filters=filters if filters else None
        )
        
        # Convert filters to RunFilter model for response
        filter_model = RunFilter(**filters) if filters else None
        
        return RunCountResponse(
            count=count,
            start_time=start_time.isoformat() if start_time else None,
            end_time=end_time.isoformat() if end_time else None,
            filters=filter_model
        )
        
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid time format. Please provide UTC time in ISO format (e.g. 2024-03-15T10:00:00Z): {str(e)}"}
        )
    except Exception as e:
        logger.error(f"Unexpected error in count_runs: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"}
        )
