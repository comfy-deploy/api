from typing import Optional, Dict, Any
from pydantic import BaseModel
import os
from api.models import UserSettings
import base64
import google.auth
from google.auth.transport import requests
import boto3
import time
import google.auth.transport.requests
import google.oauth2.id_token
from dateutil import parser
import aioboto3
import asyncio
import aiohttp
import logfire

global_bucket = os.getenv("SPACES_BUCKET_V2")
global_region = os.getenv("SPACES_REGION_V2")
global_access_key = os.getenv("SPACES_KEY_V2")
global_secret_key = os.getenv("SPACES_SECRET_V2")

# Cache for assumed role credentials
_credentials_cache: Dict[str, Dict[str, Any]] = {}
_credentials_locks: Dict[str, asyncio.Lock] = {}
_cache_lock = asyncio.Lock()

# Buffer time in seconds to refresh credentials before they expire
CREDENTIALS_REFRESH_BUFFER = 300  # 5 minutes

async def _fetch_assumed_role_credentials(assumed_role_arn: str, region: str) -> Dict[str, Any]:
    """Internal function to fetch credentials from AWS STS"""
    # Directly fetch ID token from metadata service
    audience = "sts.amazonaws.com"
    metadata_url = f"http://metadata/computeMetadata/v1/instance/service-accounts/default/identity?audience={audience}&format=full"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(metadata_url, headers={"Metadata-Flavor": "Google"}) as response:
            if response.status != 200:
                raise Exception(f"Failed to get ID token: {response.status} {await response.text()}")

            id_token = await response.text()
            logfire.info("ID token obtained", extra={"assumed_role_arn": assumed_role_arn})
            
    # Use aioboto3 for async AWS operations
    async with aioboto3.Session().client('sts', region_name=region) as sts_client:
        response = await sts_client.assume_role_with_web_identity(
            RoleArn=assumed_role_arn,
            RoleSessionName='comfydeploy-session',
            WebIdentityToken=id_token
        )
        
        logfire.info("Assume role with web identity successful", extra={
            "assumed_role_arn": assumed_role_arn,
            "role_session_name": "comfydeploy-session"
        })
        
        credentials = response['Credentials']
        
        # Handle both string and datetime object for expiration
        expiration = credentials['Expiration']
        if isinstance(expiration, str):
            # If it's a string, parse it
            expiration_time = parser.isoparse(expiration).timestamp()
        else:
            # If it's already a datetime object, convert directly
            expiration_time = expiration.timestamp()
            
        credentials = {
            "access_key": credentials['AccessKeyId'],
            "secret_key": credentials['SecretAccessKey'],
            "session_token": credentials['SessionToken'],
            "expiration": expiration_time
        }
        return credentials


async def get_assumed_role_credentials(assumed_role_arn: str, region: str) -> Dict[str, Any]:
    """
    Get AWS assumed role credentials with caching and deduplication.
    
    This function implements a cache that:
    - Caches credentials by assumed_role_arn
    - Checks expiration time before returning cached credentials
    - Deduplicates concurrent requests for the same role
    - Refreshes credentials before they expire (with buffer time)
    """
    cache_key = f"{assumed_role_arn}:{region}"
    current_time = time.time()
    
    # Check if we have valid cached credentials
    if cache_key in _credentials_cache:
        cached_creds = _credentials_cache[cache_key]
        time_until_expiry = cached_creds["expiration"] - current_time
        
        # Return cached credentials if they're still valid (with buffer)
        if time_until_expiry > CREDENTIALS_REFRESH_BUFFER:
            logfire.info("Using cached assumed role credentials", extra={
                "assumed_role_arn": assumed_role_arn, 
                "time_until_expiry": time_until_expiry
            })
            return cached_creds
        
        # If credentials are expired or about to expire, we'll fetch new ones
        logfire.info("Cached credentials expired or expiring soon", extra={
            "assumed_role_arn": assumed_role_arn,
            "time_until_expiry": time_until_expiry,
            "refresh_buffer": CREDENTIALS_REFRESH_BUFFER
        })
    
    # Get or create a lock for this specific role to deduplicate requests
    async with _cache_lock:
        if cache_key not in _credentials_locks:
            _credentials_locks[cache_key] = asyncio.Lock()
        role_lock = _credentials_locks[cache_key]
    
    # Use the role-specific lock to ensure only one request is made per role
    async with role_lock:
        # Double-check the cache after acquiring the lock
        # Another request might have populated it while we were waiting
        if cache_key in _credentials_cache:
            cached_creds = _credentials_cache[cache_key]
            time_until_expiry = cached_creds["expiration"] - time.time()
            
            if time_until_expiry > CREDENTIALS_REFRESH_BUFFER:
                logfire.info("Using cached credentials (acquired after lock)", extra={
                    "assumed_role_arn": assumed_role_arn,
                    "time_until_expiry": time_until_expiry
                })
                return cached_creds
        
        # Fetch new credentials
        logfire.info("Fetching new assumed role credentials", extra={
            "assumed_role_arn": assumed_role_arn,
            "region": region
        })
        
        try:
            credentials = await _fetch_assumed_role_credentials(assumed_role_arn, region)
            
            # Cache the new credentials
            _credentials_cache[cache_key] = credentials
            
            logfire.info("Successfully cached new assumed role credentials", extra={
                "assumed_role_arn": assumed_role_arn,
                "expiration": credentials["expiration"]
            })
            
            return credentials
            
        except Exception as e:
            logfire.error("Failed to fetch assumed role credentials", extra={
                "assumed_role_arn": assumed_role_arn,
                "region": region,
                "error": str(e)
            })
            raise


class S3Config(BaseModel):
    public: bool
    bucket: str
    region: str
    access_key: str
    secret_key: str
    is_custom: bool
    session_token: Optional[str] = None


async def retrieve_s3_config(user_settings: UserSettings) -> S3Config:
    public = True
    bucket = global_bucket
    region = global_region
    access_key = global_access_key
    secret_key = global_secret_key
    is_custom = False
    session_token = None
    
    if user_settings is not None:
        if user_settings.output_visibility == "private":
            public = False

        if user_settings.custom_output_bucket:
            bucket = user_settings.s3_bucket_name
            region = user_settings.s3_region
            access_key = user_settings.s3_access_key_id
            secret_key = user_settings.s3_secret_access_key
            is_custom = True
            
            if user_settings.assumed_role_arn:
                credentials = await get_assumed_role_credentials(user_settings.assumed_role_arn, region)
                
                access_key = credentials['access_key']
                secret_key = credentials['secret_key']
                session_token = credentials['session_token']
                # expiration = credentials['expiration']

    return S3Config(
        public=public,
        bucket=bucket,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        is_custom=is_custom,
        session_token=session_token,
    )
