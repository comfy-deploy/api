import os
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
import modal
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import Deployment, GPUEvent, Machine, WorkflowRun
from api.routes.deployments import (
    DeploymentModel,
    _deactivate_deployment_internal,
    ENVIRONMENT_TTL_MAP
)
from api.routes.utils import check_admin_org
from api.routes.internal import _as_utc, stale_event_end, track_gpu_concurrency_change
from api.utils.autumn import send_autumn_usage_event

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

class UpdatePluginRequest(BaseModel):
    machine_ids: List[UUID]
    dry_run: bool = True  # Default to dry run for safety


@router.post("/machines/update-plugin")
async def admin_update_machine_plugin(
    request: Request,
    body: UpdatePluginRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Admin only: bump the comfyui-deploy custom node to latest and rebuild
    customer machines, same as the owner clicking "Update" (no impersonation)."""
    check_admin_org(request)
    # Clerk browser sessions only (they carry "sid"); API keys / machine tokens can't call this.
    if not request.state.current_user.get("sid"):
        raise HTTPException(status_code=403, detail="Admin login session required")
    from api.routes.machines import update_machine_custom_nodes

    admin = request.state.current_user
    results = []
    for machine_id in body.machine_ids:
        machine = (
            await db.execute(
                select(Machine).where(Machine.id == machine_id, ~Machine.deleted)
            )
        ).scalars().first()
        if not machine:
            results.append({"machine_id": str(machine_id), "status": "not_found"})
            continue
        if body.dry_run:
            results.append({"machine_id": str(machine_id), "name": machine.name, "status": "would_update"})
            continue

        logger.info(
            f"admin {admin.get('user_id')} updating comfyui-deploy on machine {machine_id} "
            f"(org={machine.org_id} user={machine.user_id})"
        )
        # Act as the machine owner so the existing org-scoped update path applies.
        owner = {"user_id": machine.user_id}
        if machine.org_id:
            owner["org_id"] = machine.org_id
        request.state.current_user = owner
        try:
            await update_machine_custom_nodes(
                request=request, machine_id=machine_id, db=db, background_tasks=background_tasks
            )
            results.append({"machine_id": str(machine_id), "name": machine.name, "status": "rebuilding"})
        except HTTPException as e:
            results.append({"machine_id": str(machine_id), "status": "error", "detail": e.detail})
        finally:
            request.state.current_user = admin

    return {"results": results}


# Open GPU events older than this are never billed by the reconciler.
STALE_GPU_EVENT_WINDOW = timedelta(days=7)


@router.post("/gpu-events/close-stale")
async def close_stale_gpu_events(
    request: Request,
    dry_run: bool = True,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
):
    """Close GPU events whose gpu_end never arrived (container crash, lost callback) and bill
    them the way a normal gpu_end would (see stale_event_end). Without this they stay open
    forever: never charged, still counted in GPU concurrency, and a free-plan session blocks
    every new session. Only events started in the last 7 days are touched. Run hourly."""
    check_admin_org(request)
    now = datetime.now(timezone.utc)
    events = (
        await db.execute(
            select(GPUEvent)
            .where(
                GPUEvent.end_time.is_(None),
                GPUEvent.start_time.isnot(None),
                GPUEvent.start_time > now - STALE_GPU_EVENT_WINDOW,
            )
            .order_by(GPUEvent.start_time)
            .limit(limit)
        )
    ).scalars().all()

    closed = []
    for ev in events:
        last_run, idle_timeout = None, 0
        if not ev.session_id and ev.machine_id:
            # machine_id + created_at hit idx_workflow_run_queue_position; gpu_event_id has no index.
            last_run = (
                await db.execute(
                    select(func.max(func.coalesce(WorkflowRun.ended_at, WorkflowRun.updated_at))).where(
                        WorkflowRun.machine_id == ev.machine_id,
                        WorkflowRun.created_at >= _as_utc(ev.start_time) - timedelta(hours=1),
                        WorkflowRun.gpu_event_id == str(ev.id),
                    )
                )
            ).scalar()
            idle_timeout = (
                await db.execute(select(Machine.idle_timeout).where(Machine.id == ev.machine_id))
            ).scalar() or 0
        end = stale_event_end(ev, last_run, idle_timeout, now)
        if end is None:
            continue
        start = _as_utc(ev.start_time)
        customer_id = ev.org_id or ev.user_id
        item = {
            "event_id": str(ev.id),
            "customer_id": customer_id,
            "gpu": ev.gpu,
            "kind": "session" if ev.session_id else "run",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "billed_seconds": (end - start).total_seconds(),
        }
        closed.append(item)
        if dry_run:
            continue

        # A gpu_end that lands first wins; then we neither close nor bill it.
        won = (
            await db.execute(
                update(GPUEvent)
                .where(GPUEvent.id == ev.id, GPUEvent.end_time.is_(None))
                .values(end_time=end)
                .returning(GPUEvent.id)
            )
        ).scalar_one_or_none()
        await db.commit()
        if won is None:
            item["skipped"] = "gpu_end arrived"
            continue

        # Same idempotency keys as gpu_end (end_<id>, <id>): a late gpu_end can't double count.
        await track_gpu_concurrency_change(customer_id=customer_id, increment=-1, gpu_event_id=str(ev.id))
        item["charged"] = await send_autumn_usage_event(
            customer_id=customer_id,
            gpu_type=ev.gpu,
            start_time=start,
            end_time=end,
            environment=ev.environment,
            idempotency_key=str(ev.id),
        )
        logger.warning(f"Closed stale GPU event {ev.id}: {item}")

        # A session past its deadline may still be running on Modal (we'd keep paying for it).
        if ev.session_id and ev.modal_function_id:
            try:
                if ev.modal_function_id.startswith("sb-"):
                    await modal.Sandbox.from_id(ev.modal_function_id).terminate.aio()
                else:
                    await modal.functions.FunctionCall.from_id(ev.modal_function_id).cancel.aio()
            except Exception as e:
                logger.info(f"Stale session {ev.session_id}: Modal stop skipped ({e})")

    return {
        "dry_run": dry_run,
        "closed": len(closed),
        "billed_seconds": sum(i["billed_seconds"] for i in closed if not i.get("skipped")),
        "events": closed,
    }
