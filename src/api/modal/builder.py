from typing import Optional, Dict, List, Any

from api.database import get_db_context
from sqlalchemy import func, update, select
from api.routes.internal import insert_log_entry_to_redis, ensure_redis_stream_expires
from api.models import Deployment, Machine, MachineVersion
from api.routes.utils import select, check_admin_org
from pydantic import BaseModel, Field, field_validator
from fastapi import (
    HTTPException,
    Query,
)
from fastapi.responses import JSONResponse
import ast
import os
from enum import Enum
import json
import time
import asyncio
import logging
import modal
from fastapi import APIRouter
import tempfile
from api.database import get_db
import shutil

from fastapi import BackgroundTasks
import logging
from typing import List, Optional

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
import datetime as dt

machine_id_websocket_dict = {}
machine_id_status = {}
# from asyncio import Queue

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Machine"], prefix="/machine")

public_model_volume_name = os.environ.get(
    "PUBLIC_MODEL_VOLUME_NAME", os.environ.get("SHARED_MODEL_VOLUME_NAME", "local")
)
app_env = os.environ.get("APP_ENV", "production")


change_log_dict = {
    "0.1.2": "Initial release.",
    "0.1.3": "Fixed bugs related to websocket handling related to bytes and text.",
    "0.1.4": "Added back legacy comfy manager models install",
    "0.1.5": "Private models are mounted onto /comfyui/models instead of symlinking",
    "0.1.6": "Private models are back to symlinking",
    "0.1.7": "Extra model paths add ultralytics_bbox and ultralytics_segm, Added options to change concurrency limit and allow concurrent inputs",
    "0.1.8": "GPU usage stats",
    "0.1.9": "Inpainting model support",
    "0.1.10": "function call id, send on function run",
    "0.1.11": "Build dependencies on GPU",
    "0.1.12": "Webhook logs",
    "0.1.13": "fix: GPU build dependencies fully dependent on config",
    "0.2.0": "Workspace session, refactor machine support for custom inputs\nBreaking change: machine bundled images will not work due to the storage and inputs folder, please upload using workspace.",
    "0.2.1": "fix: building with GPU",
    "0.2.2": "fix: remove pydantic locked version\nfix: install fastapi version to 0.109.0",
    "0.2.3": "fix: log not showing up with consecutive run",
    "0.2.4": "fix: issues causing not-started runs in 0.2.3",
    "0.2.5": "fix: timezone issues",
    "0.2.6": "feat: change base docker image",
    "0.3.0": "feat: v3",
    "0.4.0": "feat: v4",
    # Add future version changes here
}
BUILDER_VERSION = list(change_log_dict.keys())[-1]


@router.get("/version")
def read_version(machine_builder_version: int = Query(None)):
    if machine_builder_version is not None and machine_builder_version >= 4:
        # Return the latest 0.4.x version
        version = max(v for v in change_log_dict.keys() if v.startswith("0.4"))
    else:
        # Return the latest 0.3.x version
        version = max(v for v in change_log_dict.keys() if v.startswith("0.3"))

    return {
        "version": version,
        "changelog": change_log_dict.get(version, "No change log available"),
    }


class GitCustomNodes(BaseModel):
    hash: str
    disabled: bool
    pip: Optional[List[str]] = None


class FileCustomNodes(BaseModel):
    filename: str
    disabled: bool


class Snapshot(BaseModel):
    comfyui: str
    git_custom_nodes: Dict[str, GitCustomNodes]
    file_custom_nodes: List[FileCustomNodes]


class Model(BaseModel):
    name: str
    type: str
    base: str
    save_path: str
    description: str
    reference: str
    filename: str
    url: str


class GPUType(str, Enum):
    CPU = "CPU"
    T4 = "T4"
    A10G = "A10G"
    L40S = "L40S"
    A100 = "A100"
    A100_80GB = "A100-80GB"
    H100 = "H100"
    L4 = "L4"
    H200 = "H200"
    B200 = "B200"


class BuildMachineItem(BaseModel):
    machine_id: str
    name: str
    auth_token: str
    snapshot: Optional[Snapshot] = None
    models: Optional[List[Model]] = None
    callback_url: str
    cd_callback_url: str
    gpu_event_callback_url: str
    model_volume_name: str
    run_timeout: Optional[int] = Field(default=60 * 5)
    idle_timeout: Optional[int] = Field(default=60)
    ws_timeout: Optional[int] = Field(default=2)
    legacy_mode: Optional[bool] = Field(default=False)
    install_custom_node_with_gpu: Optional[bool] = Field(default=False)
    gpu: GPUType = Field(default=GPUType.T4)
    concurrency_limit: Optional[int] = Field(default=1)
    allow_concurrent_inputs: Optional[int] = Field(default=50)
    deps: Optional[Any] = None
    docker_commands: Optional[List[List[str]]] = None
    machine_builder_version: Optional[str] = "2"
    allow_background_volume_commits: Optional[bool] = Field(default=False)
    skip_static_assets: Optional[bool] = Field(default=False)
    retrieve_static_assets: Optional[bool] = Field(default=False)
    base_docker_image: Optional[str] = Field(default="")
    python_version: Optional[str] = Field(default="")
    prestart_command: Optional[str] = Field(default="")
    extra_args: Optional[str] = Field(default="")
    modal_app_id: Optional[str] = None
    machine_version_id: Optional[str] = None
    machine_hash: Optional[str] = None
    modal_image_id: Optional[str] = None
    is_deployment: Optional[bool] = False
    environment: Optional[str] = None
    disable_metadata: Optional[bool] = None
    secrets: Optional[Dict] = None
    # CPU/MEMORY resource requests/limits
    cpu_request: Optional[float] = None
    cpu_limit: Optional[float] = None
    memory_request: Optional[int] = None
    memory_limit: Optional[int] = None
    models_to_cache: Optional[List[str]] = Field(default_factory=list)
    enable_gpu_memory_snapshot: Optional[bool] = False
    @field_validator("gpu")
    @classmethod
    def check_gpu(cls, value):
        if not isinstance(value, GPUType):
            try:
                return GPUType(value)
            except ValueError:
                raise ValueError(
                    f"Invalid GPU option. Choose from: {', '.join(GPUType.__members__)}"
                )
        return value


class KeepWarmBody(BaseModel):
    warm_pool_size: int = 1
    gpu: Optional[GPUType] = Field(default=None)


# Keep warm for workspace app
# Internal operations: product code calls these functions directly, never over
# HTTP, so the HTTP routes are restricted to the admin org.
@router.post("/modal/{app_name}/keep-warm", dependencies=[Depends(check_admin_org)])
def set_machine_always_on(app_name: str, body: KeepWarmBody):
    try:
        model = modal.Cls.lookup(app_name, "ComfyDeployRunner")
        print("Keep warm start", body)
        if body.gpu:
            gpu = body.gpu.value if body.gpu != GPUType.CPU else None
            print("Keep warm start", gpu)
            model.with_options(gpu=gpu)(gpu=body.gpu.value).keep_warm(
                body.warm_pool_size
            )
        else:
            model().keep_warm(body.warm_pool_size)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error keep machine warm {str(e)}")

    print("Keep warm succuss", model)
    return {"status": "success"}


class CancelFunctionBody(BaseModel):
    run_id: Optional[str] = None
    function_id: str


@router.post("/modal/cancel-function", dependencies=[Depends(check_admin_org)])
def cancel_run(body: CancelFunctionBody):
    try:
        a = modal.functions.FunctionCall.from_id(body.function_id)
        # print(a)
        a.cancel()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error cancel function {str(e)}")
    return {"status": "success"}


@router.post("/create", dependencies=[Depends(check_admin_org)])
async def create_machine(
    item: BuildMachineItem,
    background_tasks: BackgroundTasks,
):
    if item.machine_id in machine_id_status and machine_id_status[item.machine_id]:
        return JSONResponse(
            status_code=400, content={"error": "Build already in progress."}
        )

    # Run the building logic in a separate thread
    # future = executor.submit(build_logic, item)
    background_tasks.add_task(build_logic, item)

    return JSONResponse(
        status_code=200,
        content={
            "message": "Build Queued",
            "build_machine_instance_id": "not-applicable",
        },
    )


class StopAppItem(BaseModel):
    machine_id: str


def find_app_id(app_list, app_name):
    for app in app_list:
        if app["Name"] == app_name:
            return app["App ID"]
    return None


# Initialize the logs cache
machine_logs_cache = {}
import_failed_logs_cache = {}


async def update_machine_version_build_time(
    db: AsyncSession,
    machine_version_id: str,
    status: str,
    build_log: str,
    build_time: Optional[float] = None,
) -> None:
    if not machine_version_id:
        return

    machine_version = await db.execute(
        select(MachineVersion).where(MachineVersion.id == machine_version_id)
    )
    machine_version = machine_version.scalar_one_or_none()

    if machine_version:
        if build_time is not None:
            # Calculate new updated_at by adding build_time to created_at
            new_updated_at = machine_version.created_at + dt.timedelta(
                seconds=build_time
            )
        else:
            # If no build time provided, use current time
            new_updated_at = dt.datetime.now()

        await db.execute(
            update(MachineVersion)
            .where(MachineVersion.id == machine_version_id)
            .values(updated_at=new_updated_at, status=status, build_log=build_log)
        )


def render_config_source(config: dict) -> str:
    """Render the machine config as Python source that is spliced into my_app.py.

    Values include user-controlled strings (machine name, prestart_command,
    extra_args, ...). repr() always emits a correctly escaped literal, and the
    literal_eval round-trip guarantees the generated source is pure data, so a
    value can never break out of its string and run code during `modal deploy`.
    """
    body = "{\n" + "".join(f"    {key!r}: {value!r},\n" for key, value in config.items()) + "}"
    if ast.literal_eval(body) != config:
        raise ValueError("Machine config does not round-trip as a Python literal")
    return f"config = {body}"


async def build_logic(item: BuildMachineItem):
    # Deploy to modal
    with tempfile.TemporaryDirectory() as temp_dir:
        folder_path = temp_dir

        folder_path = f"{folder_path}/{item.machine_id}"
        machine_id_status[item.machine_id] = True

        version_to_file = item.machine_builder_version  # .replace(".", "_")
        to_modal_deps_version = {"2": None, "3": "2024.04", "4": "2025.06"}
        cp_process = await asyncio.subprocess.create_subprocess_exec(
            "cp",
            "-r",
            f"./src/api/modal/v{version_to_file}",
            folder_path,
        )

        await cp_process.wait()

        pip_modules = set()
        if item.snapshot != None:
            for git_custom_node in item.snapshot.git_custom_nodes.values():
                if git_custom_node.pip:
                    pip_modules.update(git_custom_node.pip)

        # Write the config file
        config = {
            "name": item.name,
            "version_id": str(item.machine_version_id),
            "deploy_test": "False",
            "gpu": item.gpu.value,
            "public_model_volume": public_model_volume_name,
            "private_model_volume": item.model_volume_name,
            "pip": list(pip_modules),
            "run_timeout": item.run_timeout,
            "idle_timeout": 2 if item.idle_timeout < 2 else item.idle_timeout,
            "ws_timeout": item.ws_timeout,
            "machine_id": item.machine_id,
            "auth_token": item.auth_token,
            "gpu_event_callback_url": item.gpu_event_callback_url,
            "cd_callback_url": item.cd_callback_url,
            "legacy_mode": "True" if item.legacy_mode else "False",
            "allow_concurrent_inputs": item.allow_concurrent_inputs,
            "concurrency_limit": item.concurrency_limit,
            "install_custom_node_with_gpu": "True"
            if item.install_custom_node_with_gpu
            else "False",
            "allow_background_volume_commits": "True"
            if item.allow_background_volume_commits
            else "False",
            "skip_static_assets": "True" if item.skip_static_assets else "False",
            "base_docker_image": item.base_docker_image,
            "python_version": item.python_version,
            "prestart_command": item.prestart_command,
            "extra_args": item.extra_args,
            "machine_hash": item.machine_hash,
            "modal_image_id": item.modal_image_id,
            "environment": item.environment,
            "disable_metadata": "True" if item.disable_metadata else "False",
            "secrets": item.secrets,
            "cpu_request": item.cpu_request,
            "cpu_limit": item.cpu_limit,
            "memory_request": item.memory_request,
            "memory_limit": item.memory_limit,
            "models_to_cache": item.models_to_cache,
            "enable_gpu_memory_snapshot": item.enable_gpu_memory_snapshot
        }

        # if os.environ.get("TAILSCALE_AUTHKEY", None) is not None:
        #     config["TAILSCALE_AUTHKEY"] = os.environ.get("TAILSCALE_AUTHKEY", None)

        if item.deps != None:
            config["deps"] = item.deps

        if item.docker_commands != None:
            config["docker_commands"] = item.docker_commands

        # Instead of creating cd_config.py, modify my_app.py directly
        my_app_path = f"{folder_path}/my_app.py"
        
        # Read the original my_app.py content
        with open(my_app_path, "r") as f:
            my_app_content = f.read()
        
        try:
            config_source = render_config_source(config)
        except (ValueError, SyntaxError) as e:
            logger.error(f"Refusing to build machine {item.machine_id}: invalid config ({e})")
            machine_id_status[item.machine_id] = False
            if not item.is_deployment:
                build_log = json.dumps(
                    [{"logs": "Invalid machine configuration.", "timestamp": time.time()}]
                )
                async with get_db_context() as db:
                    await update_machine_version_build_time(
                        db=db,
                        machine_version_id=item.machine_version_id,
                        status="error",
                        build_log=build_log,
                    )
                    await db.execute(
                        update(Machine)
                        .where(Machine.id == item.machine_id)
                        .values(status="error", build_log=build_log)
                    )
                    await db.commit()
            return

        # Replace the import statement with the actual config
        modified_content = my_app_content.replace(
            "from cd_config import config",
            config_source
        )
        
        # Write the modified content back to my_app.py
        with open(my_app_path, "w") as f:
            f.write(modified_content)
        
        print("Debug: Modified my_app.py with inline config")

        # os.chdir(folder_path)
        # process = subprocess.Popen(f"modal deploy {folder_path}/app.py", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True)
        final_env = {
            **os.environ,
        }
        if to_modal_deps_version[version_to_file]:
            final_env["MODAL_IMAGE_BUILDER_VERSION"] = to_modal_deps_version[
                version_to_file
            ]

        process = await asyncio.subprocess.create_subprocess_shell(
            "modal deploy my_app.py",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=folder_path,
            env=final_env,
        )

        url = None

        # if item.machine_id not in machine_logs_cache:
        machine_logs_cache[item.machine_id] = []

        machine_logs = machine_logs_cache[item.machine_id]

        if item.machine_id not in import_failed_logs_cache:
            import_failed_logs_cache[item.machine_id] = []

        import_failed_logs = import_failed_logs_cache[item.machine_id]

        machine_built_time = None

        build_info_queue = asyncio.Queue()

        # Send builder logs to Redis streams keyed by machine_id
        async def send_builder_log(machine_id: str, message: str):
            try:
                log_entry = {
                    "timestamp": time.time(),
                    "logs": message,
                    "level": "builder",
                    "machine_id": machine_id,
                }
                await insert_log_entry_to_redis(str(item.machine_version_id), [log_entry])
            except Exception:
                # Best effort only
                pass

        ttl_ensured = False

        async def read_stream(stream, isStderr, build_info_queue: asyncio.Queue):
            nonlocal ttl_ensured
            while True:
                line = await stream.readline()
                if line:
                    l = line.decode("utf-8").strip()

                    if l == "":
                        continue

                    await send_builder_log(item.machine_id, l)
                    if not ttl_ensured:
                        try:
                            await ensure_redis_stream_expires(str(item.machine_version_id))
                        except Exception:
                            # Non-critical; continue streaming logs
                            pass
                        ttl_ensured = True

                    if not isStderr:
                        logger.info(l)
                        machine_logs.append({"logs": l, "timestamp": time.time()})

                        if "(IMPORT FAILED)" in l:
                            import_failed_logs.append(
                                {"logs": l, "timestamp": time.time()}
                            )

                        if "App deployed in" in l and "🎉" in l:
                            time_str = l.split("in ")[1].split("s!")[0]
                            machine_built_time = float(time_str)

                        if l.startswith("APPID="):
                            app_id = l.split("=")[1].strip()
                            await build_info_queue.put(
                                {"type": "APPID", "value": app_id}
                            )

                        if (
                            "Created comfyui_api =>" in l
                            or "Created ComfyDeployRunner.api =>" in l
                            or "Created web function comfyui_api =>" in l
                        ) or (
                            (l.startswith("https://") or l.startswith("│"))
                            and l.endswith(".modal.run")
                        ):
                            if (
                                "Created comfyui_api =>" in l
                                or "Created ComfyDeployRunner.api =>" in l
                                or "Created web function comfyui_api =>" in l
                            ):
                                url = l.split("=>")[1].strip()
                            # making sure it is a url
                            elif "comfyui-api" in l:
                                # Some case it only prints the url on a blank line
                                if l.startswith("│"):
                                    url = l.split("│")[1].strip()
                                else:
                                    url = l

                            if url:
                                machine_logs.append(
                                    {
                                        "logs": f"App image built, url: {url}",
                                        "timestamp": time.time(),
                                    }
                                )

                                # await url_queue.put(url)
                                await build_info_queue.put(
                                    {"type": "URL", "value": url}
                                )

                    else:
                        # is error
                        logger.error(l)
                        machine_logs.append({"logs": l, "timestamp": time.time()})
                else:
                    break

            # Nothing to flush when using Redis

        stdout_task = asyncio.create_task(
            read_stream(process.stdout, False, build_info_queue)
        )
        stderr_task = asyncio.create_task(
            read_stream(process.stderr, True, build_info_queue)
        )

        await asyncio.wait([stdout_task, stderr_task])

        # Wait for the subprocess to finish
        await process.wait()

        url = None
        app_id = None
        while not build_info_queue.empty():
            info = await build_info_queue.get()
            if info["type"] == "APPID":
                app_id = info["value"]
            elif info["type"] == "URL":
                url = info["value"]


        if item.modal_app_id:
            app_id = item.modal_app_id
        else:
            try:
                app = await modal.App.lookup.aio(item.name)
                # print("my legit app id", app.app_id)

                if app.app_id:
                    app_id = app.app_id

            except Exception as e:
                logger.error("App ID not found in the response.")
                print("error", e)

    async def clear_machine_logs_and_remove_folder(machine_id, folder_path):
        # Close the ws connection and also pop the item
        if (
            item.machine_id in machine_id_websocket_dict
            and machine_id_websocket_dict[item.machine_id] is not None
        ):
            await machine_id_websocket_dict[item.machine_id].close()

        if item.machine_id in machine_id_websocket_dict:
            machine_id_websocket_dict.pop(item.machine_id)

        if item.machine_id in machine_id_status:
            machine_id_status[item.machine_id] = False

        if machine_id in machine_logs_cache:
            del machine_logs_cache[machine_id]

        if machine_id in import_failed_logs_cache:
            del import_failed_logs_cache[machine_id]

        # Remove the copied folder
        if os.path.exists(folder_path):
            shutil.rmtree(folder_path)
            logger.info(f"Removed folder: {folder_path}")

    # Check for errors
    if process.returncode != 0:
        logger.info("An error occurred.")
        machine_logs.append(
            {"logs": "Unable to build the app image.", "timestamp": time.time()}
        )

        if not item.is_deployment:
        # Direct database update instead of HTTP request
            async with get_db_context() as db:
                await update_machine_version_build_time(
                    db=db,
                    machine_version_id=item.machine_version_id,
                    status="error",
                    build_log=json.dumps(machine_logs),
                )

                update_stmt = (
                    update(Machine)
                    .where(Machine.id == item.machine_id)
                    .values(
                        status="error",
                        build_log=json.dumps(machine_logs),
                        import_failed_logs=json.dumps(import_failed_logs)
                        if not item.skip_static_assets
                        else None,
                    )
                    .returning(Machine)
                )
                result = await db.execute(update_stmt)
                await db.commit()
                machine = result.scalar_one()
                await db.refresh(machine)

        await clear_machine_logs_and_remove_folder(item.machine_id, folder_path)
        return

    if not item.is_deployment:
        async with get_db_context() as db:
            # Update machine version build time
            await update_machine_version_build_time(
                db=db,
                machine_version_id=item.machine_version_id,
                build_time=machine_built_time,
                status="ready",
                build_log=json.dumps(machine_logs),
            )
            
            print("machine_logs", machine_logs)

            # Update machine status
            update_stmt = (
                update(Machine)
                .where(Machine.id == item.machine_id)
                .values(
                    status="ready",
                    # endpoint=url,
                    machine_version=BUILDER_VERSION,
                    build_log=json.dumps(machine_logs),
                    modal_app_id=app_id,
                    import_failed_logs=json.dumps(import_failed_logs)
                    if not item.skip_static_assets
                    else None,
                )
                .returning(Machine)
            )
            result = await db.execute(update_stmt)
            await db.commit()
            machine = result.scalar_one()
            await db.refresh(machine)

            if machine.keep_warm > 0:
                set_machine_always_on(
                    machine.id,
                    KeepWarmBody(warm_pool_size=machine.keep_warm, gpu=machine.gpu),
                )
        
    if item.is_deployment:
        async with get_db_context() as db:
            await db.execute(
                update(Deployment)
                # This will be deployment id
                .where(Deployment.id == item.name)
                .values(
                    activated_at=func.now(),
                    modal_app_id=app_id,
                )
            )

    await clear_machine_logs_and_remove_folder(item.machine_id, folder_path)
