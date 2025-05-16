from typing import Optional, AsyncGenerator, Dict, Optional, List
import logging
import time
import os
from enum import Enum
from modal import Volume, Image, App, Secret
import aiohttp
import asyncio
import tempfile
from huggingface_hub import snapshot_download, HfApi, hf_hub_download
from huggingface_hub.utils import RepositoryNotFoundError, RevisionNotFoundError
from pathlib import Path

modal_downloader_app = App("volume-operations")

image = Image.debian_slim().pip_install(
    "huggingface_hub>=0.19.4",
    "tqdm>=4.66.1",
    "aiohttp>=3.8.6",
)

class ModelDownloadStatus(Enum):
    PROGRESS = "progress"
    SUCCESS = "success"
    FAILED = "failed"


def create_status_payload(
    model_id,
    progress,
    status: ModelDownloadStatus,
    error_log: str = None,
):
    payload = {
        "model_id": model_id,
        "download_progress": progress,
        "status": status.value,
    }
    if error_log:
        payload["error_log"] = error_log
    return payload


@modal_downloader_app.function(
    timeout=3600,
    secrets=[Secret.from_name("civitai-api-key")],
    image=image
)
async def modal_download_file_task(
    download_url,
    folder_path,
    filename,
    db_model_id,
    full_path,
    volume_name,
    upload_type,
    token,
):
    print("download_file_task start")

    print(f"download_url: {download_url}")
    print(f"folder_path: {folder_path}")
    print(f"filename: {filename}")
    print(f"db_model_id: {db_model_id}")
    print(f"full_path: {full_path}")
    print(f"volume_name: {volume_name}")
    print(f"upload_type: {upload_type}")
    print(f"token exists: {token is not None}")

    async def download_url_file(download_url: str, token: Optional[str]):
        # Create a shared state for progress tracking
        progress_state = {"downloaded_size": 0, "total_size": 0, "should_stop": False}

        async def report_progress():
            last_update_time = time.time()
            while not progress_state["should_stop"]:
                current_time = time.time()
                if current_time - last_update_time >= 5:
                    progress = (
                        int(
                            (
                                progress_state["downloaded_size"]
                                / progress_state["total_size"]
                            )
                            * 90
                        )
                        if progress_state["total_size"]
                        else 0
                    )
                    logging.info(
                        f"Download progress: {progress}% ({progress_state['downloaded_size']}/{progress_state['total_size']} bytes)"
                    )
                    progress_state["last_status"] = create_status_payload(
                        db_model_id,
                        progress,
                        ModelDownloadStatus.PROGRESS,
                    )
                    last_update_time = current_time
                await asyncio.sleep(1)  # Check progress every second

        try:
            headers = {
                "Accept-Encoding": "identity",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3600)
            ) as session:
                async with session.get(download_url, headers=headers) as response:
                    response.raise_for_status()
                    progress_state["total_size"] = int(
                        response.headers.get("Content-Length", 0)
                    )
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)

                    # Start progress reporting task
                    progress_task = asyncio.create_task(report_progress())

                    with open(full_path, "wb") as file:
                        async for data in response.content.iter_chunked(65536):
                            if data:
                                file.write(data)
                                progress_state["downloaded_size"] += len(data)
                                if "last_status" in progress_state:
                                    yield progress_state["last_status"]
                                    del progress_state["last_status"]

                    # Clean up progress reporting
                    progress_state["should_stop"] = True
                    await progress_task

        except Exception as e:
            progress_state["should_stop"] = True  # Ensure progress reporting stops
            yield create_status_payload(
                db_model_id, 0, ModelDownloadStatus.FAILED, str(e)
            )
            raise e

    try:
        downloaded_path = None
        if upload_type == "huggingface":
            async for event in download_url_file(download_url, token):
                yield event
            downloaded_path = full_path
        elif upload_type == "download-url":
            async for event in download_url_file(download_url, None):
                yield event
            downloaded_path = full_path
        elif upload_type == "civitai":
            if "civitai.com" in download_url:
                download_url += f"{'&' if '?' in download_url else '?'}token={os.environ['CIVITAI_KEY']}"
            async for event in download_url_file(download_url, None):
                yield event
            downloaded_path = full_path
        else:
            raise ValueError(f"Unsupported upload_type: {upload_type}")

        volume = Volume.lookup(volume_name, create_if_missing=True)

        with volume.batch_upload() as batch:
            batch.put_file(downloaded_path, full_path)

        yield create_status_payload(db_model_id, 100, ModelDownloadStatus.SUCCESS)
    except Exception as e:
        print(f"Error in download_file_task: {str(e)}")
        yield create_status_payload(db_model_id, 0, ModelDownloadStatus.FAILED, str(e))
        raise e

async def report_progress(
    progress: float, 
    model_id: str, 
    status: str = "progress", 
    error_log: Optional[str] = None
) -> Dict:
    """Generate progress report events"""
    return {
        "status": status,
        "download_progress": int(progress),
        "model_id": model_id,
        "error_log": error_log,
    }

@modal_downloader_app.function(timeout=3600, image=image)
async def modal_download_repo_task(
    repo_id: str,
    folder_path: str,
    model_id: str,
    volume_name: str,
    token: Optional[str] = None,
    use_repo_subfolder: bool = True,
) -> AsyncGenerator[Dict, None]:
    """
    Download an entire HuggingFace repository to a Modal volume.
    
    Args:
        repo_id: HuggingFace repository ID (e.g., "Skywork/SkyReels-A1")
        folder_path: Path within the volume to store the repository
        model_id: Database ID of the model record
        volume_name: Name of the Modal volume
        token: HuggingFace API token (optional)
    
    Yields:
        Progress updates as dictionaries
    """
    print(f"Starting download of repo {repo_id} to {folder_path}")
    try:
        # Use user token if provided, otherwise fall back to environment variable
        if not token:
            token = os.environ.get("HUGGINGFACE_TOKEN")
            print("Using environment HUGGINGFACE_TOKEN")
        else:
            print("Using provided token")
        
        # Get the Modal volume
        print(f"Getting volume {volume_name}")
        volume = Volume.from_name(volume_name)
        
        # Create a temporary directory for downloading
        with tempfile.TemporaryDirectory() as temp_dir:
            print(f"Created temp directory at {temp_dir}")
            # Report initial progress
            yield await report_progress(10, model_id)
            
            try:
                # Download the repository
                print(f"Starting repository download to {temp_dir}")
                local_dir = snapshot_download(
                    repo_id=repo_id,
                    local_dir=temp_dir,
                    token=token,
                    local_dir_use_symlinks=False,
                )
                print(f"Repository downloaded to {local_dir}")
                
                # Get list of files to count them for progress reporting
                files = list(Path(local_dir).glob("**/*"))
                total_files = len([f for f in files if f.is_file()])
                print(f"Found {total_files} files in repository")
                
                if total_files == 0:
                    print("Repository is empty, aborting")
                    yield await report_progress(
                        0, 
                        model_id, 
                        status="failed", 
                        error_log="Repository is empty"
                    )
                    return
                
                # Create destination directory in volume
                if use_repo_subfolder:
                    dest_dir = os.path.join(folder_path, repo_id.split("/")[-1])
                else:
                    dest_dir = folder_path
                print(f"Will upload to destination directory: {dest_dir}")
                
                yield await report_progress(50, model_id)
                
                print("Starting batch upload to volume")
                with volume.batch_upload() as batch:
                    batch.put_directory(local_dir, dest_dir)
                print("Batch upload completed")
                
                yield await report_progress(90, model_id)

                print("Download and upload successful")
                yield await report_progress(100, model_id, status="success")
                
            except RepositoryNotFoundError:
                print(f"Repository {repo_id} not found")
                yield await report_progress(
                    0, 
                    model_id, 
                    status="failed", 
                    error_log=f"Repository {repo_id} not found"
                )
            except RevisionNotFoundError:
                print(f"Revision not found for repository {repo_id}")
                yield await report_progress(
                    0, 
                    model_id, 
                    status="failed", 
                    error_log=f"Revision not found for repository {repo_id}"
                )
            except Exception as e:
                print(f"Error downloading repository: {str(e)}")
                yield await report_progress(
                    0, 
                    model_id, 
                    status="failed", 
                    error_log=f"Error downloading repository: {str(e)}"
                )
    
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        yield await report_progress(
            0, 
            model_id, 
            status="failed", 
            error_log=f"Unexpected error: {str(e)}"
        )
