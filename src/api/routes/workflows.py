import json
import logging
from fastapi import APIRouter, Depends, Request
from typing import List, Optional
from .types import WorkflowModel
from api.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from .utils import fetch_user_icon, post_process_output_data, select, is_valid_uuid, get_user_settings
from api.models import Workflow
from sqlalchemy import func
from fastapi.responses import JSONResponse
from sqlalchemy import text, cast, String, or_
from datetime import datetime
from uuid import UUID
from decimal import Decimal
import asyncio

logger = logging.getLogger(__name__)


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime, UUID)):
            return str(obj)
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def json_serial(obj):
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, (datetime, UUID)):
        return str(obj)
    raise TypeError(f"Type {type(obj)} not serializable")


router = APIRouter(tags=["Workflow"])


@router.get("/workflows", response_model=List[WorkflowModel])
async def get_workflows(
    request: Request,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    user_ids: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    # Base query with conditional user_ids filter
    user_filter = "AND wf.user_id = ANY(:user_ids)" if user_ids else ""
    
    raw_query = text(f"""
    WITH RECURSIVE search_param(term) AS (
        SELECT lower(:search)
    )
    SELECT
        wf.id AS id,
        wf.name AS name, 
        wf.created_at AS created_at, 
        wf.updated_at AS updated_at,
        wf.pinned AS pinned,
        wf.cover_image AS cover_image,
        users.name AS user_name,
        users.id AS user_id
    FROM 
        comfyui_deploy.workflows AS wf
    INNER JOIN 
        comfyui_deploy.users AS users ON users.id = wf.user_id
    WHERE 
        wf.deleted = false AND ((CAST(:org_id AS TEXT) IS NOT NULL AND wf.org_id = CAST(:org_id AS TEXT))
            OR (CAST(:org_id AS TEXT) IS NULL AND org_id IS NULL AND wf.user_id = CAST(:user_id AS TEXT)))
            AND (CAST(:search AS TEXT) IS NULL OR lower(wf.name) LIKE '%' || (SELECT term FROM search_param) || '%')
            {user_filter}
    ORDER BY 
        wf.pinned DESC,
        wf.updated_at DESC
    LIMIT :limit
    OFFSET :offset
    """)

    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user["org_id"] if "org_id" in current_user else None

    # Prepare parameters
    params = {
        "search": search,
        "limit": limit,
        "offset": offset,
        "org_id": org_id,
        "user_id": user_id,
    }
    
    # Only add user_ids to params if it's provided
    if user_ids:
        params["user_ids"] = user_ids.split(',')

    # Execute the query
    result = await db.execute(raw_query, params)

    workflows = [dict(row._mapping) for row in result.fetchall()]

    user_settings = await get_user_settings(request, db)

    # Fetch user icons
    unique_user_ids = list(
        set(workflow["user_id"] for workflow in workflows if workflow["user_id"])
    )
    user_icon_results = await asyncio.gather(
        *[fetch_user_icon(user_id) for user_id in unique_user_ids]
    )
    user_icons = {
        str(user_id): icon_data
        for user_id, icon_data in zip(unique_user_ids, user_icon_results)
    }

    for workflow in workflows:
        if "latest_output" in workflow and workflow["latest_output"]:
            await post_process_output_data(workflow["latest_output"], user_settings)
        user_icon = user_icons.get(str(workflow["user_id"]))
        workflow["user_icon"] = user_icon.image_url if user_icon else None

    # Use the custom encoder to serialize the data
    return JSONResponse(
        status_code=200,
        content=json.loads(json.dumps(workflows, cls=CustomJSONEncoder)),
    )


@router.get("/workflows/all", response_model=List[WorkflowModel])
async def get_all_workflows(
    request: Request,
    search: Optional[str] = None,
    limit: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    workflows_query = (
        select(Workflow)
        .order_by(Workflow.created_at.desc())
        .where(~Workflow.deleted)
        .apply_org_check(request)
    )

    if search:
        if is_valid_uuid(search):
            # Exact UUID match - most efficient
            workflows_query = workflows_query.where(Workflow.id == search)
        else:
            # Name search using trigram similarity for better performance
            workflows_query = workflows_query.where(Workflow.name.ilike(f"%{search}%"))

    if limit:
        workflows_query = workflows_query.limit(limit)

    result = await db.execute(workflows_query)
    workflows = result.unique().scalars().all()

    return [WorkflowModel.from_orm(workflow) for workflow in workflows]
