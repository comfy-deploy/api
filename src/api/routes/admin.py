import os
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import Deployment, WorkflowRun
from api.routes.deployments import (
    DeploymentModel,
    _deactivate_deployment_internal,
    ENVIRONMENT_TTL_MAP
)
from api.routes.utils import check_admin_org

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

class TTLScanResponse(BaseModel):
    deactivated: List[DeploymentModel]
    would_deactivate: List[DeploymentModel]

@router.post(
    "/deployments/scan-ttl",
    response_model=TTLScanResponse,
    openapi_extra={
        "x-speakeasy-name-override": "scanTTL",
    },
)
async def scan_deployment_ttl(
    request: Request,
    dry_run: bool = True,  # Default to dry run for safety
    db: AsyncSession = Depends(get_db),
):
    """
    Scan deployments and deactivate those that exceed their TTL based on environment:
    - Preview: 24 hours
    - Staging: 48 hours
    - Public Share: 24 hours
    - Production: Indefinite

    Parameters:
        dry_run: If True, only returns what would be deactivated without making changes
    """
    check_admin_org(request)
    
    try:
        query = (
            select(Deployment)
            .where(
                Deployment.activated_at.isnot(None),
                Deployment.environment.in_(["preview", "staging", "public-share"])
            )
        )

        result = await db.execute(query)
        deployments = result.scalars().all()
        
        deactivated_deployments = []
        would_deactivate_deployments = []
        now = datetime.utcnow()

        for deployment in deployments:
            if not deployment.activated_at:
                continue

            ttl_hours = ENVIRONMENT_TTL_MAP.get(deployment.environment)
            if ttl_hours is None:
                continue

            ttl_threshold = deployment.activated_at + timedelta(hours=ttl_hours)
            
            if now > ttl_threshold:
                recent_runs_count = await db.scalar(
                    select(func.count())
                    .select_from(WorkflowRun)
                    .where(
                        WorkflowRun.deployment_id == deployment.id,
                        WorkflowRun.created_at > deployment.activated_at
                    )
                )

                if recent_runs_count == 0:
                    if dry_run:
                        would_deactivate_deployments.append(deployment.to_dict())
                    else:
                        deactivated = await _deactivate_deployment_internal(request, deployment, db, check_active_runs=True)
                        if deactivated:
                            deactivated_deployments.append(deactivated)

        if not dry_run:
            await db.commit()
            
        # logfire.info(
        #     "admin.scan_deployment_ttl",
        #     output={
        #         "deactivated": deactivated_deployments,
        #         "would_deactivate": would_deactivate_deployments
        #     }
        # )
        
        return TTLScanResponse(
            deactivated=deactivated_deployments,
            would_deactivate=would_deactivate_deployments
        )

    except Exception as e:
        logger.error(f"Error scanning deployment TTLs: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

class LegacyDeploymentInfo(BaseModel):
    deployment: DeploymentModel
    last_run_at: Optional[datetime] = None

class LegacyDeploymentScanResponse(BaseModel):
    inactive_deployments: List[LegacyDeploymentInfo]
    total_scanned: int

@router.post(
    "/deployments/scan-legacy",
    response_model=LegacyDeploymentScanResponse,
    openapi_extra={
        "x-speakeasy-name-override": "scanLegacyDeployments",
    },
)
async def scan_legacy_deployments(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Scan legacy deployments (those without activated_at timestamps) for inactivity.
    Identifies deployments in production/staging that haven't had any runs in the last 30 days.
    Uses machine_id to correlate with workflow runs since legacy deployments aren't linked directly.
    Returns the last run time for each inactive deployment.
    """
    check_admin_org(request)
    
    try:
        # Get legacy deployments (no activated_at) in production/staging
        query = (
            select(Deployment)
            .where(
                Deployment.activated_at.is_(None),
                Deployment.environment.in_(["production", "staging"]),
                Deployment.machine_id.isnot(None)  # Must have machine_id to check runs
            )
        )

        result = await db.execute(query)
        deployments = result.scalars().all()
        
        inactive_deployments = []
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)

        for deployment in deployments:
            # Check for any runs in the last 30 days using machine_id
            recent_runs_count = await db.scalar(
                select(func.count())
                .select_from(WorkflowRun)
                .where(
                    WorkflowRun.machine_id == deployment.machine_id,
                    WorkflowRun.created_at > thirty_days_ago
                )
            )

            if recent_runs_count == 0:
                # Get the last run time for this deployment
                last_run_query = (
                    select(WorkflowRun.created_at)
                    .where(WorkflowRun.machine_id == deployment.machine_id)
                    .order_by(WorkflowRun.created_at.desc())
                    .limit(1)
                )
                last_run_result = await db.execute(last_run_query)
                last_run_time = last_run_result.scalar_one_or_none()

                # No recent runs found
                logger.info(
                    f"Legacy deployment {deployment.id} with machine {deployment.machine_id} "
                    f"has no runs in the last 30 days. Last run: {last_run_time or 'Never'}"
                )
                
                inactive_deployments.append(
                    LegacyDeploymentInfo(
                        deployment=deployment.to_dict(),
                        last_run_at=last_run_time
                    )
                )

        return LegacyDeploymentScanResponse(
            inactive_deployments=inactive_deployments,
            total_scanned=len(deployments)
        )

    except Exception as e:
        logger.error(f"Error scanning legacy deployments: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")