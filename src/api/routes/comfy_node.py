from fastapi import HTTPException, APIRouter, Request
from typing import Any, Dict, Optional, List
import logging
import os
import httpx
from .utils import async_lru_cache
from datetime import timedelta
from fastapi.responses import JSONResponse, FileResponse
import modal
import json
from api.utils.multi_level_cache import multi_level_cached
from pydantic import BaseModel


logger = logging.getLogger(__name__)

router = APIRouter(tags=["Comfy Node"])


import asyncio
from urllib.parse import urlparse


async def extract_repo_name(repo_url: str) -> str:
    cleaned_url = repo_url.strip("'\"")
    url = urlparse(cleaned_url)
    path_parts = url.path.split("/")
    path_parts = [p for p in path_parts if p]
    if len(path_parts) < 2:
        raise HTTPException(status_code=400, detail="Invalid git repository URL")
    repo_name = path_parts[1].replace(".git", "")
    author = path_parts[0]
    return f"{author}/{repo_name}"


async def fetch_github_data(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

@async_lru_cache(expire_after=timedelta(hours=1))
async def _get_branch_info(git_url: str) -> Dict[str, Any] | None:
    try:
        repo_name = await extract_repo_name(git_url)
        headers = {
            "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN')}",
            "User-Agent": "request",
        }

        repo_url = f"https://api.github.com/repos/{repo_name}"
        repo_data = await fetch_github_data(repo_url, headers)

        branch = repo_data.get("default_branch")
        print(f"Branch: {branch}")
        if not branch:
            return None

        branch_url = f"{repo_url}/branches/{branch}"
        branch_info = await fetch_github_data(branch_url, headers)

        branch_info["stargazers_count"] = repo_data.get("stargazers_count", 0)

        return branch_info
    except httpx.HTTPStatusError as e:
        logger.error(f"GitHub API error: {e.response.status_code} {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Error fetching repo info: {str(e)}")
        return None


@router.get("/branch-info", response_model=Dict[str, Any])
async def get_branch_info(request: Request, git_url: str):
    if not git_url:
        raise HTTPException(status_code=400, detail="Git URL is required")
    branch_info = await _get_branch_info(git_url)
    if branch_info is None:
        raise HTTPException(status_code=404, detail="Branch information not found")
    return JSONResponse(content=branch_info)

@async_lru_cache(expire_after=timedelta(hours=1))
async def _get_releases(git_url: str) -> list[Dict[str, Any]] | None:
    try:
        repo_name = await extract_repo_name(git_url)
        headers = {
            "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN')}",
            "User-Agent": "request",
        }

        releases_url = f"https://api.github.com/repos/{repo_name}/releases"
        releases = await fetch_github_data(releases_url, headers)
        return releases
    except httpx.HTTPStatusError as e:
        logger.error(f"GitHub API error: {e.response.status_code} {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Error fetching releases: {str(e)}")
        return None

async def _get_commit_sha_for_tag_with_headers(repo_name: str, tag: str) -> str | None:
    headers = {
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN')}",
        "User-Agent": "request",
    }
    try:
        # Get the commit SHA for a specific tag
        tag_url = f"https://api.github.com/repos/{repo_name}/git/refs/tags/{tag}"
        tag_data = await fetch_github_data(tag_url, headers)
        return tag_data["object"]["sha"]
    except Exception as e:
        logger.error(f"Error fetching commit SHA for tag {tag}: {str(e)}")
        return None

@multi_level_cached(
    key_prefix="comfyui_versions",
    ttl_seconds=300,  # Cache in local memory for 5 minutes
    redis_ttl_seconds=1800,  # Cache in Redis for 30 minutes
    version="1.0",
    key_builder=lambda: "comfyui_versions"  # Simple static key since no parameters
)
async def get_comfyui_versions_cached():
    """Get ComfyUI versions with multi-level caching."""
    git_url = "https://github.com/comfyanonymous/ComfyUI"
    repo_name = await extract_repo_name(git_url)
    
    # Get branch info and releases
    branch_info, releases = await asyncio.gather(
        _get_branch_info(git_url),
        _get_releases(git_url)
    )
    
    if branch_info is None:
        raise HTTPException(status_code=404, detail="Branch information not found")
    
    # Take only the 3 most recent releases
    recent_releases = releases[:3] if releases else []
    
    # Fetch commit SHAs for recent releases concurrently
    if recent_releases:
        sha_tasks = [
            _get_commit_sha_for_tag_with_headers(repo_name, release["tag_name"])
            for release in recent_releases
        ]
        commit_shas = await asyncio.gather(*sha_tasks)
    else:
        commit_shas = []
        
    response = {
        "latest": {
            "sha": branch_info["commit"]["sha"],
            "label": "Latest"
        },
        "releases": [
            {
                "label": release["tag_name"],
                "value": sha or release["target_commitish"],
                "description": release["name"],
                "date": release["published_at"]
            }
            for release, sha in zip(recent_releases, commit_shas) if sha
        ]
    }
    
    return response

@multi_level_cached(key_prefix="comfyui_deploy_hash", ttl_seconds=3600, redis_ttl_seconds=21600)
async def get_latest_comfydeploy_hash():
    """Fetch the latest commit hash from ComfyUI-Deploy repository"""
    git_url = "https://github.com/bennykok/comfyui-deploy"
    branch_info = await _get_branch_info(git_url)
    return branch_info["commit"]["sha"] if branch_info else None

@router.get("/latest-hashes", response_model=Dict[str, Any])
async def get_latest_hashes():
    """Return latest hashes and release info for ComfyUI and ComfyUI-Deploy"""
    comfyui_versions = await get_comfyui_versions_cached()
    comfydeploy_hash = await get_latest_comfydeploy_hash()
    
    # Get latest release info if available
    latest_release = None
    comfyui_hash = comfyui_versions["latest"]["sha"]  # Default to main branch
    
    if comfyui_versions.get("releases") and len(comfyui_versions["releases"]) > 0:
        latest_release = comfyui_versions["releases"][0]
        # Use the latest release commit SHA as the main hash
        comfyui_hash = latest_release["value"]
    
    return JSONResponse(content={
        "comfyui_hash": comfyui_hash,
        "comfyui_latest_release": latest_release,
        "comfydeploy_hash": comfydeploy_hash
    })

@router.get("/comfyui-versions", response_model=Dict[str, Any])
async def get_comfyui_versions(request: Request):
    """API endpoint to get ComfyUI versions."""
    result = await get_comfyui_versions_cached()
    return JSONResponse(content=result)

async def fetch_comfy_node_metadata(node_id: str, version: str = None) -> Dict[str, Any]:
    """Fetch node metadata from ComfyUI API"""
    if version:
        url = f"https://api.comfy.org/nodes/{node_id}/install?version={version}"
    else:
        url = f"https://api.comfy.org/nodes/{node_id}/install"
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        if response.status_code == 404:
            raise ValueError(f"Node {node_id} version {version} not found")
        response.raise_for_status()
        return response.json()

@router.get("/custom-node-list")
async def get_nodes_json():
    try:
        # Get the data from modal function
        function = modal.Function.from_name("comfy-nodes", "read_custom_nodes")
        nodes_data = await function.remote.aio()

        # Create a temporary file to serve
        temp_file_path = "/tmp/nodes_cache.json"
        with open(temp_file_path, "w") as f:
            json.dump(nodes_data, f)

        headers = {
            "Cache-Control": "public, max-age=86400",
            "ETag": f"\"{hash(json.dumps(nodes_data))}\"",
        }

        return FileResponse(
            path=temp_file_path,
            media_type="application/json",
            filename="nodes.json",
            headers=headers
        )
    except Exception as e:
        logger.error(f"Error fetching nodes.json: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve nodes.json: {str(e)}")


# Models for snapshot to docker conversion
class GitCustomNode(BaseModel):
    hash: str
    disabled: bool

class SnapshotInput(BaseModel):
    comfyui: Optional[str] = None
    git_custom_nodes: Dict[str, GitCustomNode]
    cnr_custom_nodes: Dict[str, str]

class DockerCommandStep(BaseModel):
    id: str
    data: Dict[str, Any]
    type: str

class DockerCommandResponse(BaseModel):
    steps: List[DockerCommandStep]


@async_lru_cache(expire_after=timedelta(hours=1))
async def fetch_commit_metadata(git_url: str, commit_hash: str) -> Dict[str, Any]:
    """Fetch metadata for a specific commit from GitHub with caching"""
    try:
        repo_name = await extract_repo_name(git_url)
        headers = {
            "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN')}",
            "User-Agent": "request",
        }
        
        # Get commit details
        commit_url = f"https://api.github.com/repos/{repo_name}/commits/{commit_hash}"
        commit_data = await fetch_github_data(commit_url, headers)
        
        # Get repository info for additional metadata
        repo_url = f"https://api.github.com/repos/{repo_name}"
        repo_data = await fetch_github_data(repo_url, headers)
        
        # Get latest commit hash from default branch
        branch_info = await _get_branch_info(git_url)
        latest_hash = branch_info["commit"]["sha"] if branch_info else commit_hash
        
        return {
            "message": commit_data["commit"]["message"],
            "committer": {
                "date": commit_data["commit"]["committer"]["date"],
                "name": commit_data["commit"]["committer"]["name"],
                "email": commit_data["commit"]["committer"]["email"]
            },
            "commit_url": commit_data["html_url"],
            "latest_hash": latest_hash,
            "stargazers_count": repo_data.get("stargazers_count", 0),
            "repo_name": repo_data.get("name", ""),  # Add repo name from GitHub
            "repo_full_name": repo_data.get("full_name", "")  # Add full name (owner/repo)
        }
    except Exception as e:
        logger.error(f"Error fetching commit metadata for {git_url}@{commit_hash}: {str(e)}")
        return {
            "message": "Unable to fetch commit message",
            "committer": {
                "date": None,
                "name": None,
                "email": None
            },
            "commit_url": None,
            "latest_hash": commit_hash,
            "stargazers_count": 0,
            "repo_name": "",
            "repo_full_name": ""
        }


@router.post("/snapshot-to-docker", response_model=DockerCommandResponse)
async def convert_snapshot_to_docker(snapshot: SnapshotInput):
    """
    Convert a snapshot JSON to docker command format.
    
    The snapshot contains:
    - comfyui: The ComfyUI commit hash
    - git_custom_nodes: Git repositories with their commit hashes
    - cnr_custom_nodes: ComfyUI Node Registry nodes with versions
    
    Returns docker command steps for installing these components.
    """
    steps = []
    
    # Process git custom nodes
    if snapshot.git_custom_nodes:
        # Prepare all git nodes for concurrent processing
        git_tasks = []
        git_nodes_info = []
        
        for git_url, node_info in snapshot.git_custom_nodes.items():
            if not node_info.disabled:
                git_tasks.append(fetch_commit_metadata(git_url, node_info.hash))
                git_nodes_info.append((git_url, node_info.hash))
        
        # Fetch all metadata concurrently
        if git_tasks:
            metadata_results = await asyncio.gather(*git_tasks)
            
            # Create steps from results
            for (git_url, commit_hash), metadata in zip(git_nodes_info, metadata_results):
                # Generate a unique ID from the repo full name (author/repo) in lowercase
                # Use repo_full_name from metadata if available, otherwise extract from URL
                if metadata.get("repo_full_name"):
                    node_id = metadata["repo_full_name"].lower().replace("/", "-")
                else:
                    # Fallback: extract from URL
                    repo_name = await extract_repo_name(git_url)
                    node_id = repo_name.lower().replace("/", "-")
                
                # Use repo_name from GitHub API for the display name
                display_name = metadata.get("repo_name", "")
                if not display_name:
                    # Fallback: extract from URL
                    display_name = git_url.split("/")[-1].replace(".git", "")
                
                step = DockerCommandStep(
                    id=node_id,
                    data={
                        "url": git_url,
                        "hash": commit_hash,
                        "meta": metadata,
                        "name": display_name,
                        "files": [git_url],
                        "install_type": "git-clone"
                    },
                    type="custom-node"
                )
                steps.append(step)
    
    # Process CNR (ComfyUI Node Registry) custom nodes
    if snapshot.cnr_custom_nodes:
        for node_id, version in snapshot.cnr_custom_nodes.items():
            step = DockerCommandStep(
                id=node_id,
                data={
                    "node_id": node_id,
                    "version": version
                },
                type="custom-node-manager"
            )
            steps.append(step)
    
    return DockerCommandResponse(steps=steps)

