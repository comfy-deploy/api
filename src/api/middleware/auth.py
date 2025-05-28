from datetime import datetime, timezone
from uuid import uuid4
from api.routes.utils import select
from api.utils.multi_level_cache import multi_level_cached
from fastapi import Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from jose import JWTError, jwt
import os
from typing import Optional, List
from fastapi.responses import JSONResponse
from sqlalchemy import and_
from api.models import APIKey
from api.database import get_db, get_db_context
from hashlib import sha256

JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"
CLERK_PUBLIC_JWT_KEY = os.getenv("CLERK_PUBLIC_JWT_KEY")

# Function to parse JWT
async def parse_jwt(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


async def parse_clerk_jwt(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, CLERK_PUBLIC_JWT_KEY, algorithms=["RS256"])
        return payload
    except JWTError:
        return None

# Function to hash API key
def hash_api_key(key: str) -> str:
    return sha256(key.encode()).hexdigest()

# Function to check if key is revoked
@multi_level_cached(
    key_prefix="api_key",
    # Time for local memory cache to refresh from redis
    ttl_seconds=2,
    # Time for redis to refresh from source (autumn)
    redis_ttl_seconds=5,
    version="1.0",
    key_builder=lambda key: f"api_key:{hash_api_key(key)}",
)
async def is_key_revoked(key: str) -> bool:
    query = select(APIKey).where(APIKey.key == key, APIKey.revoked == True)
    async with get_db_context() as db:
        result = await db.execute(query)
        revoked_key = result.scalar_one_or_none()
        # logger.info(f"Revoked key: {revoked_key}")
        return revoked_key is not None


async def get_api_keys(request: Request, db: AsyncSession) -> List[APIKey]:
    limit = request.query_params.get("limit")
    offset = request.query_params.get("offset") 
    search = request.query_params.get("search")


    filter_conditions = and_(
        APIKey.revoked == False,
        # Include name filter if search is provided, otherwise ignore this filter
        APIKey.name.ilike(f"%{search}%") if search else True
    )
    query = (
        select(APIKey)
        .where(filter_conditions)
        .order_by(APIKey.created_at.desc())
        .limit(limit)
        .offset(offset)
        .apply_org_check(request)
    )

    result = await db.execute(query)
    
    # Get all keys first
    keys = result.scalars().all()
    
    # Mask API keys to only show last 4 digits
    for key in keys:
        key.key = f"****{key.key[-4:]}"

    return keys

async def delete_api_key(request: Request, db: AsyncSession):
    key_id = request.path_params.get("key_id")

    # Query the API key
    query = (
        select(APIKey)
        .where(APIKey.id == key_id)
        .apply_org_check(request)
    )
    result = await db.execute(query)
    fetchedKey = result.scalar_one_or_none()

    if not fetchedKey:
        return JSONResponse(status_code=404, content={"error": "API key not found"})

    fetchedKey.revoked = True
    await db.commit()
    return fetchedKey

async def create_api_key(request: Request, db: AsyncSession):
    user_id = request.state.current_user["user_id"]
    org_id = request.state.current_user.get("org_id")
    
    # Get request body
    body = await request.json()
    name = body.get("name")
    
    if not name:
        return JSONResponse(status_code=400, content={"error": "Name is required"})

    # Generate JWT token with user/org info
    payload = {
        "user_id": user_id,
        "iat": datetime.now(timezone.utc)
    }

    if org_id:
        payload["org_id"] = org_id

    token = jwt.encode(payload, os.environ["JWT_SECRET"])

    # Create new API key
    api_key = APIKey(
        id=uuid4(),
        name=name,
        key=token,
        user_id=user_id,
        org_id=org_id,
        revoked=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )
    
    db.add(api_key)
    try:
        await db.commit()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to commit API key: {str(e)}"})

    return {"key": api_key.key}

# Dependency to get user data from token
async def get_current_user(request: Request):
    # Check for Clerk session cookie
    session_token = request.cookies.get("__session")

    # print(f"Session token: {session_token}")
    
    # Check for cd_token in query parameters
    # Coming from native run
    cd_token = request.query_params.get("cd_token")

    # Check for Authorization header
    auth_header = request.headers.get("Authorization")

    token = None
    if session_token:
        token = session_token
    elif cd_token:
        token = cd_token
    elif auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    else:
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    # Check API auth
    user_data = await parse_jwt(token)

    is_clerk_token = False

    # Check Clerk auth
    if not user_data:
        user_data = await parse_clerk_jwt(token)
        # backward compatibility for old clerk tokens
        if user_data is not None:
            user_data["user_id"] = user_data["sub"]
            is_clerk_token = True

    if not user_data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # If the key has no expiration, it's not a temporary key, so we check if it's revoked
    if "exp" not in user_data and not is_clerk_token:
        if await is_key_revoked(token):
            raise HTTPException(status_code=401, detail="Revoked token")

    return user_data