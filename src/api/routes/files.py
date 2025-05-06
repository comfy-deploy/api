from enum import Enum
import os
from api.models import Asset
from api.utils.storage_helper import get_s3_config
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from .utils import (
    get_temporary_download_url,
    get_user_settings,
)

# from sqlalchemy import select
from api.database import get_db
from typing import Optional
import logging
from typing import Optional
from botocore.config import Config
import random
import aioboto3
import mimetypes
from sqlalchemy import and_
from datetime import datetime
from uuid import uuid4
from .utils import select

# Implement nanoid-like function
def custom_nanoid(size=16):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return "".join(random.choice(alphabet) for _ in range(size))


# Define prefixes
prefixes = {
    "img": "img",
    "zip": "zip",
    "vid": "vid",
    "audio": "audio",
    "file": "file",
    "folder": "folder"
}


def new_id(prefix):
    return f"{prefixes[prefix]}_{custom_nanoid(16)}"


logger = logging.getLogger(__name__)

router = APIRouter(tags=["File"])


class UploadType(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


class FileUploadResponse(BaseModel):
    message: str = Field(
        ..., description="A message indicating the result of the file upload"
    )
    file_id: str = Field(..., description="The unique identifier for the uploaded file")
    file_name: str = Field(..., description="The original name of the uploaded file")
    file_url: str = Field(
        ..., description="The URL where the uploaded file can be accessed"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "message": "File uploaded successfully",
                "file_id": "img_1a2b3c4d5e6f7g8h",
                "file_name": "example_image.jpg",
                "file_url": "https://your-bucket.s3.your-region.amazonaws.com/inputs/img_1a2b3c4d5e6f7g8h.jpg",
            }
        }
    }


class FileUploadRequest(BaseModel):
    file: UploadFile = File(...)
    run_id: Optional[str] = Query(None),
    upload_type: UploadType = Query(UploadType.INPUT),
    file_type: Optional[str] = Query(None, regex="^(image/|video/|application/)"),


class CreateFolderRequest(BaseModel):
    name: str = Field(..., description="Folder name")
    parent_path: Optional[str] = Field(default="/", description="Parent folder path")


class AssetResponse(BaseModel):
    id: str
    user_id: Optional[str] = None
    org_id: Optional[str] = None
    name: str
    is_folder: bool
    path: str
    file_size: Optional[int] = None
    url: Optional[str] = None
    mime_type: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    deleted: Optional[bool] = False

UPLOAD_FILE_SIZE_LIMIT_MB = 250 * 1024 * 1024  # 250MB

# Return the session tunnel url
@router.post(
    "/file/upload",
    openapi_extra={
        "x-speakeasy-name-override": "upload",
    },
)
async def upload_file(
    request: Request,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
) -> FileUploadResponse:
    # Infer file type from the filename
    inferred_type, _ = mimetypes.guess_type(file.filename)
    file_type = (
        inferred_type or "application/octet-stream"
    )  # Default to binary data if type can't be guessed

    # Check if the file type is allowed
    if not file_type.startswith(("image/", "video/", "application/", "audio/")):
        raise HTTPException(status_code=400, detail="Unsupported file type")
    
    s3_config = await get_s3_config(request, db)

    # File size check
    file_size = file.size
    if file_size > UPLOAD_FILE_SIZE_LIMIT_MB:
        raise HTTPException(
            status_code=400,
            detail=f"File size exceeds {UPLOAD_FILE_SIZE_LIMIT_MB // (1024 * 1024)}MB limit",
        )

    # Generate file ID and path
    file_extension = os.path.splitext(file.filename)[1]
    if file_type.startswith("video/"):
        file_id = new_id("vid")
    elif file_type.startswith("audio/"):
        file_id = new_id("audio")
    elif file_type.startswith("image/"):
        file_id = new_id("img")
    elif file_type.startswith("application/zip"):
        file_id = new_id("zip")
    else:
        file_id = new_id("file")

    file_path = f"inputs/{file_id}{file_extension}"

    try:
        file_content = await file.read()
        s3_result = await upload_file_to_s3(
            file_content=file_content,
            key=file_path,
            content_type=file_type,
            s3_config=s3_config,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"S3 upload failed: {str(e)}")

    file_url = s3_result.get("file_url")
    public = s3_result.get("public")

    if not file_url:
        raise HTTPException(status_code=500, detail="File upload failed: No file_url returned from S3.")

    if not public:
        file_url = get_temporary_download_url(
            file_url,
            s3_result["region"],
            s3_result["access_key"],
            s3_result["secret_key"],
            expiration=3600,  # Set expiration to 1 hour
        )

    return {
        "message": "File uploaded successfully",
        "file_id": file_id,
        "file_name": file.filename,
        "file_url": file_url,
    }


@router.post("/assets/folder", response_model=AssetResponse)
async def create_folder(
    request: Request,
    folder_data: CreateFolderRequest,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.state.current_user["user_id"]
    org_id = request.state.current_user.get("org_id")
    
    # Normalize parent path by stripping leading and trailing slashes
    parent_path = folder_data.parent_path.strip("/")
    # For root folder, use empty string instead of "/"
    parent_path = parent_path if parent_path else ""
    
    # Join paths and ensure forward slashes
    full_path = os.path.join(parent_path, folder_data.name).replace("\\", "/")
    
    # Check if folder already exists
    query = select(Asset).apply_org_check(request).where(
        and_(Asset.path == full_path, Asset.is_folder == True)
    )
    existing = await db.execute(query)
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Folder already exists")

    new_folder = Asset(
        user_id=user_id,
        org_id=org_id,
        id=new_id("folder"),
        name=folder_data.name,
        is_folder=True,
        path=full_path,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    
    db.add(new_folder)
    await db.commit()
    await db.refresh(new_folder)
    
    return new_folder


@router.get("/assets", response_model=list[AssetResponse])
async def list_assets(
    request: Request,
    path: str = Query(default="/", description="Folder path to list items from"),
    db: AsyncSession = Depends(get_db),
):
    # Normalize the path to ensure consistent format
    normalized_path = path.rstrip("/") + "/"
    if normalized_path == "//":
        normalized_path = "/"
    
    # Modified query to only get direct children
    query = (
        select(Asset)
        .apply_org_check(request)
        .where(
            and_(
                Asset.path.like(f"{normalized_path}%"),
                ~Asset.path.like(f"{normalized_path}%/%")  # Excludes nested items
            ) if normalized_path != "/" else
            and_(
                ~Asset.path.contains("/"),  # For root level, only get items without /
                Asset.path != "/"  # Exclude root folder itself
            )
        )
    )
    
    result = await db.execute(query)
    assets = result.scalars().all()
    
    # Get user settings for S3 configuration
    s3_config = await get_s3_config(request, db)
    
    # Generate temporary URLs for private files
    if s3_config and not s3_config.public:
        for asset in assets:
            if asset.url:  # Only for files, not folders
                asset.url = get_temporary_download_url(
                    asset.url,
                    region=s3_config.region,
                    access_key=s3_config.access_key,
                    secret_key=s3_config.secret_key,
                    expiration=3600,
                )
    
    return assets


@router.delete("/assets/{asset_id}")
async def delete_asset(
    request: Request,
    asset_id: str,
    db: AsyncSession = Depends(get_db),
):
    # Get the asset
    query = select(Asset).apply_org_check(request).where(Asset.id == asset_id)
    result = await db.execute(query)
    asset = result.scalar_one_or_none()
    
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    
    # If it's a folder, delete all children
    if asset.is_folder:
        children_query = select(Asset).where(Asset.path.like(f"{asset.path}/%"))
        children = await db.execute(children_query)
        for child in children.scalars():
            await db.delete(child)
    
    # If it's a file, delete from S3
    elif not asset.is_folder:
        # Get S3 credentials from user settings
        s3_config = await get_s3_config(request, db)
        bucket = s3_config.bucket
        region = s3_config.region
        access_key = s3_config.access_key
        secret_key = s3_config.secret_key

        # Prefix the path with 'assets/' for S3 operations
        s3_path = f"assets/{asset.path}"

        async with aioboto3.Session().client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        ) as s3_client:
            await s3_client.delete_object(Bucket=bucket, Key=s3_path)

    await db.delete(asset)
    await db.commit()
    
    return {"message": "Asset deleted successfully"}


@router.post("/assets/upload", response_model=AssetResponse)
async def upload_asset_file(
    request: Request,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    parent_path: str = Query(default="/", description="Parent folder path"),
) -> AssetResponse:
    user_id = request.state.current_user["user_id"]
    org_id = request.state.current_user.get("org_id")
    # Infer file type from the filename
    inferred_type, _ = mimetypes.guess_type(file.filename)
    file_type = inferred_type or "application/octet-stream"

    # Check if the file type is allowed
    if not file_type.startswith(("image/", "video/", "application/", "audio/")):
        raise HTTPException(status_code=400, detail="Unsupported file type")
    
    s3_config = await get_s3_config(request, db)

    bucket = s3_config.bucket
    region = s3_config.region
    access_key = s3_config.access_key
    secret_key = s3_config.secret_key
    public = s3_config.public

    # File size check
    file_size = file.size
    if file_size > UPLOAD_FILE_SIZE_LIMIT_MB:
        raise HTTPException(
            status_code=400,
            detail=f"File size exceeds {UPLOAD_FILE_SIZE_LIMIT_MB // (1024 * 1024)}MB limit",
        )

    # Generate file ID and path
    file_extension = os.path.splitext(file.filename)[1]
    if file_type.startswith("video/"):
        file_id = new_id("vid")
    elif file_type.startswith("audio/"):
        file_id = new_id("audio")
    elif file_type.startswith("image/"):
        file_id = new_id("img")
    elif file_type.startswith("application/zip"):
        file_id = new_id("zip")
    else:
        file_id = new_id("file")

    # Create the full path for S3 with 'assets' prefix
    s3_file_path = os.path.join("assets", parent_path.lstrip("/"), f"{file_id}{file_extension}").replace("\\", "/")
    
    # Create the database path without 'assets' prefix
    db_file_path = os.path.join(parent_path.lstrip("/"), f"{file_id}{file_extension}").replace("\\", "/")

    async with aioboto3.Session().client(
        "s3",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
    ) as s3_client:
        try:
            file_content = await file.read()
            await s3_client.put_object(
                Bucket=bucket,
                Key=s3_file_path,  # Use S3 path with assets prefix
                Body=file_content,
                ACL="public-read" if public else "private",
                ContentType=file_type,
            )

            file_url = f"https://{bucket}.s3.{region}.amazonaws.com/{s3_file_path}"  # Use S3 path
            
            # Store the original URL without generating temporary URL
            new_asset = Asset(
                user_id=user_id,
                org_id=org_id,
                id=file_id,
                name=file.filename,
                is_folder=False,
                path=db_file_path,  # Use DB path without assets prefix
                url=file_url,
                mime_type=file_type,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                file_size=file.size
            )
            
            db.add(new_asset)
            await db.commit()
            await db.refresh(new_asset)
            
            # Generate temporary URL only for the response if needed
            if not public:
                new_asset.file_url = get_temporary_download_url(
                    file_url,
                    region,
                    access_key,
                    secret_key,
                    expiration=3600,
                )
            
            return new_asset

        except Exception as e:
            logger.error(f"Error uploading file: {str(e)}")
            raise HTTPException(status_code=500, detail="Error uploading file")


@router.get("/assets/{asset_id}", response_model=AssetResponse)
async def get_asset(
    request: Request,
    asset_id: str,
    db: AsyncSession = Depends(get_db),
):
    # Get the asset
    query = select(Asset).apply_org_check(request).where(Asset.id == asset_id)
    result = await db.execute(query)
    asset = result.scalar_one_or_none()
    
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    
    # Get user settings for S3 configuration
    s3_config = await get_s3_config(request, db)

    # Generate temporary URL for private files
    if s3_config and not s3_config.public and asset.url:
        asset.url = get_temporary_download_url(
            asset.url,
            region=s3_config.region,
            access_key=s3_config.access_key,
            secret_key=s3_config.secret_key,
            expiration=3600,
        )
    
    return asset

async def upload_file_to_s3(
    file_content: bytes,
    key: str,
    content_type: str,
    s3_config,
):
    bucket = s3_config.bucket
    region = s3_config.region
    access_key = s3_config.access_key
    secret_key = s3_config.secret_key
    public = s3_config.public

    try:
        async with aioboto3.Session().client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4"),
        ) as s3_client:
            await s3_client.put_object(
                Bucket=bucket,
                Key=key,
                Body=file_content,
                ACL="public-read" if public else "private",
                ContentType=content_type,
            )
        file_url = f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
        return {
            "file_url": file_url,
            "region": region,
            "access_key": access_key,
            "secret_key": secret_key,
            "public": public,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error uploading file to S3: {str(e)}")
