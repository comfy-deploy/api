from fastapi import APIRouter, HTTPException, Request, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from sqlalchemy import func, and_, or_
from typing import List, Optional, Any
from pydantic import BaseModel
import uuid
from datetime import datetime

from api.database import get_db
from api.models import OutputShare, WorkflowRun, User, WorkflowRunOutput, Deployment
from api.routes.utils import select

import os
import dub
import logging
from typing import Optional

router = APIRouter()


class UserInfo(BaseModel):
    """User authentication information extracted from request."""
    is_authenticated: bool
    user_id: Optional[str] = None
    org_id: Optional[str] = None


def get_user_info(request: Request) -> UserInfo:
    """
    Extract user information from request if authenticated.

    Returns:
        UserInfo object with authentication status and user details
    """
    if (
        not hasattr(request, "state")
        or not hasattr(request.state, "current_user")
        or request.state.current_user is None
    ):
        return UserInfo(is_authenticated=False, user_id=None, org_id=None)

    current_user = request.state.current_user
    return UserInfo(
        is_authenticated=True,
        user_id=current_user.get("user_id"),
        org_id=current_user.get("org_id"),
    )


# Replace the module-level initialization with a singleton pattern
class DubClient:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            dub_api_key = os.getenv("DUB_API_KEY")
            if not dub_api_key:
                logging.warning("DUB_API_KEY environment variable is not set")
                return None
            cls._instance = dub.Dub(token=dub_api_key)
        return cls._instance


def _check_dub_client() -> bool:
    client = DubClient.get_instance()
    if not client:
        logging.error("Dub client not initialized - missing API key")
        return False
    return True


async def create_dub_link(url: str, slug: str) -> Optional[str]:
    if not _check_dub_client():
        return None

    res = None
    client = DubClient.get_instance()

    try:
        res = await client.links.create_async(
            request={
                "url": url,
                "domain": "comfydeploy.link",
                "doIndex": True,
                "tagIds": "tag_Oxo856QUGcEhziqjHZ3PO0Hv",
                "external_id": slug,
                "proxy": True,
                "title": f"Comfy Deploy Share - {slug}",
                # "rewrite": True,
            }
        )
    except Exception as e:
        logging.error(f"Error creating dub link: {str(e)}")
        return None

    if res is not None:
        logging.info(f"link created: {res.short_link}")
        return res.short_link
    return None


async def get_dub_link(slug: str) -> Optional[str]:
    if not _check_dub_client():
        return None

    client = DubClient.get_instance()
    try:
        res = await client.links.get_async(
            request={"external_id": f"ext_{slug}"}
        )
        if res is not None:
            logging.info(f"link found: {res.short_link}")
            return res
    except Exception as e:
        logging.info(f"Link not found for slug: {slug}")
        return None

    return None


async def update_dub_link(link_id: str, url: str, slug: str) -> Optional[str]:
    if not _check_dub_client():
        return None

    res = None
    client = DubClient.get_instance()

    try:
        res = await client.links.update_async(
            link_id=link_id,
            request_body={
                "url": url,
                "domain": "comfydeploy.link",
                "doIndex": True,
                "tagIds": "tag_Oxo856QUGcEhziqjHZ3PO0Hv",
                "external_id": slug,
                "proxy": True,
                "title": f"Comfy Deploy Share - {slug}",
                # "rewrite": True,
            },
        )
    except Exception as e:
        logging.error(f"Error updating dub link: {str(e)}")
        return None

    if res is not None:
        logging.info(f"link updated: {res.short_link}")
        return res
    else:
        return None


class StudioUrlResponse(BaseModel):
    studio_url: str
    api_url: str


def get_studio_url_from_api_url(api_url: str, share_slug: str) -> str:
    """
    Convert API URL to studio URL based on environment.
    
    Examples:
    - api.comfydeploy.com -> studio.comfydeploy.com
    - staging.api.comfydeploy.com -> staging.studio.comfydeploy.com
    """
    if not api_url:
        raise ValueError("API URL is required")
    
    # Remove protocol if present
    if api_url.startswith("https://"):
        api_url = api_url[8:]
    elif api_url.startswith("http://"):
        api_url = api_url[7:]
    
    # Remove trailing slashes
    api_url = api_url.rstrip("/")
    
    # Handle different environments
    if api_url.startswith("staging.api."):
        # staging.api.comfydeploy.com -> staging.studio.comfydeploy.com
        studio_domain = api_url.replace("staging.api.", "staging.studio.")
    elif api_url.startswith("api."):
        # api.comfydeploy.com -> studio.comfydeploy.com
        studio_domain = api_url.replace("api.", "studio.")
    else:
        # For local development or other environments, assume similar pattern
        studio_domain = api_url.replace("api", "studio")
    
    # Split the share_slug to get username and slug parts
    if "_" in share_slug:
        username, slug_part = share_slug.split("_", 1)
        return f"https://{studio_domain}/share/playground/{username}/{slug_part}"
    else:
        # Fallback if slug doesn't contain underscore
        return f"https://{studio_domain}/share/playground/{share_slug}"


class OutputShareCreate(BaseModel):
    run_id: uuid.UUID
    output_id: uuid.UUID
    # deployment_id: Optional[uuid.UUID] = None
    output_type: str = "other"
    visibility: str = "private"


class OutputShareResponse(BaseModel):
    id: uuid.UUID
    user_id: str
    org_id: Optional[str]
    run_id: uuid.UUID
    output_id: uuid.UUID
    deployment_id: Optional[uuid.UUID]
    output_data: dict
    output_type: str
    visibility: str
    created_at: datetime
    updated_at: datetime
    inputs: Optional[Any] = None


def determine_output_type(output_data: dict) -> str:
    """Determine output type based on output data"""
    if not output_data:
        return "other"

    if any(
        key.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        for key in output_data.keys()
        if isinstance(key, str)
    ):
        return "image"

    if any(
        key.lower().endswith((".mp4", ".avi", ".mov", ".webm"))
        for key in output_data.keys()
        if isinstance(key, str)
    ):
        return "video"

    if any(
        key.lower().endswith((".obj", ".fbx", ".gltf", ".glb", ".ply"))
        for key in output_data.keys()
        if isinstance(key, str)
    ):
        return "3d"

    return "other"


@router.post("/share/output", response_model=OutputShareResponse)
async def create_output_share(
    share_data: OutputShareCreate, request: Request, db: AsyncSession = Depends(get_db)
):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    run_query = (
        select(WorkflowRun)
        .where(WorkflowRun.id == share_data.run_id)
        .apply_org_check(request)
    )
    run_result = await db.execute(run_query)
    run = run_result.scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found")

    output_query = (
        select(WorkflowRunOutput)
        .join(WorkflowRun, WorkflowRun.id == WorkflowRunOutput.run_id)
        .where(
            and_(
                WorkflowRunOutput.id == share_data.output_id,
                WorkflowRunOutput.run_id == share_data.run_id,
            )
        )
        .apply_org_check_by_type(WorkflowRun, request)
    )
    output_result = await db.execute(output_query)
    output = output_result.scalar_one_or_none()

    if not output:
        raise HTTPException(status_code=404, detail="Output not found")

    # Check if this output was already shared
    duplicate_query = select(OutputShare).where(
        OutputShare.output_id == share_data.output_id
    )
    duplicate_result = await db.execute(duplicate_query)
    existing_share = duplicate_result.scalars().first()
    if existing_share:
        return OutputShareResponse(
            id=existing_share.id,
            user_id=existing_share.user_id,
            org_id=existing_share.org_id,
            run_id=existing_share.run_id,
            output_id=existing_share.output_id,
            deployment_id=existing_share.deployment_id,
            output_data=existing_share.output_data,
            output_type=existing_share.output_type,
            visibility=existing_share.visibility,
            created_at=existing_share.created_at,
            updated_at=existing_share.updated_at,
            inputs=existing_share.inputs,
        )

    if share_data.output_type == "other":
        detected_type = determine_output_type(output.data or {})
        share_data.output_type = detected_type

    output_share = OutputShare(
        user_id=user["user_id"],
        org_id=user.get("org_id"),
        run_id=share_data.run_id,
        output_id=share_data.output_id,
        deployment_id=run.deployment_id,
        output_data=output.data or {},
        inputs=run.workflow_inputs,
        output_type=share_data.output_type,
        visibility=share_data.visibility,
    )

    db.add(output_share)
    await db.commit()
    await db.refresh(output_share)

    return OutputShareResponse(
        id=output_share.id,
        user_id=output_share.user_id,
        org_id=output_share.org_id,
        run_id=output_share.run_id,
        output_id=output_share.output_id,
        deployment_id=output_share.deployment_id,
        output_data=output_share.output_data,
        output_type=output_share.output_type,
        visibility=output_share.visibility,
        created_at=output_share.created_at,
        updated_at=output_share.updated_at,
        inputs=output_share.inputs,
    )


@router.get("/share/output", response_model=List[OutputShareResponse])
async def list_output_shares(
    request: Request,
    output_type: Optional[str] = Query(None, description="Filter by output type"),
    visibility: Optional[str] = Query(None, description="Filter by visibility"),
    deployment_id: Optional[str] = Query(None, description="Filter by deployment ID"),
    include_public: bool = Query(
        True, description="Include public shares for authenticated users"
    ),
    limit: int = Query(20, description="Maximum number of items to return"),
    offset: int = Query(0, description="Number of items to skip"),
    db: AsyncSession = Depends(get_db),
):
    user = getattr(request.state, "current_user", None)

    query = select(OutputShare)
    conditions = []

    if user:
        org_condition = and_(
            OutputShare.user_id == user["user_id"],
            or_(OutputShare.visibility == "private", OutputShare.visibility == "link"),
        )

        if include_public:
            conditions.append(or_(org_condition, OutputShare.visibility == "public"))
        else:
            conditions.append(org_condition)
    else:
        conditions.append(OutputShare.visibility == "public")

    if output_type:
        conditions.append(OutputShare.output_type == output_type)

    if visibility:
        conditions.append(OutputShare.visibility == visibility)

    if deployment_id:
        conditions.append(OutputShare.deployment_id == deployment_id)

    if conditions:
        query = query.where(and_(*conditions))

    query = query.order_by(OutputShare.created_at.desc()).paginate(limit, offset)

    result = await db.execute(query)
    shares = result.scalars().all()

    return [
        OutputShareResponse(
            id=share.id,
            user_id=share.user_id,
            org_id=share.org_id,
            run_id=share.run_id,
            output_id=share.output_id,
            deployment_id=share.deployment_id,
            output_data=share.output_data,
            output_type=share.output_type,
            visibility=share.visibility,
            created_at=share.created_at,
            updated_at=share.updated_at,
            inputs=share.inputs,
        )
        for share in shares
    ]


@router.get("/share/output/{share_id}")
async def get_shared_output(
    share_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
):
    user = getattr(request.state, "current_user", None)

    query = (
        select(OutputShare)
        .options(
            joinedload(OutputShare.run),
            joinedload(OutputShare.user),
            joinedload(OutputShare.output),
        )
        .where(OutputShare.id == share_id)
    )

    result = await db.execute(query)
    share = result.scalar_one_or_none()

    if not share:
        raise HTTPException(status_code=404, detail="Shared output not found")

    if share.visibility == "private":
        if not user or user["user_id"] != share.user_id:
            raise HTTPException(status_code=403, detail="Access denied")
    elif share.visibility == "link":
        pass
    elif share.visibility == "public":
        pass

    return {
        "share": OutputShareResponse(
            id=share.id,
            user_id=share.user_id,
            org_id=share.org_id,
            run_id=share.run_id,
            output_id=share.output_id,
            deployment_id=share.deployment_id,
            output_data=share.output_data,
            output_type=share.output_type,
            visibility=share.visibility,
            created_at=share.created_at,
            updated_at=share.updated_at,
            inputs=share.inputs,
        ),
        "run": (
            {
                "id": share.run.id,
                "status": share.run.status,
                "created_at": share.run.created_at,
            }
            if share.run
            else None
        ),
    }


@router.delete("/share/output/{share_id}")
async def delete_output_share(
    share_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    query = (
        select(OutputShare)
        .where(OutputShare.id == share_id)
        .apply_org_check(request)
    )
    result = await db.execute(query)
    share = result.scalar_one_or_none()

    if not share:
        raise HTTPException(status_code=404, detail="Output share not found")

    await db.delete(share)
    await db.commit()

    return {"message": "Output share deleted successfully"}


@router.get("/share/deployment/{deployment_id}/studio-url", response_model=StudioUrlResponse)
async def get_deployment_studio_url(
    deployment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Get the studio URL for a deployment based on the current environment.
    
    This endpoint converts a deployment ID to the proper studio URL by:
    1. Finding the deployment's share_slug
    2. Determining the current environment from CURRENT_API_URL
    3. Constructing the appropriate studio URL
    
    Only allows deployments with environments: public-share, private-share, community-share
    For private-share deployments, checks if user has matching org_id or user_id
    """
    # Get user info for authorization
    user_info = get_user_info(request)
    
    # Query the deployment by ID, only allowing share environments
    query = (
        select(Deployment)
        .where(
            Deployment.id == deployment_id,
            Deployment.environment.in_(["public-share", "private-share", "community-share"])
        )
    )
    result = await db.execute(query)
    deployment = result.scalar_one_or_none()
    
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")
    
    # Check authorization for private-share deployments
    if deployment.environment == "private-share":
        if not user_info.is_authenticated:
            raise HTTPException(status_code=401, detail="Authentication required")
        
        # Check if user has access to this private deployment
        if deployment.org_id is not None:
            if deployment.org_id != user_info.org_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
        else:
            if deployment.user_id != user_info.user_id:
                raise HTTPException(status_code=401, detail="Unauthorized")
    
    if not deployment.share_slug:
        raise HTTPException(
            status_code=400, 
            detail="Deployment does not have a share slug. Only shared deployments can be accessed in studio."
        )
    
    # Get the current API URL from environment
    current_api_url = os.getenv("CURRENT_API_URL")
    if not current_api_url:
        raise HTTPException(
            status_code=500, 
            detail="CURRENT_API_URL environment variable not set"
        )
    
    try:
        studio_url = get_studio_url_from_api_url(current_api_url, deployment.share_slug)
        return StudioUrlResponse(
            studio_url=studio_url,
            api_url=current_api_url
        )
    except Exception as e:
        logging.error(f"Error generating studio URL: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Error generating studio URL: {str(e)}"
        )
