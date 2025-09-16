from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

# from .utils import select
from api.models import SubscriptionStatus, GPUEvent, Machine
import json
from datetime import datetime
from uuid import UUID
from decimal import Decimal
from sqlalchemy import func, extract


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime, UUID)):
            return str(obj)
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


async def get_current_plan(
    request: Request, db: AsyncSession, select
) -> SubscriptionStatus:
    query = (
        select(SubscriptionStatus)
        .where(SubscriptionStatus.status != "deleted")
        .apply_org_check(request)
        .order_by(SubscriptionStatus.created_at.desc())
        .limit(1)
    )

    result = await db.execute(query)
    subscription_status = result.scalar()
    return subscription_status


async def get_usage_detail(
    request: Request, start_date: datetime, db: AsyncSession, select
):
    # end_date is now
    end_date = datetime.now()

    query = (
        select(
            GPUEvent.machine_id,
            GPUEvent.gpu,
            GPUEvent.ws_gpu,
            Machine.name.label("machine_name"),
            func.sum(
                extract("epoch", GPUEvent.end_time)
                - extract("epoch", GPUEvent.start_time)
            ).label("usage_in_sec"),
        )
        .join(Machine, Machine.id == GPUEvent.machine_id)
        .where(GPUEvent.start_time >= start_date, GPUEvent.start_time < end_date)
        .group_by(GPUEvent.machine_id, GPUEvent.gpu, GPUEvent.ws_gpu, Machine.name)
        .order_by(
            func.sum(
                extract("epoch", GPUEvent.end_time)
                - extract("epoch", GPUEvent.start_time)
            ).desc()
        )
    )

    result = await db.execute(query)
    usage_details = result.fetchall()
    return usage_details
