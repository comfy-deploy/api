import asyncio
import logging
import uuid
from api.routes.run import redeploy_comfy_deploy_runner_if_exists
from api.utils.inputs import get_inputs_from_workflow_api
from api.utils.outputs import get_outputs_from_workflow
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import List, Optional
from .types import (
    DeploymentModel,
    DeploymentEnvironment,
    DeploymentShareModel,
    DeploymentFeaturedModel,
)
from sqlalchemy.ext.asyncio import AsyncSession
from .utils import select
from api.models import (
    Deployment,
    Machine,
    MachineVersion,
    Workflow,
    WorkflowRun,
    WorkflowVersion,
)
from api.database import get_db
from sqlalchemy.orm import joinedload
from pydantic import BaseModel
from enum import Enum
from api.modal.builder import GPUType, KeepWarmBody, set_machine_always_on
from .platform import slugify
from .share import create_dub_link, get_dub_link, update_dub_link
from sqlalchemy import func
import asyncio
from nanoid import generate
import re

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Deployments"])

# Add TTL constants at the top with other imports
PREVIEW_TTL_HOURS = 24
STAGING_TTL_HOURS = 48  # 2 days
PUBLIC_SHARE_TTL_HOURS = 24
PRODUCTION_TTL_HOURS = None  # Indefinite

# Create a map of environment to TTL hours for easier lookup
ENVIRONMENT_TTL_MAP = {
    "preview": PREVIEW_TTL_HOURS,
    "staging": STAGING_TTL_HOURS,
    "public-share": PUBLIC_SHARE_TTL_HOURS,
    "production": PRODUCTION_TTL_HOURS,
}


class GPUType(str, Enum):
    CPU = "CPU"
    T4 = "T4"
    L4 = "L4"
    A10G = "A10G"
    L40S = "L40S"
    A100 = "A100"
    A100_80GB = "A100-80GB"
    H100 = "H100"
    H200 = "H200"
    B200 = "B200"


class DeploymentCreate(BaseModel):
    workflow_version_id: str
    workflow_id: str
    machine_id: Optional[str] = None
    machine_version_id: Optional[str] = None
    environment: str
    description: Optional[str] = None


class DeploymentUpdate(BaseModel):
    workflow_version_id: Optional[str] = None
    machine_id: Optional[str] = None
    machine_version_id: Optional[str] = None
    concurrency_limit: Optional[int] = None
    gpu: Optional[GPUType] = None
    run_timeout: Optional[int] = None
    idle_timeout: Optional[int] = None
    keep_warm: Optional[int] = None


async def update_deployment_with_machine(
    deployment: Deployment,
    machine_id: str,
    machine_version: Optional[MachineVersion],
    db: AsyncSession,
    update_data: Optional[DeploymentUpdate] = None,
) -> Deployment:
    """Update deployment with machine and machine version information."""
    # Store original values to check for changes
    original_modal_image_id = deployment.modal_image_id
    original_run_timeout = deployment.run_timeout
    original_idle_timeout = deployment.idle_timeout
    original_concurrency_limit = deployment.concurrency_limit
    original_keep_warm = deployment.keep_warm

    deployment.machine_id = machine_id

    if machine_version is not None and machine_version.modal_image_id is not None:
        deployment.machine_version_id = machine_version.id
        deployment.modal_image_id = machine_version.modal_image_id

        # Only update these fields from machine_version if not provided in update_data
        if update_data is None or update_data.gpu is None:
            deployment.gpu = machine_version.gpu
        if update_data is None or update_data.run_timeout is None:
            deployment.run_timeout = machine_version.run_timeout
        if update_data is None or update_data.idle_timeout is None:
            deployment.idle_timeout = machine_version.idle_timeout

    # Update fields from update_data if provided
    if update_data is not None:
        if update_data.concurrency_limit is not None:
            deployment.concurrency_limit = update_data.concurrency_limit
        if update_data.gpu is not None:
            deployment.gpu = update_data.gpu
        if update_data.run_timeout is not None:
            deployment.run_timeout = update_data.run_timeout
        if update_data.idle_timeout is not None:
            deployment.idle_timeout = update_data.idle_timeout
        if update_data.keep_warm is not None:
            deployment.keep_warm = update_data.keep_warm

    # Check if any deployment-critical parameters have changed
    should_redeploy = (
        (
            machine_version is not None
            and machine_version.modal_image_id is not None
            and original_modal_image_id != machine_version.modal_image_id
        )
        or original_run_timeout != deployment.run_timeout
        or original_idle_timeout != deployment.idle_timeout
        or original_concurrency_limit != deployment.concurrency_limit
    )

    if should_redeploy:
        # We should trigger a redeploy with the final values
        await redeploy_comfy_deploy_runner_if_exists(
            machine_id, deployment.gpu, deployment
        )

    # Handle keep_warm changes
    keep_warm_changed = original_keep_warm != deployment.keep_warm
    if keep_warm_changed:
        logger.info(
            f"Keep warm changed for deployment {deployment.id} to {deployment.keep_warm}"
        )
        try:
            await set_machine_always_on(
                str(deployment.id),
                KeepWarmBody(
                    warm_pool_size=deployment.keep_warm, gpu=GPUType(deployment.gpu)
                ),
            )
        except Exception as e:
            # This is expected to fail if the deployment is not found
            logger.warning(f"Error setting machine always on: {e}", exc_info=True)

    return deployment


@router.post(
    "/deployment",
    response_model=DeploymentModel,
    openapi_extra={
        "x-speakeasy-name-override": "create",
    },
)
async def create_deployment(
    request: Request,
    deployment_data: DeploymentCreate,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.state.current_user["user_id"]
    org_id = (
        request.state.current_user["org_id"]
        if "org_id" in request.state.current_user
        else None
    )

    if (
        deployment_data.machine_id is None
        and deployment_data.machine_version_id is None
    ):
        raise HTTPException(
            status_code=400, detail="Machine ID or Machine Version ID is required"
        )

    try:
        # Check for existing deployment with same environment
        existing_deployment_query = (
            select(Deployment)
            .where(
                Deployment.workflow_id == deployment_data.workflow_id,
                Deployment.environment == deployment_data.environment,
            )
            .apply_org_check(request)
        )

        result = await db.execute(existing_deployment_query)
        existing_deployment = result.scalar_one_or_none()

        machine_version = None
        machine_id = deployment_data.machine_id

        if deployment_data.machine_version_id is not None:
            machine_version_query = select(MachineVersion).where(
                MachineVersion.id == deployment_data.machine_version_id
            )
            result = await db.execute(machine_version_query)
            machine_version = result.scalar_one_or_none()
            machine_id = machine_version.machine_id

        # Get current machine and machine version
        machine_query = select(Machine).where(Machine.id == machine_id)
        result = await db.execute(machine_query)
        machine = result.scalar_one_or_none()
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

        if machine.machine_version_id and machine_version is None:
            machine_version_query = select(MachineVersion).where(
                MachineVersion.id == machine.machine_version_id
            )
            result = await db.execute(machine_version_query)
            machine_version = result.scalar_one_or_none()

        isPublicShare = deployment_data.environment == "public-share"

        if isPublicShare:
            workflow_id_for_slug = (
                existing_deployment.workflow_id
                if existing_deployment
                else deployment_data.workflow_id
            )
            workflow_result = await db.execute(
                select(Workflow).where(Workflow.id == workflow_id_for_slug)
            )
            workflow_obj = workflow_result.scalar_one_or_none()
            if not workflow_obj:
                raise HTTPException(status_code=404, detail="Workflow not found")
            current_user_id = org_id if org_id else user_id

            generated_slug = await slugify(workflow_obj.name, current_user_id)

        share_link = None
        if isPublicShare and generated_slug and "_" in generated_slug:
            name_part, id_part = generated_slug.split("_", 1)
            domain = (
                "https://www.comfydeploy.com"  # TODO: Figure out how to get the domain
            )
            share_link = f"{domain}/share/{name_part}/{id_part}"
        else:
            generated_slug = None

        isUpdate = existing_deployment is not None
        previous_share_slug = existing_deployment.share_slug if isUpdate else None

        if existing_deployment:
            # Update existing deployment
            if deployment_data.environment == "public-share":
                existing_deployment.share_slug = previous_share_slug
            else:
                existing_deployment.share_slug = None
            existing_deployment.workflow_version_id = (
                deployment_data.workflow_version_id
            )
            existing_deployment.description = deployment_data.description
            deployment = await update_deployment_with_machine(
                existing_deployment, machine_id, machine_version, db
            )
        else:
            # Create new deployment object
            deployment = Deployment(
                id=uuid.uuid4(),
                user_id=user_id,
                org_id=org_id,
                workflow_version_id=deployment_data.workflow_version_id,
                workflow_id=deployment_data.workflow_id,
                environment=deployment_data.environment,
                description=deployment_data.description,
                share_slug=generated_slug,
            )
            deployment = await update_deployment_with_machine(
                deployment, machine_id, machine_version, db
            )
            db.add(deployment)

        await db.commit()
        await db.refresh(deployment)

        # Skip dub link creation for now
        # if isPublicShare and share_link and generated_slug:
        #     if isUpdate:
        #         # This is an existing deployment being updated
        #         if previous_share_slug is None:
        #             # Case 1: Deployment wasn't previously public, create new dub link
        #             await create_dub_link(share_link, generated_slug)
        #         elif previous_share_slug != generated_slug:
        #             # Case 2: Share slug changed, need to update existing dub link
        #             current_dub_link = await get_dub_link(previous_share_slug)
        #             if current_dub_link:
        #                 # Update existing dub link
        #                 await update_dub_link(
        #                     current_dub_link.id, share_link, generated_slug
        #                 )
        #             else:
        #                 # Dub link doesn't exist (edge case), create new one
        #                 await create_dub_link(share_link, generated_slug)
        #         else:
        #             # Case 3: Share slug hasn't changed, verify dub link exists
        #             current_dub_link = await get_dub_link(previous_share_slug)
        #             if not current_dub_link:
        #                 # Dub link missing but should exist, create it
        #                 await create_dub_link(share_link, generated_slug)
        #     else:
        #         # Case 4: New deployment, create new dub link
        #         await create_dub_link(share_link, generated_slug)

        # Convert to dict
        deployment_dict = deployment.to_dict()
        return deployment_dict
    except Exception as e:
        logger.error(f"Error creating deployment: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error: " + str(e))


@router.patch(
    "/deployment/{deployment_id}",
    response_model=DeploymentModel,
    openapi_extra={
        "x-speakeasy-name-override": "update",
    },
)
async def update_deployment(
    request: Request,
    deployment_id: str,
    deployment_data: DeploymentUpdate,
    db: AsyncSession = Depends(get_db),
):
    try:
        # Get existing deployment
        deployment_query = (
            select(Deployment)
            .where(Deployment.id == deployment_id)
            .apply_org_check(request)
        )

        result = await db.execute(deployment_query)
        deployment = result.scalar_one_or_none()

        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

        logger.info(f"Deployment data: {deployment_data}")

        machine_version = None
        machine_id = deployment_data.machine_id or deployment.machine_id

        if deployment_data.machine_version_id is not None:
            machine_version_query = select(MachineVersion).where(
                MachineVersion.id == deployment_data.machine_version_id
            )
            result = await db.execute(machine_version_query)
            machine_version = result.scalar_one_or_none()
            machine_id = machine_version.machine_id

        # Get current machine and machine version
        if machine_id:
            machine_query = select(Machine).where(Machine.id == machine_id)
            result = await db.execute(machine_query)
            machine = result.scalar_one_or_none()
            if not machine:
                raise HTTPException(status_code=404, detail="Machine not found")

            if machine.machine_version_id and machine_version is None:
                machine_version_query = select(MachineVersion).where(
                    MachineVersion.id == machine.machine_version_id
                )
                result = await db.execute(machine_version_query)
                machine_version = result.scalar_one_or_none()

        # Update workflow version if provided
        if deployment_data.workflow_version_id:
            deployment.workflow_version_id = deployment_data.workflow_version_id

        # Update machine-related fields and other deployment settings
        deployment = await update_deployment_with_machine(
            deployment, machine_id, machine_version, db, deployment_data
        )

        await db.commit()
        await db.refresh(deployment)

        # Convert to dict
        deployment_dict = deployment.to_dict()
        return deployment_dict

    except Exception as e:
        logger.error(f"Error updating deployment: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/deployments",
    response_model=List[DeploymentModel],
    openapi_extra={
        "x-speakeasy-name-override": "list",
    },
)
async def get_deployments(
    request: Request,
    environment: Optional[DeploymentEnvironment] = None,
    # Fluid deployment meaning they are modal app per deployment
    is_fluid: bool = False,
    page_size: Optional[int] = None,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    try:
        query = (
            select(Deployment)
            .options(
                joinedload(Deployment.workflow).load_only(Workflow.name),
                joinedload(Deployment.version),
            )
            .join(Workflow)
            .where(Workflow.deleted == False)
            .order_by(Deployment.updated_at.desc())
        )

        if is_fluid:
            query = query.where(Deployment.modal_image_id.is_not(None))
        else:
            query = query.where(Deployment.modal_image_id.is_(None))

        if environment is not None:
            query = query.where(Deployment.environment == environment)

        query = query.apply_org_check(request)

        if page_size is not None:
            query = query.paginate(page_size, offset)

        result = await db.execute(query)
        deployments = result.scalars().all()

        deployments_data = []
        dub_link_tasks = {}

        for idx, deployment in enumerate(deployments):
            deployment_dict = deployment.to_dict()

            workflow_api = (
                deployment.version.workflow_api if deployment.version else None
            )
            inputs = get_inputs_from_workflow_api(workflow_api)

            workflow = deployment.version.workflow if deployment.version else None
            outputs = get_outputs_from_workflow(workflow)

            if inputs:
                deployment_dict["input_types"] = inputs

            if outputs:
                deployment_dict["output_types"] = outputs

            if deployment.environment == "public-share" and deployment.share_slug:
                dub_link_tasks[idx] = asyncio.create_task(
                    get_dub_link(deployment.share_slug)
                )

            deployments_data.append(deployment_dict)

        if dub_link_tasks:
            dub_link_results = await asyncio.gather(*dub_link_tasks.values())
            for idx, dub_link in zip(dub_link_tasks.keys(), dub_link_results):
                if dub_link:
                    deployments_data[idx]["dub_link"] = dub_link.short_link

        return deployments_data

    except Exception as e:
        logger.error(f"Error getting deployments: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/share/{username}/{slug}",
)
async def get_share_deployment(
    request: Request,
    username: str,
    slug: str,
    db: AsyncSession = Depends(get_db),
) -> DeploymentShareModel:
    slug = f"{username}_{slug}"

    deployment_query = (
        select(Deployment)
        .options(
            joinedload(Deployment.workflow).load_only(Workflow.name),
            joinedload(Deployment.version),
        )
        .join(Workflow)
        .where(
            Deployment.share_slug == slug,
            Deployment.environment == "public-share",
            Workflow.deleted == False,
        )
    )

    result = await db.execute(deployment_query)
    deployment = result.scalar_one_or_none()
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    workflow_api = deployment.version.workflow_api if deployment.version else None
    inputs = get_inputs_from_workflow_api(workflow_api)

    workflow = deployment.version.workflow if deployment.version else None
    outputs = get_outputs_from_workflow(workflow)

    # Just update the deployment with the additional fields
    deployment_dict = deployment.to_dict()
    deployment_dict["input_types"] = inputs
    deployment_dict["output_types"] = outputs

    # FastAPI will automatically filter based on DeploymentModel
    return deployment_dict


@router.get(
    "/deployments/featured",
    response_model=List[DeploymentFeaturedModel],
)
async def get_featured_deployments(
    db: AsyncSession = Depends(get_db),
) -> List[DeploymentFeaturedModel]:
    featured_deployments_org_id = "org_2toS7J4JJDhTdlSlIy2Lb2IAmQA"

    deployments_featured_query = (
        select(Deployment)
        .options(
            joinedload(Deployment.workflow).load_only(
                Workflow.name, Workflow.cover_image
            ),
            joinedload(Deployment.version),
        )
        .join(Workflow)
        .where(
            Deployment.environment == "public-share",
            Workflow.deleted == False,
            Deployment.org_id == featured_deployments_org_id,
            Deployment.share_slug.isnot(None),  # Ensure share_slug is not None
        )
        .order_by(Deployment.updated_at.desc())
    )

    result = await db.execute(deployments_featured_query)
    deployments = result.scalars().all()

    deployments_data = []
    for deployment in deployments:
        deployment_dict = deployment.to_dict()

        if deployment.version and hasattr(deployment.version, "workflow"):
            deployment_dict["workflow"]["workflow"] = deployment.version.workflow

        deployments_data.append(deployment_dict)

    return deployments_data


@router.post(
    "/deployment/{deployment_id}/deactivate",
    response_model=DeploymentModel,
    openapi_extra={
        "x-speakeasy-name-override": "deactivate",
    },
)
async def deactivate_deployment(
    request: Request,
    deployment_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        # Get the deployment
        deployment_query = (
            select(Deployment)
            .where(Deployment.id == deployment_id)
            .apply_org_check(request)
        )

        result = await db.execute(deployment_query)
        deployment = result.scalar_one_or_none()

        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

        deactivated = await _deactivate_deployment_internal(
            request, deployment, db, check_active_runs=True
        )
        if not deactivated:
            raise HTTPException(
                status_code=400,
                detail="Failed to deactivate deployment. It may have active runs or the modal app could not be stopped.",
            )

        await db.commit()
        return deactivated

    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        logger.error(f"Error deactivating deployment: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


async def _deactivate_deployment_internal(
    request: Request,
    deployment: Deployment,
    db: AsyncSession,
    check_active_runs: bool = True,
) -> Optional[dict]:
    """Internal function to deactivate a deployment, returns the deactivated deployment dict if successful"""
    try:
        if check_active_runs:
            # Check for active runs using count
            active_runs_count = await db.scalar(
                select(func.count())
                .select_from(WorkflowRun)
                .where(
                    WorkflowRun.deployment_id == deployment.id,
                    WorkflowRun.status.not_in(
                        ["success", "failed", "timeout", "cancelled"]
                    ),
                )
            )

            if active_runs_count > 0:
                logger.info(
                    f"Skipping deployment {deployment.id} with {active_runs_count} active runs"
                )
                return None

        if deployment.modal_app_id:
            try:
                app_name = deployment.modal_app_id
                process = await asyncio.subprocess.create_subprocess_shell(
                    "modal app stop " + app_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await process.wait()
                logger.info(f"Successfully stopped modal app: {app_name}")
                deployment.activated_at = None
                return deployment.to_dict()
            except Exception as e:
                logger.error(
                    f"Error stopping modal app for deployment {deployment.id}: {str(e)}"
                )
                return None

        return True

    except Exception as e:
        logger.error(
            f"Error in internal deactivation for deployment {deployment.id}: {e}"
        )
        return None


@router.get(
    "/deployment/{deployment_id}",
    response_model=DeploymentModel,
    openapi_extra={
        "x-speakeasy-name-override": "get",
    },
)
async def get_deployment(
    request: Request,
    deployment_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        # Get the deployment with workflow and version information
        deployment_query = (
            select(Deployment)
            .options(
                joinedload(Deployment.workflow).load_only(Workflow.name),
                joinedload(Deployment.version),
            )
            .join(Workflow)
            .where(
                Deployment.id == deployment_id,
                Workflow.deleted == False,
            )
            .apply_org_check(request)
        )

        result = await db.execute(deployment_query)
        deployment = result.scalar_one_or_none()

        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

        # Get workflow inputs and outputs
        workflow_api = deployment.version.workflow_api if deployment.version else None
        inputs = get_inputs_from_workflow_api(workflow_api)

        workflow = deployment.version.workflow if deployment.version else None
        outputs = get_outputs_from_workflow(workflow)

        # Convert to dict and add additional fields
        deployment_dict = deployment.to_dict()
        if inputs:
            deployment_dict["input_types"] = inputs
        if outputs:
            deployment_dict["output_types"] = outputs

        # Add dub link for public share deployments
        if deployment.environment == "public-share" and deployment.share_slug:
            dub_link = await get_dub_link(deployment.share_slug)
            if dub_link:
                deployment_dict["dub_link"] = dub_link.short_link

        return deployment_dict

    except Exception as e:
        logger.error(f"Error getting deployment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete(
    "/deployment/{deployment_id}",
    openapi_extra={
        "x-speakeasy-name-override": "delete",
    },
)
async def delete_deployment(
    request: Request,
    deployment_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        # Query deployment with environment check
        deployment_query = (
            select(Deployment)
            .where(
                Deployment.id == deployment_id,
                Deployment.environment.in_(["public-share", "private-share"]),
            )
            .apply_org_check(request)
        )

        result = await db.execute(deployment_query)
        deployment = result.scalar_one_or_none()

        if not deployment:
            raise HTTPException(
                status_code=404,
                detail="Share deployment not found or you can only delete share deployments",
            )

        await deactivate_deployment(request, deployment_id, db)

        # Delete the deployment
        await db.delete(deployment)
        await db.commit()

        return {"message": "Share deployment deleted successfully"}

    except Exception as e:
        logger.error(f"Error deleting deployment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# === Helper: build v0 UI spec ==================================================

import os
from api.utils.component_templates import generate_component_code, generate_api_routes, _slugify_simple

CURRENT_API_URL = os.getenv("CURRENT_API_URL")


# === Route: Generate v0 UI spec ===============================================

@router.get(
    "/deployment/{deployment_id}/v0-ui-spec",
    openapi_extra={"x-speakeasy-name-override": "getV0UiSpec"},
)
async def get_deployment_v0_ui_spec(
    request: Request,
    deployment_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Return a Vercel v0 compatible registry JSON describing a simple UI for the deployment."""
    try:
        # Re-use the same logic from get_deployment to fetch deployment & inputs
        deployment_query = (
            select(Deployment)
            .options(
                joinedload(Deployment.workflow).load_only(Workflow.name),
                joinedload(Deployment.version),
            )
            .join(Workflow)
            .where(
                Deployment.id == deployment_id,
                Workflow.deleted == False,
            )
            .apply_org_check(request)
        )

        result = await db.execute(deployment_query)
        deployment = result.scalar_one_or_none()
        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

        workflow_api = deployment.version.workflow_api if deployment.version else None
        inputs = get_inputs_from_workflow_api(workflow_api) or []

        # Build component code
        component_code, deps = generate_component_code(inputs)

        # Build registry spec
        workflow_name = deployment.workflow.name if deployment.workflow else "deployment"
        slug_name = _slugify_simple(workflow_name)
        block_name = f"workflow-{slug_name}"

        # --- server action/route examples ---
        api_base = CURRENT_API_URL + "/api"
        generate_route, poll_route = generate_api_routes(deployment_id, api_base)

        spec = {
            "$schema": "https://ui.shadcn.com/schema/registry-item.json",
            "name": block_name,
            "type": "registry:block",
            "author": "comfydeploy",
            "description": f"Autogenerated UI for deployment {deployment_id}",
            "registryDependencies": deps,
            "files": [
                {
                    "path": f"blocks/{block_name}/page.tsx",
                    "content": component_code,
                    "type": "registry:page",
                    "target": "app/page.tsx",
                },
                {
                    "path": "blocks/common/api_generate_route.ts",
                    "content": generate_route,
                    "type": "registry:component",
                    "target": "app/api/generate/route.ts",
                },
                {
                    "path": "blocks/common/api_poll_route.ts",
                    "content": poll_route,
                    "type": "registry:component",
                    "target": "app/api/poll/route.ts",
                },
            ],
            "categories": ["workflow", "form"],
        }

        return spec

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating v0 UI spec: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
