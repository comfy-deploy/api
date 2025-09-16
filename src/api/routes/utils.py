from datetime import datetime, timedelta, timezone
from functools import wraps
from api.utils.retrieve_s3_config_helper import retrieve_s3_config, S3Config
from api.utils.multi_level_cache import multi_level_cached
from upstash_redis.asyncio import Redis
import logging
from typing import Any, Literal, Self, TypeVar, Tuple, Union, Dict
from api.routes.types import WorkflowRunOutputModel
from fastapi import Request
import logfire
from sqlalchemy import GenerativeSelect, Select, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql.selectable import _ColumnsClauseArgument
import os
from typing import Optional, cast
from sqlalchemy.ext.asyncio import AsyncSession
from api.database import get_db, get_db_context
from api.models import UserSettings
from pprint import pprint
import json
import aiohttp
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from urllib.parse import urlparse, unquote
import asyncio
from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
import httpx
from functools import lru_cache, wraps
import os
# from google.cloud import pubsub_v1
# from google.api_core import exceptions
from jose import JWTError, jwt
from datetime import datetime, timedelta
from .subscription import get_current_plan, get_usage_detail
from decimal import Decimal
import math
from typing import Dict, List, Set
from pydantic import ValidationError, BaseModel
from uuid import UUID
import random
from cryptography.fernet import Fernet
import base64
import boto3
import re

# Get JWT secret from environment variable
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"

# Initialize Redis client at the module level
redis_url = os.getenv("UPSTASH_REDIS_META_REST_URL")
redis_token = os.getenv("UPSTASH_REDIS_META_REST_TOKEN")
redis = Redis(url=redis_url, token=redis_token)

def generate_temporary_token(
    user_id: str, org_id: Optional[str] = None, expires_in: str = "1h"
) -> str:
    """
    Generate a temporary JWT token for the given user_id and org_id.

    Args:
        user_id (str): The user ID to include in the token.
        org_id (Optional[str]): The organization ID to include in the token, if any.
        expires_in (str): The expiration time for the token. Default is "1h".

    Returns:
        str: The generated JWT token.
    """
    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(hours=1),  # Default expiration of 1 hour
    }

    if org_id:
        payload["org_id"] = org_id

    if expires_in != "1h":
        # Parse the expiration time
        value = int(expires_in[:-1])
        unit = expires_in[-1].lower()
        if unit == "m":
            delta = timedelta(minutes=value)
        elif unit == "h":
            delta = timedelta(hours=value)
        elif unit == "d":
            delta = timedelta(days=value)
        elif unit == "w":
            delta = timedelta(weeks=value)
        else:
            raise ValueError("Invalid expiration format. Use m, h, d, or w.")

        payload["exp"] = datetime.utcnow() + delta

    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def generate_persistent_token(user_id: str, org_id: Optional[str] = None) -> str:
    return jwt.encode(
        {"user_id": user_id, "org_id": org_id}, JWT_SECRET, algorithm=ALGORITHM
    )


def generate_machine_token(user_id: str, org_id: Optional[str] = None) -> str:
    """
    Generate a scoped JWT token for machine authentication with limited endpoint access.
    
    Args:
        user_id (str): The user ID to include in the token.
        org_id (Optional[str]): The organization ID to include in the token, if any.
    
    Returns:
        str: The generated JWT token with machine scopes.
    """
    payload = {
        "user_id": user_id,
        "token_type": "machine",
        "scopes": [
            "/api/gpu_event",
            "/api/update-run", 
            "/api/file-upload",
            "/api/comfy-org/*",
            "/api/session/callback*",
            "/api/session/*",
            "/api/machine-version/*/error-start-logs",
            
            # Backward compatibility for ComfyOrg API
            "/proxy/*",
            "/customers/storage"
        ]
    }
    
    if org_id:
        payload["org_id"] = org_id
    
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


Base = declarative_base()

T = TypeVar("T")

clerk_token = os.getenv("CLERK_SECRET_KEY")


def async_lru_cache(maxsize=128, typed=False, expire_after=None, key_builder=None):
    def decorator(async_func):
        cache = {}

        @wraps(async_func)
        async def wrapper(*args, **kwargs):
            if key_builder:
                cache_key = key_builder(*args, **kwargs)
            else:
                cache_key = args + tuple(sorted(kwargs.items()))
            now = datetime.now()

            if cache_key in cache:
                result, timestamp = cache[cache_key]
                if expire_after is None or now - timestamp < expire_after:
                    # logfire.info(f"Cache hit for {cache_key}")
                    return result

            result = await async_func(*args, **kwargs)
            cache[cache_key] = (result, now)
            
            if len(cache) > maxsize:
                oldest_key = min(cache, key=lambda k: cache[k][1])
                del cache[oldest_key]

            return result

        return wrapper

    return decorator


# Add this cache at the module level
user_icon_cache = {}


class UserIconData(BaseModel):
    user_id: str
    image_url: Optional[str]
    username: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]


class ClerkUserResponse(BaseModel):
    image_url: Optional[str] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None


@async_lru_cache(maxsize=1000)
async def fetch_user_icon(user_id: str) -> UserIconData:
    current_time = datetime.now()

    # Check if the user_id is in the cache and not expired (1 day)
    if user_id in user_icon_cache:
        cached_data, timestamp = user_icon_cache[user_id]
        if current_time - timestamp < timedelta(days=1):
            return UserIconData(
                user_id=user_id,
                image_url=cached_data[0],
                username=cached_data[2],
                first_name=cached_data[3],
                last_name=cached_data[4]
            )

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.clerk.com/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {clerk_token}"},
        )
        if response.status_code == 200:
            user_data = ClerkUserResponse.model_validate(response.json())
            # Update the cache
            user_icon_cache[user_id] = (
                user_data.image_url,
                current_time,
                user_data.username,
                user_data.first_name,
                user_data.last_name,
            )
            return UserIconData(
                user_id=user_id,
                image_url=user_data.image_url,
                username=user_data.username,
                first_name=user_data.first_name,
                last_name=user_data.last_name
            )

    # If fetching fails, cache None for 1 hour to avoid frequent retries
    user_icon_cache[user_id] = (None, current_time, None, None, None)
    return UserIconData(user_id=user_id, image_url=None, username=None, first_name=None, last_name=None)


def get_org_or_user_condition(target: Base, request: Request):
    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user["org_id"] if "org_id" in current_user else None

    return (
        (target.org_id == org_id)
        if org_id
        else ((target.user_id == user_id) & (target.org_id.is_(None)))
    )


class OrgAwareSelect(Select[Tuple[T]]):
    inherit_cache = True

    def apply_org_check(self, request: Request) -> Self:
        return self.where(
            get_org_or_user_condition(self.column_descriptions[0]["entity"], request)
        )

    def apply_org_check_by_type(self, type, request: Request) -> Self:
        return self.where(get_org_or_user_condition(type, request))

    def paginate(self, limit: int, offset: int) -> Self:
        return self.limit(limit).offset(offset)
    

def apply_org_check_direct(object, request: Request):
    user_id = request.state.current_user["user_id"]
    org_id = request.state.current_user.get("org_id", None)

    if org_id is not None:
        if object.org_id == org_id:
            pass
        else:
            raise HTTPException(status_code=403, detail="You are not authorized to access this resource")
    else:
        if object.user_id == user_id:
            pass
        else:
            raise HTTPException(status_code=403, detail="You are not authorized to access this resource")


def select(__ent0: _ColumnsClauseArgument[T], /, *entities: Any) -> OrgAwareSelect[T]:
    return OrgAwareSelect(__ent0, *entities)


def ensure_run_timeout(run):
    # Helper function to safely get attributes from either object or dict
    def get_value(item, attr):
        value = item[attr] if isinstance(item, dict) else getattr(item, attr)
        # Handle datetime string conversion if needed
        if isinstance(value, str) and attr in ['created_at', 'queued_at', 'started_at', 'updated_at']:
            try:
                # Parse ISO format string to datetime with UTC timezone
                value = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                return None
        # Ensure datetime is timezone aware
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
        return value
    
    def set_value(item, attr, value):
        if isinstance(item, dict):
            item[attr] = value
        else:
            setattr(item, attr, value)

    # Apply timeout logic
    timeout_hours = 24
    timeout_delta = timedelta(hours=timeout_hours)
    now = datetime.now(timezone.utc)

    status = get_value(run, "status")
    
    # Not started for 24 hours
    created_at = get_value(run, "created_at")
    if (
        status == "not-started"
        and created_at
        and now - created_at > timeout_delta
    ):
        set_value(run, "status", "timeout")

    # Queued for 24 hours
    queued_at = get_value(run, "queued_at")
    if (
        status == "queued"
        and queued_at
        and now - queued_at > timeout_delta
    ):
        set_value(run, "status", "timeout")

    # Started for 24 hours
    started_at = get_value(run, "started_at")
    if (
        status == "started"
        and started_at
        and now - started_at > timeout_delta
    ):
        set_value(run, "status", "timeout")

    # Running and not updated in the last 24 hours
    if status not in ["success", "failed", "timeout", "cancelled"]:
        updated_at = get_value(run, "updated_at")
        if updated_at and now - updated_at > timeout_delta:
            set_value(run, "status", "timeout")


upload_types = ["images", "files", "gifs", "mesh"]

def guess_output_id(output):
    """
    Aggregates all output IDs from an output object, groups them by upload type,
    and returns the first output ID from the first upload type that has output IDs.
    
    Args:
        output: The output object containing data with upload types
        
    Returns:
        The first output ID found, or None if no output IDs are found
    """
    if output is None or output.data is None or output.data == []:
        return None
        
    # Dictionary to store output IDs by upload type
    output_ids_by_type = {}
    
    for upload_type in upload_types:
        if upload_type in output.data:
            # Collect all output IDs for this upload type
            output_ids = []
            for output_item in output.data[upload_type]:
                if "output_id" in output_item:
                    output_ids.append(output_item["output_id"])
            
            # If we found output IDs, store them by type
            if output_ids:
                output_ids_by_type[upload_type] = output_ids
    
    # If we have output IDs, return the first one
    if output_ids_by_type:
        # Get the first upload type that has output IDs
        first_type = next(iter(output_ids_by_type))
        # Get the first output ID from that type
        return output_ids_by_type[first_type][0]
    
    return None

def clean_up_outputs(outputs: List):
    """
    Clean up outputs by validating each against WorkflowRunOutputModel.
    Invalid outputs will be removed from the list entirely.
    """
    # Iterate through the list in reverse to safely remove items
    for i in range(len(outputs) - 1, -1, -1):
        output = outputs[i]
        if output.data is None or output.data == []:  # Check for None or empty list
            logfire.info("Empty output data", output=output)
            outputs.pop(i)
            continue

        # Guess the output ID and assign it to the top level
        output_id = guess_output_id(output)
        if output_id:
            output.output_id = output_id
            
        if output.data:
            try:
                WorkflowRunOutputModel.model_validate(output)
            except ValidationError:
                logfire.info("Invalid output", output=output)
                outputs.pop(i)
                
    return outputs


async def post_process_outputs(outputs, user_settings):
    for output in outputs:
        if output.data and isinstance(output.data, dict):
            await post_process_output_data(output.data, user_settings)


async def post_process_output_data(data, user_settings):
    s3_config = await retrieve_s3_config(user_settings)

    # public = s3_config.public
    region = s3_config.region
    access_key = s3_config.access_key
    secret_key = s3_config.secret_key
    session_token = s3_config.session_token

    for upload_type in upload_types:
        if upload_type in data:
            for output_item in data[upload_type]:
                if output_item.get("is_public") is False:
                    # pprint(output_item)
                    # Replace the output URL with a session key
                    if "url" in output_item:
                        output_item["url"] = get_temporary_download_url(
                            output_item["url"],
                            region,
                            access_key,
                            secret_key,
                            session_token
                        )
    return data


async def update_user_settings(request: Request, db: AsyncSession, body: any):
    logging.info(f"Updating user settings: {request.state.current_user}")
    # Check for authorized user
    try:
        user_id = request.state.current_user["user_id"]
    except (AttributeError, KeyError):
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized access. "}
        )
    # Clean out None values from the update request
    update_data = {
        key: value 
        for key, value in body.model_dump().items() 
        if value is not None
    }

    if not update_data:
        return JSONResponse(
            status_code=400,
            content={"error": "No valid fields to update"}
        )

    # Get existing settings or create new
    # If the current user operates under an organisation, we want exactly **one** row per org.
    # Otherwise we fall back to one row per individual user.

    org_id = request.state.current_user.get("org_id")

    if org_id is not None:
        # Query by org_id only; do NOT filter by user_id so that all members share the same settings row.
        user_settings_query = select(UserSettings).where(UserSettings.org_id == org_id).limit(1).order_by(UserSettings.created_at.desc())
    else:
        # Personal account (no org) – query by user_id with org_id IS NULL.
        user_settings_query = select(UserSettings).where(
            (UserSettings.user_id == user_id) & (UserSettings.org_id.is_(None))
        )

    user_settings_result = await db.execute(user_settings_query)
    user_settings = user_settings_result.scalar_one_or_none()
    
    secret_manager = SecretManager()

    if user_settings is None:
        # Create new settings if none exist
        org_id = request.state.current_user.get("org_id")
        user_settings = UserSettings(
            user_id=user_id,
            org_id=org_id
        )

        db.add(user_settings)
    
    if "s3_secret_access_key" in update_data:
        user_settings.encrypted_s3_key = secret_manager.encrypt_value(update_data["s3_secret_access_key"])
        update_data["s3_secret_access_key"] = None
    # Update fields in the database
    # This uses SQLAlchemy's setattr to update the model attributes
    for key, value in update_data.items():
        setattr(user_settings, key, value)

    await db.commit()

    # Update Redis with new spend limit if it was provided in the update
    if "spend_limit" in update_data:
        logging.info("Updating spend limit in Redis")

        entity_id = request.state.current_user.get("org_id") or user_id
        redis_key = f"plan:{entity_id}"

        raw_value = await redis.get(redis_key)
        if raw_value:
            plan_data = json.loads(raw_value)
            # Update spend_limit while preserving other fields
            plan_data["spend_limit"] = update_data["spend_limit"]
        else:
            # Create new Redis entry with spend_limit
            plan_data = {"spend_limit": update_data["spend_limit"]}

        # Save updated data back to Redis
        await redis.set(redis_key, json.dumps(plan_data))

    return JSONResponse(content=user_settings.to_dict())


def get_default_user_settings():
    """
    Return default user settings for unauthenticated users.
    These settings will use the default public bucket.
    """
    return UserSettings(
        user_id=None,
        org_id=None,
        api_version="v2",
        spend_limit=5,
        max_spend_limit=5,
        output_visibility="public",
        custom_output_bucket=False,
        enable_custom_output_bucket=False,
        # These will be None, indicating to use default public bucket
        s3_access_key_id=None,
        s3_secret_access_key=None,
        s3_bucket_name=None,
        s3_region=None,
        assumed_role_arn=None,
        hugging_face_token=None,
        workflow_limit=None,
        machine_limit=None,
        always_on_machine_limit=None,
        max_gpu=0,
        credit=0
    )


async def get_user_settings(request: Request, db: AsyncSession):
    # Check if user is authenticated
    current_user = getattr(request.state, 'current_user', None)
    
    if current_user is None:
        # Return default settings for unauthenticated users
        return get_default_user_settings()
    
    user_query = select(UserSettings).apply_org_check(request).order_by(UserSettings.created_at.desc()).limit(1)
    user_settings = await db.execute(user_query)
    user_settings = user_settings.scalar_one_or_none()
    user_settings = cast(Optional[UserSettings], user_settings)

    if user_settings is None:
        # print("Creating user settings")
        org_id = (
            request.state.current_user["org_id"]
            if "org_id" in request.state.current_user
            else None
        )

        plan = (
            request.state.current_user["plan"]
            if "plan" in request.state.current_user
            else "free"
        )

        user_settings = UserSettings(
            user_id=request.state.current_user["user_id"],
            org_id=org_id,
            api_version="v2",
            spend_limit=5 if plan == "free" else 500,
            # max_spend_limit=5 if plan == "free" else 1000,
        )
        # db.add(user_settings)
        # await db.commit()
        # await db.refresh(user_settings)
    elif hasattr(user_settings, 'encrypted_s3_key') and user_settings.encrypted_s3_key:
        # Decrypt the S3 secret access key if it exists
        secret_manager = SecretManager()
        user_settings.s3_secret_access_key = secret_manager.decrypt_value(user_settings.encrypted_s3_key)

    return user_settings


@multi_level_cached(
    key_prefix="user_settings",
    ttl_seconds=60,  # Local memory cache
    redis_ttl_seconds=300,  # Redis cache
    version="1.0",
    key_builder=lambda req: f"user_settings:{req.state.current_user['user_id'] if req.state.current_user else 'default'}:{req.state.current_user.get('org_id') if req.state.current_user else 'none'}"
)
async def get_user_settings_cached(request: Request):
    """
    Cached version of get_user_settings with 60-second memory cache and 5-minute Redis cache.
    Cache key is based on user_id and org_id from the request.
    Uses multi-level caching with stale-while-revalidate pattern.
    """
    print("get_user_settings_cached")
    
    async with get_db_context() as db:
        result = await get_user_settings(request, db)
    
        # Convert UserSettings object to dict for proper caching serialization
        if isinstance(result, UserSettings):
            return result.to_dict()
        
        return result


async def get_user_settings_cached_as_object(request: Request, db: AsyncSession):
    """
    Wrapper that returns a proper UserSettings object, handling reconstruction from cached dict.
    """
    cached_data = await get_user_settings_cached(request)
    
    # If cached data is a dict, reconstruct UserSettings object
    if isinstance(cached_data, dict):
        return UserSettings.from_dict(cached_data)
    
    # If it's already a UserSettings object (shouldn't happen with current caching), return as-is
    if isinstance(cached_data, UserSettings):
        return cached_data
    
    return cached_data


def get_temporary_download_url(
    url: str, region: str, access_key: str, secret_key: str, session_token: str, expiration: int = 3600
) -> str:
    # Parse the URL
    parsed_url = urlparse(url)

    # Extract bucket name and object key
    bucket = parsed_url.netloc.split(".")[0]
    object_key = unquote(parsed_url.path.lstrip("/"))

    # Generate and return the presigned URL
    return generate_presigned_download_url(
        bucket=bucket,
        object_key=object_key,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        expiration=expiration,
    )


def generate_presigned_download_url(
    bucket: str,
    object_key: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    expiration: int = 3600,
):
    s3_client = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        config=Config(signature_version="s3v4"),
    )

    try:
        response = s3_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": bucket,
                "Key": object_key,
            },
            ExpiresIn=expiration,
        )
        return response
    except ClientError as e:
        logging.error(f"Error generating presigned download URL: {e}")
        return None


def generate_presigned_url(
    bucket,
    object_key,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    expiration=3600,
    http_method="PUT",
    size=None,
    content_type=None,
    public=False,
):
    s3_client = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        config=Config(signature_version="s3v4"),
    )
    params = {
        "Bucket": bucket,
        "Key": object_key,
    }

    if public:
        params["ACL"] = "public-read"

    # if size is not None:
    #     params["ContentLength"] = size
    if content_type is not None:
        params["ContentType"] = content_type

    try:
        response = s3_client.generate_presigned_url(
            ClientMethod=f"{http_method.lower()}_object",
            Params=params,
            ExpiresIn=expiration,
            HttpMethod=http_method,
        )
    except ClientError as e:
        print(e)
        return None

    return response

async def initiate_multipart_upload(request: Request, db: AsyncSession, key: str, content_type: str) -> str:
    user_settings = await get_user_settings_cached_as_object(request, db)
    s3_config = await retrieve_s3_config(user_settings)
    s3 = boto3.client(
        "s3",
        region_name=s3_config.region,
        aws_access_key_id=s3_config.access_key,
        aws_secret_access_key=s3_config.secret_key,
        aws_session_token=getattr(s3_config, "session_token", None),
        config=Config(signature_version="s3v4"),
    )
    resp = s3.create_multipart_upload(Bucket=s3_config.bucket, Key=key, ContentType=content_type)
    return resp["UploadId"]


async def generate_part_upload_url(request: Request, db: AsyncSession, key: str, upload_id: str, part_number: int, expires: int = 3600) -> str:
    user_settings = await get_user_settings_cached_as_object(request, db)
    s3_config = await retrieve_s3_config(user_settings)
    s3 = boto3.client(
        "s3",
        region_name=s3_config.region,
        aws_access_key_id=s3_config.access_key,
        aws_secret_access_key=s3_config.secret_key,
        aws_session_token=getattr(s3_config, "session_token", None),
        config=Config(signature_version="s3v4"),
    )
    return s3.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": s3_config.bucket,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": int(part_number),
        },
        ExpiresIn=expires,
        HttpMethod="PUT",
    )


async def complete_multipart_upload(request: Request, db: AsyncSession, key: str, upload_id: str, parts: List[Dict[str, Any]]):
    user_settings = await get_user_settings_cached_as_object(request, db)
    s3_config = await retrieve_s3_config(user_settings)
    s3 = boto3.client(
        "s3",
        region_name=s3_config.region,
        aws_access_key_id=s3_config.access_key,
        aws_secret_access_key=s3_config.secret_key,
        aws_session_token=getattr(s3_config, "session_token", None),
        config=Config(signature_version="s3v4"),
    )
    sorted_parts = sorted(
        [{"ETag": p["eTag"], "PartNumber": int(p["partNumber"])} for p in parts],
        key=lambda p: p["PartNumber"],
    )
    return s3.complete_multipart_upload(
        Bucket=s3_config.bucket,
        Key=key,
        MultipartUpload={"Parts": sorted_parts},
        UploadId=upload_id,
    )


async def abort_multipart_upload(request: Request, db: AsyncSession, key: str, upload_id: str):
    user_settings = await get_user_settings_cached_as_object(request, db)
    s3_config = await retrieve_s3_config(user_settings)
    s3 = boto3.client(
        "s3",
        region_name=s3_config.region,
        aws_access_key_id=s3_config.access_key,
        aws_secret_access_key=s3_config.secret_key,
        aws_session_token=getattr(s3_config, "session_token", None),
        config=Config(signature_version="s3v4"),
    )
    return s3.abort_multipart_upload(Bucket=s3_config.bucket, Key=key, UploadId=upload_id)



def delete_s3_object(
    bucket: str,
    object_key: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
):
    """
    Delete an object from an S3 bucket
    
    Args:
        bucket: The name of the S3 bucket
        object_key: The key of the object to delete
        region: The AWS region
        access_key: The AWS access key
        secret_key: The AWS secret key
        
    Returns:
        None
    """
    try:
        s3_client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
            config=Config(signature_version="s3v4"),
        )
        
        s3_client.delete_object(
            Bucket=bucket,
            Key=object_key
        )
        
        return True
    except ClientError as e:
        logging.error(f"Error deleting S3 object: {e}")
        return False


project_id = os.getenv("GOOGLE_CLOUD_PROJECT")


async def send_workflow_update(workflow_id: str, data: dict):
    return
    logging.info(f"Sending updateWorkflow event via POST: {workflow_id}")
    # try:
    #     topic_id = "workflow-updates"

    #     # Create the topic if it doesn't exist
    #     # await create_topic_if_not_exists(project_id, topic_id)

    #     publisher = pubsub_v1.PublisherClient()
    #     # Create a publisher client
    #     topic_path = publisher.topic_path(project_id, topic_id)

    #     # Prepare the message
    #     message_data = json.dumps({"workflowId": workflow_id, "data": data}).encode(
    #         "utf-8"
    #     )

    #     # Publish the message
    #     publish_future = publisher.publish(
    #         topic_path,
    #         data=message_data,
    #         id=str(workflow_id),  # Add the ID as a message attribute
    #     )
    #     # Use asyncio to wait for the future without blocking
    #     message_id = await asyncio.wrap_future(publish_future)

    #     logging.info(f"Published message with ID: {message_id}")
    # except Exception as error:
    #     print(data)
    #     logging.error(f"Error sending updateWorkflow event: {error}")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{os.getenv('NEXT_PUBLIC_REALTIME_SERVER_2')}/updateWorkflow"
            json_data = json.dumps({"workflowId": workflow_id, "data": data})
            async with session.post(
                url, data=json_data, headers={"Content-Type": "application/json"}
            ) as response:
                if response.status >= 400:
                    raise aiohttp.ClientResponseError(
                        response.request_info,
                        response.history,
                        status=response.status,
                        message=f"Failed to send update: {response.reason}",
                        headers=response.headers,
                    )
    except Exception as error:
        print(data)
        logging.error(f"Error sending updateWorkflow event: {error}")


# async def create_topic_if_not_exists(project_id: str, topic_id: str):
#     publisher = pubsub_v1.PublisherClient()
#     topic_path = publisher.topic_path(project_id, topic_id)

#     try:
#         publisher.create_topic(request={"name": topic_path})
#         print(f"Topic {topic_path} created.")
#     except exceptions.AlreadyExists:
#         print(f"Topic {topic_path} already exists.")
#     except Exception as e:
#         print(f"Error creating topic: {e}")
#         raise


# async def send_realtime_update(id: str, data: dict):
#     return


async def fetch_with_timeout(url, options, timeout=20):
    try:
        async with aiohttp.ClientSession() as session:
            # logging.info(options)
            method = options.pop("method", "GET")  # Extract method from options
            async with session.request(
                method, url, **options, timeout=timeout
            ) as response:
                return response
    except asyncio.TimeoutError:
        raise TimeoutError("Request timed out")


async def retry_fetch(url, options, num_retries=3):
    for i in range(num_retries):
        try:
            response = await fetch_with_timeout(url, options)
            if not response.ok and i < num_retries - 1:
                # Calculate exponential backoff with jitter
                base_delay = 0.5 * (2 ** i)  # 0.5s, 1s, 2s, 4s, etc.
                jitter = random.uniform(0, 0.1 * base_delay)  # Add up to 10% jitter
                await asyncio.sleep(base_delay + jitter)
                continue
            return response
        except Exception as error:
            if i == num_retries - 1:
                raise error
            # Same exponential backoff for exceptions
            base_delay = 0.5 * (2 ** i)
            jitter = random.uniform(0, 0.1 * base_delay)
            await asyncio.sleep(base_delay + jitter)


PermissionType = Literal[
    # API Permissions
    "api:runs:get",
    "api:runs:create",
    #
    "api:runs:update",
    "api:file_upload:get",
    #
    "api:machines:update",
    #
    "api:gpu_event:create",
    "api:gpu_event:update",
]


def require_permission(permission: Union[PermissionType, list[PermissionType]]):
    def decorator(func):
        @wraps(func)
        async def wrapper(request: Request, *args, **kwargs):
            if not has(request, permission):
                # raise HTTPException(status_code=403, detail="Permission denied")
                return JSONResponse(
                    status_code=403, content={"detail": "Permission denied"}
                )
            return await func(request, *args, **kwargs)

        return wrapper

    return decorator


def has(
    request: Request, permission: Union[PermissionType, list[PermissionType]]
) -> bool:
    current_user = request.state.current_user

    if current_user is None:
        return False

    user_permissions = current_user.get("org_permissions", [])

    if isinstance(permission, str):
        return permission in user_permissions
    elif isinstance(permission, list):
        return all(perm in user_permissions for perm in permission)

def is_valid_uuid(uuid_string: str) -> bool:
    try:
        UUID(uuid_string)
        return True
    except ValueError:
        return False


async def execute_with_org_check(
    db: AsyncSession,
    sql: str,
    request: Request,
    model: Base,  # SQLAlchemy model
    params: dict = None
) -> Any:
    """
    Execute SQL with organization check.
    
    Args:
        db: The database session
        sql: The SQL query
        request: The FastAPI request object
        model: The SQLAlchemy model class
        params: Additional query parameters (optional)
    """
    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user.get("org_id")

    # Add org check to WHERE clause
    if "WHERE" in sql.upper():
        sql = f"{sql} AND"
    else:
        sql = f"{sql} WHERE"

    table_name = model.__table__.name
    
    # Use model columns for proper type handling
    if org_id:
        sql = f"{sql} ({table_name}.org_id = :org_id)"
    else:
        sql = f"{sql} ({table_name}.user_id = :user_id AND {table_name}.org_id IS NULL)"

    # Combine parameters
    execute_params = {
        "user_id": user_id,
        "org_id": org_id,
        **(params or {})
    }

    print(sql)
    return await db.execute(text(sql), execute_params)


def is_s3_url(url: str) -> bool:
    """Check if a URL is an S3 URL"""
    if not isinstance(url, str) or not url.startswith("http"):
        return False
    
    try:
        parsed = urlparse(url)
        # Check for common S3 URL patterns
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        return (
            ".s3." in hostname and "amazonaws.com" in hostname
        ) or (
            "s3." in hostname and "amazonaws.com" in hostname
        ) or hostname.endswith(".amazonaws.com")
    except:
        return False


def extract_s3_info_from_url(url: str) -> tuple[str, str]:
    """Extract bucket and object key from S3 URL"""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        path = parsed.path.lstrip("/")
        
        if ".s3." in hostname and "amazonaws.com" in hostname:
            # Format: bucket.s3.region.amazonaws.com/key
            bucket = hostname.split(".")[0]
            object_key = path
        elif "s3." in hostname and "amazonaws.com" in hostname:
            # Format: s3.region.amazonaws.com/bucket/key
            path_parts = path.split("/", 1)
            bucket = path_parts[0] if path_parts else ""
            object_key = path_parts[1] if len(path_parts) > 1 else ""
        else:
            # Fallback pattern
            bucket = hostname.split(".")[0] if hostname else ""
            object_key = path
            
        return bucket, object_key
    except:
        return "", ""


def is_private_s3_url(url: str, user_s3_config) -> bool:
    """Check if S3 URL is from a private bucket that requires credentials"""
    if not is_s3_url(url):
        return False
    
    bucket, _ = extract_s3_info_from_url(url)
    
    # Check if it's our configured private bucket
    if user_s3_config and not user_s3_config.public:
        return bucket == user_s3_config.bucket
    
    return False


async def process_inputs_s3_urls(inputs: Dict[str, Any], user_s3_config) -> Dict[str, Any]:
    """
    Recursively process inputs and replace private S3 URLs with temporary access URLs
    """
    if not user_s3_config:
        return inputs
    
    def process_value(value: Any) -> Any:
        if isinstance(value, str) and is_private_s3_url(value, user_s3_config):
            # Replace with temporary access URL
            temp_url = get_temporary_download_url(
                url=value,
                region=user_s3_config.region,
                access_key=user_s3_config.access_key,
                secret_key=user_s3_config.secret_key,
                session_token=user_s3_config.session_token or "",
                expiration=3600  # 1 hour expiration
            )
            return temp_url if temp_url else value
        elif isinstance(value, dict):
            return {k: process_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [process_value(item) for item in value]
        else:
            return value
    
    return process_value(inputs)


class SecretManager:
    def __init__(self, master_key=None):
        if master_key is None:
            master_key = os.environ.get('SECRET_ENCRYPTION_KEY')
            if not master_key:
                raise ValueError("SECRET_ENCRYPTION_KEY environment variable is not set")
        
        if isinstance(master_key, str):
            master_key = master_key.encode('utf-8')
            
        try:
            self.cipher = Fernet(master_key)
        except ValueError:
            try:
                decoded = base64.urlsafe_b64decode(master_key + b'=' * (-len(master_key) % 4))
                if len(decoded) != 32:
                    import hashlib
                    decoded = hashlib.sha256(master_key).digest()
                fixed_key = base64.urlsafe_b64encode(decoded)
                self.cipher = Fernet(fixed_key)
            except Exception as e:
                raise ValueError(f"Unable to create a valid Fernet key: {str(e)}")
    
    def encrypt_value(self, value):
        if isinstance(value, str):
            value = value.encode('utf-8')
        
        encrypted_value = self.cipher.encrypt(value)
        return base64.b64encode(encrypted_value).decode('utf-8')
    
    def decrypt_value(self, encrypted_value):
        if isinstance(encrypted_value, str):
            encrypted_value = base64.b64decode(encrypted_value)
        
        decrypted_value = self.cipher.decrypt(encrypted_value)
        return decrypted_value.decode('utf-8')
