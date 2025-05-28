# This will be str replaced
from cd_config import config

import gc
import logging
import threading
import modal
import time
from modal import (
    Image,
    App,
    enter,
    exit,
)
from typing import Optional, cast
import json
import urllib.request
import urllib.parse
from pydantic import BaseModel, Field
from fastapi import HTTPException, WebSocket
import modal
import os
from datetime import datetime, timezone
import aiohttp
from aiohttp import TCPConnector
import os
import uuid
from enum import Enum
import asyncio
from collections import deque
from contextlib import contextmanager
from io import StringIO

public_model_volume = modal.Volume.from_name(config["public_model_volume"], create_if_missing=True)
private_volume = modal.Volume.from_name(config["private_model_volume"], create_if_missing=True)

PUBLIC_BASEMODEL_DIR = "/public_models"
PRIVATE_BASEMODEL_DIR_SYM = "/private_models"

volumes = {
    PUBLIC_BASEMODEL_DIR: public_model_volume,
    PRIVATE_BASEMODEL_DIR_SYM: private_volume,
}

# NOTE: long/specific name to avoid conflicts in the ComfyUI model directory
# Required because we can only symlink to the volume path, which doesn't exist during image build
INPUT_DIR = f"{PRIVATE_BASEMODEL_DIR_SYM}/comfydeploy-comfyui-inputs"
OUTPUT_DIR = f"{PRIVATE_BASEMODEL_DIR_SYM}/comfydeploy-comfyui-outputs"

logger = logging.getLogger(__name__)

current_directory = os.path.dirname(os.path.realpath(__file__))

deploy_test = config["deploy_test"] == "True"

print("Builder Version: 4")
print("Builder Deps: ", os.getenv("MODAL_IMAGE_BUILDER_VERSION"))
print("Modal Version: ", modal.__version__)

app = App(name=config["name"])

skip_static_assets = config["skip_static_assets"]

if os.getenv("MODAL_ENVIRONMENT") == "dev":
    machine_name_for_label = config["name"][:14]
else:
    machine_name_for_label = config["name"]

compile_with_gpu = config["install_custom_node_with_gpu"] == "True"
final_gpu_param = config["gpu"] if config["gpu"] != "CPU" else None
gpu_param = final_gpu_param if compile_with_gpu else None
# deps = config["deps"]

python_version = (
    config["python_version"]
    if config["python_version"] is not None and config["python_version"] != ""
    else "3.11"
)
prestart_command = config["prestart_command"]
global_extra_args = config["extra_args"]
modal_image_id = config["modal_image_id"]
disable_metadata = config["disable_metadata"] == "True"

print("disable_metadata: ", disable_metadata)
secrets = config["secrets"]

# --- CPU/MEMORY resource config ---
cpu_request = config["cpu_request"]
cpu_limit = config["cpu_limit"]
memory_request = config["memory_request"]
memory_limit = config["memory_limit"]

cpu = (cpu_request, cpu_limit) if cpu_request and cpu_limit else cpu_request or None
memory = (memory_request, memory_limit) if memory_request and memory_limit else memory_request or None
print(f"CPU: {cpu}, Memory: {memory}")
# --- END CPU/MEMORY resource config ---

logger = logging.getLogger(__name__)

# print(base_docker_image, python_version, prestart_command, global_extra_args)

dockerfile_image = None

if modal_image_id is not None:
    print("using modal image id", modal_image_id)
    dockerfile_image = modal.Image.from_id(modal_image_id)
else:
    dockerfile_image = modal.Image.debian_slim(python_version=python_version)

    base_docker_image = config["base_docker_image"]
    if base_docker_image is not None and base_docker_image != "":
        dockerfile_image = modal.Image.from_registry(
            base_docker_image, add_python=python_version
        )

    docker_commands = config["docker_commands"]

    # Install all custom nodes
    if docker_commands is not None:
        # print("docker_commands: ", docker_commands)
        for commands in docker_commands:
            dockerfile_image = dockerfile_image.dockerfile_commands(
                commands,
                gpu=gpu_param,
            )

    dockerfile_image = dockerfile_image.run_commands(
        [
            f"rm -rf {PRIVATE_BASEMODEL_DIR_SYM} /comfyui/models {PUBLIC_BASEMODEL_DIR}",
            f"ln -s {PRIVATE_BASEMODEL_DIR_SYM} /comfyui/models",
        ]
    )

dockerfile_image = dockerfile_image.add_local_file(
    "./data/extra_model_paths.yaml", "/comfyui/extra_model_paths.yaml"
)


# Time to wait between API check attempts in milliseconds
COMFY_API_AVAILABLE_INTERVAL_MS = 50
# Maximum number of API check attempts
COMFY_API_AVAILABLE_MAX_RETRIES = 1000
# Time to wait between poll attempts in milliseconds
COMFY_POLLING_INTERVAL_MS = 250
# Maximum number of poll attempts
COMFY_POLLING_MAX_RETRIES = 1000
# Host where ComfyUI is running
COMFY_HOST = "127.0.0.1:8188"

input_directory = "/private_models/input"
output_directory = "/private_models/output"
temp_directory = "/private_models"

extra_model_path_config = "/comfyui/extra_model_paths.yaml"

def comfyui_cmd(
    cpu: bool = False,
    extra_args: Optional[str] = None,
    mountIO: bool = False,
    dontCreateFolders: bool = False,
):
    if not dontCreateFolders:
        if not os.path.exists(input_directory):
            os.makedirs(input_directory)
        if not os.path.exists(output_directory):
            os.makedirs(output_directory)
        if not os.path.exists(temp_directory):
            os.makedirs(temp_directory)

    cmd = f"python main.py --dont-print-server --enable-cors-header --listen --port 8188 --input-directory {input_directory} --preview-method auto"
    if mountIO:
        cmd += (
            f" --output-directory {output_directory} --temp-directory {temp_directory}"
        )
    if cpu:
        cmd += " --cpu"
    if extra_args is not None:
        cmd += f" {extra_args}"

    if global_extra_args is not None and global_extra_args != "":
        cmd += f" {global_extra_args}"

    if prestart_command is not None and prestart_command != "":
        cmd = f"{prestart_command} && {cmd}"

    cmd += " --disable-metadata" if disable_metadata else ""

    print("Actual file command: ", cmd)

    return cmd


async def interrupt_comfyui():
    print("Interrupting comfyui")
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://{COMFY_HOST}/interrupt") as response:
            # If the response status code is 200, the server is up and running
            if response.status == 200:
                return True
            else:
                return False


async def check_server(url, retries=50, delay=500):
    import aiohttp

    # for i in range(retries):
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    # If the response status code is 200, the server is up and running
                    if response.status == 200:
                        print(f"comfy-modal - API is reachable")
                        return True
        except Exception as e:
            # If an exception occurs, the server may not be ready
            pass

        # Wait for the specified delay before retrying
        await asyncio.sleep(delay / 1000)

    print(
        f"comfy-modal - Failed to connect to server at {url} after {retries} attempts."
    )
    return False


async def check_server_with_log(
    url, retries=50, delay=500, machine_logs=[], last_sent_log_index=-1
):
    import aiohttp

    # for i in range(retries):
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    # If the response status code is 200, the server is up and running
                    if response.status == 200:
                        print(f"comfy-modal - API is reachable")
                        yield f"event: event_update\ndata: {json.dumps({'event': 'comfyui_api_ready'})}\n\n"
                        return

        except Exception as e:
            # If an exception occurs, the server may not be ready
            pass

        if machine_logs and last_sent_log_index != -1:
            while last_sent_log_index < len(machine_logs):
                log = machine_logs[last_sent_log_index]
                if isinstance(log["timestamp"], float):
                    log["timestamp"] = (
                        datetime.utcfromtimestamp(log["timestamp"]).isoformat() + "Z"
                    )
                print(log)
                yield f"event: log_update\ndata: {json.dumps(log)}\n\n"
                last_sent_log_index += 1

        # Wait for the specified delay before retrying
        await asyncio.sleep(delay / 1000)


async def check_server_with_log_2(
    url, retries=50, delay=500, machine_logs=[], last_sent_log_index=-1
):
    import aiohttp

    # for i in range(retries):
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    # If the response status code is 200, the server is up and running
                    if response.status == 200:
                        print(f"comfy-modal - API is reachable")
                        # yield f"event: event_update\ndata: {json.dumps({'event': 'comfyui_api_ready'})}\n\n"
                        return

        except Exception as e:
            # If an exception occurs, the server may not be ready
            pass

        if machine_logs and last_sent_log_index != -1:
            new_logs = machine_logs[last_sent_log_index:]
            for log in new_logs:
                yield log
            last_sent_log_index = len(machine_logs)

        # Wait for the specified delay before retrying
        await asyncio.sleep(delay / 1000)


async def check_status(prompt_id: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"http://{COMFY_HOST}/comfyui-deploy/check-status?prompt_id={prompt_id}"
        ) as response:
            return await response.json()


async def check_ws_status(client_id: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"http://{COMFY_HOST}/comfyui-deploy/check-ws-status?client_id={client_id}"
        ) as response:
            return await response.json()


class Input(BaseModel):
    prompt_id: str
    workflow_api: Optional[dict] = None
    inputs: Optional[dict]
    workflow_api_raw: dict
    status_endpoint: str
    file_upload_endpoint: str
    workflow: Optional[dict] = None
    gpu_event_id: str | None = None


async def queue_workflow_comfy_deploy(data: Input):
    data_str = data.json()
    data_bytes = data_str.encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['auth_token']}",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://{COMFY_HOST}/comfyui-deploy/run", data=data_bytes, headers=headers
        ) as response:
            return await response.json()


class GPUEventType(str, Enum):
    GPU_START = "gpu_start"
    GPU_END = "gpu_end"


environment = config.get("environment", None)

async def sync_report_gpu_event(
    event_id: str | None,
    is_workspace: bool = False,
    gpu: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    session_id: str | None = None,
    custom_timestamp: datetime | None = None,
) -> str:
    import requests
    from pprint import pprint

    event_type = GPUEventType.GPU_START if event_id is None else GPUEventType.GPU_END

    machine_id = config["machine_id"]
    auth_token = config["auth_token"]
    gpu_type = config["gpu"]  # Assuming this is properly set in your config
    gpu_event_callback_url = config["gpu_event_callback_url"]

    # Prepare the headers
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "bypass-tunnel-reminder": "true",
    }

    # Prepare the body with the current timestamp and optional instance_id
    body = {
        "machine_id": machine_id,
        "timestamp": custom_timestamp.isoformat() if custom_timestamp is not None else datetime.now(timezone.utc).isoformat(),
        "gpuType": gpu_type if gpu is None else gpu,
        "eventType": event_type.value,
        "gpu_provider": "modal",
        "event_id": event_id,
        # "is_workspace": is_workspace,
        "user_id": user_id,
        "org_id": org_id,
        "session_id": session_id,
        "modal_function_id": modal.current_function_call_id(),
        "environment": environment,
    }

    # print("body", body, gpu_event_callback_url)

    # Perform the asynchronous POST request
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                gpu_event_callback_url,
                json=body,
                headers=headers,
            ) as response:
                print(f"finished sending gpu event {event_type.value}")

                # Verify the response status
                if response.status != 200:
                    # Handle the error case appropriately; logging, raising an exception, etc.
                    print(f"Error: {response.status}")
                    response_data = (
                        await response.json()
                    )  # Adjust based on API response behavior
                    pprint(response_data)
                    return None
                else:
                    # Optionally process the response data
                    response_data = (
                        await response.json()
                    )  # Adjust based on API response behavior
                    pprint("SUCCESS")
                    pprint(response_data)
                    return response_data["event_id"]
        except aiohttp.ClientError as e:
            print(f"An error occurred during the request: {e}")
            return None


class GPUType(str, Enum):
    T4 = "T4"
    A10G = "A10G"
    L40S = "L40S"
    A100 = "A100"
    A100_80GB = "A100-80GB"
    H100 = "H100"
    L4 = "L4"


class OverrideInput(BaseModel):
    gpu: Optional[GPUType] = Field(default=None)
    cpu_only: Optional[bool] = Field(default=False)
    concurrency_limit: Optional[int] = Field(default=None)
    allow_concurrent_inputs: Optional[int] = Field(default=None)
    timeout: Optional[int] = Field(default=None)
    container_timeout: Optional[int] = Field(default=None)
    container_idle_timeout: Optional[int] = Field(default=None)
    private_volume_name: Optional[str] = Field(default=None)
    mountIO: Optional[bool] = Field(default=False)
    is_workspace: Optional[bool] = Field(default=False)
    user_id: Optional[str] = Field(default=None)
    org_id: Optional[str] = Field(default=None)
    kill: Optional[bool] = Field(default=False)
    extend_timeout: Optional[int] = Field(default=None)


class RequestInput(OverrideInput):
    input: Input


image = Image.debian_slim()

target_image = image if deploy_test else dockerfile_image

run_timeout = config["run_timeout"]
idle_timeout = config["idle_timeout"]
ws_timeout = config["ws_timeout"]


async def send_status_update(
    input: Input,
    status: str,
    gpu_event_id: str | None = None,
    function_id: str | None = None,
):
    print("sending status update", input.status_endpoint, status, function_id)
    async with aiohttp.ClientSession() as session:
        data = {
            "run_id": input.prompt_id,
            "status": status,
            "time": datetime.now(timezone.utc).isoformat(),
            "gpu_event_id": gpu_event_id,
        }
        if function_id is not None:
            data["modal_function_call_id"] = function_id
        async with session.post(
            input.status_endpoint,
            data=json.dumps(data).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "bypass-tunnel-reminder": "true",
                "Authorization": f"Bearer {config['auth_token']}",
            },
        ) as response:
            pass


async def send_log_update(input: Input, log: str):
    async with aiohttp.ClientSession() as session:
        data = {
            "run_id": input.prompt_id,
            "log": log,
        }
        async with session.post(
            input.status_endpoint,
            data=json.dumps(data).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "bypass-tunnel-reminder": "true",
                "Authorization": f"Bearer {config['auth_token']}",
            },
        ) as response:
            pass


import pickle

load = pickle.load

class Empty:
    pass

class Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        #TODO: safe unpickle
        if module.startswith("pytorch_lightning"):
            return Empty
        return super().find_class(module, name)

async def wait_for_server(
    url=f"http://{COMFY_HOST}", delay=50, logs=[], last_sent_log_index=-1
):
    """
    Checks if the API is reachable
    """
    import aiohttp

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    # If the response status code is 200, the server is up and running
                    if response.status == 200:
                        print("API is reachable")
                        # yield f"event: event_update\ndata: {json.dumps({'event': 'comfyui_api_ready'})}\n\n"
                        return

        except Exception as e:
            # If an exception occurs, the server may not be ready
            pass

        if logs and last_sent_log_index != -1:
            while last_sent_log_index < len(logs):
                log = logs[last_sent_log_index]
                if isinstance(log["timestamp"], float):
                    log["timestamp"] = (
                        datetime.utcfromtimestamp(log["timestamp"]).isoformat() + "Z"
                    )
                print(log)
                yield log
                # yield f"event: log_update\ndata: {json.dumps(log)}\n\n"
                last_sent_log_index += 1

        # Wait for the specified delay before retrying
        await asyncio.sleep(delay / 1000)

async def send_log_async(
    update_endpoint: str, session_id: uuid.UUID, machine_id: str, log_message: str
):
    import aiohttp

    async with aiohttp.ClientSession() as client:
        # token = generate_temporary_token("modal")
        token = config["auth_token"]
        async with client.post(
            update_endpoint + "/api/session/callback/log",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "session_id": str(session_id),
                "machine_id": machine_id,
                "log": log_message,
            },
        ) as response:
            pass
            # print("send_log_async", await response.text())


def send_log_entry(
    update_endpoint: str, session_id: uuid.UUID, machine_id: str, log_message: str
):  # noqa: F821
    asyncio.create_task(
        send_log_async(update_endpoint, session_id, machine_id, log_message)
    )


async def check_for_timeout(
    update_endpoint: str, session_id: str
):
    max_retries = 5
    base_delay = 1  # seconds
    try:
        while True:
            retry_count = 0
            while retry_count < max_retries:
                try:
                    async with aiohttp.ClientSession() as client:
                        token = config["auth_token"]
                        async with client.post(
                            update_endpoint + "/api/session/callback/check-timeout",
                            headers={"Authorization": f"Bearer {token}"},
                            json={
                                "session_id": str(session_id),
                            },
                        ) as response:
                            if response.status == 200:
                                data = await response.json()
                                if data.get("continue", True):
                                    await asyncio.sleep(1)
                                else:
                                    send_log_entry(
                                        update_endpoint,
                                        session_id,
                                        config["machine_id"],
                                        "Session closed due to timeout",
                                    )
                                    return
                                break  # Success, break out of retry loop
                            else:
                                logger.warning(f"Non-200 response: {response.status}")
                                retry_count += 1
                                await asyncio.sleep(base_delay * (2 ** (retry_count - 1)))
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(f"Timeout or connection error: {e}, retry {retry_count+1}/{max_retries}")
                    retry_count += 1
                    await asyncio.sleep(base_delay * (2 ** (retry_count - 1)))
                except Exception as e:
                    logger.error(f"Unexpected error in check_for_timeout: {str(e)}")
                    retry_count += 1
                    await asyncio.sleep(base_delay * (2 ** (retry_count - 1)))
                if retry_count >= max_retries:
                    logger.error(f"Max retries reached for check_for_timeout (session {session_id})")
                    break
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info(f"Timeout checker cancelled for session {session_id}")
        raise
    except Exception as e:
        logger.error(f"Error in timeout checker for session {session_id}: {str(e)}")
        raise


async def delete_session(
    update_endpoint: str, session_id: str
):
    try:
        while True:
            async with aiohttp.ClientSession() as client:
                # token = generate_temporary_token(user_id, org_id)
                token = config["auth_token"]
                async with client.delete(
                    update_endpoint + "/api/session/" + str(session_id),
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("continue", True):
                            await asyncio.sleep(1)
                        else:
                            send_log_entry(
                                update_endpoint,
                                session_id,
                                config["machine_id"],
                                "Session closed",
                            )
                            break
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        # Handle cancellation gracefully
        logger.info(f"Timeout checker cancelled for session {session_id}")
        raise
    except Exception as e:
        logger.error(f"Error in timeout checker for session {session_id}: {str(e)}")
        raise

class BaseComfyDeployRunner:
    _is_workspace: bool = False
    _gpu: str | None = None
    _session_id: str | None = None
    
    machine_logs = []
    last_sent_log_index = 0

    cleanup_done = False
    current_function_call_id = None
    status_endpoint = None
    current_input = None

    session_timeout = 0
    start_time = time.time()

    async def timeout_and_exit(self, timeout_seconds: int, soft_exit: bool = False):
        import os

        await asyncio.sleep(timeout_seconds)
        print(f"Exiting container after {timeout_seconds} seconds.")
        # await asyncio.get_event_loop().shutdown_default_executor()  # Gracefully stop the event loop
        print(f"comfy-modal - cleanup")

        await sync_report_gpu_event(
            self.gpu_event_id, self._is_workspace, self._gpu, self.user_id, self.org_id
        )
        self.stdout_task.cancel()
        self.stderr_task.cancel()

        current_function_call_id = (
            self.current_function_call_id
        )  # modal.current_function_call_id()
        print("current_function_call_id", current_function_call_id)
        try:
            print("Interrupting comfyui")
            ok = await interrupt_comfyui()
        except Exception as e:
            print("Issues when interrupting comfyui", e)
            pass

        try:
            modal.functions.FunctionCall.from_id(current_function_call_id).cancel()
        except Exception as e:
            print("Issues when canceling function call", e)
            pass

        print(f"comfy-modal - cleanup done")
        if not soft_exit:
            os._exit(0)
    
    org_id: str = None
    user_id: str = None
    mountIO: bool = False
    volume_name: str = config["private_model_volume"]
    private_volume = None
    timeout: int = None
    # workspace_tunnel: bool = modal.parameter(default=False)
    
    cold_start_queue = deque()
    log_queues = deque()
    gpu_event_id = None
    
    async def process_log_queue(self):
        try:
            while True:
                if len(self.cold_start_queue) > 0 and self.status_endpoint:
                    logs = list(self.cold_start_queue)
                    print("sending log batch", len(logs))
                    await self.send_log_batch(
                        run_id=self.current_input.prompt_id
                        if self.current_input
                        else None,
                        session_id=self._session_id,
                        logs=logs,
                        status_endpoint=self.status_endpoint,
                    )
                    self.cold_start_queue.clear()

                if self.log_queues and len(self.log_queues) > 0:
                    # Get the first item (prompt_id and queue_data) from the log_queues
                    logs = list(self.log_queues[0]["logs"])
                    input = self.log_queues[0]["current_input"]
                    if logs:
                        await self.send_log_batch(
                            run_id=input.prompt_id,
                            session_id=self._session_id,
                            logs=logs,
                            status_endpoint=input.status_endpoint,
                        )
                        self.log_queues.clear()

                await asyncio.sleep(1)
        except Exception as e:
            print("Error in process_log_queue", e)

    async def send_log_batch(self, run_id, session_id, logs, status_endpoint):
        if not logs:
            return  # Don't send empty log batches

        async with aiohttp.ClientSession() as session:
            data = json.dumps(
                {
                    "run_id": run_id,
                    "session_id": session_id,
                    "machine_id": config["machine_id"],
                    "logs": logs,
                }
            ).encode("utf-8")
            print("sending log batch", run_id, session_id, status_endpoint)
            try:
                async with session.post(
                    status_endpoint,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "bypass-tunnel-reminder": "true",
                        "Authorization": f"Bearer {config['auth_token']}",
                    },
                ) as response:
                    if response.status != 200:
                        print(
                            f"Failed to send logs for run_id {run_id}. Status: {response.status}"
                        )
            except Exception as e:
                print(f"Error sending logs for run_id {run_id}: {str(e)}")

    @modal.method()
    async def streaming(
        self, input: Input, kill: bool = False, extend_timeout: Optional[int] = None
    ):
        if isinstance(input, dict):
            input = Input(**input)

        try:
            print("post_run_streaming")

            # print(input)

            yield f"event: event_update\ndata: {json.dumps({'event': 'function_call_id', 'data': modal.current_function_call_id()})}\n\n"

            self.current_function_call_id = modal.current_function_call_id()

            asyncio.create_task(send_status_update(input, "queued", self.gpu_event_id))

            if extend_timeout is not None:
                remaining_time = self.timeout - (time.time() - self.timeout_start_time)
                self.timeout_task.cancel()
                self.timeout_task = asyncio.create_task(
                    self.timeout_and_exit(remaining_time + extend_timeout)
                )
                raise Exception("Extended Timeout")

            if kill:
                asyncio.create_task(self.timeout_and_exit(0))
                raise Exception("Killed")

            yield f"event: event_update\ndata: {json.dumps({'event': 'gpu_ready'})}\n\n"
            yield f"event: event_update\ndata: {json.dumps({'event': 'gpu_event_id', 'data': self.gpu_event_id})}\n\n"

            # ok = await check_server(
            #     f"http://{COMFY_HOST}",
            #     COMFY_API_AVAILABLE_MAX_RETRIES,
            #     COMFY_API_AVAILABLE_INTERVAL_MS,
            # )

            # self.private_volume.reload()

            async for event in check_server_with_log(
                f"http://{COMFY_HOST}",
                COMFY_API_AVAILABLE_MAX_RETRIES,
                COMFY_API_AVAILABLE_INTERVAL_MS,
                self.machine_logs,
                self.last_sent_log_index,
            ):
                yield event

            await send_status_update(input, "started", self.gpu_event_id)

            yield f"event: event_update\ndata: {json.dumps({'event': 'comfyui_ready'})}\n\n"

            # if not ok:
            #     raise Exception("ComfyUI API is not available")

            async with aiohttp.ClientSession() as session:
                data_str = input.json()
                data_bytes = data_str.encode("utf-8")
                async with session.post(
                    f"http://{COMFY_HOST}/comfyui-deploy/run/streaming",
                    data=data_bytes,
                    headers={"Authorization": f"Bearer {config['auth_token']}"},
                ) as response:
                    async for event in response.content.iter_any():
                        # print(event)
                        yield event

                        # Send the machine logs
                        while self.last_sent_log_index < len(self.machine_logs):
                            log = self.machine_logs[self.last_sent_log_index]
                            if isinstance(log["timestamp"], float):
                                log["timestamp"] = (
                                    datetime.utcfromtimestamp(
                                        log["timestamp"]
                                    ).isoformat()
                                    + "Z"
                                )
                            yield f"event: log_update\ndata: {json.dumps(log)}\n\n"
                            self.last_sent_log_index += 1

            # current_directories = os.listdir('/comfyui/output')
            # print("Finished Current directories in '/comfyui/output':", current_directories)

            # current_directories = os.listdir(output_directory)
            # print("Finished Current directories in :", output_directory, current_directories)
        except asyncio.CancelledError as e:
            print("This got fucking cancelled", e)  # this doesn't happen
            try:
                ok = await interrupt_comfyui()
            except Exception as e:
                pass
            finally:
                await send_status_update(input, "cancelled", self.gpu_event_id)

    @modal.method()
    async def read_output_file(self, file):
        # Read the file from the volume and yield its contents as bytes
        try:
            with open(file, "rb") as f:
                while True:
                    chunk = f.read()  # Read as much as possible
                    if not chunk:
                        break
                    yield chunk
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="File not found")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")

    @modal.method()
    async def websocket_endpoint(self, websocket: WebSocket):
        await websocket.accept()

        query_params = websocket.query_params
        query_string = urllib.parse.urlencode(query_params)
        sid = uuid.uuid4().hex
        ws_url = (
            f"http://127.0.0.1:8188/comfyui-deploy/ws?{query_string}&clientId={sid}"
        )

        try:
            ok = await check_server(
                f"http://{COMFY_HOST}",
                COMFY_API_AVAILABLE_MAX_RETRIES,
                COMFY_API_AVAILABLE_INTERVAL_MS,
            )

            # ws_timeout = 2

            if not ok:
                raise Exception("ComfyUI API is not available")

            async def timeout_check(sid: str):
                wait_time = 0
                while True:
                    data = await check_ws_status(sid)
                    # print(data)
                    if "remaining_queue" in data and data["remaining_queue"] == 0:
                        if wait_time >= ws_timeout:
                            return True

                        await asyncio.sleep(0.10)
                        wait_time += 0.10
                    else:
                        await asyncio.sleep(0.10)

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(ws_url) as local_ws:

                        async def websocket_to_local():
                            try:
                                while True:
                                    ws_receive_task = asyncio.create_task(
                                        websocket.receive()
                                    )
                                    # Prepare the check_ws_status coroutine (with some delay if needed)
                                    check_status_task = asyncio.create_task(
                                        timeout_check(sid)
                                    )

                                    done, pending = await asyncio.wait(
                                        {ws_receive_task, check_status_task},
                                        return_when=asyncio.FIRST_COMPLETED,
                                    )

                                    for task in pending:
                                        task.cancel()

                                    if ws_receive_task in done:
                                        ws_msg = ws_receive_task.result()

                                        # Timeout for the input
                                        # ws_msg = await asyncio.wait_for(websocket.receive(), ws_timeout)
                                        # print(ws_msg)

                                        if (
                                            "type" in ws_msg
                                            and ws_msg["type"] == "websocket.disconnect"
                                        ):
                                            local_to_websocket_task.cancel()
                                            websocket_to_local_task.cancel()
                                            print("cancelled from remote client")
                                            break

                                        if (
                                            "bytes" in ws_msg
                                            and ws_msg["bytes"] is not None
                                        ):
                                            print("Received binary data")
                                            await local_ws.send_bytes(ws_msg["bytes"])
                                        elif (
                                            "text" in ws_msg
                                            and ws_msg["text"] is not None
                                        ):
                                            print("Received text data")
                                            await local_ws.send_str(ws_msg["text"])

                                    if (
                                        check_status_task in done
                                        and check_status_task.result()
                                    ):
                                        raise asyncio.TimeoutError(
                                            "Queue processing timeout."
                                        )

                            except asyncio.TimeoutError:
                                local_to_websocket_task.cancel()
                                websocket_to_local_task.cancel()
                                print(
                                    f"Timeout: No message received in {ws_timeout} seconds."
                                )
                                await websocket.close(
                                    1000,
                                    reason=f"Timeout: No message received in {ws_timeout} seconds.",
                                )
                            except asyncio.CancelledError:
                                await local_ws.close()

                        async def local_to_websocket():
                            try:
                                while True:
                                    msg = await local_ws.receive()  # await asyncio.wait_for(, ws_timeout)  # 10-second timeout
                                    # print(msg.type.name)
                                    if msg.type in [
                                        aiohttp.WSMsgType.CLOSE,
                                        aiohttp.WSMsgType.CLOSED,
                                    ]:
                                        local_to_websocket_task.cancel()
                                        websocket_to_local_task.cancel()
                                        print("cancelled from localhost")
                                        break
                                    elif msg.type in [aiohttp.WSMsgType.TEXT]:
                                        await websocket.send_text(msg.data)
                                    elif msg.type in [aiohttp.WSMsgType.BINARY]:
                                        await websocket.send_bytes(msg.data)
                            except asyncio.TimeoutError:
                                print(
                                    f"Timeout: No message received in {ws_timeout} seconds."
                                )
                                await websocket.close(
                                    1000,
                                    reason=f"Timeout: No message received in {ws_timeout} seconds.",
                                )
                            except asyncio.CancelledError:
                                pass  # Task was cancelled, ignore
                                await websocket.close()

                        websocket_to_local_task = asyncio.create_task(
                            websocket_to_local()
                        )
                        local_to_websocket_task = asyncio.create_task(
                            local_to_websocket()
                        )

                        try:
                            await asyncio.gather(
                                websocket_to_local_task, local_to_websocket_task
                            )
                        except asyncio.CancelledError:
                            pass  # Task was cancelled, ignore

                        print("both task finished")
            except Exception as e:
                print(f"Error in WebSocket communication: {e}")
            finally:
                pass
                # if websocket.state !=
                # await websocket.close()
                # print("closing for ws");

        except Exception as e:
            pass
        finally:
            pass
            # stdout_task.cancel()
            # stderr_task.cancel()
            # await stdout_task
            # await stderr_task

    async def read_stream(self, stream, isStderr):
        import time

        while True:
            try:
                line = await stream.readline()
                if line:
                    l = line.decode("utf-8").strip()

                    if l == "":
                        continue

                    if self.log_queues is not None and len(self.log_queues) > 0:
                        target_log = self.log_queues[0]["logs"]
                    else:
                        target_log = self.cold_start_queue

                    target_log = cast(deque, target_log)
                    target_log.append({"logs": l, "timestamp": time.time()})
                    # print("appending to log queue", len(target_log), target_log)

                    if not isStderr:
                        print(l, flush=True)
                        self.machine_logs.append({"logs": l, "timestamp": time.time()})
                    else:
                        print(l, flush=True)
                        self.machine_logs.append({"logs": l, "timestamp": time.time()})
                else:
                    break
            except asyncio.CancelledError:
                # Handle the cancellation here if needed
                break  # Break out of the loop on cancellation

    current_tunnel_url = ""

    @modal.method()
    async def new_tunnel_params(self):
        pass

    @modal.method()
    async def increase_timeout_v2(self):
        pass

    @modal.method()
    async def create_tunnel(self, q, status_endpoint, timeout, session_id: str | None = None):
        update_endpoint = status_endpoint.split("/api/")[0]
        if self.current_tunnel_url == "exhausted":
            send_log_entry(update_endpoint, session_id, config["machine_id"], "Previous session exhausted, please start a new one.")
            await delete_session(update_endpoint, session_id)
            raise Exception("Previous session exhausted, please start a new one.")

        if self._is_workspace and not self.gpu_event_id and session_id:
            print("creating gpu event id")
            self._session_id = session_id
            if self.container_start_time is None:
                self.container_start_time = datetime.now(timezone.utc)
            self.gpu_event_id = await sync_report_gpu_event(
                event_id=None,
                is_workspace= self._is_workspace,
                gpu=self._gpu,
                user_id=self.user_id,
                org_id=self.org_id,
                session_id=str(session_id),
                custom_timestamp=self.container_start_time,
            )
        print("gpu event id", self.gpu_event_id)
        
        self.status_endpoint = status_endpoint
        self.start_time = time.time()
        # timeout input is in minutes, so we need to convert it to seconds
        self.session_timeout = timeout * 60
        # print("status_endpoint", status_endpoint)

        task = asyncio.create_task(check_for_timeout(update_endpoint, self._session_id))

        try:
            async for event in check_server_with_log(
                f"http://{COMFY_HOST}",
                COMFY_API_AVAILABLE_MAX_RETRIES,
                COMFY_API_AVAILABLE_INTERVAL_MS,
                self.machine_logs,
                self.last_sent_log_index,
            ):
                pass
        except asyncio.CancelledError:
            self.current_tunnel_url = "exhausted"
            print("cancelled")
        
            # asyncio.create_task(q.put.aio(event))

        if self.current_tunnel_url != "":
            # await q.put.aio("url:" + self.current_tunnel_url)
            return

        with modal.forward(8188) as tunnel:
            self.current_tunnel_url = tunnel.url
            self.current_function_call_id = modal.current_function_call_id()

            await q.put.aio("url:" + self.current_tunnel_url)
            print(f"tunnel.url        = {tunnel.url}")
            print(f"tunnel.tls_socket = {tunnel.tls_socket}")

            # Wait for the server process to exit
            try:
                while True:
                    # if self.kill_session_asap:
                    #     ok = await interrupt_comfyui()
                    #     break
                    # if self.start_time + self.session_timeout < time.time():
                    #     await self.timeout_and_exit(0, True)
                    await asyncio.sleep(1)  # Che  ck every 1 seconds
            except asyncio.CancelledError:
                print("cancelled")
                try:
                    ok = await interrupt_comfyui()
                except Exception as e:
                    pass

        # Ended
        self.current_tunnel_url = "exhausted"
        self.container_start_time = None

        await task

    @modal.method()
    async def close_container(self):
        await self.timeout_and_exit(0, True)

    def disable_customnodes(self, nodes_to_disable: list[str]):
        """
        Disable specified custom nodes by renaming their directories
        Args:
            nodes_to_disable: List of custom node directory names to disable
        """
        import shutil
        from datetime import datetime
        import time
        
        for node in nodes_to_disable:
            node_path = f"/comfyui/custom_nodes/{node}"
            node_disabled_path = f"/comfyui/custom_nodes/{node}.disabled"
            if os.path.exists(node_path):
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{timestamp}] Disabling custom node: {node}")
                try:
                    start_time = time.time()
                    shutil.move(node_path, node_disabled_path)
                    elapsed_time = time.time() - start_time
                    print(f"[{timestamp}] Successfully disabled {node} (took {elapsed_time:.2f} seconds)")
                except Exception as e:
                    elapsed_time = time.time() - start_time
                    print(f"[{timestamp}] Failed to disable {node}: {e} (took {elapsed_time:.2f} seconds)")

    @modal.method()
    async def increase_timeout(self, timeout):
        # timeout input is in minutes, so we need to convert it to seconds
        self.session_timeout = self.session_timeout + (timeout * 60)

    # kill_session_asap = False

    # @modal.method()
    # async def kill_session(self):
    #     self.kill_session_asap = True

    @modal.method()
    async def run(self, input: Input):
        if isinstance(input, dict):
            input = Input(**input)
            input.gpu_event_id = self.gpu_event_id

        try:
            self.log_queues.append({"logs": deque(), "current_input": input})

            if len(self.log_queues) == 1:
                self.current_input = input
                self.status_endpoint = input.status_endpoint

            import signal
            import time

            print("Got input")

            # self.private_volume.reload()

            await send_status_update(
                input, "queued", self.gpu_event_id, modal.current_function_call_id()
            )

            result = {"status": "queued"}

            class TimeoutError(Exception):
                pass

            my_function_call_id = modal.current_function_call_id()

            def timeout_handler(signum, frame):
                asyncio.create_task(interrupt_comfyui())

                data = json.dumps(
                    {
                        "run_id": input.prompt_id,
                        "status": "timeout",
                        "time": datetime.now(timezone.utc).isoformat(),
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    input.status_endpoint,
                    data=data,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "bypass-tunnel-reminder": "true",
                        "Authorization": f"Bearer {config['auth_token']}",
                    },
                )
                urllib.request.urlopen(req)

                print("current_function_call_id", my_function_call_id)

                try:
                    modal.functions.FunctionCall.from_id(my_function_call_id).cancel()
                except Exception as e:
                    print("Issues when canceling function call", e)
                    pass

                # cancel self, instead of crashing it.
                # asyncio.create_task(self.timeout_and_exit(0, True))

            signal.signal(signal.SIGALRM, timeout_handler)

            try:
                signal.alarm(run_timeout)

                ok = await check_server(
                    f"http://{COMFY_HOST}",
                    COMFY_API_AVAILABLE_MAX_RETRIES,
                    COMFY_API_AVAILABLE_INTERVAL_MS,
                )

                await send_status_update(input, "started", self.gpu_event_id)

                job_input = input

                try:
                    queued_workflow = await queue_workflow_comfy_deploy(
                        job_input
                    )  # queue_workflow(workflow)
                    prompt_id = queued_workflow["prompt_id"]
                    print(f"comfy-modal - queued workflow with ID {prompt_id}")
                except Exception as e:
                    import traceback

                    print(traceback.format_exc())
                    raise e

                    # return {"error": f"Error queuing workflow: {str(e)}"}

                # Poll for completion
                print(f"comfy-modal - wait until image generation is complete")
                retries = 0
                status = ""
                try:
                    print("getting request")
                    while True:
                        status_result = await check_status(prompt_id=prompt_id)

                        # print("status_result", status_result)

                        if (
                            "status" in status_result
                            and status_result["status"] == "running"
                        ):
                            self.current_input = input
                            self.status_endpoint = input.status_endpoint

                        if "status" in status_result and (
                            status_result["status"] == "success"
                            or status_result["status"] == "failed"
                        ):
                            status = status_result["status"]
                            print(status)
                            break
                        else:
                            # Wait before trying again
                            await asyncio.sleep(COMFY_POLLING_INTERVAL_MS / 1000)
                            retries += 1
                except Exception as e:
                    print(traceback.format_exc())
                    raise e
                    # return {"error": f"Error waiting for image generation: {str(e)}"}

                print(f"comfy-modal - Finished, turning off")

                result = {"status": status}

            except TimeoutError:
                print("Operation timed out")
                # return {"status": "failed"}
            except Exception as e:
                print(f"Unexpected error occurred: {str(e)}")
                await send_status_update(input, "failed", self.gpu_event_id)
                self.machine_logs.append({"logs": str(e), "timestamp": time.time()})
            finally:
                signal.alarm(0)

            print("uploading log_data")
            data = json.dumps(
                {
                    "run_id": input.prompt_id,
                    "time": datetime.now(timezone.utc).isoformat(),
                    "log_data": self.machine_logs,
                }
            ).encode("utf-8")
            print("my logs", len(self.machine_logs))
            # Clear logs
            timeout = aiohttp.ClientTimeout(total=60)  # 60 seconds total timeout
            # Use HTTP/1.1 explicitly and increase the connection pool size
            connector = TCPConnector(
                limit=100, force_close=True, enable_cleanup_closed=True
            )

            async with aiohttp.ClientSession(
                timeout=timeout, connector=connector, trust_env=True
            ) as session:
                try:
                    async with session.post(
                        input.status_endpoint,
                        data=data,
                        headers={
                            "Content-Type": "application/json",
                            "bypass-tunnel-reminder": "true",
                            "Authorization": f"Bearer {config['auth_token']}",
                        },
                    ) as response:
                        print("response", response)
                        # Process your response here
                except asyncio.TimeoutError:
                    print("Request timed out")
                except Exception as e:
                    print(f"An error occurred: {e}")
            print("uploaded log_data")
            # print(data)
            self.machine_logs = []
        except asyncio.CancelledError as e:
            print("This got fucking cancelled", e)  # this doesn't happen
            try:
                ok = await interrupt_comfyui()
            except Exception as e:
                pass
            finally:
                await send_status_update(input, "cancelled", self.gpu_event_id)

        finally:
            # self.log_queues.pop(input.prompt_id)
            if len(self.log_queues) > 0:
                self.log_queues.pop()
            # if self.log_task:
            #     self.log_task.cancel()
            #     try:
            #         await self.log_task
            #     except asyncio.CancelledError:
            #         pass

        # commit by default so commit after the workflow is executed
        if (self.private_volume is None):
            self.private_volume = modal.Volume.from_name(self.volume_name, create_if_missing=True)
        self.private_volume.commit()
        return result

    async def handle_container_enter_before_comfy(self):
        # Make sure that the ComfyUI API is available
        print("comfy-modal - check server")
        
        if (self.private_volume is None):
            self.private_volume = modal.Volume.from_name(self.volume_name, create_if_missing=True)

        # reload volumes
        await public_model_volume.reload.aio()
        await self.private_volume.reload.aio()

        # Disable specified custom nodes
        self.disable_customnodes(["ComfyUI-Manager"])

        # directory_path = "/comfyui/models"
        # if os.path.exists(directory_path):
        #     directory_contents = os.listdir(directory_path)
        #     directory_path = "/comfyui/models/ipadapter"
        #     print(directory_contents)
        #     if os.path.exists(directory_path):
        #         directory_contents = os.listdir(directory_path)
        #         print(directory_contents)
        # else:
        #     print(f"Directory {directory_path} does not exist.")

        pass

    container_start_time: datetime | None = None

    async def handle_container_enter(self):
        print("setting up stdout and stderr")

        self.log_task = asyncio.create_task(self.process_log_queue())

        self.container_start_time = datetime.now(timezone.utc)

        if not self._is_workspace:
            print("setting up gpu event id")
            self.gpu_event_id = await sync_report_gpu_event(
                None,
                self._is_workspace,
                self._gpu,
                self.user_id,
                self.org_id,
                self._session_id,
            )

        print("setting up timeout")

        if self.timeout is not None:
            print("timeout", self.timeout)
            self.timeout_start_time = time.time()
            self.timeout_task = asyncio.create_task(self.timeout_and_exit(self.timeout))

    async def handle_container_exit(self):
        if self.cleanup_done:
            return

        print("comfy-modal - cleanup")

        await sync_report_gpu_event(
            self.gpu_event_id, self._is_workspace, self._gpu, self.user_id, self.org_id
        )
        self.log_task.cancel()

        print("comfy-modal - cleanup done")

    def setup_native_logging(self):
        """
        Sets up logging for native ComfyUI implementation to capture logs similarly to subprocess implementation.
        Returns the configured handler for cleanup if needed.
        """
        # Create a StringIO object to capture logs
        log_stream = StringIO()
        
        class DualHandler(logging.Handler):
            def __init__(self, machine_logs, log_queues, cold_start_queue, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.machine_logs = machine_logs
                self.log_queues = log_queues
                self.cold_start_queue = cold_start_queue

            def emit(self, record):
                try:
                    msg = self.format(record)
                    print(msg, flush=True)  # Print to stdout
                    
                    log_entry = {
                        "logs": msg,
                        "timestamp": time.time()
                    }
                    
                    # Add to machine logs
                    self.machine_logs.append(log_entry)
                    
                    # Add to appropriate queue
                    target_log = (self.log_queues[0]["logs"] 
                                if self.log_queues is not None and len(self.log_queues) > 0 
                                else self.cold_start_queue)
                    target_log.append(log_entry)
                except Exception as e:
                    print(f"Error in log handler: {e}")

        # Create and configure the handler
        handler = DualHandler(
            machine_logs=self.machine_logs,
            log_queues=self.log_queues,
            cold_start_queue=self.cold_start_queue
        )
        
        # Set format
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)

        # Get the root logger and add our handler
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)

        return handler

class _ComfyDeployRunner(BaseComfyDeployRunner):
    workflow_api_raw = None

    # load_workflow_path = "/root/workflow/workflow_api.json"

    native = True
    # native: int = (  # see section on torch.compile below for details
    #     modal.parameter(default=1)
    # )

    skip_workflow_api_validation: bool = False

    logs = []

    models_cache = {}

    nodes_cache = {}

    model_urls = {
        # "checkpoints": [
        #     "https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.ckpt",
        # ],
    }

    loading_time = {}

    start_time = None
    current_time = None

    prompt_executor = None

    # To optimize imports, we duplicated the function
    # - prompt_worker
    # - run_server
    # - start_native_comfy_server
    # from ComfyUI, better to figure out cleaner way to do this

    def prompt_worker(self, q, server):
        import execution
        import comfy
        import time

        e = execution.PromptExecutor(server)
        self.__class__.prompt_executor = e

        last_gc_collect = 0
        need_gc = False
        gc_collect_interval = 10.0
        current_time = time.perf_counter()  # Initialize current_time

        while True:
            timeout = 1000.0
            if need_gc:
                timeout = max(
                    gc_collect_interval - (current_time - last_gc_collect), 0.0
                )

            queue_item = q.get(timeout=timeout)
            if queue_item is not None:
                item, item_id = queue_item
                execution_start_time = time.perf_counter()
                prompt_id = item[1]
                server.last_prompt_id = prompt_id

                e.execute(item[2], prompt_id, item[3], item[4])
                need_gc = True
                q.task_done(
                    item_id,
                    e.history_result,
                    status=execution.PromptQueue.ExecutionStatus(
                        status_str="success" if e.success else "error",
                        completed=e.success,
                        messages=e.status_messages,
                    ),
                )
                if server.client_id is not None:
                    server.send_sync(
                        "executing",
                        {"node": None, "prompt_id": prompt_id},
                        server.client_id,
                    )

                current_time = time.perf_counter()
                execution_time = current_time - execution_start_time
                logging.info("Prompt executed in {:.2f} seconds".format(execution_time))

            flags = q.get_flags()
            free_memory = flags.get("free_memory", False)

            if flags.get("unload_models", free_memory):
                comfy.model_management.unload_all_models()
                need_gc = True
                last_gc_collect = 0

            if free_memory:
                e.reset()
                need_gc = True
                last_gc_collect = 0

            if need_gc:
                current_time = time.perf_counter()
                if (current_time - last_gc_collect) > gc_collect_interval:
                    comfy.model_management.cleanup_models()
                    gc.collect()
                    comfy.model_management.soft_empty_cache()
                    last_gc_collect = current_time
                    need_gc = False

    async def run_server(
        self, server, address="", port=8188, verbose=True, call_on_start=None
    ):
        addresses = []
        for addr in address.split(","):
            addresses.append((addr, port))
        await asyncio.gather(
            server.start_multi_address(addresses, call_on_start), server.publish_loop()
        )

    def apply_custom_paths(self):
        import os
        import importlib.util
        import folder_paths
        import time
        from comfy.cli_args import args
        from app.logger import setup_logger
        import itertools
        import utils.extra_config
        import logging

        utils.extra_config.load_extra_path_config(extra_model_path_config)

        # extra model paths
        extra_model_paths_config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "extra_model_paths.yaml")
        if os.path.isfile(extra_model_paths_config_path):
            utils.extra_config.load_extra_path_config(extra_model_paths_config_path)

        if args.extra_model_paths_config:
            for config_path in itertools.chain(*args.extra_model_paths_config):
                utils.extra_config.load_extra_path_config(config_path)

        # --output-directory, --input-directory, --user-directory
        if args.output_directory:
            output_dir = os.path.abspath(args.output_directory)
            logging.info(f"Setting output directory to: {output_dir}")
            folder_paths.set_output_directory(output_dir)

        # These are the default folders that checkpoints, clip and vae models will be saved to when using CheckpointSave, etc.. nodes
        folder_paths.add_model_folder_path("checkpoints", os.path.join(folder_paths.get_output_directory(), "checkpoints"))
        folder_paths.add_model_folder_path("clip", os.path.join(folder_paths.get_output_directory(), "clip"))
        folder_paths.add_model_folder_path("vae", os.path.join(folder_paths.get_output_directory(), "vae"))
        folder_paths.add_model_folder_path("diffusion_models",
                                        os.path.join(folder_paths.get_output_directory(), "diffusion_models"))
        folder_paths.add_model_folder_path("loras", os.path.join(folder_paths.get_output_directory(), "loras"))

        if args.input_directory:
            input_dir = os.path.abspath(args.input_directory)
            logging.info(f"Setting input directory to: {input_dir}")
            folder_paths.set_input_directory(input_dir)

        if args.user_directory:
            user_dir = os.path.abspath(args.user_directory)
            logging.info(f"Setting user directory to: {user_dir}")
            folder_paths.set_user_directory(user_dir)


    def execute_prestartup_script(self):
        import comfy.options
        comfy.options.enable_args_parsing()

        import os
        import importlib.util
        import folder_paths
        import time
        from comfy.cli_args import args
        import logging

        def execute_script(script_path):
            module_name = os.path.splitext(script_path)[0]
            try:
                spec = importlib.util.spec_from_file_location(module_name, script_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return True
            except Exception as e:
                logging.error(f"Failed to execute startup-script: {script_path} / {e}")
            return False

        if args.disable_all_custom_nodes:
            return

        node_paths = folder_paths.get_folder_paths("custom_nodes")
        for custom_node_path in node_paths:
            possible_modules = os.listdir(custom_node_path)
            node_prestartup_times = []

            for possible_module in possible_modules:
                module_path = os.path.join(custom_node_path, possible_module)
                if os.path.isfile(module_path) or module_path.endswith(".disabled") or module_path == "__pycache__":
                    continue

                script_path = os.path.join(module_path, "prestartup_script.py")
                if os.path.exists(script_path):
                    time_before = time.perf_counter()
                    success = execute_script(script_path)
                    node_prestartup_times.append((time.perf_counter() - time_before, module_path, success))
        if len(node_prestartup_times) > 0:
            logging.info("\nPrestartup times for custom nodes:")
            for n in sorted(node_prestartup_times):
                if n[2]:
                    import_message = ""
                else:
                    import_message = " (PRESTARTUP FAILED)"
                logging.info("{:6.1f} seconds{}: {}".format(n[0], import_message, n[1]))
            logging.info("")

    def hijack_progress(self, server_instance):
        import comfy
        from server import BinaryEventTypes

        def hook(value, total, preview_image):
            comfy.model_management.throw_exception_if_processing_interrupted()
            progress = {
                "value": value,
                "max": total,
                "prompt_id": server_instance.last_prompt_id,
                "node": server_instance.last_node_id,
            }

            server_instance.send_sync("progress", progress, server_instance.client_id)
            if preview_image is not None:
                server_instance.send_sync(BinaryEventTypes.UNENCODED_PREVIEW_IMAGE, preview_image, server_instance.client_id)

        comfy.utils.set_progress_bar_global_hook(hook)


    async def start_native_comfy_server(self):
        self.log_handler = self.setup_native_logging()
        # with cProfile.Profile() as pr:
        import sys
        from pathlib import Path

        # Add the ComfyUI directory to the Python path
        comfy_path = Path("/comfyui")
        sys.path.append(str(comfy_path))

        # Add the ComfyUI/utils directory to the Python path
        utils_path = comfy_path / "utils"
        sys.path.append(str(utils_path))

        t = time.time()
        import nodes

        self.loading_time["import_nodes"] = time.time() - t
        t = time.time()
        import server

        self.loading_time["import_server"] = time.time() - t
        t = time.time()
        import execution

        self.loading_time["import_execution"] = time.time() - t
        t = time.time()
        import comfy

        self.loading_time["import_comfy"] = time.time() - t

        from app.logger import setup_logger
        setup_logger()

        self.apply_custom_paths()
        self.execute_prestartup_script()

        # original_load_checkpoint_guess_config = comfy.sd.load_checkpoint_guess_config

        # # Override the load_checkpoint_guess_config function
        # def custom_load_checkpoint_guess_config(ckpt_path, *args, **kwargs):
        #     if ckpt_path in self.models_cache:
        #         print(f"Loading checkpoint from cache: {ckpt_path}")
        #         return self.models_cache[ckpt_path]
        #     else:
        #         print(f"Loading checkpoint from: {ckpt_path}")
        #     return original_load_checkpoint_guess_config(ckpt_path, *args, **kwargs)

        # # Replace the original function with our custom one
        # comfy.sd.load_checkpoint_guess_config = custom_load_checkpoint_guess_config

        original_load_torch_file = comfy.utils.load_torch_file

        def custom_load_torch_file(ckpt, *args, **kwargs):
            if ckpt in self.models_cache:
                print(f"Loading torch file from cache: {ckpt}")
                return self.models_cache[ckpt]
            else:
                print(f"Loading torch file from: {ckpt}")
                return original_load_torch_file(ckpt, *args, **kwargs)

        comfy.utils.load_torch_file = custom_load_torch_file

        original_validate_prompt = execution.validate_prompt

        def custom_validate_prompt(prompt):
            if self.skip_workflow_api_validation:
                outputs = set()
                for x in prompt:
                    if "class_type" not in prompt[x]:
                        error = {
                            "type": "invalid_prompt",
                            "message": f"Cannot execute because a node is missing the class_type property.",
                            "details": f"Node ID '#{x}'",
                            "extra_info": {},
                        }
                        return (False, error, [], [])

                    class_type = prompt[x]["class_type"]
                    class_ = nodes.NODE_CLASS_MAPPINGS.get(class_type, None)
                    if class_ is None:
                        error = {
                            "type": "invalid_prompt",
                            "message": f"Cannot execute because node {class_type} does not exist.",
                            "details": f"Node ID '#{x}'",
                            "extra_info": {},
                        }
                        return (False, error, [], [])

                    if hasattr(class_, "OUTPUT_NODE") and class_.OUTPUT_NODE is True:
                        outputs.add(x)

                if len(outputs) == 0:
                    error = {
                        "type": "prompt_no_outputs",
                        "message": "Prompt has no outputs",
                        "details": "",
                        "extra_info": {},
                    }
                    return (False, error, [], [])

                good_outputs = set()
                errors = []
                node_errors = {}
                validated = {}
                for o in outputs:
                    valid = True

                    if valid is True:
                        good_outputs.add(o)

                return (True, None, list(good_outputs), node_errors)
            return original_validate_prompt(prompt)

        execution.validate_prompt = custom_validate_prompt

        # loop = asyncio.new_event_loop()
        # asyncio.set_event_loop(loop)
        loop = asyncio.get_running_loop()
        server = server.PromptServer(loop)
        q = execution.PromptQueue(server)

        print("Initializing extra nodes")
        t = time.time()
        nodes.init_extra_nodes()
        self.loading_time["init_extra_nodes"] = time.time() - t
        print("Adding routes")
        server.add_routes()
        self.hijack_progress(server)

        threading.Thread(
            target=self.prompt_worker,
            daemon=True,
            args=(
                q,
                server,
            ),
        ).start()

        await server.setup()
        # loop.run_until_complete()
        asyncio.create_task(self.run_server(server, verbose=True, port=8188))
        # loop.run_until_complete()

        # pr.print_stats()

    def load_args(self):
        from comfy.cli_args import args

        args.disable_metadata = disable_metadata
        args.port = 8188
        args.enable_cors_header = "*"

        args.input_directory = input_directory
        args.preview_method = "auto"
        # args.extra_model_paths_config = extra_model_path_config

        # args.output_directory = output_directory
        # args.temp_directory = temp_directory

        if self._gpu == "CPU":
            args.cpu = True
        else:
            args.cpu = False
        

    @modal.exit()
    async def exit(self):
        print("Exiting ComfyUI")
        root_logger = logging.getLogger()
        root_logger.removeHandler(self.log_handler)
        await self.handle_container_exit()


@app.cls(
    image=target_image,
    # will be overridden by the run function
    gpu=None,
    volumes=volumes,
    timeout=(config["run_timeout"] + 20),
    scaledown_window=config["idle_timeout"],
    max_containers=config["concurrency_limit"],
    enable_memory_snapshot=True,
    secrets=[modal.Secret.from_dict(secrets)],
    cpu=cpu,
    memory=memory,
)
@modal.concurrent(max_inputs=config["allow_concurrent_inputs"])
class ComfyDeployRunnerOptimizedImports(_ComfyDeployRunner):
    
    is_workspace: bool = modal.parameter(default=False)
    gpu: str = modal.parameter(default="")
    session_id: str = modal.parameter(default="")
    
    @modal.enter(snap=False)
    async def launch_comfy_background(self):
        self._is_workspace = self.is_workspace
        if self.gpu != "":
            self._gpu = self.gpu
        if self.session_id != "":
            self._session_id = self.session_id
        
        await self.handle_container_enter_before_comfy()
        await self.handle_container_enter()

        t = time.time()
        import torch

        self.loading_time["import_torch"] = time.time() - t

        # if self.load_workflow_path is not None and self.workflow_api_raw is None:
        #     self.workflow_api_raw = (Path(self.load_workflow_path)).read_text()

        # print(f"Time to import torch: {time.time() - t:.2f} seconds")
        print(f"GPUs available: {torch.cuda.is_available()}")

        self.start_time = time.time()
        print("Launching ComfyUI")

        self.load_args()
        await self.start_native_comfy_server()

        async for event in wait_for_server():
            # print(event)
            pass

        from comfy_execution.graph import (
            get_input_info,
            ExecutionList,
            DynamicPrompt,
            ExecutionBlocker,
        )
        from execution import IsChangedCache

        t = time.time()
        # prompt = json.loads(self.workflow_api_raw)
        # dynamic_prompt = DynamicPrompt(prompt)
        # is_changed_cache = IsChangedCache(
        #     dynamic_prompt, self.prompt_executor.caches.outputs
        # )
        # for cache in self.prompt_executor.caches.all:
        #     cache.set_prompt(dynamic_prompt, prompt.keys(), is_changed_cache)
        #     cache.clean_unused()
        # for node in self.config.nodes_to_preload:
        #     await preload_node(node, self.workflow_api_raw, self.prompt_executor)
        self.loading_time["preload_nodes"] = time.time() - t

        # workflow_start_time = time.perf_counter()
        # if self.config.warmup_workflow:
        #     input = Input(
        #         inputs={},
        #         workflow_api_raw=self.workflow_api_raw,
        #         prompt_id=str(uuid.uuid4()),
        #     )
        #     await queue_workflow(input)
        #     await wait_for_completion(input.prompt_id)
        #     workflow_end_time = time.perf_counter()
        # self.loading_time["warmup_workflow_runtime"] = (
        #     workflow_end_time - workflow_start_time
        # )
    
    @contextmanager
    def force_cpu_during_snapshot(self):
        import torch

        """Monkeypatch Torch CUDA checks during model loading/snapshotting"""
        original_is_available = torch.cuda.is_available
        original_current_device = torch.cuda.current_device

        # Force Torch to report no CUDA devices
        torch.cuda.is_available = lambda: False
        torch.cuda.current_device = lambda: torch.device("cpu")

        try:
            yield
        finally:
            # Restore original implementations
            torch.cuda.is_available = original_is_available
            torch.cuda.current_device = original_current_device
        
    @modal.enter(snap=True)
    async def load(self):
        with self.force_cpu_during_snapshot():
            import torch
            import sys
            from pathlib import Path
            import os
            import importlib

            # Add the ComfyUI directory to the Python path
            comfy_path = Path("/comfyui")
            
            print("\n=== Debug Information ===")
            print(f"Current working directory: {os.getcwd()}")
            print(f"Initial sys.path: {sys.path}")
            
            # Check utils directory structure
            utils_path = comfy_path / "utils"
            print(f"\n=== Utils Directory Check ===")
            print(f"Utils path: {utils_path}")
            print(f"Utils path exists: {utils_path.exists()}")
            if utils_path.exists():
                print(f"Utils contents: {os.listdir(utils_path)}")
                init_file = utils_path / "__init__.py"
                json_util_file = utils_path / "json_util.py"
                print(f"__init__.py exists: {init_file.exists()}")
                print(f"json_util.py exists: {json_util_file.exists()}")
                if init_file.exists():
                    with open(init_file, 'r') as f:
                        print(f"__init__.py contents:\n{f.read()}")
            
            # Clear and rebuild sys.path
            sys.path = [p for p in sys.path if 'utils' not in p]
            sys.path.insert(0, str(comfy_path))
            sys.path.insert(1, str(utils_path))
            
            print("\n=== Final Python Path ===")
            print(f"Updated sys.path: {sys.path}")

            self.load_args()
            
            print("\n=== Import Attempt ===")
            try:
                print("Attempting direct import of utils package...")
                import utils
                print(f"Utils package: {utils}")
                print(f"Utils package location: {utils.__file__}")
                
                print("\nAttempting import of json_util...")
                from utils import json_util
                print("Successfully imported json_util")
                
            except ImportError as e:
                print(f"Import failed: {e}")
                print(f"Error type: {type(e)}")
                print(f"Error args: {e.args}")
                
            print("\n=== Starting Main Imports ===")
            try:
                import nodes
                import comfy
                import server
                import execution
                import xformers
            except ImportError as e:
                print(f"Main import error: {e}")
                print(f"Error type: {type(e)}")
                print(f"Error args: {e.args}")
                raise

            print(f"\nGPUs available: {torch.cuda.is_available()}")



@app.function(
    image=target_image,
    gpu=None,
)
async def get_image_id():
    return target_image.object_id

@app.function(
    image=target_image,
    gpu=None,
)
async def get_file_tree(path="/"):
    """
    Lists files and directories at the specified path (non-recursively).

    Args:
        path (str): The path to list contents from. Defaults to root '/'.

    Returns:
        dict: A dictionary representing the contents of the specified directory
    """
    import os

    result = {
        "path": path,
        "contents": {},
        "directories": [],
        "files": []
    }

    try:
        # List contents of the directory
        entries = os.listdir(path)

        # Sort entries - directories first, then files
        dirs = []
        files = []

        for entry in entries:
            full_path = os.path.join(path, entry)
            if os.path.isdir(full_path):
                dirs.append(entry)
            else:
                files.append(entry)

        # Add sorted directories to result
        for dir_name in sorted(dirs):
            full_path = os.path.join(path, entry)
            result["directories"].append(dir_name)
            result["contents"][dir_name] = {
                "type": "directory"
            }

        # Add sorted files to result
        for file_name in sorted(files):
            full_path = os.path.join(path, file_name)
            # For files, store size and last modified time
            stat = os.stat(full_path)
            result["files"].append(file_name)
            result["contents"][file_name] = {
                "type": "file",
                "size": stat.st_size,
                "last_modified": stat.st_mtime
            }

    except PermissionError:
        result["error"] = "Permission denied"
    except FileNotFoundError:
        result["error"] = "Path not found"
    except Exception as e:
        result["error"] = str(e)

    return result

@app.cls(
    image=target_image,
    # will be overridden by the run function
    gpu=None,
    volumes=volumes,
    timeout=(config["run_timeout"] + 20),
    scaledown_window=config["idle_timeout"],
    max_containers=config["concurrency_limit"],
    secrets=[modal.Secret.from_dict(secrets)],
    cpu=cpu,
    memory=memory,
)
@modal.concurrent(max_inputs=config["allow_concurrent_inputs"])
class ComfyDeployRunner(BaseComfyDeployRunner):
    
    is_workspace: bool = modal.parameter(default=False)
    gpu: str = modal.parameter(default="")
    session_id: str = modal.parameter(default="")
    
    @enter()
    async def setup(self):
        self._is_workspace = self.is_workspace
        if self.gpu != "":
            self._gpu = self.gpu
        if self.session_id != "":
            self._session_id = self.session_id
        
        await self.handle_container_enter_before_comfy()

        self.server_process = await asyncio.subprocess.create_subprocess_shell(
            comfyui_cmd(mountIO=self.mountIO, cpu=self._gpu == "CPU"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/comfyui",
        )

        self.stdout_task = asyncio.create_task(
            self.read_stream(self.server_process.stdout, False)
        )
        self.stderr_task = asyncio.create_task(
            self.read_stream(self.server_process.stderr, True)
        )

        print("setting up log queue 2")

        await self.handle_container_enter()

    @exit()
    async def cleanup(self):
        self.stdout_task.cancel()
        self.stderr_task.cancel()
        await self.handle_container_exit()


HOST = "127.0.0.1"
PORT = "8188"
