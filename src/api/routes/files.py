from enum import Enum
import os
from api.models import Asset
from api.utils.storage_helper import get_s3_config
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File, Body
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from .utils import (
    get_temporary_download_url,
    get_user_settings,
    generate_presigned_url,
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
from sqlalchemy import and_, func
from datetime import datetime
from uuid import uuid4
from .utils import select
import urllib.parse
import aiohttp

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


def get_id_prefix_from_type(mime_type: str):
    if mime_type.startswith("video/"):
        return "vid"
    elif mime_type.startswith("audio/"):
        return "audio"
    elif mime_type.startswith("image/"):
        return "img"
    elif mime_type.startswith("application/zip"):
        return "zip"
    else:
        return "file"


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
    # if not file_type:
    # Infer file type from the filename
    inferred_type, _ = mimetypes.guess_type(file.filename)
    file_type = (
        inferred_type or "application/octet-stream"
    )  # Default to binary data if type can't be guessed

    # Check if the file type is allowed
    if not file_type.startswith(("image/", "video/", "application/", "audio/")):
        raise HTTPException(status_code=400, detail="Unsupported file type")
    
    s3_config = await get_s3_config(request, db)

    public = s3_config.public
    bucket = s3_config.bucket
    region = s3_config.region
    access_key = s3_config.access_key
    secret_key = s3_config.secret_key
    session_token = s3_config.session_token

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

    # if upload_type == UploadType.OUTPUT and run_id:
    #     file_path = f"outputs/runs/{run_id}/{file_id}{file_extension}"
    # else:
    file_path = f"inputs/{file_id}{file_extension}"

    async with aioboto3.Session().client(
        "s3",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        config=Config(signature_version="s3v4"),
    ) as s3_client:
        try:
            file_content = await file.read()
            await s3_client.put_object(
                Bucket=bucket,
                Key=file_path,
                Body=file_content,
                ACL="public-read" if public else "private",
                ContentType=file_type,
            )

            file_url = f"https://{bucket}.s3.{region}.amazonaws.com/{file_path}"

            if not public:
                file_url = get_temporary_download_url(
                    file_url,
                    region,
                    access_key,
                    secret_key,
                    expiration=3600,  # Set expiration to 1 hour
                )

            # TODO: Implement PostHog event capture here if needed
            
            return {
                "message": "File uploaded successfully",
                "file_id": file_id,
                "file_name": file.filename,
                "file_url": file_url,
            }

            # # After successful upload, create asset record
            # new_asset = Asset(
            #     id=file_id,
            #     name=file.filename,
            #     is_folder=False,
            #     path=file_path,
            #     url=file_url,
            #     mime_type=file_type,
            #     created_at=datetime.utcnow(),
            #     updated_at=datetime.utcnow(),
            #     file_size=file.size
            # )
            
            # db.add(new_asset)
            # await db.commit()
            # await db.refresh(new_asset)
            
            # return new_asset
        except Exception as e:
            logger.error(f"Error uploading file: {str(e)}")
            raise HTTPException(status_code=500, detail="Error uploading file")


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
                    session_token=s3_config.session_token,
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
        session_token = s3_config.session_token

        # Prefix the path with 'assets/' for S3 operations
        s3_path = f"assets/{asset.path}"

        async with aioboto3.Session().client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
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
    session_token = s3_config.session_token
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
        aws_session_token=session_token,
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


@router.get("/assets/presigned-url")
async def get_asset_upload_url(
    request: Request,
    file_name: str = Query(..., description="Name of the file to upload"),
    parent_path: str = Query(default="/", description="Parent folder path"),
    size: Optional[int] = Query(None, description="Size of the file in bytes"),
    type: str = Query(..., description="Content type of the file"),
    db: AsyncSession = Depends(get_db),
):
    s3_config = await get_s3_config(request, db)
    public = s3_config.public
    session_token = s3_config.session_token
    # Generate object key
    file_extension = os.path.splitext(file_name)[1]
    file_id = new_id(get_id_prefix_from_type(type))
    
    object_key = os.path.join("assets", parent_path.lstrip("/"), f"{file_id}{file_extension}").replace("\\", "/")
    db_file_path = os.path.join(parent_path.lstrip("/"), f"{file_id}{file_extension}").replace("\\", "/")
    
    composed_endpoint = f"https://{s3_config.bucket}.s3.{s3_config.region}.amazonaws.com"
    download_url = f"{composed_endpoint}/{object_key}"
    
    # Generate pre-signed URL
    upload_url = generate_presigned_url(
        object_key=object_key,
        expiration=3600,
        http_method="PUT",
        size=size,
        content_type=type,
        public=public,
        bucket=s3_config.bucket,
        region=s3_config.region,
        access_key=s3_config.access_key,
        secret_key=s3_config.secret_key,
        session_token=session_token,
    )
    
    return {
        "url": upload_url,
        "download_url": download_url,
        "file_id": file_id,
        "db_path": db_file_path,
        "is_public": public
    }


@router.post("/assets/register", response_model=AssetResponse)
async def register_asset(
    request: Request,
    db: AsyncSession = Depends(get_db),
    file_id: str = Body(...),
    file_name: str = Body(...),
    file_size: int = Body(...),
    db_path: str = Body(...),
    url: str = Body(...),
    mime_type: str = Body(...),
):
    user_id = request.state.current_user["user_id"]
    org_id = request.state.current_user.get("org_id")
    
    new_asset = Asset(
        user_id=user_id,
        org_id=org_id,
        id=file_id,
        name=file_name,
        is_folder=False,
        path=db_path,
        url=url,
        mime_type=mime_type,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        file_size=file_size
    )
    
    db.add(new_asset)
    await db.commit()
    await db.refresh(new_asset)
    
    return new_asset


@router.get("/assets/search", response_model=list[AssetResponse])
async def search_assets(
    request: Request,
    db: AsyncSession = Depends(get_db),
    query: Optional[str] = Query("", description="Search query"),
    limit: Optional[int] = Query(5, description="Number of results to return"),
):
    # Escape special characters used in LIKE patterns
    escaped_query = query.replace("%", r"\%").replace("_", r"\_")
    
    logger.info(f"Searching for: {escaped_query}")
    
    # Create the search query with ILIKE for case-insensitive search
    search_query = (
        select(Asset)
        .apply_org_check(request)
        .where(
            and_(
                Asset.name.ilike(f"%{escaped_query}%"),
                Asset.is_folder.is_(False)
            )
        )
        .limit(limit)
    )
    
    result = await db.execute(search_query)
    assets = result.scalars().all()
    
    # Get S3 config to handle URLs if needed
    s3_config = await get_s3_config(request, db)
    session_token = s3_config.session_token
    # Generate temporary URLs for private files if needed
    if s3_config and not s3_config.public:
        for asset in assets:
            if asset.url:  # Only for files, not folders
                asset.url = get_temporary_download_url(
                    asset.url,
                    region=s3_config.region,
                    access_key=s3_config.access_key,
                    secret_key=s3_config.secret_key,
                    session_token=session_token,
                    expiration=3600,
                )
    
    return assets

@router.get("/assets/{asset_id}", response_model=AssetResponse)
async def get_asset(
    request: Request,
    asset_id: str,
    db: AsyncSession = Depends(get_db),
):
    query = select(Asset).apply_org_check(request).where(Asset.id == asset_id)
    result = await db.execute(query)
    asset = result.scalar_one_or_none()
    
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    
    s3_config = await get_s3_config(request, db)
    session_token = s3_config.session_token
    
    if s3_config and not s3_config.public and asset.url:
        asset.url = get_temporary_download_url(
            asset.url,
            region=s3_config.region,
            access_key=s3_config.access_key,
            secret_key=s3_config.secret_key,
            session_token=session_token,
            expiration=3600,
        )
    
    return asset


@router.patch("/assets/{asset_id}", response_model=AssetResponse)
async def update_asset(
    request: Request,
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    path: str = Body(..., embed=True),
):
    query = select(Asset).apply_org_check(request).where(Asset.id == asset_id)
    result = await db.execute(query)
    asset = result.scalar_one_or_none()

    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    
    if asset.is_folder:
        raise HTTPException(status_code=400, detail="Cannot update path of a folder")

    if path is not None:
        if path != "":
            normalized_path = urllib.parse.quote(path)

            folder_query = select(Asset).apply_org_check(request).where(
                and_(
                    Asset.path == urllib.parse.unquote(normalized_path),
                    Asset.is_folder.is_(True)
                )
            )
            existing_folder = await db.execute(folder_query)
            
            if not existing_folder.scalar_one_or_none():
                raise HTTPException(
                    status_code=404, 
                    detail="The target path does not exist"
                )

        filename = os.path.basename(asset.path)
        new_path = os.path.join(path, filename).replace("\\", "/")
        asset.path = new_path

    await db.commit()
    await db.refresh(asset)

    return asset

@router.post("/assets/add", response_model=AssetResponse)
async def add_asset_via_url(
    request: Request,
    db: AsyncSession = Depends(get_db),
    url: str = Body(..., embed=True),
    path: str = Body(..., embed=True),
):
    user_id = request.state.current_user["user_id"]
    org_id = request.state.current_user.get("org_id")

    # Get file info from URL
    parsed_url = urllib.parse.urlparse(url)
    url_path = parsed_url.path
    file_name = os.path.basename(url_path)
    
    # Guess mime type from URL
    mime_type, _ = mimetypes.guess_type(url)
    if not mime_type:
        raise HTTPException(status_code=400, detail="Could not determine file type")

    # Validate mime type
    if not mime_type.startswith(("image/", "video/", "application/", "audio/")):
        raise HTTPException(status_code=400, detail="Unsupported file type")

    # Generate file ID based on mime type
    file_id = new_id(get_id_prefix_from_type(mime_type))

    # Check file size
    async with aiohttp.ClientSession() as session:
        async with session.head(url) as response:
            if response.status != 200:
                raise HTTPException(status_code=400, detail="Could not access URL")
            
            # Get content length
            file_size = int(response.headers.get('content-length', 0))
            if file_size > UPLOAD_FILE_SIZE_LIMIT_MB:
                raise HTTPException(
                    status_code=400,
                    detail=f"File size exceeds {UPLOAD_FILE_SIZE_LIMIT_MB // (1024 * 1024)}MB limit"
                )
            
            # Verify content type from response
            content_type = response.headers.get('content-type')
            if content_type and not content_type.startswith(("image/", "video/", "application/", "audio/")):
                raise HTTPException(status_code=400, detail="Unsupported file type")

    # Validate parent path exists if not root
    if path and path != "/":
        normalized_path = urllib.parse.quote(path)
        folder_query = select(Asset).apply_org_check(request).where(
            and_(
                Asset.path == urllib.parse.unquote(normalized_path),
                Asset.is_folder.is_(True)
            )
        )
        existing_folder = await db.execute(folder_query)
        
        if not existing_folder.scalar_one_or_none():
            raise HTTPException(
                status_code=404, 
                detail="The target path does not exist"
            )

    # Create the asset path
    file_extension = os.path.splitext(file_name)[1]
    db_file_path = os.path.join(path.lstrip("/"), f"{file_id}{file_extension}").replace("\\", "/")

    # Create new asset
    new_asset = Asset(
        user_id=user_id,
        org_id=org_id,
        id=file_id,
        name=file_name,
        is_folder=False,
        path=db_file_path,
        url=url,
        mime_type=mime_type,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        file_size=file_size
    )
    
    db.add(new_asset)
    await db.commit()
    await db.refresh(new_asset)
    
    return new_asset