import os
from api.routes.utils import get_user_settings
from api.utils.retrieve_s3_config_helper import S3Config, retrieve_s3_config
from fastapi import (
    Request,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def get_s3_config(
    request: Request, db: AsyncSession
) -> S3Config:
    user_settings = await get_user_settings(request, db)

    return await retrieve_s3_config(user_settings)
