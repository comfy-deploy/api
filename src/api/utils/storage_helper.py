from api.routes.utils import get_user_settings
from api.utils.retrieve_s3_config_helper import S3Config, retrieve_s3_config
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_s3_config(
    request: Request, db: AsyncSession
) -> S3Config:

    # Some routes may be publicly accessible and not set ``request.state.current_user``.
    # In that case we fall back to the global S3 configuration instead of
    # attempting to load user-specific settings which would raise an error.

    current_user = getattr(request.state, "current_user", None)
    if current_user:
        user_settings = await get_user_settings(request, db)
    else:
        user_settings = None

    return await retrieve_s3_config(user_settings)
