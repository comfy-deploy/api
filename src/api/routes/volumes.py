import time

from api.utils.multi_level_cache import multi_level_cached
from .types import Model, GenerateUploadUrlRequest, GenerateUploadUrlResponse
from fastapi import HTTPException, APIRouter, Request, BackgroundTasks
from typing import Any, Dict, List, Tuple, Union, Optional, Literal
import logging
import os
import uuid
from .utils import get_user_settings, select, generate_presigned_url, delete_s3_object
from api.utils.storage_helper import get_s3_config
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from api.database import get_db
from api.models import Model as ModelDB, UserVolume
from datetime import datetime, timedelta
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from .types import ModalVolFile
from sqlalchemy.exc import MultipleResultsFound
from pydantic import BaseModel
from enum import Enum
import re
import grpclib
from modal import Volume, Secret
import modal
from huggingface_hub import HfApi, repo_info
import logfire
from huggingface_hub.utils import RepositoryNotFoundError

import aiohttp
from urllib.parse import urlparse
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Volumes"])

async def retrieve_model_volumes(
    request: Request, db: AsyncSession
) -> List[Dict[str, str]]:
    volumes = await get_user_volumes(request, db)
    if len(volumes) == 0:
        volumes = [await add_model_volume(request, db)]
    return volumes

async def get_volume_name(request: Request, db: AsyncSession) -> str:
    volumes = await retrieve_model_volumes(request, db)
    return volumes[0]["volume_name"]

async def get_user_volumes(request: Request, db: AsyncSession) -> List[Dict[str, str]]:
    user_volume_query = (
        select(UserVolume)
        .apply_org_check(request)
        .where(
            UserVolume.disabled == False,
        )
        .order_by(UserVolume.created_at.desc())
    )
    result = await db.execute(user_volume_query)
    volumes = result.scalars().all()
    return [volume.to_dict() for volume in volumes]


async def add_model_volume(request: Request, db: AsyncSession) -> Dict[str, Any]:
    user_id = request.state.current_user["user_id"]
    org_id = (
        request.state.current_user["org_id"]
        if "org_id" in request.state.current_user
        else None
    )

    # Insert new volume
    new_volume = UserVolume(
        user_id=user_id,
        org_id=org_id,
        volume_name=f"models_{org_id if org_id else user_id}",
        disabled=False,
    )
    db.add(new_volume)
    await db.commit()
    await db.refresh(new_volume)

    return new_volume.to_dict()


async def upsert_model_to_db(
    db: AsyncSession, model_data: Dict[str, Any], request: Request
) -> None:
    user_id = request.state.current_user["user_id"]
    org_id = (
        request.state.current_user["org_id"]
        if "org_id" in request.state.current_user
        else None
    )
    user_volume_id = model_data.get("user_volume_id")
    model_name = model_data.get("name")
    folder_path = model_data.get("path")
    category = model_data.get("category")
    size = model_data.get("size")

    new_model = ModelDB(
        user_id=user_id,
        org_id=org_id,
        user_volume_id=user_volume_id,
        model_name=model_name,
        folder_path=folder_path,
        is_public=True,
        status="success",
        download_progress=100,
        upload_type="other",
        size=size,
        model_type="custom"
        if category
        not in [
            "checkpoint",
            "lora",
            "embedding",
            "vae",
            "clip",
            "clip_vision",
            "configs",
            "controlnet",
            "upscale_models",
            "ipadapter",
            "gligen",
            "unet",
            "custom_node",
        ]
        else category,
    )
    if model_data.get("id") is not None:
        new_model.id = model_data.get("id")

    with logfire.span("Upserting model", output=new_model):
        query = insert(ModelDB).values(new_model.to_dict())
        # update_dict = {c.name: c for c in query.excluded if not c.primary_key}
        query = query.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "model_name": new_model.model_name,
                "folder_path": new_model.folder_path,
                "download_progress": 100,
                "size": new_model.size,
                "updated_at": datetime.now(),
            },
        )
        # logger.info(f"model_name: {model_name}, size: {new_model.size}")
        await db.execute(query)
        await db.commit()


async def get_downloading_models(request: Request, db: AsyncSession):
    model_query = (
        select(ModelDB)
        .order_by(ModelDB.model_name.desc())
        .apply_org_check(request)
        .where(
            ModelDB.deleted == False,
            ModelDB.download_progress != 100,
            ModelDB.status != "failed",
            ModelDB.created_at > datetime.now() - timedelta(hours=24),
        )
    )
    result = await db.execute(model_query)
    volumes = result.scalars().all()
    return volumes

@router.get("/volume/private-models", response_model=List[ModalVolFile])
async def private_models(
    request: Request, db: AsyncSession = Depends(get_db)
):
    volume_name = await get_volume_name(request, db)
    return await get_volumn_files(volume_name)

public_volume_name = os.environ.get("SHARED_MODEL_VOLUME_NAME")

@router.get("/volume/public-models", response_model=List[ModalVolFile])
async def public_models(
    request: Request
):
    return await get_volumn_files(public_volume_name)

# @multi_level_cached(
#     # key_prefix="api_key",
#     # Time for local memory cache to refresh from redis
#     ttl_seconds=2,
#     # Time for redis to refresh from source (modal)
#     redis_ttl_seconds=5,
#     version="1.0",
#     key_builder=lambda key: f"volume_files_{key}",
# )
async def get_volumn_files(key):
    volume = await lookup_volume(key, create_if_missing=True)
    files = await volume.listdir.aio("/", recursive=True)
    # Convert to dictionaries for JSON serialization
    return [
        {
            "path": file.path,
            "type": file.type,
            "size": file.size,
            "mtime": file.mtime
        }
        for file in files
    ]


@router.get("/volume/downloading-models", response_model=List[Model])
async def downloading_models(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        data = await get_downloading_models(request, db)
        model_data = []
        for model in data:
            model_dict = model.to_dict()
            logger.info(f"download_progress: {model.download_progress}")
            if model.download_progress == 100:
                model_dict["is_done"] = True
            model_data.append(model_dict)
        return JSONResponse(content=model_data)
    except Exception as e:
        logger.error(f"Error fetching downloading models: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Type definitions
class MoveFileBody(BaseModel):
    src_path: str
    dst_path: str


class RemovePath(BaseModel):
    path: str


class AddFileInput(BaseModel):
    # volume_name: str
    download_url: str
    folder_path: str
    filename: str
    # callback_url: str
    db_model_id: str
    upload_type: str


class ModelDownloadStatus(Enum):
    PROGRESS = "progress"
    SUCCESS = "success"
    FAILED = "failed"


# Constants
FILE_TYPE = 1
DIRECTORY_TYPE = 2

class NewRenameFileBody(BaseModel):
    filename: str
    
class AddFileInputNew(BaseModel):
    url: str
    filename: Optional[str]
    folder_path: str


# TODO: verify removal
@router.post("/volume/file")
async def add_file_volume(
    request: Request, 
    body: AddFileInputNew,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Handle model file uploads from different sources (Civitai, HuggingFace, or generic URLs)"""
    
    if "civitai.com/models/" in body.url:
        return await handle_civitai_model(request, body, db, background_tasks)
    elif "huggingface.co/" in body.url:
        return await handle_huggingface_model(request, body, db, background_tasks)
    else:
        return await handle_generic_model(request, body, db, background_tasks)

# TODO: verify removal
@router.post("/file")
async def add_file(
    request: Request, 
    body: AddFileInputNew,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Handle model file uploads from different sources (Civitai, HuggingFace, or generic URLs)"""
    
    if "civitai.com/models/" in body.url:
        return await handle_civitai_model(request, body, db, background_tasks)
    elif "huggingface.co/" in body.url:
        return await handle_huggingface_model(request, body, db, background_tasks)
    else:
        return await handle_generic_model(request, body, db, background_tasks)

# TODO: verify removal
@router.post("/file/{file_id}/rename")
async def rename_file(
    request: Request,
    body: NewRenameFileBody,
    file_id: str,
    db: AsyncSession = Depends(get_db),
):
    new_filename = body.filename
    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user["org_id"] if "org_id" in current_user else None

    volume_name = f"models_{org_id}" if org_id else f"models_{user_id}"

    volume = Volume.from_name(volume_name)

    model = (
        await db.execute(
            select(ModelDB)
            .apply_org_check(request)
            .where(ModelDB.id == file_id, ~ModelDB.deleted)
        )
    ).scalar_one()

    src_path = os.path.join(model.folder_path, model.model_name)

    # check src_path is a file
    is_valid, error_message = await validate_file_path_aio(src_path, volume)
    if not is_valid:
        if "not found" in error_message:
            raise HTTPException(status_code=404, detail=error_message)
        else:
            raise HTTPException(status_code=400, detail=error_message)

    filename_valid = is_valid_filename(new_filename)
    if not filename_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid filename{new_filename}, only allow characters, numerics, underscores, and hyphens.",
        )

    folder_path = os.path.dirname(src_path)
    dst_path = os.path.join(folder_path, new_filename)

    # Check if the destination file exists and if we can overwrite it
    try:
        contents = await volume.listdir.aio(dst_path)
        if contents:
            # if not overwrite:
            raise HTTPException(
                status_code=400,
                detail="Destination file exists and overwrite is False.",
            )
    except Exception as _:
        pass

    print("src_path: ", src_path)
    print("dst_path: ", dst_path)

    await volume.copy_files.aio([src_path], dst_path)
    await volume.remove_file.aio(src_path)

    model.model_name = new_filename

    await db.commit()

    return model.to_dict()


# Used by v1 dashboard
@router.post("/volume/mv", include_in_schema=False)
async def move_file(request: Request, body: MoveFileBody, db: AsyncSession = Depends(get_db)):
    src_path = body.src_path
    dst_path = body.dst_path 
    volume_name = await get_volume_name(request, db)

    try:
        volume = await lookup_volume(volume_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Check src_path is a file
    is_valid, error_message = await validate_file_path(src_path, volume)
    if not is_valid:
        if "not found" in error_message:
            raise HTTPException(status_code=404, detail=error_message)
        else:
            raise HTTPException(status_code=400, detail=error_message)

    # Check if destination file exists and if we can overwrite it
    try:
        dst_contents = await volume.listdir.aio(dst_path)
        if dst_contents:
            raise HTTPException(
                status_code=400,
                detail="Destination file exists.",
            )
    except grpclib.exceptions.GRPCError as e:
        # NOT_FOUND is expected and means we can proceed
        if e.status != grpclib.Status.NOT_FOUND:
            raise HTTPException(status_code=500, detail=str(e))

    # Perform the move operation (copy + delete)
    volume.copy_files([src_path], dst_path)
    volume.remove_file(src_path)

    return {
        "old_path": src_path,
        "new_path": dst_path,
    }


@router.post("/volume/rm")
async def remove_file(request: Request, body: RemovePath, db: AsyncSession = Depends(get_db)):
    volume_name = await get_volume_name(request, db)
    try:
        volume = Volume.from_name(volume_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    is_valid, error_message = await validate_path_aio(body.path, volume)
    if not is_valid:
        if "not found" in error_message:
            raise HTTPException(status_code=404, detail=error_message)
        else:
            raise HTTPException(status_code=400, detail=error_message)

    volume.remove_file(body.path, recursive=True)
    return {"deleted_path": body.path}


async def handle_file_download(
    request: Request,
    db: AsyncSession,
    download_url: str,
    folder_path: str,
    filename: str,
    upload_type: str,
    db_model_id: str,
    background_tasks: BackgroundTasks,
    s3_temp_object: Optional[Dict[str, str]] = None
) -> StreamingResponse:
    """Helper function to handle file downloads with progress tracking"""
    try:
        volume_name = await get_volume_name(request, db)

        user_settings = await get_user_settings(request, db)
        hugging_face_token = os.environ.get("HUGGINGFACE_TOKEN")
        if user_settings is not None and user_settings.hugging_face_token:
            hugging_face_token = (
                user_settings.hugging_face_token.strip() or hugging_face_token
            )

        volume = await lookup_volume(volume_name, create_if_missing=True)
        full_path = os.path.join(folder_path, filename)

        try:
            file_exists = await does_file_exist(full_path, volume)
            if file_exists:
                raise HTTPException(status_code=400, detail="File already exists.")
        except grpclib.exceptions.GRPCError as e:
            print("e: ", str(e))
            raise HTTPException(status_code=400, detail="Error: " + str(e))

        modal_download_file_task = modal.Function.lookup(
            "volume-operations", "modal_download_file_task"
        )

        async def event_generator():
            try:
                async for event in modal_download_file_task.remote_gen.aio(
                    download_url,
                    folder_path,
                    filename,
                    db_model_id,
                    full_path,
                    volume_name,
                    "huggingface" if "huggingface.co" in download_url else upload_type,
                    hugging_face_token,
                ):
                    # Update database with the event status
                    if event.get("status") == "progress":
                        model_status_query = (
                            update(ModelDB)
                            .where(ModelDB.id == db_model_id)
                            .values(
                                updated_at=datetime.now(),
                                download_progress=event.get("download_progress", 0),
                            )
                        )
                    elif event.get("status") == "success":
                        model_status_query = (
                            update(ModelDB)
                            .where(ModelDB.id == db_model_id)
                            .values(
                                status="success",
                                updated_at=datetime.now(),
                                download_progress=100,
                            )
                        )
                    elif event.get("status") == "failed":
                        model_status_query = (
                            update(ModelDB)
                            .where(ModelDB.id == db_model_id)
                            .values(
                                status="failed",
                                error_log=event.get("error_log"),
                                updated_at=datetime.now(),
                            )
                        )

                    await db.execute(model_status_query)
                    await db.commit()

                    # yield json.dumps(event) + "\n"
            except Exception as e:
                error_event = {
                    "status": "failed",
                    "error_log": str(e),
                    "model_id": db_model_id,
                    "download_progress": 0,
                }
                # yield json.dumps(error_event) + "\n"

                model_status_query = (
                    update(ModelDB)
                    .where(ModelDB.id == db_model_id)
                    .values(
                        status="failed",
                        error_log=str(e),
                        updated_at=datetime.now(),
                    )
                )
                await db.execute(model_status_query)
                await db.commit()
                raise e

        if s3_temp_object:
            try:
                async def delete_temp_s3_object():
                    try:
                        await event_generator()  # Wait for download to complete
                        
                        s3_config = await get_s3_config(request, db)
                        
                        delete_s3_object(
                            bucket=s3_temp_object["bucket"],
                            object_key=s3_temp_object["object_key"],
                            region=s3_config.region,
                            access_key=s3_config.access_key,
                            secret_key=s3_config.secret_key,
                            session_token=s3_config.session_token
                        )
                        logger.info(f"Deleted temporary S3 object: {s3_temp_object['object_key']}")
                    except Exception as e:
                        logger.error(f"Error deleting S3 object: {str(e)}")
                
                background_tasks.add_task(delete_temp_s3_object)
            except Exception as e:
                logger.error(f"Error setting up S3 object deletion: {str(e)}")
        else:
            background_tasks.add_task(event_generator)

        return JSONResponse(content={"message": "success"})

    except Exception as e:
        print(f"Error in file download: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# TODO: verify removal
@router.post("/volume/add_file", deprecated=True, include_in_schema=False)
async def add_file_old(
    request: Request,
    body: AddFileInput,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    return await handle_file_download(
        request=request,
        db=db,
        download_url=body.download_url,
        folder_path=body.folder_path,
        filename=body.filename,
        upload_type=body.upload_type,
        db_model_id=body.db_model_id,
        background_tasks=background_tasks,
    )

async def handle_generic_model(
    request: Request,
    data: AddFileInputNew, 
    db: AsyncSession,
    background_tasks: BackgroundTasks,
    s3_temp_object: Optional[Dict[str, str]] = None
):
    filename = data.filename or await get_filename_from_url(data.url)
    if not filename:
        model = await create_model_error_record(
            request=request,
            db=db,
            url=data.url,
            error_message="filename not found",
            upload_type="download-url",
            folder_path=data.folder_path
        )
        raise HTTPException(status_code=400, detail="No filename found")

    # Check if file exists
    await check_file_existence(filename, data.folder_path, data.url, "download-url", request, db)

    volumes = await retrieve_model_volumes(request, db)
    
    model = await add_model_download_url(
        request=request,
        db=db,
        upload_type="download-url",
        model_name=filename,
        url=data.url,
        volume_id=volumes[0]["id"],
        custom_path=data.folder_path
    )

    await handle_file_download(
        request=request,
        db=db,
        download_url=data.url,
        folder_path=data.folder_path,
        filename=filename,
        upload_type="download-url", 
        db_model_id=str(model.id),
        background_tasks=background_tasks,
        s3_temp_object=s3_temp_object
    )

    return {"message": "Generic model download started"}

async def add_model_download_url(
    request: Request,
    db: AsyncSession,
    upload_type: str,
    model_name: str,
    url: str,
    volume_id: str,
    custom_path: str
) -> ModelDB:
    """Create a new model record in the database"""
    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user["org_id"] if "org_id" in current_user else None

    new_model = ModelDB(
        user_id=user_id,
        org_id=org_id,
        user_volume_id=volume_id,
        upload_type=upload_type,
        model_name=model_name,
        user_url=url,
        model_type="custom",
        folder_path=custom_path,
        status="started",
        download_progress=0
    )

    db.add(new_model)
    await db.commit()
    await db.refresh(new_model)
    
    return new_model

async def create_model_error_record(
    request: Request,
    db: AsyncSession,
    url: str,
    error_message: str,
    upload_type: str,
    folder_path: str,
    model_name: Optional[str] = None
) -> ModelDB:
    """Create an error record in the model table"""
    volumes = await retrieve_model_volumes(request, db)
    current_user = request.state.current_user
    
    new_model = ModelDB(
        user_id=current_user["user_id"],
        org_id=current_user.get("org_id"),
        user_volume_id=volumes[0]["id"],
        upload_type=upload_type,
        model_type="custom",
        user_url=url if upload_type == "download-url" else None,
        civitai_url=url if upload_type == "civitai" else None,
        error_log=error_message,
        folder_path=folder_path,
        model_name=model_name,
        status="failed"
    )

    db.add(new_model)
    await db.commit()
    await db.refresh(new_model)
    
    return new_model

# # TODO: verify removal
# @router.post("/volume/file/{file_id}/retry", include_in_schema=False)
# @router.post("/file/{file_id}/retry")
# async def retry_download(
#     request: Request,
#     file_id: str,
#     background_tasks: BackgroundTasks,
#     db: AsyncSession = Depends(get_db),
# ):
#     # Query to get the existing model record
#     query = (
#         select(ModelDB)
#         .where(
#             ModelDB.id == file_id,
#             ModelDB.deleted == False,
#             ModelDB.status == "failed",  # Only allow retrying failed downloads
#         )
#         .apply_org_check(request)
#     )

#     result = await db.execute(query)
#     model = result.scalar_one_or_none()

#     if not model:
#         raise HTTPException(
#             status_code=404, detail="Model not found or not eligible for retry"
#         )

#     # Use the helper function for the download
#     res = await handle_file_download(
#         request=request,
#         db=db,
#         download_url=model.user_url,
#         folder_path=model.folder_path,
#         filename=model.model_name,
#         upload_type=model.upload_type,
#         db_model_id=str(model.id),
#         background_tasks=background_tasks,
#     )

#     # Reset the model status for retry
#     model_status_query = (
#         update(ModelDB)
#         .where(ModelDB.id == file_id)
#         .values(
#             status="started",
#             error_log=None,
#             updated_at=datetime.now(),
#             download_progress=0,
#         )
#     )
#     await db.execute(model_status_query)
#     await db.commit()

#     return res


async def does_file_exist(path: str, volume: Volume) -> bool:
    try:
        contents = await volume.listdir.aio(path)
        if not contents:
            return False
        if len(contents) == 1 and contents[0].type == FILE_TYPE:
            return True
        return False
    except grpclib.exceptions.GRPCError as e:
        if e.status == grpclib.Status.NOT_FOUND:
            return False
        else:
            raise e

async def validate_path_aio(path: str, volume: Volume):
    try:
        await volume.listdir.aio(path)
        return True, None
    except grpclib.exceptions.GRPCError as e:
        if e.status == grpclib.Status.NOT_FOUND:
            return False, f"path: {path} not found."
        else:
            return False, str(e)

async def validate_file_path_aio(path: str, volume: Volume):
    try:
        contents = await volume.listdir.aio(path)
        if not contents:
            return False, "No file found or the first item is not a file."
        if len(contents) > 1:
            return False, "directory supplied"
        if contents[0].type == DIRECTORY_TYPE:
            return False, "directory supplied"
        if contents[0].type != FILE_TYPE:
            return False, "not a file"
        return True, None
    except grpclib.exceptions.GRPCError as e:
        if e.status == grpclib.Status.NOT_FOUND:
            return False, f"path: {path} not found."
        else:
            return False, str(e)


async def validate_file_path(path: str, volume: Volume):
    try:
        contents = await volume.listdir.aio(path)
        if not contents:
            return False, "No file found or the first item is not a file."
        if len(contents) > 1:
            return False, "directory supplied"
        if contents[0].type == DIRECTORY_TYPE:
            return False, "directory supplied"
        if contents[0].type != FILE_TYPE:
            return False, "not a file"
        return True, None
    except grpclib.exceptions.GRPCError as e:
        if e.status == grpclib.Status.NOT_FOUND:
            return False, f"path: {path} not found."
        else:
            return False, str(e)


def is_valid_filename(filename):
    pattern = r"^[\w\-\.]+$"
    if re.match(pattern, filename):
        return True
    else:
        return False


async def lookup_volume(volume_name: str, create_if_missing: bool = False):
    try:
        return await Volume.lookup.aio(volume_name, create_if_missing=create_if_missing)
    except Exception as e:
        raise Exception(f"Can't find Volume: {e}")

class StatusEnum(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PROGRESS = "progress"


class RequestModel(BaseModel):
    model_id: str
    status: StatusEnum
    error_log: Optional[str] = None
    filehash_sha256: Optional[str] = None
    download_progress: Optional[float] = None


# TODO: needed?
@router.post("/volume/volume-upload", include_in_schema=False)
async def update_status(
    request: Request, body: RequestModel, db: AsyncSession = Depends(get_db)
):
    import requests

    try:
        # Convert the Pydantic model to a dictionary
        body_dict = body.dict()

        # Convert Enum to string
        body_dict["status"] = body_dict["status"].value

        response = requests.post(
            "http://127.0.0.1:3010/api/volume-upload", json=body_dict
        )
        response.raise_for_status()  # Raise an exception for HTTP errors

        return {"message": "Status updated successfully"}
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error updating status: {str(e)}")

class ListModelsInput(BaseModel):
    limit: int = 10
    search: Optional[str] = None


# Helper functions for file operations
async def get_filename_from_url(url: str) -> Optional[str]:
    """Extract filename from URL or Content-Disposition header"""
    try:
        # First try to get filename from the URL path
        parsed_url = urlparse(url)
        path_filename = parsed_url.path.split('/')[-1]
        if path_filename and '.' in path_filename:
            return path_filename

        # If no filename in URL, try to get it from Content-Disposition header
        async with aiohttp.ClientSession() as session:
            async with session.head(url) as response:
                if response.status == 200:
                    content_disposition = response.headers.get('Content-Disposition')
                    if content_disposition:
                        filename_match = re.search(r'filename="?([^"]+)"?', content_disposition)
                        if filename_match:
                            return filename_match.group(1)
        
        return None
    except Exception as e:
        logger.error(f"Error getting filename from URL: {str(e)}")
        return None

async def check_file_existence(
    filename: str, 
    custom_path: str, 
    url: str, 
    upload_type: str,
    request: Request,
    db: AsyncSession
) -> None:
    """Check if a file already exists in the volume"""
    try:
        volume_name = await get_volume_name(request, db)
        volume = Volume.from_name(volume_name)
        
        full_path = os.path.join(custom_path, filename)
        try:
            contents = await volume.listdir.aio(full_path)
            if contents:
                await create_model_error_record(
                    request=request,
                    db=db,
                    url=url,
                    error_message="File already exists",
                    upload_type=upload_type,
                    folder_path=custom_path,
                    model_name=filename
                )
                raise HTTPException(
                    status_code=409, 
                    detail=f"File {full_path} already exists"
                )
        except grpclib.exceptions.GRPCError as e:
            if e.status != grpclib.Status.NOT_FOUND:
                raise e
    except Exception as e:
        logger.error(f"Error checking file existence: {str(e)}")
        status_code = getattr(e, "status_code", 500)

        raise HTTPException(status_code=status_code, detail=str(e))

# Civitai handler
async def handle_civitai_model(
    request: Request,
    data: AddFileInputNew,
    db: AsyncSession,
    background_tasks: BackgroundTasks
):
    """Handle Civitai model downloads"""
    try:
        # Parse Civitai URL and get model info
        model_id, version_id = parse_civitai_url(data.url)
        civitai_info = await get_civitai_model_info(model_id)
        
        if not civitai_info.get("modelVersions"):
            raise HTTPException(status_code=400, detail="No model versions found")
            
        # Select version (latest if not specified)
        if version_id:
            selected_version = next(
                (v for v in civitai_info["modelVersions"] if str(v["id"]) == version_id),
                civitai_info["modelVersions"][0]
            )
        else:
            selected_version = civitai_info["modelVersions"][0]
        
        if not selected_version:
            raise HTTPException(status_code=400, detail="Model version not found")
            
        # Get filename
        filename = data.filename or selected_version["files"][0]["name"]
        
        # Check if file exists
        await check_file_existence(
            filename, 
            data.folder_path, 
            data.url, 
            "civitai",
            request,
            db
        )
        
        # Create model record
        volumes = await retrieve_model_volumes(request, db)
        model = ModelDB(
            user_id=request.state.current_user["user_id"],
            org_id=request.state.current_user.get("org_id"),
            upload_type="civitai",
            model_name=filename,
            civitai_id=str(civitai_info["id"]),
            civitai_version_id=str(selected_version["id"]),
            civitai_url=data.url,
            civitai_download_url=selected_version["files"][0]["downloadUrl"],
            civitai_model_response=civitai_info,
            user_volume_id=volumes[0]["id"],
            model_type="custom",
            folder_path=data.folder_path,
            status="started",
            download_progress=0
        )
        
        db.add(model)
        await db.commit()
        await db.refresh(model)
        
        # Start download
        await handle_file_download(
            request=request,
            db=db,
            download_url=selected_version["files"][0]["downloadUrl"],
            folder_path=data.folder_path,
            filename=filename,
            upload_type="civitai",
            db_model_id=str(model.id),
            background_tasks=background_tasks
        )
        
        return {"message": "Civitai model download started"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error handling Civitai model: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# HuggingFace handler
async def handle_huggingface_model(
    request: Request,
    data: AddFileInputNew,
    db: AsyncSession,
    background_tasks: BackgroundTasks
):
    """Handle HuggingFace model downloads"""
    try:
        # Extract repo ID from URL
        repo_id = extract_huggingface_repo_id(data.url)
        if not repo_id:
            raise HTTPException(status_code=400, detail="Invalid Hugging Face URL")
            
        # Get filename
        filename = data.filename or await get_filename_from_url(data.url)
        if not filename:
            await create_model_error_record(
                request=request,
                db=db,
                url=data.url,
                error_message="filename not found",
                upload_type="huggingface",
                folder_path=data.folder_path
            )
            raise HTTPException(status_code=400, detail="No filename found")
            
        # Check if file exists
        await check_file_existence(
            filename, 
            data.folder_path, 
            data.url, 
            "huggingface",
            request,
            db
        )
        
        # Create model record
        volumes = await retrieve_model_volumes(request, db)
        model = await add_model_download_url(
            request=request,
            db=db,
            upload_type="huggingface",
            model_name=filename,
            url=data.url,
            volume_id=volumes[0]["id"],
            custom_path=data.folder_path
        )
        
        # Start download
        await handle_file_download(
            request=request,
            db=db,
            download_url=data.url,
            folder_path=data.folder_path,
            filename=filename,
            upload_type="huggingface",
            db_model_id=str(model.id),
            background_tasks=background_tasks
        )
        
        return {"message": "Hugging Face model download started"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error handling HuggingFace model: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Helper functions for Civitai and HuggingFace
def parse_civitai_url(url: str) -> Tuple[str, Optional[str]]:
    """Extract model ID and version ID from Civitai URL"""
    model_match = re.search(r'civitai\.com/models/(\d+)(?:/.*?)?(?:\?modelVersionId=(\d+))?', url)
    if not model_match:
        raise HTTPException(status_code=400, detail="Invalid Civitai URL")
    return model_match.group(1), model_match.group(2)

async def get_civitai_model_info(model_id: str) -> dict:
    """Fetch model information from Civitai API"""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://civitai.com/api/v1/models/{model_id}") as response:
            if response.status != 200:
                raise HTTPException(
                    status_code=response.status,
                    detail="Error fetching model info from Civitai"
                )
            return await response.json()

def extract_huggingface_repo_id(url: str) -> Optional[str]:
    """Extract repository ID from HuggingFace URL"""
    match = re.search(r'huggingface\.co/([^/]+/[^/]+)', url)
    return match.group(1) if match else None

# Add new request/response models
class HuggingFaceValidateRequest(BaseModel):
    repo_id: str

class HuggingFaceValidateResponse(BaseModel):
    exists: bool

class CivitaiValidateRequest(BaseModel):
    url: str

class CivitaiValidateResponse(BaseModel):
    exists: bool
    title: Optional[str] = None
    preview_url: Optional[str] = None
    filename: Optional[str] = None
    model_id: Optional[str] = None
    version_id: Optional[str] = None

# Add new validation endpoints
@router.post("/volume/validate/huggingface", response_model=HuggingFaceValidateResponse)
async def validate_huggingface_repo(
    request: Request,
    body: HuggingFaceValidateRequest,
    db: AsyncSession = Depends(get_db)
):
    """Validate a HuggingFace repository exists and return its metadata"""
    try:
        # Reuse existing user settings function
        user_settings = await get_user_settings(request, db)
        token = user_settings.hugging_face_token if user_settings else None

        # Check if repo exists
        repo_info(body.repo_id, token=token)

        return HuggingFaceValidateResponse(
            exists=True,
        )

    except RepositoryNotFoundError:
        return HuggingFaceValidateResponse(exists=False)
    except Exception as e:
        logger.error(f"Error validating HuggingFace repo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/volume/validate/civitai", response_model=CivitaiValidateResponse)
async def validate_civitai_url(body: CivitaiValidateRequest):
    """Validate a Civitai URL and return model metadata"""
    try:
        # Reuse existing Civitai URL parsing function
        model_id, version_id = parse_civitai_url(body.url)
        if not model_id:
            return CivitaiValidateResponse(exists=False)

        # Reuse existing Civitai info fetching function
        model_data = await get_civitai_model_info(model_id)
        if not model_data:
            return CivitaiValidateResponse(exists=False)
        

        # Get version info
        if version_id:
            version = next(
                (v for v in model_data["modelVersions"] if str(v["id"]) == version_id),
                model_data["modelVersions"][0]
            )
        else:
            version = model_data["modelVersions"][0]

        # Get preview image/animation
        preview_url = None
        if version and version.get("images"):
            preview = version["images"][0]
            preview_url = preview.get("url") or preview.get("nsfw") or None

        return CivitaiValidateResponse(
            exists=True,
            title=model_data["name"],
            preview_url=preview_url,
            filename=version["files"][0]["name"],
            model_id=model_id,
            version_id=version_id
        )

    except HTTPException as http_e:
        raise http_e

    except Exception as e:
        logger.error(f"Error validating Civitai URL: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

class HuggingFaceRepoRequest(BaseModel):
    repo_id: str
    folder_path: str
    
async def add_huggingface_repo(
    request: Request,
    body: HuggingFaceRepoRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Download an entire HuggingFace repository to a volume"""
    try:
        # Validate repo exists
        user_settings = await get_user_settings(request, db)
        token = user_settings.hugging_face_token if user_settings else None
        
        try:
            repo_info(body.repo_id, token=token)
        except RepositoryNotFoundError:
            raise HTTPException(status_code=404, detail="HuggingFace repository not found")
        
        # Check if folder exists
        volume_name = await get_volume_name(request, db)
        volume = Volume.from_name(volume_name)
        
        # Get repo name for folder check
        repo_name = body.repo_id.split("/")[-1]
        target_folder = os.path.join(body.folder_path, repo_name)
        
        # Check if the target folder already exists
        try:
            contents = await volume.listdir.aio(target_folder)
            if contents:
                # Folder exists and has content
                raise HTTPException(
                    status_code=409, 
                    detail=f"Folder '{target_folder}' already exists. Please choose a different folder path."
                )
        except grpclib.exceptions.GRPCError as e:
            # NOT_FOUND is expected and means we can proceed
            if e.status != grpclib.Status.NOT_FOUND:
                raise HTTPException(status_code=500, detail=f"Error checking folder: {str(e)}")
        
        # Create model record
        volumes = await retrieve_model_volumes(request, db)
        model = ModelDB(
            user_id=request.state.current_user["user_id"],
            org_id=request.state.current_user.get("org_id"),
            upload_type="huggingface",
            model_name=repo_name,  # Use repo name as model name
            user_url=f"https://huggingface.co/{body.repo_id}",
            user_volume_id=volumes[0]["id"],
            model_type="custom",
            folder_path=body.folder_path,
            status="started",
            download_progress=0,
            hf_url=f"https://huggingface.co/{body.repo_id}"
        )
        
        db.add(model)
        await db.commit()
        await db.refresh(model)
        
        # Use Modal to download the entire repo
        modal_download_repo_task = modal.Function.lookup(
            "volume-operations", "modal_download_repo_task"
        )
        
        async def download_repo_task():
            try:
                async for event in modal_download_repo_task.remote_gen.aio(
                    body.repo_id,
                    body.folder_path,
                    str(model.id),
                    volume_name,
                    token
                ):
                    # Update database with the event status
                    if event.get("status") == "progress":
                        model_status_query = (
                            update(ModelDB)
                            .where(ModelDB.id == model.id)
                            .values(
                                updated_at=datetime.now(),
                                download_progress=event.get("download_progress", 0),
                            )
                        )
                    elif event.get("status") == "success":
                        model_status_query = (
                            update(ModelDB)
                            .where(ModelDB.id == model.id)
                            .values(
                                status="success",
                                updated_at=datetime.now(),
                                download_progress=100,
                            )
                        )
                    elif event.get("status") == "failed":
                        model_status_query = (
                            update(ModelDB)
                            .where(ModelDB.id == model.id)
                            .values(
                                status="failed",
                                error_log=event.get("error_log"),
                                updated_at=datetime.now(),
                            )
                        )

                    await db.execute(model_status_query)
                    await db.commit()
            except Exception as e:
                error_event = {
                    "status": "failed",
                    "error_log": str(e),
                    "model_id": str(model.id),
                    "download_progress": 0,
                }

                model_status_query = (
                    update(ModelDB)
                    .where(ModelDB.id == model.id)
                    .values(
                        status="failed",
                        error_log=str(e),
                        updated_at=datetime.now(),
                    )
                )
                await db.execute(model_status_query)
                await db.commit()
                raise e
                
        background_tasks.add_task(download_repo_task)
        
        return {"message": "HuggingFace repository download started", "model_id": str(model.id)}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error handling HuggingFace repo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

class HuggingfaceModel(BaseModel):
    repoId: str

class CivitaiModel(BaseModel):
    url: str

class AddModelRequest(BaseModel):
    source: Literal["huggingface", "civitai", "link"]
    folderPath: str
    filename: Optional[str] = None
    
    huggingface: Optional[HuggingfaceModel] = None
    civitai: Optional[CivitaiModel] = None
    downloadLink: Optional[str] = None
    isTemporaryUpload: Optional[bool] = False
    s3ObjectKey: Optional[str] = None

@router.post("/volume/file/generate-upload-url")
async def generate_upload_url(
    request: Request,
    body: GenerateUploadUrlRequest,
    db: AsyncSession = Depends(get_db)
):
    """Generate a presigned URL for direct file uploads to S3"""
    try:
        s3_config = await get_s3_config(request, db)
        session_token = s3_config.session_token
        bucket = s3_config.bucket
        region = s3_config.region
        access_key = s3_config.access_key
        secret_key = s3_config.secret_key
        
        if not all([bucket, region, access_key, secret_key]):
            raise HTTPException(status_code=500, detail="S3 configuration is incomplete")
        
        user_id = request.state.current_user.get("user_id")
        org_id = request.state.current_user.get("org_id")
        
        owner_id = org_id if org_id else user_id
        
        object_key = f"temp-uploads/{owner_id}/{uuid.uuid4()}/{body.filename}"
        
        presigned_url = generate_presigned_url(
            bucket=bucket,
            object_key=object_key,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
            expiration=3600,  # 1 hour expiration
            http_method="PUT",
            content_type=body.contentType,
            public=False,  # Keep uploads private
            session_token=session_token
        )
        
        if not presigned_url:
            raise HTTPException(status_code=500, detail="Failed to generate upload URL")
        
        return GenerateUploadUrlResponse(
            uploadUrl=presigned_url,
            objectKey=object_key
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating upload URL: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/volume/model")
async def add_model(
    request: Request,
    body: AddModelRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    plan = request.state.current_user.get("plan")
    if plan == "free":
        raise HTTPException(status_code=403, detail="Free plan users cannot add models")
    
    """Unified endpoint to add models from different sources"""
    try:
        # Validate request based on source
        if body.source == "huggingface":
            if not body.huggingface or not body.huggingface.repoId:
                raise HTTPException(status_code=400, detail="HuggingFace repo ID is required")
                
            # Check if it's a repo download or file download
            if "/" in body.huggingface.repoId and not body.filename:
                # It's a repository download - use the correct upload_type
                return await add_huggingface_repo(
                    request=request,
                    body=HuggingFaceRepoRequest(
                        repo_id=body.huggingface.repoId,
                        folder_path=body.folderPath
                    ),
                    background_tasks=background_tasks,
                    db=db
                )
            else:
                # It's a file download
                url = f"https://huggingface.co/{body.huggingface.repoId}"
                if not body.filename:
                    raise HTTPException(status_code=400, detail="Filename is required for HuggingFace file downloads")
                
                return await handle_huggingface_model(
                    request=request,
                    data=AddFileInputNew(
                        url=url,
                        filename=body.filename,
                        folder_path=body.folderPath
                    ),
                    db=db,
                    background_tasks=background_tasks
                )
                
        elif body.source == "civitai":
            if not body.civitai or not body.civitai.url:
                raise HTTPException(status_code=400, detail="Civitai URL is required")
                
            return await handle_civitai_model(
                request=request,
                data=AddFileInputNew(
                    url=body.civitai.url,
                    filename=body.filename,
                    folder_path=body.folderPath
                ),
                db=db,
                background_tasks=background_tasks
            )
            
        elif body.source == "link":
            if not body.downloadLink:
                raise HTTPException(status_code=400, detail="Download link is required")
            
            url = body.downloadLink
            
            if body.isTemporaryUpload and body.s3ObjectKey:
                s3_config = await get_s3_config(request, db)
                
                bucket = s3_config.bucket
                region = s3_config.region
                access_key = s3_config.access_key
                secret_key = s3_config.secret_key
                session_token = s3_config.session_token
                if not all([bucket, region, access_key, secret_key]):
                    raise HTTPException(status_code=500, detail="S3 configuration is incomplete")
                
                url = generate_presigned_url(
                    bucket=bucket,
                    object_key=body.s3ObjectKey,
                    region=region,
                    access_key=access_key,
                    secret_key=secret_key,
                    session_token=session_token,
                    expiration=3600,
                    http_method="GET",
                    public=False
                )
                
                if not url:
                    raise HTTPException(status_code=500, detail="Failed to generate download URL for temporary upload")
                
                data = AddFileInputNew(
                    url=url,
                    filename=body.filename,
                    folder_path=body.folderPath
                )
                
                return await handle_generic_model(
                    request=request,
                    data=data,
                    db=db,
                    background_tasks=background_tasks,
                    s3_temp_object={
                        "bucket": bucket,
                        "object_key": body.s3ObjectKey
                    }
                )
            else:
                return await handle_generic_model(
                    request=request,
                    data=AddFileInputNew(
                        url=url,
                        filename=body.filename,
                        folder_path=body.folderPath
                    ),
                    db=db,
                    background_tasks=background_tasks
                )
            
        else:
            raise HTTPException(status_code=400, detail="Invalid source type")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding model: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/volume/name")
async def get_volume_name_route(request: Request, db: AsyncSession = Depends(get_db)):
    """Get the volume name for the current user"""
    try:
        volume_name = await get_volume_name(request, db)
        return {"volume_name": volume_name}
    except Exception as e:
        logger.error(f"Error getting volume name: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

class MoveFileRequest(BaseModel):
    source_path: str
    destination_path: str
    overwrite: bool = False

@router.post("/volume/move", response_model=Dict[str, str])
async def move_file(
    request: Request, 
    body: MoveFileRequest, 
    db: AsyncSession = Depends(get_db)
):
    """
    Move a file or directory from one location to another within a volume.
    
    Args:
        source_path: The path to the file or directory to be moved
        destination_path: The destination path where the file or directory will be moved to
        overwrite: Whether to overwrite existing files at the destination (default: False)
    
    Returns:
        A dictionary containing the old and new paths
    """
    source_path = body.source_path
    destination_path = body.destination_path
    volume_name = await get_volume_name(request, db)

    try:
        volume = await lookup_volume(volume_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Check source_path exists
    is_valid, error_message = await validate_file_path_aio(source_path, volume)
    if not is_valid:
        if "not found" in error_message:
            raise HTTPException(status_code=404, detail=error_message)
        else:
            raise HTTPException(status_code=400, detail=error_message)

    # Check if destination path exists and if we can overwrite it
    try:
        dst_contents = await volume.listdir.aio(destination_path)
        if dst_contents and not body.overwrite:
            raise HTTPException(
                status_code=409,
                detail="Destination already exists and overwrite is not enabled.",
            )
    except grpclib.exceptions.GRPCError as e:
        # NOT_FOUND is expected and means we can proceed
        if e.status != grpclib.Status.NOT_FOUND:
            raise HTTPException(status_code=500, detail=str(e))

    # Perform the move operation (copy + delete)
    try:
        await volume.copy_files.aio([source_path], destination_path)
        await volume.remove_file.aio(source_path, recursive=True)

        return {
            "old_path": source_path,
            "new_path": destination_path,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Error moving file: {str(e)}"
        )
