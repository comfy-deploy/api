import asyncio
from datetime import timedelta
import datetime
import hashlib
import json
import aiohttp
import os
import httpx
from .comfy_node import fetch_github_data, extract_repo_name, fetch_comfy_node_metadata

# from http.client import HTTPException
from fastapi import Request, HTTPException, Depends
import os
from uuid import UUID
import uuid

from api.routes.log import redis

from api.modal.builder import (
    BuildMachineItem,
    GPUType,
    KeepWarmBody,
    build_logic,
    set_machine_always_on,
)
from api.routes.volumes import retrieve_model_volumes
from api.utils.docker import (
    DepsBody,
    DockerCommandResponse,
    generate_all_docker_commands,
    comfyui_hash,
    comfydeploy_hash,
)
from pydantic import BaseModel, constr, Field
from .types import (
    GPUEventModel,
    MachineGPU,
    MachineModel,
    MachineType,
)
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .utils import generate_persistent_token, generate_machine_token, select, SecretManager
from sqlalchemy import func, text
from fastapi.responses import JSONResponse

from api.models import (
    Deployment,
    GPUEvent,
    Machine,
    Workflow,
    MachineVersion,
    get_machine_columns,
    Secret,
    MachineSecret
)

# from sqlalchemy import select
from api.database import get_db, get_db_context
import logging
from typing import Any, Dict, List, Optional, Literal

# from fastapi_pagination import Page, add_pagination, paginate
from api.utils.multi_level_cache import multi_level_cached
import modal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Machine"])
public_router = APIRouter(tags=["Machine"])


@router.get("/machines", response_model=List[MachineModel])
async def get_machines(
    request: Request,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    is_deleted: Optional[bool] = None,
    include_has_workflows: bool = False,
    is_docker: bool = False,
    is_workspace: bool = False,
    is_self_hosted: bool = False,
    include_docker_command_steps: bool = False,
    db: AsyncSession = Depends(get_db),
):
    # Build the SQL query
    params = {}

    # Base query with organization check
    sql = """
    SELECT m.id,
        m.user_id,
        m.name,
        m.created_at,
        m.updated_at,
        m.type,
        m.org_id,
        m.status,
        m.gpu,
        m.machine_version,
        m.machine_builder_version,
        m.comfyui_version,
        m.import_failed_logs,
        m.machine_version_id,
        m.is_workspace,
    """

    if include_docker_command_steps:
        sql += """
        m.docker_command_steps,
        """

    # Add workflow check if needed
    if include_has_workflows:
        sql += """
        (SELECT COUNT(w.id) > 0 FROM "comfyui_deploy"."workflows" w WHERE w.selected_machine_id = m.id) AS has_workflows
        """
    else:
        sql += "FALSE AS has_workflows"

    sql += """
    FROM "comfyui_deploy"."machines" m
    WHERE m.is_workspace = :is_workspace
    """
    params["is_workspace"] = is_workspace

    # Add organization check
    org_id = request.state.current_user.get("org_id")
    user_id = request.state.current_user.get("user_id")

    if org_id:
        sql += " AND m.org_id = :org_id"
        params["org_id"] = org_id
    else:
        sql += " AND m.user_id = :user_id AND m.org_id IS NULL"
        params["user_id"] = user_id

    # Add filters
    if is_deleted is not None:
        sql += " AND m.deleted = :is_deleted"
        params["is_deleted"] = is_deleted

    if is_self_hosted:
        sql += " AND (m.type = 'classic' OR m.type = 'runpod-serverless')"
    elif is_docker:
        sql += " AND m.type = 'comfy-deploy-serverless'"

    if search:
        sql += " AND m.name ILIKE :search"
        params["search"] = f"%{search}%"

    # Add ordering and pagination
    sql += """
    ORDER BY m.updated_at DESC
    LIMIT :limit OFFSET :offset
    """
    params["limit"] = limit
    params["offset"] = offset

    # Execute the raw query
    result = await db.execute(text(sql), params)
    rows = result.mappings().all()

    if not rows:
        return []

    # Convert to dict
    machines_data = []
    for row in rows:
        machine_dict = {}
        for k, v in row.items():
            if k != "has_workflows":
                # Handle various non-serializable types
                if isinstance(v, UUID):
                    machine_dict[k] = str(v)
                elif isinstance(v, datetime.datetime):
                    machine_dict[k] = v.isoformat()
                else:
                    machine_dict[k] = v

        if include_has_workflows:
            machine_dict["has_workflows"] = row["has_workflows"]
        machines_data.append(machine_dict)

    return JSONResponse(content=machines_data)


@router.get("/machines/all", response_model=List[MachineModel])
async def get_all_machines(
    request: Request,
    search: Optional[str] = None,
    limit: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    params = {}

    sql = """
    SELECT m.id,
        m.user_id,
        m.name,
        m.created_at,
        m.updated_at,
        m.type,
        m.org_id,
        m.status,
        m.gpu,
        m.machine_version,
        m.machine_builder_version,
        m.comfyui_version,
        m.import_failed_logs,
        m.machine_version_id,
        m.is_workspace
    """

    sql += """
    FROM "comfyui_deploy"."machines" m
    WHERE m.deleted = FALSE
    """

    org_id = request.state.current_user.get("org_id")
    user_id = request.state.current_user.get("user_id")

    if org_id:
        sql += " AND m.org_id = :org_id"
        params["org_id"] = org_id
    else:
        sql += " AND m.user_id = :user_id AND m.org_id IS NULL"
        params["user_id"] = user_id

    if search:
        sql += " AND m.name ILIKE :search"
        params["search"] = f"%{search}%"

    sql += " ORDER BY m.updated_at DESC"

    if limit:
        sql += " LIMIT :limit"
        params["limit"] = limit

    # execute the query
    result = await db.execute(text(sql), params)
    rows = result.mappings().all()

    # Convert to dict and handle UUID/datetime serialization
    machines_data = []
    for row in rows:
        machine_dict = {}
        for k, v in row.items():
            # Handle various non-serializable types
            if isinstance(v, UUID):
                machine_dict[k] = str(v)
            elif isinstance(v, datetime.datetime):
                machine_dict[k] = v.isoformat()
            else:
                machine_dict[k] = v
        machines_data.append(machine_dict)

    return JSONResponse(content=machines_data)

@router.get("/machine/{machine_id}", response_model=MachineModel)
async def get_machine(
    request: Request,
    machine_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    params = {"machine_id": machine_id}

    # Base query
    sql = """
    SELECT m.id,
        m.user_id,
        m.name,
        m.org_id,
        m.created_at,
        m.updated_at,
        m.type,
        m.machine_version,
        m.deleted,
        m.import_failed_logs,
        m.machine_version_id,
        m.status,
        m.gpu,
        m.machine_builder_version,
        m.comfyui_version,
        m.is_workspace,
        m.endpoint,
        -- auto scaling
        m.concurrency_limit,
        m.run_timeout,
        m.idle_timeout,
        m.docker_command_steps,
        m.keep_warm,
        -- advanced
        m.allow_concurrent_inputs,
        m.base_docker_image,
        m.python_version,
        m.extra_args,
        m.prestart_command,
        m.install_custom_node_with_gpu,
        m.optimized_runner,
        m.disable_metadata,
        m.cpu_request,
        m.cpu_limit,
        m.memory_request,
        m.memory_limit,
        m.models_to_cache,
        m.enable_gpu_memory_snapshot
    FROM "comfyui_deploy"."machines" m
    WHERE m.id = :machine_id
    AND m.deleted = FALSE
    """

    # Add organization check
    org_id = request.state.current_user.get("org_id")
    user_id = request.state.current_user.get("user_id")

    if org_id:
        sql += " AND m.org_id = :org_id"
        params["org_id"] = org_id
    else:
        sql += " AND m.user_id = :user_id AND m.org_id IS NULL"
        params["user_id"] = user_id

    # Execute the raw query
    result = await db.execute(text(sql), params)
    row = result.mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Machine not found")

    # Convert to dict and handle UUID/datetime serialization
    machine_dict = {}
    for k, v in row.items():
        # Handle various non-serializable types
        if isinstance(v, UUID):
            machine_dict[k] = str(v)
        elif isinstance(v, datetime.datetime):
            machine_dict[k] = v.isoformat()
        else:
            machine_dict[k] = v

    return JSONResponse(content=machine_dict)


@router.get("/machine/{machine_id}/events", response_model=List[GPUEventModel])
async def get_machine_events(
    request: Request,
    machine_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    # Calculate the timestamp for 24 hours ago
    twenty_four_hours_ago = func.now() - timedelta(hours=24)

    events = await db.execute(
        select(GPUEvent)
        .where(GPUEvent.machine_id == machine_id)
        .where(GPUEvent.start_time >= twenty_four_hours_ago)
        .order_by(GPUEvent.start_time.desc())
        .apply_org_check(request)
    )
    events = events.scalars().all()
    return JSONResponse(content=[event.to_dict() for event in events])


@router.get("/machine/{machine_id}/workflows")
async def get_machine_workflows(
    request: Request,
    machine_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    workflows = await db.execute(
        select(Workflow)
        .where(Workflow.selected_machine_id == machine_id)
        .apply_org_check(request)
    )
    return JSONResponse(
        content=[workflow.to_dict() for workflow in workflows.scalars().all()]
    )


class DockerCommand(BaseModel):
    when: Literal["before", "after"]
    commands: List[str]


GitCommitHash = constr(pattern=r"^[a-fA-F0-9]{40}$")


class ServerlessMachineModel(BaseModel):
    name: str
    comfyui_version: Optional[GitCommitHash] = comfyui_hash
    gpu: MachineGPU
    docker_command_steps: Optional[Dict[str, Any]] = {"steps": []}
    allow_concurrent_inputs: int = 1
    concurrency_limit: int = 2
    install_custom_node_with_gpu: bool = False
    # ws_timeout: int = 2
    run_timeout: int = 300
    idle_timeout: int = 60
    extra_docker_commands: Optional[List[DockerCommand]] = None
    machine_builder_version: Optional[str] = "4"
    base_docker_image: Optional[str] = None
    python_version: Optional[str] = None
    extra_args: Optional[str] = None
    prestart_command: Optional[str] = None
    keep_warm: Optional[int] = 0
    wait_for_build: Optional[bool] = False
    optimized_runner: Optional[bool] = None
    disable_metadata: Optional[bool] = None
    cpu_request: Optional[float] = None
    cpu_limit: Optional[float] = None
    memory_request: Optional[int] = None
    memory_limit: Optional[int] = None
    models_to_cache: Optional[List[str]] = Field(default_factory=list)
    enable_gpu_memory_snapshot: Optional[bool] = None


class UpdateServerlessMachineModel(BaseModel):
    name: Optional[str] = None
    comfyui_version: Optional[GitCommitHash] = None
    gpu: Optional[MachineGPU] = None
    docker_command_steps: Optional[Dict[str, Any]] = None
    allow_concurrent_inputs: Optional[int] = None
    concurrency_limit: Optional[int] = None
    install_custom_node_with_gpu: Optional[bool] = None
    run_timeout: Optional[int] = None
    idle_timeout: Optional[int] = None
    extra_docker_commands: Optional[List[DockerCommand]] = None
    machine_builder_version: Optional[str] = None
    base_docker_image: Optional[str] = None
    python_version: Optional[str] = None
    extra_args: Optional[str] = None
    prestart_command: Optional[str] = None
    keep_warm: Optional[int] = None
    is_trigger_rebuild: Optional[bool] = False
    optimized_runner: Optional[bool] = None
    disable_metadata: Optional[bool] = None
    cpu_request: Optional[float] = None
    cpu_limit: Optional[float] = None
    memory_request: Optional[int] = None
    memory_limit: Optional[int] = None
    models_to_cache: Optional[List[str]] = None
    enable_gpu_memory_snapshot: Optional[bool] = None


current_endpoint = os.getenv("CURRENT_API_URL")


async def create_machine_version(
    db: AsyncSession,
    machine: Machine,
    user_id: str,
    version: int = 1,
    current_version_data: MachineVersion = None,
) -> MachineVersion:
    # if the machine builder version is not 4, pass this function
    if machine.machine_builder_version != "4":
        return

    new_version_id = uuid.uuid4()

    # Create new version without transaction (caller manages transaction)
    machine_version = MachineVersion(
        id=new_version_id,
        machine_id=machine.id,
        version=version,
        user_id=user_id,
        created_at=func.now(),
        updated_at=func.now(),
        **{col: getattr(machine, col) for col in get_machine_columns().keys()},
        modal_image_id=current_version_data.modal_image_id
        if current_version_data
        else None,
    )
    db.add(machine_version)
    await db.flush()

    # Update machine with new version
    machine.machine_version_id = machine_version.id
    machine.updated_at = func.now()

    return machine_version


def hash_machine_dependencies(docker_commands: DockerCommandResponse):
    return hashlib.sha256(json.dumps(docker_commands.model_dump()).encode()).hexdigest()


async def validate_free_plan_restrictions(
    request: Request,
    machine_data: dict,
    db: AsyncSession,
    is_update: bool = False,
    existing_machine_id: UUID = None,
) -> None:
    """
    Validates restrictions for free plan users.
    Raises HTTPException if restrictions are violated.
    """
    plan = request.state.current_user.get("plan")
    if plan != "free":
        return

    # Check machine count limit (only for creation)
    if not is_update:
        max_machine_count = 1
        machine_count = (
            await db.execute(
                select(func.count())
                .select_from(Machine)
                .where(~Machine.deleted)
                .apply_org_check_by_type(Machine, request)
            )
        ).scalar()

        if machine_count >= max_machine_count:
            raise HTTPException(
                status_code=400, 
                detail="Free plan is limited to 1 machine. Please upgrade to create more machines."
            )

    # Check restricted fields
    restricted_fields = [
        "extra_docker_commands",
        "base_docker_image",
        "prestart_command",
        "extra_args",
        "install_custom_node_with_gpu",
    ]

    for field in restricted_fields:
        if field in machine_data and machine_data[field]:
            raise HTTPException(
                status_code=403,
                detail=f"Free plan users cannot use {field}. Please upgrade to use this feature.",
            )

    # Check docker_command_steps - only allow ComfyUI Deploy node
    if (
        "docker_command_steps" in machine_data
        and machine_data["docker_command_steps"] is not None
    ):
        steps = machine_data["docker_command_steps"].get("steps", [])
        
        # Count ComfyUI Deploy nodes and other nodes
        comfydeploy_count = 0
        other_nodes_count = 0
        
        for step in steps:
            step_type = step.get("type")
            
            # Check for custom commands
            if step_type == "commands":
                raise HTTPException(
                    status_code=403,
                    detail="Free plan users cannot include custom commands. Please upgrade to use this feature.",
                )
            
            # Check for custom nodes
            if step_type == "custom-node":
                data = step.get("data", {})
                url = data.get("url", "").lower()
                
                # Check if it's the ComfyUI Deploy node
                is_comfydeploy = any(allowed_url in url for allowed_url in [
                    "github.com/bennykok/comfyui-deploy",
                    "github.com/bennykok/comfyui-deploy.git",
                    "git@github.com:bennykok/comfyui-deploy"
                ])
                
                if is_comfydeploy:
                    comfydeploy_count += 1
                else:
                    other_nodes_count += 1
                    node_name = data.get("name", "Unknown")
                    raise HTTPException(
                        status_code=403,
                        detail=f"Free plan users can only use the ComfyUI Deploy custom node. '{node_name}' is not allowed. Please upgrade to use additional custom nodes.",
                    )
            
            # Check for custom node manager
            if step_type == "custom-node-manager":
                data = step.get("data", {})
                node_id = data.get("node_id", "")
                
                # Only allow comfyui-deploy through the manager
                if node_id == "comfyui-deploy":
                    comfydeploy_count += 1
                else:
                    other_nodes_count += 1
                    raise HTTPException(
                        status_code=403,
                        detail=f"Free plan users can only use the ComfyUI Deploy custom node. Please upgrade to use additional custom nodes.",
                    )
        
        # Ensure at least one ComfyUI Deploy node exists (for updates)
        if other_nodes_count == 0 and comfydeploy_count == 0 and len(steps) > 0:
            # This shouldn't happen, but just in case
            pass


@router.post("/machine/serverless")
async def create_serverless_machine(
    request: Request,
    machine: ServerlessMachineModel,
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
) -> MachineModel:
    # Validate free plan restrictions
    await validate_free_plan_restrictions(
        request=request, machine_data=machine.model_dump(), db=db
    )

    wait_for_build = machine.wait_for_build

    new_machine_id = uuid.uuid4()
    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user["org_id"] if "org_id" in current_user else None

    docker_commands = await generate_all_docker_commands(machine)
    docker_commands_hash = hash_machine_dependencies(docker_commands)

    # async with db.begin():  # Single transaction for entire operation
    # Create initial machine
    machine_data = machine.model_dump(exclude={"wait_for_build"})
    machine = Machine(
        **machine_data,
        id=new_machine_id,
        type=MachineType.COMFY_DEPLOY_SERVERLESS,
        user_id=user_id,
        org_id=org_id,
        status="building",
        endpoint="not-ready",
        created_at=func.now(),
        updated_at=func.now(),
        machine_hash=docker_commands_hash,
    )
    db.add(machine)
    await db.flush()
    # Create initial version (uses same transaction)
    await create_machine_version(db, machine, user_id)
    await db.commit()
    # Transaction automatically commits here if successful
    await db.refresh(machine)  # Only one refresh at the end

    volumes = await retrieve_model_volumes(request, db)
    # docker_commands = generate_all_docker_commands(machine)
    machine_token = generate_machine_token(user_id, org_id)

    secrets = await get_machine_secrets(db=db, machine_id=machine.id)

    if machine.machine_hash is not None:
        existing_machine_info_url = f"https://comfyui.comfydeploy.com/static-assets/{machine.machine_hash}/object_info.json"
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.head(existing_machine_info_url) as response:
                    skip_static_assets = response.status == 200
        except Exception as e:
            logger.warning(f"Error checking static assets: {e}")
            skip_static_assets = False
    else:
        skip_static_assets = False

    params = BuildMachineItem(
        machine_id=str(machine.id),
        name=str(machine.id),
        cd_callback_url=f"{current_endpoint}/api/machine-built",
        callback_url=f"{current_endpoint}/api",
        gpu_event_callback_url=f"{current_endpoint}/api/gpu_event",
        models=machine.models,
        gpu=machine.gpu,
        model_volume_name=volumes[0]["volume_name"],
        run_timeout=machine.run_timeout,
        idle_timeout=machine.idle_timeout,
        auth_token=machine_token,
        ws_timeout=machine.ws_timeout,
        concurrency_limit=machine.concurrency_limit,
        allow_concurrent_inputs=machine.allow_concurrent_inputs,
        legacy_mode=machine.legacy_mode,
        install_custom_node_with_gpu=machine.install_custom_node_with_gpu,
        allow_background_volume_commits=machine.allow_background_volume_commits,
        retrieve_static_assets=machine.retrieve_static_assets,
        # skip_static_assets=skip_static_assets,
        skip_static_assets=True,
        docker_commands=docker_commands.model_dump()["docker_commands"],
        machine_builder_version=machine.machine_builder_version,
        base_docker_image=machine.base_docker_image,
        python_version=machine.python_version,
        prestart_command=machine.prestart_command,
        extra_args=machine.extra_args,
        machine_version_id=str(machine.machine_version_id),
        machine_hash=docker_commands_hash,
        disable_metadata=machine.disable_metadata,
        secrets=secrets,  # Add the decrypted secrets to the build parameters
        cpu_request=machine.cpu_request,
        cpu_limit=machine.cpu_limit,
        memory_request=machine.memory_request,
        memory_limit=machine.memory_limit,
        models_to_cache=machine.models_to_cache,
        enable_gpu_memory_snapshot=machine.enable_gpu_memory_snapshot
    )

    if wait_for_build:
        await build_logic(params)
    else:
        background_tasks.add_task(build_logic, params)

    return JSONResponse(content=machine.to_dict())

class SecretKeyValue(BaseModel):
    key: constr(pattern=r"^[^\s]+$")
    value: str

class SecretInput(BaseModel):
    secret: List[SecretKeyValue]
    secret_name: str
    machine_id: UUID

@router.post("/machine/secret")
async def create_secret(
    request: Request,
    request_body: SecretInput,
    db: AsyncSession = Depends(get_db)
    ):
        try:
            current_user = request.state.current_user
            user_id = current_user["user_id"]
            org_id = current_user["org_id"] if "org_id" in current_user else None
            new_secret_id = uuid.uuid4()
            secret = request_body.secret
            machine_id = request_body.machine_id
            
            machine_query = await db.execute(
                select(Machine).where(Machine.id == machine_id).apply_org_check(request)
            )
            machine = machine_query.scalars().first()
            if not machine:
                raise HTTPException(
                    status_code=404,
                    detail="Machine not found"
                )
        
            secret_manager = SecretManager()
            encrypted_secrets = []
            for item in secret:
                encrypted_item = {
                    "key": item.key,
                    "encrypted_value": secret_manager.encrypt_value(item.value)
                }
                encrypted_secrets.append(encrypted_item)

            secret = Secret(
                id=new_secret_id,
                name=request_body.secret_name,
                user_id=user_id,
                org_id=org_id,
                environment_variables=encrypted_secrets,
                created_at=func.now(),
                updated_at=func.now(),
            )

            db.add(secret)
            await db.flush()
            
            machine_secret = MachineSecret(
                id=uuid.uuid4(),
                machine_id=machine_id,
                secret_id=new_secret_id,
                created_at=func.now(),
            )
            db.add(machine_secret)
            await db.commit()
            
            return secret
        except ValueError as e:
            await db.rollback()
            raise HTTPException(
                status_code=500,
                detail=str(e)
                )


class UpdateMachineWithSecretInput(BaseModel):
    machine_id: UUID
    secret_id: UUID

@router.patch("/machine/secret")
async def update_machine_with_secret(
    request: Request,
    request_body: UpdateMachineWithSecretInput,
    db: AsyncSession = Depends(get_db)
):
    try:
        machine_id = request_body.machine_id
        secret_id = request_body.secret_id

        machine_query = await db.execute(
            select(Machine).where(Machine.id == machine_id)
        )
        machine = machine_query.scalars().first()
        if not machine:
            raise HTTPException(
                status_code=404,
                detail="Machine doesn't exist"
            )
        
        secret_query = await db.execute(
            select(Secret).where(Secret.id == secret_id).apply_org_check(request)
        )
        secret = secret_query.scalars().first()
        if not secret:
            raise HTTPException(
                status_code=404,
                detail="Secret doesn't exist!"
            )
        
        existing_query = await db.execute(
            select(MachineSecret).where(
                MachineSecret.machine_id == machine_id,
                MachineSecret.secret_id == secret_id
            )
        )
        existing = existing_query.scalars().first()
        
        if existing:
            await db.delete(existing)
            await db.commit()
            return JSONResponse(
                status_code=200, 
                content={"message": "Secret removed from the machine successfully", "status": 200, "is_selected": False}
            )
        
        secret.updated_at = func.now()
        
        new_machine_secret_id = uuid.uuid4()
        machine_secret = MachineSecret(
           id=new_machine_secret_id,
           machine_id=machine_id,
           secret_id=secret_id,
           created_at=func.now(),
        )
        
        db.add(machine_secret)
        await db.commit()
        
        return JSONResponse(
            status_code=200, 
            content={"message": "Secret added to machine successfully", "status": 200, "is_selected": True}
        )
    except Exception as e:
        await db.rollback()
        raise e


class SecretUpdateInput(BaseModel):
    secret: List[SecretKeyValue]

@router.patch("/machine/secret/{secret_id}/envs")
async def update_secret_envs(
    request: Request,
    request_body: SecretUpdateInput,
    secret_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    try:
        secret_query = await db.execute(
            select(Secret).where(Secret.id == secret_id).apply_org_check(request)
        )
        secret = secret_query.scalars().first()
        if not secret:
            raise HTTPException(
                status_code=404,
                detail="Secret doesn't exist!"
            )
        
        secret_data = request_body.secret
        secret_manager = SecretManager()
        encrypted_secrets = []
        for item in secret_data:
            encrypted_item = {
                "key": item.key,
                "encrypted_value": secret_manager.encrypt_value(item.value)
                }
            encrypted_secrets.append(encrypted_item)
        
        secret.environment_variables = encrypted_secrets
        secret.updated_at = func.now()
        await db.commit()
        
        return secret
    except Exception as e:
        raise e
 

@router.get("/machine/secrets/all")
async def get_all_secrets(
    request: Request,
    db: AsyncSession = Depends(get_db)
    ):
        try:
            current_user = request.state.current_user
            user_id = current_user["user_id"]
            org_id = current_user["org_id"] if "org_id" in current_user else None
            
            if org_id:
                query = select(Secret).where(Secret.org_id == org_id)
            else:
                query = select(Secret).where(Secret.user_id == user_id)
                
            result = await db.execute(query)
            secrets = result.scalars().all()
            
            secret_manager = SecretManager()
            
            secrets_response = []
            for secret in secrets:
                secret_dict = secret.to_dict() if hasattr(secret, 'to_dict') else {
                    "id": str(secret.id),
                    "user_id": str(secret.user_id),
                    "org_id": str(secret.org_id) if secret.org_id else None,
                    "machine_id": str(secret.machine_id) if secret.machine_id else None,
                    "created_at": secret.created_at.isoformat() if hasattr(secret.created_at, 'isoformat') else str(secret.created_at),
                    "updated_at": secret.updated_at.isoformat() if hasattr(secret.updated_at, 'isoformat') else str(secret.updated_at)
                }
                
                # Decrypt environment variables
                if hasattr(secret, 'environment_variables') and secret.environment_variables:
                    decrypted_env_vars = []
                    for env_var in secret.environment_variables:
                        decrypted_env_var = {
                            "key": env_var["key"],
                            "value": secret_manager.decrypt_value(env_var["encrypted_value"])
                        }
                        decrypted_env_vars.append(decrypted_env_var)
                    
                    secret_dict["environment_variables"] = decrypted_env_vars
                else:
                    secret_dict["environment_variables"] = []
                
                # Get associated machines through machine_secrets table
                machine_secrets_query = await db.execute(
                    select(MachineSecret).where(MachineSecret.secret_id == secret.id)
                )
                machine_secrets = machine_secrets_query.scalars().all()
                secret_dict["machines"] = [str(ms.machine_id) for ms in machine_secrets]
                    
                secrets_response.append(secret_dict)
            
            return JSONResponse(content=secrets_response)
            
        except Exception as e:
            logger.error(f"Error getting secrets: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=str(e)
            )


@router.get("/machine/{machine_id}/secrets/linked")
async def get_all_linked_machine_secrets(
    machine_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    try:
        # Verify the machine exists and user has access
        machine_query = await db.execute(
            select(Machine).where(Machine.id == machine_id)
        )
        machine = machine_query.scalars().first()
        if not machine:
            raise HTTPException(
                status_code=404,
                detail="Machine not found"
            )
        
        # Get all machine_secrets linking the machine to secrets
        machine_secrets_query = await db.execute(
            select(MachineSecret).where(MachineSecret.machine_id == machine_id)
        )
        machine_secrets = machine_secrets_query.scalars().all()
        
        secret_ids = [ms.secret_id for ms in machine_secrets]
        
        if not secret_ids:
            return JSONResponse(content=[])
        
        # Get all secrets linked to this machine
        query = select(Secret).where(Secret.id.in_(secret_ids))
        result = await db.execute(query)
        secrets = result.scalars().all()
        
        secret_manager = SecretManager()
        
        secrets_response = []
        for secret in secrets:
            secret_dict = secret.to_dict() if hasattr(secret, 'to_dict') else {
                "id": str(secret.id),
                "name": secret.name,
                "user_id": str(secret.user_id),
                "org_id": str(secret.org_id) if secret.org_id else None,
                "created_at": secret.created_at.isoformat() if hasattr(secret.created_at, 'isoformat') else str(secret.created_at),
                "updated_at": secret.updated_at.isoformat() if hasattr(secret.updated_at, 'isoformat') else str(secret.updated_at)
            }
            
            # Decrypt environment variables
            if hasattr(secret, 'environment_variables') and secret.environment_variables:
                decrypted_env_vars = []
                for env_var in secret.environment_variables:
                    decrypted_env_var = {
                        "key": env_var["key"],
                        "value": secret_manager.decrypt_value(env_var["encrypted_value"])
                    }
                    decrypted_env_vars.append(decrypted_env_var)
                
                secret_dict["environment_variables"] = decrypted_env_vars
            else:
                secret_dict["environment_variables"] = []
                
            secrets_response.append(secret_dict)
        
        return JSONResponse(content=secrets_response)
        
    except Exception as e:
        logger.error(f"Error getting linked machine secrets: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@router.get("/machine/{machine_id}/secrets/unlinked")
async def get_all_unlinked_machine_secrets(
    request: Request,
    machine_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    try:
        # Verify the machine exists and user has access
        machine_query = await db.execute(
            select(Machine).where(Machine.id == machine_id)
        )
        machine = machine_query.scalars().first()
        if not machine:
            raise HTTPException(
                status_code=404,
                detail="Machine not found"
            )
        
        current_user = request.state.current_user
        user_id = current_user["user_id"]
        org_id = current_user["org_id"] if "org_id" in current_user else None
        
        # Get all secrets already linked to this machine
        machine_secrets_query = await db.execute(
            select(MachineSecret).where(MachineSecret.machine_id == machine_id)
        )
        machine_secrets = machine_secrets_query.scalars().all()
        linked_secret_ids = [ms.secret_id for ms in machine_secrets]
        
        # Get all available secrets for the user/org that are not linked to this machine
        if org_id:
            query = select(Secret).where(Secret.org_id == org_id)
        else:
            query = select(Secret).where(Secret.user_id == user_id)
            
        if linked_secret_ids:
            query = query.where(Secret.id.not_in(linked_secret_ids))
            
        result = await db.execute(query)
        secrets = result.scalars().all()
        
        secret_manager = SecretManager()
        
        secrets_response = []
        for secret in secrets:
            secret_dict = secret.to_dict() if hasattr(secret, 'to_dict') else {
                "id": str(secret.id),
                "name": secret.name,
                "user_id": str(secret.user_id),
                "org_id": str(secret.org_id) if secret.org_id else None,
                "created_at": secret.created_at.isoformat() if hasattr(secret.created_at, 'isoformat') else str(secret.created_at),
                "updated_at": secret.updated_at.isoformat() if hasattr(secret.updated_at, 'isoformat') else str(secret.updated_at)
            }
            
            # Decrypt environment variables
            if hasattr(secret, 'environment_variables') and secret.environment_variables:
                decrypted_env_vars = []
                for env_var in secret.environment_variables:
                    decrypted_env_var = {
                        "key": env_var["key"],
                        "value": secret_manager.decrypt_value(env_var["encrypted_value"])
                    }
                    decrypted_env_vars.append(decrypted_env_var)
                
                secret_dict["environment_variables"] = decrypted_env_vars
            else:
                secret_dict["environment_variables"] = []
                
            secrets_response.append(secret_dict)
        
        return JSONResponse(content=secrets_response)
        
    except Exception as e:
        logger.error(f"Error getting unlinked machine secrets: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@router.delete("/machine/secret/{secret_id}")
async def delete_secret(
    request: Request,
    secret_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    try:
        secret_query = await db.execute(
            select(Secret).where(Secret.id == secret_id).apply_org_check(request)
        )
        secret = secret_query.scalars().first()
        if not secret:
            raise HTTPException(
                status_code=404,
                detail="Secret doesn't exist!"
            )
        
        # Delete associated machine_secrets entries
        machine_secrets_query = await db.execute(
            select(MachineSecret).where(MachineSecret.secret_id == secret_id)
        )
        machine_secrets = machine_secrets_query.scalars().all()
        
        for machine_secret in machine_secrets:
            await db.delete(machine_secret)
        
        # Delete the secret itself
        await db.delete(secret)
        await db.commit()
        
        return JSONResponse(content={"message": "Secret deleted successfully"})
    except Exception as e:
        raise e 


@router.patch("/machine/serverless/{machine_id}")
async def update_serverless_machine(
    request: Request,
    machine_id: UUID,
    update_machine: UpdateServerlessMachineModel,
    rollback_version_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
) -> MachineModel:
    # Validate free plan restrictions
    await validate_free_plan_restrictions(
        request=request,
        machine_data=update_machine.model_dump(exclude_unset=True),
        db=db,
        is_update=True,
        existing_machine_id=machine_id,
    )

    try:
        machine = await db.execute(
            select(Machine).where(Machine.id == machine_id).apply_org_check(request)
        )
        machine = machine.scalars().first()
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

        if machine.type != MachineType.COMFY_DEPLOY_SERVERLESS:
            raise HTTPException(
                status_code=400, detail="Machine is not a serverless machine"
            )

        current_user = request.state.current_user
        user_id = current_user["user_id"]
        org_id = current_user["org_id"] if "org_id" in current_user else None

        if (
            machine.machine_version_id is None
            and machine.machine_builder_version == "4"
        ):
            # give it a default version 1
            await create_machine_version(db, machine, user_id)
            await db.commit()
            await db.refresh(machine)  # Single refresh at the end

        fields_to_trigger_rebuild = [
            "allow_concurrent_inputs",
            "comfyui_version",
            "concurrency_limit",
            "docker_command_steps",
            "extra_docker_commands",
            "idle_timeout",
            "ws_timeout",
            "machine_builder_version",
            "install_custom_node_with_gpu",
            "run_timeout",
            "base_docker_image",
            "extra_args",
            "prestart_command",
            "python_version",
            "install_custom_node_with_gpu",
            "cpu_request",
            "cpu_limit",
            "memory_request",
            "memory_limit",
            "models_to_cache",
            "enable_gpu_memory_snapshot",
        ]

        update_machine_dict = update_machine.model_dump()

        rebuild = update_machine_dict.get(
            "is_trigger_rebuild", False
        ) or check_fields_for_changes(
            machine, update_machine_dict, fields_to_trigger_rebuild
        )
        keep_warm_changed = check_fields_for_changes(
            machine, update_machine_dict, ["keep_warm"]
        )
        gpu_changed = check_fields_for_changes(machine, update_machine_dict, ["gpu"])

        # We dont need to trigger a rebuild if we only change the gpu.
        # We need to trigger a rebuild if we change the gpu and install_custom_node_with_gpu is true
        if gpu_changed and machine.install_custom_node_with_gpu:
            rebuild = True

        print(update_machine.model_dump())

        for key, value in update_machine.model_dump().items():
            if hasattr(machine, key) and value is not None:
                setattr(machine, key, value)

        if machine.comfyui_version is None:
            # put something here so it wont crash
            machine.comfyui_version = comfyui_hash

        docker_commands = await generate_all_docker_commands(machine)
        docker_commands_hash = hash_machine_dependencies(docker_commands)
        machine.machine_hash = docker_commands_hash

        machine.updated_at = func.now()

        if rebuild:
            machine.status = "building"

            # Get next version number
            if not rollback_version_id:
                # Combine queries to get both max version and current version data
                result = await db.execute(
                    select(
                        func.max(MachineVersion.version).label("max_version"),
                        MachineVersion,
                    )
                    .where(MachineVersion.machine_id == machine.id)
                    .group_by(MachineVersion)
                    .order_by(MachineVersion.version.desc())
                    .limit(1)
                )
                row = result.first()
                next_version = (row.max_version or 0) + 1 if row else 1
                current_version_data = row.MachineVersion if row else None

                # Create new version
                machine_version = await create_machine_version(
                    db,
                    machine,
                    user_id,
                    version=next_version,
                    current_version_data=current_version_data,
                )
            else:
                machine.machine_version_id = rollback_version_id

                # update that machine version's created_at to now
                machine_version = await db.execute(
                    select(MachineVersion).where(
                        MachineVersion.id == rollback_version_id
                    )
                )
                machine_version = machine_version.scalars().first()
                machine_version.created_at = func.now()
                machine_version.updated_at = func.now()
                machine_version.status = machine.status
                await db.commit()

            if machine.machine_hash is not None:
                existing_machine_info_url = f"https://comfyui.comfydeploy.com/static-assets/{machine.machine_hash}/object_info.json"
                try:
                    import aiohttp

                    async with aiohttp.ClientSession() as session:
                        async with session.head(existing_machine_info_url) as response:
                            skip_static_assets = response.status == 200
                except Exception as e:
                    logger.warning(f"Error checking static assets: {e}")
                    skip_static_assets = False
            else:
                skip_static_assets = False

            # Prepare build parameters
            volumes = await retrieve_model_volumes(request, db)
            docker_commands = await generate_all_docker_commands(machine)
            machine_token = generate_machine_token(user_id, org_id)
            secrets = await get_machine_secrets(db=db, machine_id=machine.id)

            params = BuildMachineItem(
                machine_id=str(machine.id),
                name=str(machine.id),
                cd_callback_url=f"{current_endpoint}/api/machine-built",
                callback_url=f"{current_endpoint}/api",
                gpu_event_callback_url=f"{current_endpoint}/api/gpu_event",
                models=machine.models,
                gpu=machine.gpu,
                model_volume_name=volumes[0]["volume_name"],
                run_timeout=machine.run_timeout,
                idle_timeout=machine.idle_timeout,
                auth_token=machine_token,
                ws_timeout=machine.ws_timeout,
                concurrency_limit=machine.concurrency_limit,
                allow_concurrent_inputs=machine.allow_concurrent_inputs,
                legacy_mode=machine.legacy_mode,
                install_custom_node_with_gpu=machine.install_custom_node_with_gpu,
                allow_background_volume_commits=machine.allow_background_volume_commits,
                retrieve_static_assets=machine.retrieve_static_assets,
                # skip_static_assets=skip_static_assets,
                skip_static_assets=True,
                docker_commands=docker_commands.model_dump()["docker_commands"],
                machine_builder_version=machine.machine_builder_version,
                base_docker_image=machine.base_docker_image,
                python_version=machine.python_version,
                prestart_command=machine.prestart_command,
                extra_args=machine.extra_args,
                machine_version_id=str(machine.machine_version_id),
                machine_hash=docker_commands_hash,
                secrets=secrets,
                modal_image_id=machine_version.modal_image_id
                if machine_version
                else None,
                disable_metadata=machine.disable_metadata,
                cpu_request=machine.cpu_request,
                cpu_limit=machine.cpu_limit,
                memory_request=machine.memory_request,
                memory_limit=machine.memory_limit,
                models_to_cache=machine.models_to_cache,
                enable_gpu_memory_snapshot=machine.enable_gpu_memory_snapshot
            )
            background_tasks.add_task(build_logic, params)

        if keep_warm_changed and not rebuild:
            print("Keep warm changed", machine.keep_warm)
            set_machine_always_on(
                str(machine.id),
                KeepWarmBody(
                    warm_pool_size=machine.keep_warm, gpu=GPUType(machine.gpu)
                ),
            )

        await db.commit()
        await db.refresh(machine)

        return JSONResponse(content=machine.to_dict())
    except HTTPException as e:
        raise e
    # except Exception as e:
    #     raise HTTPException(status_code=500, detail="Internal server error: " + str(e))


async def redeploy_machine(
    request: Request,
    db: AsyncSession,
    machine: Machine,
    machine_version: MachineVersion,
    background_tasks: Optional[BackgroundTasks] = None,
):
    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user["org_id"] if "org_id" in current_user else None

    volumes = await retrieve_model_volumes(request, db)
    machine_token = generate_machine_token(user_id, org_id)
    secrets = await get_machine_secrets(db=db, machine_id=machine.id)
    
    params = BuildMachineItem(
        machine_id=str(machine.id),
        name=str(machine.id),
        cd_callback_url=f"{current_endpoint}/api/machine-built",
        callback_url=f"{current_endpoint}/api",
        gpu_event_callback_url=f"{current_endpoint}/api/gpu_event",
        models=machine.models,
        gpu=machine.gpu,
        model_volume_name=volumes[0]["volume_name"],
        run_timeout=machine.run_timeout,
        idle_timeout=machine.idle_timeout,
        auth_token=machine_token,
        ws_timeout=machine.ws_timeout,
        concurrency_limit=machine.concurrency_limit,
        allow_concurrent_inputs=machine.allow_concurrent_inputs,
        # skip_static_assets=skip_static_assets,
        skip_static_assets=True,
        modal_image_id=machine_version.modal_image_id,
        machine_builder_version=machine.machine_builder_version,
        base_docker_image=machine.base_docker_image,
        python_version=machine.python_version,
        prestart_command=machine.prestart_command,
        extra_args=machine.extra_args,
        machine_version_id=str(machine.machine_version_id),
        machine_hash=machine_version.machine_hash,
        disable_metadata=machine.disable_metadata,
        secrets=secrets,
        cpu_request=machine.cpu_request,
        cpu_limit=machine.cpu_limit,
        memory_request=machine.memory_request,
        memory_limit=machine.memory_limit,
        models_to_cache=machine.models_to_cache,
        enable_gpu_memory_snapshot=machine.enable_gpu_memory_snapshot
    )
    print("params", params)
    if background_tasks:
        background_tasks.add_task(build_logic, params)
    else:
        await build_logic(params)


async def redeploy_machine_internal(
    machine_id: str,
):
    async with get_db_context() as db:
        machine = await db.execute(select(Machine).where(Machine.id == machine_id))
        machine = machine.scalars().first()
        # volumes = await retrieve_model_volumes(request, db)
        volume_name = "models_" + machine.org_id if machine.org_id else machine.user_id
        machine_token = generate_machine_token(machine.user_id, machine.org_id)
        secrets = await get_machine_secrets(db=db, machine_id=machine.id)
        machine_version = await db.execute(
            select(MachineVersion).where(
                MachineVersion.id == machine.machine_version_id
            )
        )
        machine_version = machine_version.scalars().first()

        if machine_version.modal_image_id is None:
            raise HTTPException(
                status_code=404,
                detail="Machine doesnt support quick redeploy, and also doesnt have a modal image id",
            )

    params = BuildMachineItem(
        machine_id=str(machine.id),
        name=str(machine.id),
        cd_callback_url=f"{current_endpoint}/api/machine-built",
        callback_url=f"{current_endpoint}/api",
        gpu_event_callback_url=f"{current_endpoint}/api/gpu_event",
        models=machine.models,
        gpu=machine.gpu,
        model_volume_name=volume_name,
        run_timeout=machine.run_timeout,
        idle_timeout=machine.idle_timeout,
        auth_token=machine_token,
        ws_timeout=machine.ws_timeout,
        concurrency_limit=machine.concurrency_limit,
        allow_concurrent_inputs=machine.allow_concurrent_inputs,
        # skip_static_assets=skip_static_assets,
        skip_static_assets=True,
        modal_image_id=machine_version.modal_image_id,
        machine_builder_version=machine.machine_builder_version,
        base_docker_image=machine.base_docker_image,
        python_version=machine.python_version,
        prestart_command=machine.prestart_command,
        extra_args=machine.extra_args,
        machine_version_id=str(machine.machine_version_id),
        machine_hash=machine_version.machine_hash,
        disable_metadata=machine.disable_metadata,
        secrets=secrets,
        cpu_request=machine.cpu_request,
        cpu_limit=machine.cpu_limit,
        memory_request=machine.memory_request,
        memory_limit=machine.memory_limit,
        models_to_cache=machine.models_to_cache,
        enable_gpu_memory_snapshot=machine.enable_gpu_memory_snapshot
    )
    await build_logic(params)


async def redeploy_machine_deployment_internal(
    deployment: Deployment,
):
    async with get_db_context() as db:
        volume_name = (
            "models_" + deployment.org_id if deployment.org_id else deployment.user_id
        )
        machine_token = generate_machine_token(deployment.user_id, deployment.org_id)
        secrets = await get_machine_secrets(db=db, machine_id=deployment.machine_id)
        machine_version = await db.execute(
            select(MachineVersion).where(
                MachineVersion.id == deployment.machine_version_id
            )
        )
        machine_version = machine_version.scalars().first()

        if machine_version.modal_image_id is None:
            raise HTTPException(
                status_code=404,
                detail="Machine doesnt support quick redeploy, and also doesnt have a modal image id",
            )

    params = BuildMachineItem(
        machine_id=str(deployment.machine_id),
        name=str(deployment.id),
        cd_callback_url=f"{current_endpoint}/api/machine-built",
        callback_url=f"{current_endpoint}/api",
        gpu_event_callback_url=f"{current_endpoint}/api/gpu_event",
        # models=machine.models,
        gpu=deployment.gpu,
        model_volume_name=volume_name,
        run_timeout=deployment.run_timeout,
        idle_timeout=deployment.idle_timeout,
        auth_token=machine_token,
        # ws_timeout=machine.ws_timeout,
        concurrency_limit=deployment.concurrency_limit,
        allow_concurrent_inputs=False,
        # skip_static_assets=skip_static_assets,
        skip_static_assets=True,
        modal_image_id=machine_version.modal_image_id,
        machine_builder_version="4",
        # base_docker_image=machine.base_docker_image,
        # python_version=machine.python_version,
        # prestart_command=machine.prestart_command,
        # extra_args=machine.extra_args,
        machine_version_id=str(deployment.machine_version_id),
        machine_hash=machine_version.machine_hash,
        is_deployment=True,
        environment=deployment.environment,
        disable_metadata=deployment.disable_metadata,
        secrets=secrets,
        cpu_request=deployment.cpu_request,
        cpu_limit=deployment.cpu_limit,
        memory_request=deployment.memory_request,
        memory_limit=deployment.memory_limit
    )
    await build_logic(params)


@router.get("/machine/serverless/{machine_id}/versions")
async def get_machine_versions(
    request: Request,
    machine_id: UUID,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")

    params = {
        "machine_id": machine_id,
        "limit": limit,
        "offset": offset,
        "user_id": user_id,
        "org_id": org_id,
    }

    sql = """
    SELECT
        mv.id,
        mv.machine_id,
        mv.version,
        mv.user_id,
        mv.created_at,
        mv.updated_at,
        mv.comfyui_version,
        mv.gpu,
        mv.status,
        m.org_id
    FROM "comfyui_deploy"."machine_versions" mv
    JOIN "comfyui_deploy"."machines" m ON mv.machine_id = m.id
    WHERE mv.machine_id = :machine_id
    """

    if org_id:
        sql += " AND m.org_id = :org_id"
    else:
        sql += " AND mv.user_id = :user_id AND m.org_id IS NULL"

    sql += """
    ORDER BY mv.version DESC
    LIMIT :limit OFFSET :offset
    """

    # Execute the raw query
    result = await db.execute(text(sql), params)
    rows = result.mappings().all()

    # Convert to dict and handle UUID/datetime serialization
    versions_data = []
    for row in rows:
        version_dict = {}
        for k, v in row.items():
            # Handle various non-serializable types
            if isinstance(v, UUID):
                version_dict[k] = str(v)
            elif isinstance(v, datetime.datetime):
                version_dict[k] = v.isoformat()
            else:
                version_dict[k] = v
        versions_data.append(version_dict)

    return JSONResponse(content=versions_data)


@router.get("/machine/serverless/{machine_id}/versions/all")
async def get_all_machine_versions(
    request: Request,
    machine_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.state.current_user.get("user_id")
    org_id = request.state.current_user.get("org_id")

    params = {
        "machine_id": machine_id,
        "user_id": user_id,
        "org_id": org_id,
    }

    sql = """
    SELECT
        mv.id,
        mv.machine_id,
        mv.version,
        mv.user_id,
        mv.created_at,
        mv.updated_at,
        mv.comfyui_version,
        mv.gpu,
        mv.status,
        m.org_id
    FROM "comfyui_deploy"."machine_versions" mv
    JOIN "comfyui_deploy"."machines" m ON mv.machine_id = m.id
    WHERE mv.machine_id = :machine_id
    """

    if org_id:
        sql += " AND m.org_id = :org_id"
    else:
        sql += " AND mv.user_id = :user_id AND m.org_id IS NULL"

    sql += """
    ORDER BY mv.version DESC
    """

    # Execute the raw query
    result = await db.execute(text(sql), params)
    rows = result.mappings().all()

    # Convert to dict and handle UUID/datetime serialization
    versions_data = []
    for row in rows:
        version_dict = {}
        for k, v in row.items():
            # Handle various non-serializable types
            if isinstance(v, UUID):
                version_dict[k] = str(v)
            elif isinstance(v, datetime.datetime):
                version_dict[k] = v.isoformat()
            else:
                version_dict[k] = v
        versions_data.append(version_dict)

    return JSONResponse(content=versions_data)


# get specific machine version
@router.get("/machine/serverless/{machine_id}/versions/{version_id}")
async def get_machine_version(
    request: Request,
    machine_id: UUID,
    version_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    try:
        machine_query = select(Machine).where(
            Machine.id == machine_id
        ).apply_org_check(request)
        
        machine_result = await db.execute(machine_query)
        machine = machine_result.scalars().first()
        
        if machine is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"Machine not found"}
            )
        
        version_query = select(MachineVersion).where(
            MachineVersion.id == version_id,
            MachineVersion.machine_id == machine_id
        )

        
        version_result = await db.execute(version_query)
        machine_version = version_result.scalars().first()
        
        if machine_version is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"Machine version not found"}
            )
        
        version_dict = {}
        for k in machine_version.__dict__:
            if not k.startswith('_'):
                v = getattr(machine_version, k)
                if isinstance(v, UUID):
                    version_dict[k] = str(v)
                elif isinstance(v, datetime.datetime):
                    version_dict[k] = v.isoformat()
                else:
                    version_dict[k] = v
        
        return JSONResponse(content=version_dict)
        
    except Exception as e:
        logger.error(f"Error getting machine version: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"detail": str(e)}
        )

@router.get("/machine/serverless/{machine_id}/files")
async def get_machine_files(
    request: Request,
    machine_id: UUID,
    path: Optional[str] = "/",
    db: AsyncSession = Depends(get_db),
):
    # get machine id
    machine = await db.execute(
        select(Machine).where(Machine.id == machine_id).apply_org_check(request)
    )
    machine = machine.scalars().first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    # Path validation to prevent directory traversal
    import os.path
    import re

    # Remove any ".." path traversal attempts and normalize the path
    normalized_path = os.path.normpath(path)
    if normalized_path == ".":
        normalized_path = "/"

    # Don't allow paths that try to go above the custom_nodes directory
    if normalized_path.startswith("/..") or normalized_path.startswith("..") or "/../" in normalized_path:
        raise HTTPException(
            status_code=403,
            detail="Access to parent directories is not allowed"
        )

    # Additional validation to ensure we're staying within custom_nodes
    if re.search(r'^/+$', normalized_path) or normalized_path == "/":
        # Allow the root of custom_nodes
        pass
    elif not normalized_path.startswith("/"):
        # Ensure all paths start with /
        normalized_path = "/" + normalized_path

    try:
        # URL-encode the path to handle spaces and special characters
        import urllib.parse
        encoded_path = urllib.parse.quote(f"/comfyui/custom_nodes{normalized_path}")

        get_file_tree = modal.Function.from_name(str(machine_id), "get_file_tree")
        result = await get_file_tree.remote.aio(path=encoded_path)
        return JSONResponse(content=result)
    except modal.exception.NotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Try to rebuild the machine to view the file tree."
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error getting file tree: {str(e)}"
        )

class RollbackMachineVersionBody(BaseModel):
    machine_version_id: Optional[UUID] = None
    version: Optional[int] = None


@router.post("/machine/serverless/{machine_id}/rollback")
async def rollback_serverless_machine(
    request: Request,
    machine_id: UUID,
    version: RollbackMachineVersionBody,
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    async def get_machine(machine_id: UUID):
        machine = await db.execute(
            select(Machine).where(Machine.id == machine_id).apply_org_check(request)
        )
        machine = machine.scalars().first()
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")
        return machine

    if version.machine_version_id is None and version.version is None:
        raise HTTPException(
            status_code=400,
            detail="Either machine_version_id or version must be provided",
        )

    machine = await get_machine(machine_id)

    if version.machine_version_id is not None:
        if machine.machine_version_id == version.machine_version_id:
            raise HTTPException(
                status_code=400, detail="Cannot rollback to current version"
            )

        machine_version = await db.execute(
            select(MachineVersion).where(
                MachineVersion.id == version.machine_version_id
            )
        )
        machine_version = machine_version.scalars().first()
    else:
        # Get current version first
        current_version = await db.execute(
            select(MachineVersion).where(
                MachineVersion.id == machine.machine_version_id
            )
        )
        current_version = current_version.scalars().first()

        if current_version and current_version.version == version.version:
            raise HTTPException(
                status_code=400, detail="Cannot rollback to current version"
            )

        # Get target version
        machine_version = await db.execute(
            select(MachineVersion)
            .where(MachineVersion.machine_id == machine_id)
            .where(MachineVersion.version == version.version)
        )
        machine_version = machine_version.scalars().first()

    if machine_version is None:
        raise HTTPException(status_code=404, detail="Machine version not found")

    # Call update_serverless_machine with rollback flag
    return await update_serverless_machine(
        request=request,
        machine_id=machine_id,
        update_machine=UpdateServerlessMachineModel(
            **{
                col: getattr(machine_version, col)
                for col in get_machine_columns().keys()
                if hasattr(machine_version, col)
            },
            is_trigger_rebuild=machine_version.modal_image_id is not None,
        ),
        rollback_version_id=machine_version.id,
        db=db,
        background_tasks=background_tasks,
    )


@router.delete("/machine/{machine_id}")
async def delete_machine(
    request: Request,
    machine_id: UUID,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
):
    machine = await db.execute(
        select(Machine).where(Machine.id == machine_id).apply_org_check(request)
    )
    machine = machine.scalars().first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    if machine.keep_warm > 0:
        raise HTTPException(
            status_code=400,
            detail="Please set keep warm to 0 before deleting the machine.",
        )

    if not force:
        # Check if there are existing deployments
        deployments = await db.execute(
            select(Deployment)
            .join(Workflow)
            .where(Deployment.machine_id == machine_id)
            .where(~Workflow.deleted)
            .with_only_columns(Deployment.workflow_id, Workflow.name)
        )
        deployments = deployments.all()

        if len(deployments) > 0:
            logger.info(f"Deployments: {deployments}")
            workflow_names = ",".join([name for _, name in deployments])
            raise HTTPException(
                status_code=402,
                detail=f"You still have these workflows related to this machine: {workflow_names}",
            )

    machine.deleted = True
    await db.commit()
    await db.refresh(machine)

    if machine.type == MachineType.COMFY_DEPLOY_SERVERLESS and machine.modal_app_id:
        try:
            app_name = machine.modal_app_id
            process = await asyncio.subprocess.create_subprocess_shell(
                "modal app stop " + app_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.wait()
            logger.info(f"Successfully stopped modal app: {app_name}")
        except Exception as e:
            logger.error(f"Error stopping modal app for machine {machine.id}: {str(e)}")

    return JSONResponse(content={"message": "Machine deleted"})


class CustomMachineModel(BaseModel):
    name: str
    type: MachineType
    endpoint: str
    auth_token: Optional[str]


class UpdateCustomMachineModel(BaseModel):
    name: Optional[str] = None
    type: Optional[MachineType] = None
    endpoint: Optional[str] = None
    auth_token: Optional[str] = None


@router.post("/machine/custom")
async def create_custom_machine(
    request: Request,
    machine: CustomMachineModel,
    db: AsyncSession = Depends(get_db),
) -> MachineModel:
    plan = request.state.current_user.get("plan")
    if plan == "free":
        raise HTTPException(
            status_code=403, detail="Free plan users cannot create custom machines"
        )

    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user["org_id"] if "org_id" in current_user else None

    machine = Machine(
        id=uuid.uuid4(),
        name=machine.name,
        endpoint=machine.endpoint,
        type=machine.type,
        auth_token=machine.auth_token,
        user_id=user_id,
        org_id=org_id,
        created_at=func.now(),
        updated_at=func.now(),
    )

    db.add(machine)
    await db.commit()
    await db.refresh(machine)

    return JSONResponse(content=machine.to_dict())


@router.patch("/machine/custom/{machine_id}")
async def update_custom_machine(
    request: Request,
    machine_id: UUID,
    machine_update: UpdateCustomMachineModel,
    db: AsyncSession = Depends(get_db),
) -> MachineModel:
    machine = await db.execute(
        select(Machine).where(Machine.id == machine_id).apply_org_check(request)
    )
    machine = machine.scalars().first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    for key, value in machine_update.model_dump().items():
        if hasattr(machine, key) and value is not None:
            setattr(machine, key, value)

    machine.updated_at = func.now()

    await db.commit()
    await db.refresh(machine)

    return JSONResponse(content=machine.to_dict())


def has_field_changed(old_obj, new_value, field_name):
    """Check if a field's value has changed."""
    existing_value = getattr(old_obj, field_name)
    return existing_value != new_value


def check_fields_for_changes(old_obj, new_data, fields_to_check):
    """Check if any of the specified fields have changed."""
    for field in fields_to_check:
        if (
            field in new_data
            and new_data[field] is not None
            and hasattr(old_obj, field)
            and has_field_changed(old_obj, new_data[field], field)
        ):
            return True
    return False


@router.get("/machine/{machine_id}/docker-commands")
async def get_machine_docker_commands(
    request: Request,
    machine_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    # Get the machine
    machine = await db.execute(
        select(Machine)
        .where(Machine.id == machine_id)
        .where(~Machine.deleted)
        .apply_org_check(request)
    )
    machine = machine.scalars().first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    if machine.type != MachineType.COMFY_DEPLOY_SERVERLESS:
        raise HTTPException(
            status_code=400, detail="Machine is not a Comfy Deploy Serverless machine"
        )

    # Get machine version if it exists
    machine_version = None
    if machine.machine_version_id:
        machine_version = await db.execute(
            select(MachineVersion).where(
                MachineVersion.id == machine.machine_version_id
            )
        )
        machine_version = machine_version.scalars().first()

    # Generate docker commands
    docker_commands = await generate_all_docker_commands(machine)
    docker_commands_hash = hash_machine_dependencies(docker_commands)

    return JSONResponse(
        content={
            "docker_commands": docker_commands.model_dump()["docker_commands"],
            "machine_hash": docker_commands_hash,
            "python_version": machine.python_version,
            "base_docker_image": machine.base_docker_image,
            "machine_version": {
                "id": str(machine.machine_version_id)
                if machine.machine_version_id
                else None,
                "modal_image_id": machine_version.modal_image_id
                if machine_version
                else None,
            },
        }
    )


@multi_level_cached(
    key_prefix="custom_nodes_version",
    ttl_seconds=60,  # Cache in local memory for 1 minute
    redis_ttl_seconds=300,  # Cache in Redis for 5 minutes
    version="1.0",
    key_builder=lambda repo_name: f"custom_nodes_version:{repo_name}",
)
async def get_latest_commit_info(repo_name: str):
    """Get the latest commit information from GitHub with caching."""
    headers = {
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN')}",
        "User-Agent": "request",
    }

    branch_url = f"https://api.github.com/repos/{repo_name}/branches/main"
    branch_info = await fetch_github_data(branch_url, headers)
    latest_commit = branch_info["commit"]["sha"]

    commit_info = await fetch_github_data(
        f"https://api.github.com/repos/{repo_name}/commits/{latest_commit}", headers
    )

    return {
        "hash": latest_commit,
        "message": commit_info["commit"]["message"],
        "date": commit_info["commit"]["committer"]["date"],
    }


@router.get("/machine/{machine_id}/check-custom-nodes")
async def check_custom_nodes_version(
    request: Request,
    machine_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Check if the comfyui-deploy custom nodes are up to date by comparing with the latest commit hash from the main branch.
    """
    # First verify the machine exists and user has access
    machine = await db.execute(
        select(Machine)
        .where(Machine.id == machine_id)
        .where(~Machine.deleted)
        .apply_org_check(request)
    )
    machine = machine.scalars().first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    try:
        # Get the repository name
        repo_name = await extract_repo_name(
            "https://github.com/BennyKok/comfyui-deploy"
        )

        # Get latest commit info using cached function
        latest_commit_info = await get_latest_commit_info(repo_name)

        # Find comfyui-deploy in docker_command_steps
        local_commit = None
        local_version = None
        comfy_manager_node = None
        
        if machine.docker_command_steps and "steps" in machine.docker_command_steps:
            for step in machine.docker_command_steps["steps"]:
                # Check for custom-node type (existing logic)
                if (
                    step["type"] == "custom-node"
                    and "bennykok/comfyui-deploy" in step["data"]["url"].lower()
                ):
                    local_commit = step["data"]["hash"]
                    break
                # Check for custom-node-manager type
                elif (
                    step["type"] == "custom-node-manager"
                    and step["data"]["node_id"] == "comfyui-deploy"
                ):
                    comfy_manager_node = step
                    local_version = step["data"].get("version")
                    break

        # If found custom-node-manager, use ComfyUI API to check version
        if comfy_manager_node and not local_commit:
            try:
                # Get latest version from ComfyUI API
                latest_node_info = await fetch_comfy_node_metadata("comfyui-deploy")
                latest_version = latest_node_info.get("version")
                
                return JSONResponse(
                    content={
                        "status": "success",
                        "local_commit": {
                            "hash": None,
                            "message": f"Using custom-node-manager version {local_version}",
                            "date": None,
                        },
                        "latest_commit": {
                            "hash": None,
                            "message": f"Latest custom-node-manager version {latest_version}",
                            "date": latest_node_info.get("createdAt"),
                        },
                        "is_up_to_date": local_version == latest_version,
                    }
                )
            except Exception as e:
                logger.error(f"Error fetching ComfyUI node metadata: {str(e)}")
                # Fall back to default behavior if API fails
                pass

        # If not found in steps, use the default hash from docker.py
        if not local_commit:
            local_commit = comfydeploy_hash

        # Get local commit info
        headers = {
            "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN')}",
            "User-Agent": "request",
        }
        local_commit_info = (
            await fetch_github_data(
                f"https://api.github.com/repos/{repo_name}/commits/{local_commit}",
                headers,
            )
            if local_commit
            else None
        )

        return JSONResponse(
            content={
                "status": "success",
                "local_commit": {
                    "hash": local_commit,
                    "message": local_commit_info["commit"]["message"]
                    if local_commit_info
                    else None,
                    "date": local_commit_info["commit"]["committer"]["date"]
                    if local_commit_info
                    else None,
                },
                "latest_commit": latest_commit_info,
                "is_up_to_date": local_commit == latest_commit_info["hash"],
            }
        )

    except Exception as e:
        logger.error(f"Error checking custom nodes version: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error checking custom nodes version: {str(e)}"
        )


async def update_comfyui_deploy_custom_node(docker_command_steps: dict) -> dict:
    """
    Updates or adds the comfyui-deploy custom node to the docker command steps.
    Ensures it's placed at the end and prevents duplicates.
    """
    try:
        if not docker_command_steps or "steps" not in docker_command_steps:
            docker_command_steps = {"steps": []}

        # Check for existing comfyui-deploy installations and determine type
        filtered_steps = []
        has_old_custom_node = False
        has_new_custom_node_manager = False
        
        for step in docker_command_steps["steps"]:
            if (
                step["type"] == "custom-node"
                and "bennykok/comfyui-deploy" in step["data"]["url"].lower()
            ):
                has_old_custom_node = True
                continue
            elif (
                step["type"] == "custom-node-manager"
                and step["data"]["node_id"] == "comfyui-deploy"
            ):
                has_new_custom_node_manager = True
                continue
            filtered_steps.append(step)

        # Handle old custom-node type (GitHub-based approach)
        if has_old_custom_node:
            # Get the latest commit info
            repo_name = "BennyKok/comfyui-deploy"
            latest_commit_info = await get_latest_commit_info(repo_name)
            if not latest_commit_info or "hash" not in latest_commit_info:
                logger.error("Failed to get latest commit info")
                raise HTTPException(
                    status_code=500,
                    detail="Failed to get latest commit information for comfyui-deploy",
                )

            # Add the updated comfyui-deploy custom node at the end
            comfyui_deploy_step = {
                "type": "custom-node",
                "id": "comfyui-deploy",
                "data": {
                    "url": "https://github.com/BennyKok/comfyui-deploy",
                    "name": "ComfyUI Deploy",
                    "hash": latest_commit_info["hash"],
                    "install_type": "git-clone",
                    "files": ["https://github.com/BennyKok/comfyui-deploy"],
                },
            }
            filtered_steps.append(comfyui_deploy_step)
            
        # Handle new custom-node-manager type (ComfyUI API approach)
        elif has_new_custom_node_manager:
            try:
                latest_node_info = await fetch_comfy_node_metadata("comfyui-deploy")
                latest_version = latest_node_info.get("version")
                if not latest_version:
                    raise ValueError("No version found in API response")
                    
                # Add the updated comfyui-deploy custom node manager at the end
                comfyui_deploy_step = {
                    "id": "comfyui-deploy",
                    "data": {
                        "node_id": "comfyui-deploy",
                        "version": latest_version,
                    },
                    "type": "custom-node-manager",
                }
                filtered_steps.append(comfyui_deploy_step)
                
            except Exception as e:
                logger.error(f"Failed to get latest version from ComfyUI API: {str(e)}")
                raise HTTPException(
                    status_code=500,
                    detail="Failed to get latest version information for comfyui-deploy",
                )

        return {"steps": filtered_steps}
    except Exception as e:
        logger.error(f"Error updating comfyui-deploy custom node: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to update custom nodes: {str(e)}"
        )


@router.post("/machine/{machine_id}/update-custom-nodes")
async def update_machine_custom_nodes(
    request: Request,
    machine_id: UUID,
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
) -> MachineModel:
    """
    Updates the comfyui-deploy custom nodes to the latest version and creates a new machine version.
    """
    try:
        # Get the machine
        machine = await db.execute(
            select(Machine)
            .where(Machine.id == machine_id)
            .where(~Machine.deleted)
            .apply_org_check(request)
        )
        machine = machine.scalars().first()
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

        if machine.type != MachineType.COMFY_DEPLOY_SERVERLESS:
            raise HTTPException(
                status_code=400,
                detail="Machine is not a Comfy Deploy Serverless machine",
            )

        # Update the docker command steps
        updated_steps = await update_comfyui_deploy_custom_node(
            machine.docker_command_steps
        )

        # Create update model with only the necessary changes
        update_model = UpdateServerlessMachineModel(
            docker_command_steps=updated_steps, is_trigger_rebuild=True
        )

        # Use existing update endpoint to handle the update
        return await update_serverless_machine(
            request=request,
            machine_id=machine_id,
            update_machine=update_model,
            db=db,
            background_tasks=background_tasks,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating machine custom nodes: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to update custom nodes: {str(e)}"
        )

async def get_machine_secrets(
    db: AsyncSession = Depends(get_db),
    machine_id = UUID,
):
        
    # Get all secrets linked to this machine
    machine_secrets_query = await db.execute(
        select(MachineSecret).where(MachineSecret.machine_id == machine_id)
    )
    machine_secrets = machine_secrets_query.scalars().all()
    
    # Get all secret IDs linked to this machine
    secret_ids = [ms.secret_id for ms in machine_secrets]
    
    # Initialize an empty dictionary to store all decrypted environment variables
    secrets = {}
    
    if secret_ids:
        # Get all secrets linked to this machine
        secrets_query = await db.execute(
            select(Secret).where(Secret.id.in_(secret_ids))
        )
        all_secrets = secrets_query.scalars().all()
        
        # Decrypt all environment variables and merge them into a single dictionary
        secret_manager = SecretManager()
        for secret in all_secrets:
            if hasattr(secret, 'environment_variables') and secret.environment_variables:
                for env_var in secret.environment_variables:
                    key = env_var["key"]
                    decrypted_value = secret_manager.decrypt_value(env_var["encrypted_value"])
                    secrets[key] = decrypted_value

    return secrets


@router.get("/machine/{machine_id}/export")
async def get_machine_export(
    request: Request,
    machine_id: str,
    version: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    machine_query = select(Machine).where(
        Machine.id == machine_id,
        Machine.deleted == False
    ).apply_org_check_by_type(Machine, request)
    
    result = await db.execute(machine_query)
    machine = result.scalar_one_or_none()
    
    if not machine:
        raise HTTPException(
            status_code=404,
            detail="Machine not found or you don't have access to it"
        )
    
    machine_version = None
    if version is not None:
        version_query = select(MachineVersion).where(
            MachineVersion.machine_id == machine_id,
            MachineVersion.version == version
        )
    else:
        version_query = select(MachineVersion).where(
            MachineVersion.machine_id == machine_id
        ).order_by(MachineVersion.created_at.desc()).limit(1)
    
    result = await db.execute(version_query)
    machine_version = result.scalar_one_or_none()
    
    base_machine_data = {
        "name": machine.name,
        "type": machine.type,
        "export_version": "1.0",
        "exported_at": datetime.datetime.utcnow().isoformat(),
    }
    
    environment = {}
    if machine_version:
        environment = {
            "comfyui_version": machine_version.comfyui_version,
            "gpu": machine.gpu,
            "docker_command_steps": machine_version.docker_command_steps,
            "max_containers": machine_version.concurrency_limit,
            "install_custom_node_with_gpu": machine_version.install_custom_node_with_gpu,
            "run_timeout": machine_version.run_timeout,
            "scaledown_window": machine_version.idle_timeout,
            "extra_docker_commands": machine_version.extra_docker_commands,
            "base_docker_image": machine_version.base_docker_image,
            "python_version": machine_version.python_version,
            "extra_args": machine_version.extra_args,
            "prestart_command": machine_version.prestart_command,
            "min_containers": machine_version.keep_warm,
            "machine_hash": getattr(machine_version, 'machine_hash', None),
            "disable_metadata": machine_version.disable_metadata,
            "allow_concurrent_inputs": machine_version.allow_concurrent_inputs,
            "machine_builder_version": machine_version.machine_builder_version,
            "version": machine_version.version,
        }
    
    base_machine_data["environment"] = environment
    
    # Convert any UUIDs to strings
    for key, value in base_machine_data.items():
        if isinstance(value, uuid.UUID):
            base_machine_data[key] = str(value)
    
    return JSONResponse(content=base_machine_data)


@router.post("/machine/import")
async def import_machine(
    request: Request,
    machine_data: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    try:
        # Ensure we always have a reference for later checks
        machine_version = None

        required_fields = ["name", "type"]
        for field in required_fields:
            if field not in machine_data:
                raise HTTPException(
                    status_code=400,
                    detail=f"Missing required field: {field}"
                )
        
        existing_query = select(Machine).where(
            Machine.name == machine_data["name"],
            Machine.deleted == False
        ).apply_org_check_by_type(Machine, request)
        
        result = await db.execute(existing_query)
        existing_machine = result.scalar_one_or_none()
        
        if existing_machine:
            timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            machine_data["name"] = f"{machine_data['name']}_imported_{timestamp}"
        
        environment = machine_data.get("environment", {})
        
        comfyui_version = environment.get("comfyui_version") or machine_data.get("comfyui_version")
        
        machine_create_data = {
            "id": uuid.uuid4(),
            "name": machine_data["name"],
            "type": machine_data.get("type", "classic"),
            "endpoint": (
                "not-ready"
                if machine_data.get("type") == "comfy-deploy-serverless"
                else "http://127.0.0.1:8188"
            ),
            "user_id": request.state.current_user["user_id"],
            "org_id": request.state.current_user.get("org_id"),
            "created_at": func.now(),
            "updated_at": func.now(),
        }
        
        shared_machine_data = {
            "comfyui_version": comfyui_version,
            "docker_command_steps": environment.get("docker_command_steps") or machine_data.get("docker_command_steps"),
            "concurrency_limit": environment.get("max_containers") or machine_data.get("concurrency_limit", 2),
            "install_custom_node_with_gpu": environment.get("install_custom_node_with_gpu") or machine_data.get("install_custom_node_with_gpu", False),
            "run_timeout": environment.get("run_timeout") or machine_data.get("run_timeout", 300),
            "idle_timeout": environment.get("scaledown_window") or machine_data.get("idle_timeout", 60),
            "extra_docker_commands": environment.get("extra_docker_commands") or machine_data.get("extra_docker_commands"),
            "allow_concurrent_inputs": environment.get("allow_concurrent_inputs") or machine_data.get("allow_concurrent_inputs", 1),
            "machine_builder_version": environment.get("machine_builder_version") or machine_data.get("machine_builder_version", "4"),
            "keep_warm": environment.get("min_containers") or machine_data.get("keep_warm", 0),
            "base_docker_image": environment.get("base_docker_image") or machine_data.get("base_docker_image"),
            "python_version": environment.get("python_version") or machine_data.get("python_version", "3.11"),
            "extra_args": environment.get("extra_args") or machine_data.get("extra_args"),
            "prestart_command": environment.get("prestart_command") or machine_data.get("prestart_command"),
            "disable_metadata": environment.get("disable_metadata") or machine_data.get("disable_metadata", True),
            "gpu": environment.get("gpu") or machine_data.get("gpu"),
        }
        
        new_machine = Machine(**machine_create_data, **shared_machine_data)
        db.add(new_machine)
        await db.flush()
        
        if machine_data.get("type") == "comfy-deploy-serverless":
            if comfyui_version:
                version_data = {
                    "id": uuid.uuid4(),
                    "machine_id": new_machine.id,
                    "version": 1,
                    "user_id": request.state.current_user["user_id"],

                    # Required timestamp columns
                    "created_at": func.now(),
                    "updated_at": func.now(),
                    # Ensure status column has a value
                    "status": "ready",
                    
                    **shared_machine_data,
                }
                
                machine_version = MachineVersion(**version_data)
                db.add(machine_version)
                await db.flush()
                
                new_machine.machine_version_id = machine_version.id
        
        await db.commit()
        
        if new_machine.type == "comfy-deploy-serverless" and machine_version:
            current_endpoint = str(request.base_url).rstrip("/")
            volumes = await retrieve_model_volumes(request, db)
            # Build DepsBody for docker command generation
            deps_body = DepsBody(
                docker_command_steps=environment.get("docker_command_steps") or machine_data.get("docker_command_steps"),
                comfyui_version=comfyui_version,
                extra_docker_commands=environment.get("extra_docker_commands") or machine_data.get("extra_docker_commands"),
            )

            docker_commands = await generate_all_docker_commands(deps_body)
            machine_token = generate_machine_token(request.state.current_user["user_id"], request.state.current_user.get("org_id"))
            secrets = await get_machine_secrets(db=db, machine_id=new_machine.id)
            
            params = BuildMachineItem(
                machine_id=str(new_machine.id),
                name=str(new_machine.id),
                cd_callback_url=f"{current_endpoint}/api/machine-built",
                callback_url=f"{current_endpoint}/api",
                gpu_event_callback_url=f"{current_endpoint}/api/gpu_event",
                models=new_machine.models,
                gpu=new_machine.gpu,
                model_volume_name=volumes[0]["volume_name"] if volumes else "default",
                run_timeout=machine_version.run_timeout,
                idle_timeout=machine_version.idle_timeout,
                auth_token=machine_token,
                ws_timeout=new_machine.ws_timeout,
                concurrency_limit=machine_version.concurrency_limit,
                allow_concurrent_inputs=machine_version.allow_concurrent_inputs,
                legacy_mode=new_machine.legacy_mode,
                install_custom_node_with_gpu=machine_version.install_custom_node_with_gpu,
                allow_background_volume_commits=new_machine.allow_background_volume_commits,
                retrieve_static_assets=new_machine.retrieve_static_assets,
                skip_static_assets=True,
                docker_commands=docker_commands.model_dump()["docker_commands"] if docker_commands else [],
                machine_builder_version=machine_version.machine_builder_version,
                base_docker_image=machine_version.base_docker_image,
                python_version=machine_version.python_version,
                prestart_command=machine_version.prestart_command,
                extra_args=machine_version.extra_args,
                machine_version_id=str(machine_version.id),
                machine_hash=hash_machine_dependencies(docker_commands) if docker_commands else None,
                disable_metadata=machine_version.disable_metadata,
                secrets=secrets,
                cpu_request=new_machine.cpu_request,
                cpu_limit=new_machine.cpu_limit,
                memory_request=new_machine.memory_request,
                memory_limit=new_machine.memory_limit
            )
            
            background_tasks.add_task(build_logic, params)
        
        return JSONResponse(content={
            "id": str(new_machine.id),
            "name": new_machine.name,
            "message": "Machine imported successfully"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error importing machine: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to import machine: {str(e)}"
        )


class ErrorLogEntry(BaseModel):
    timestamp: float
    message: str
    

@router.post("/machine-version/{version_id}/error-start-logs")
async def add_error_logs(
    request: Request,
    version_id: UUID,
    error_start_logs: List[ErrorLogEntry]
):
    try:
        # Convert logs to format expected by Redis
        log_entries = []
        for log in error_start_logs:
            log_entries.append({
                "timestamp": log.timestamp,
                "logs": log.message,
            })

        # Insert logs into Redis stream
        await redis.set(f"error_start_log:{str(version_id)}", json.dumps(log_entries))
        await redis.expire(f"error_start_log:{str(version_id)}", 14400)
        
        return JSONResponse(content={"message": "Error logs added successfully"})

    except Exception as e:
        logger.error(f"Error adding error logs: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to add error logs: {str(e)}"
        )
        
@router.get("/machine-version/{version_id}/error-start-logs")
async def get_error_logs(
    request: Request,
    version_id: str,
):
    try:
        # Get logs from Redis stream
        logs = await redis.get(f"error_start_log:{str(version_id)}")
        return JSONResponse(content={"logs": json.loads(logs)})
    except Exception as e:
        logger.error(f"Error getting error logs: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get error logs: {str(e)}"
        )