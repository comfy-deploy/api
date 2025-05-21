import asyncio
import contextlib
from datetime import datetime, timedelta
import inspect
import os
from pprint import pprint
from api.modal.builder import insert_to_clickhouse
from api.routes.machines import (
    GitCommitHash,
    redeploy_machine,
)
from api.utils.docker import (
    comfyui_hash,
)
from api.routes.types import GPUEventModel, MachineGPU, MachineType
from api.utils.docker import (
    CustomNode,
    DepsBody,
    DockerStep,
    DockerSteps,
    comfyui_cmd,
    generate_all_docker_commands,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import logfire
import modal
from sqlalchemy.ext.asyncio import AsyncSession
from upstash_redis.asyncio import Redis
from .utils import select
from pydantic import BaseModel, Field


from modal._output import OutputManager
from modal.call_graph import InputStatus


class CustomOutputManager(OutputManager):
    _context_id = None
    _machine_id = None

    @classmethod
    @contextlib.contextmanager
    def enable_output_with_context(
        cls, context_id: str, machine_id: str, show_progress: bool = True
    ):
        print(f"[Intercepted Modal Log] enable_output with context {context_id}")
        if show_progress:
            cls._instance = CustomOutputManager()
            cls._context_id = context_id
            cls._machine_id = machine_id
        try:
            yield
        finally:
            cls._instance = None
            cls._context_id = None
            cls._machine_id = None

    def _print_log(self, fd: int, data: str) -> None:
        # context_info = f"[Context: {self._context_id} {self._machine_id}] " if self._context_id else ""
        # print(f"[Intercepted Modal Log] {context_info}_print_log", fd, data)

        if self._context_id is not None:
            item = [
                (
                    uuid4(),
                    self._context_id,
                    None,
                    self._machine_id,
                    datetime.now(),
                    "info",
                    data,
                )
            ]
            # print("inserting to clickhouse")
            asyncio.create_task(insert_to_clickhouse("log_entries", item))

        super()._print_log(fd, data)


modal._output.OutputManager = CustomOutputManager

# from sqlalchemy import select
from api.models import (
    GPUEvent,
    Machine,
    MachineVersion,
    get_machine_columns,
)
from api.database import get_clickhouse_client, get_db, get_db_context
from typing import Any, ClassVar, Dict, Generator, List, Optional, cast, Union
from uuid import UUID, uuid4
import logging
from typing import Optional
from sqlalchemy import update, func
from fastapi import BackgroundTasks
from api.utils.autumn import send_autumn_usage_event
import re

redis_url = os.getenv("UPSTASH_REDIS_META_REST_URL")
redis_token = os.getenv("UPSTASH_REDIS_META_REST_TOKEN")
redis = Redis(url=redis_url, token=redis_token)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Session"])
beta_router = APIRouter(tags=["Beta"])

status_endpoint = os.environ.get("CURRENT_API_URL") + "/api/update-run"
# session_callback_endpoint = os.environ.get("CURRENT_API_URL") + "/api/session-callback"
session_callback_endpoint = os.environ.get("CURRENT_API_URL")
# print("status_endpoint", status_endpoint)


async def get_comfy_runner_for_workspace(
    machine_id: str, session_id: str | UUID, timeout: int, gpu: str, optimized_runner: bool = False
):
    logger.info(machine_id)
    ComfyDeployRunner = await modal.Cls.lookup.aio(str(machine_id), "ComfyDeployRunner" if not optimized_runner else "ComfyDeployRunnerOptimizedImports")
    
    try:
        ComfyDeployRunner.new_tunnel_params
        has_new_tunnel_params = True
    except (AttributeError, modal.exception.Error):
        has_new_tunnel_params = False

    print("has_new_tunnel_params", has_new_tunnel_params)
    if has_new_tunnel_params:
        runner = ComfyDeployRunner.with_options(
            max_containers=5,
            allow_concurrent_inputs=1,
            # 2 seconds minimum idle timeout
            scaledown_window=2,
            timeout=timeout * 60,
            gpu=gpu if gpu != "CPU" else None,
        )(gpu=gpu, is_workspace=True)
    else:
        runner = ComfyDeployRunner.with_options(
            max_containers=1,
            allow_concurrent_inputs=1000,
            # 2 seconds minimum idle timeout
            scaledown_window=2,
            timeout=timeout * 60,
            gpu=gpu if gpu != "CPU" else None,
        )(session_id=str(session_id), gpu=gpu)

    return runner

class SessionResponse(BaseModel):
    id: str = Field(..., description="The session ID")
    session_id: str = Field(..., description="The session ID", deprecated=True)
    gpu_event_id: str = Field(..., description="The GPU event ID")
    url: Optional[str] = Field(None, description="The tunnel URL for the session")
    gpu: str = Field(..., description="The GPU type being used")
    created_at: datetime = Field(..., description="When the session was created")
    timeout: Optional[int] = Field(None, description="Session timeout in minutes")
    timeout_end: Optional[datetime] = Field(None, description="When the session will timeout")
    machine_id: Optional[str] = Field(None, description="Associated machine ID")
    machine_version_id: Optional[str] = Field(None, description="Associated machine version ID")
    status: str = Field("running", description="Session status")
    user_id: Optional[str] = Field(None, description="Associated user ID")
    org_id: Optional[str] = Field(None, description="Associated organization ID")

# Return the session tunnel url
@router.get(
    "/session/{session_id}",
    openapi_extra={
        "x-speakeasy-name-override": "get",
    },
)
async def get_session(
    request: Request, session_id: str, db: AsyncSession = Depends(get_db)
) -> SessionResponse:
    result = await db.execute(
        select(
            GPUEvent.id,
            GPUEvent.tunnel_url,
            GPUEvent.gpu,
            GPUEvent.created_at,
            GPUEvent.session_timeout,
            GPUEvent.machine_id,
            GPUEvent.machine_version_id,
            GPUEvent.user_id,
            GPUEvent.org_id,
        )
        .where((GPUEvent.session_id == session_id) & (GPUEvent.end_time.is_(None)))
        .apply_org_check(request)
        .limit(1)
    )

    timeout_end = await redis.get(f"session:{session_id}:timeout_end")
    gpuEvent = result.first()

    if gpuEvent is None:
        raise HTTPException(status_code=404, detail="GPUEvent not found")

    return SessionResponse(
        id=session_id,
        session_id=session_id,
        user_id=str(gpuEvent.user_id),
        org_id=str(gpuEvent.org_id),
        gpu_event_id=str(gpuEvent.id),
        url=gpuEvent.tunnel_url,
        gpu=gpuEvent.gpu,
        created_at=gpuEvent.created_at,
        timeout=gpuEvent.session_timeout,
        machine_id=str(gpuEvent.machine_id) if gpuEvent.machine_id else None,
        machine_version_id=str(gpuEvent.machine_version_id) if gpuEvent.machine_version_id else None,
        timeout_end=datetime.fromisoformat(timeout_end) if timeout_end else None,
    )


class GetSessionsBody(BaseModel):
    machine_id: str


# Return the sessions for a machine
@router.get(
    "/sessions",
    openapi_extra={
        "x-speakeasy-name-override": "list",
    },
)
async def get_machine_sessions(
    request: Request,
    machine_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> List[SessionResponse]:
    query = (
        select(GPUEvent)
        .where(GPUEvent.end_time.is_(None))
        .where(GPUEvent.session_id.isnot(None))
        .order_by(GPUEvent.start_time.desc())
        .apply_org_check(request)
    )

    if machine_id:
        query = query.where(GPUEvent.machine_id == machine_id)

    result = await db.execute(query)
    gpu_events = result.scalars().all()
    
    sessions = []
    for event in gpu_events:
        # Skipping this for now to save performance
        timeout_end = await redis.get(f"session:{event.session_id}:timeout_end")
        sessions.append(
            SessionResponse(
                session_id=str(event.session_id),
                user_id=str(event.user_id),
                org_id=str(event.org_id),
                id=str(event.session_id),
                gpu_event_id=str(event.id),
                url=event.tunnel_url,
                gpu=event.gpu,
                created_at=event.created_at,
                timeout=event.session_timeout,
                machine_id=str(event.machine_id) if event.machine_id else None,
                machine_version_id=str(event.machine_version_id) if event.machine_version_id else None,
                timeout_end=datetime.fromisoformat(timeout_end) if timeout_end else None,
            )
        )
    return sessions


async def increase_timeout_task(
    machine_id: str, session_id: UUID, timeout: int, gpu: str, optimized_runner: bool = False
):
    runner = await get_comfy_runner_for_workspace(machine_id, session_id, 60 * 24, gpu, optimized_runner)
    await runner.increase_timeout.spawn.aio(timeout)


async def create_session_background_task(
    machine_id: str,
    session_id: UUID,
    request: Request,
    timeout: int,
    gpu: str,
    status_queue: Optional[asyncio.Queue] = None,
):
    send_log_entry(session_id, machine_id, "Creating session...", "info")
    try:
        async with get_db_context() as db:
            machine = await db.execute(select(Machine.optimized_runner).where(Machine.id == machine_id))
            optimized_runner = machine.scalar_one()
    except Exception as e:
        print("optimized_runner", e)
        optimized_runner = False

    print("optimized_runner", optimized_runner)

    if optimized_runner:
        send_log_entry(session_id, machine_id, "Using optimized runner...", "info")
        send_log_entry(session_id, machine_id, "Waiting for container snapshot to be ready...", "info")
        send_log_entry(session_id, machine_id, "Note: First time after new machine configuration might take a while", "info")

    runner = await get_comfy_runner_for_workspace(machine_id, session_id, 60 * 24, gpu, optimized_runner)

    try:
        runner.increase_timeout
        has_increase_timeout = True
    except (AttributeError, modal.exception.Error):
        has_increase_timeout = False

    try:
        runner.new_tunnel_params
        has_new_tunnel_params = True
    except (AttributeError, modal.exception.Error):
        has_new_tunnel_params = False

    try:
        runner.increase_timeout_v2
        has_increase_timeout_v2 = True
    except (AttributeError, modal.exception.Error):
        has_increase_timeout_v2 = False

    async def set_timeout_redis():
        timeout_duration = timeout or 15
        timeout_end_time = datetime.utcnow() + timedelta(minutes=timeout_duration)
        await redis.set(
            f"session:{session_id}:timeout_end", timeout_end_time.isoformat()
        )

    if has_increase_timeout_v2:
        try:
            await set_timeout_redis()
        except Exception as e:
            logfire.error(f"Error setting timeout redis: {str(e)}")

    if not has_increase_timeout:
        runner = await get_comfy_runner_for_workspace(machine_id, session_id, timeout, gpu, optimized_runner)

    try:
        print("async_creation", status_queue)
        async with modal.Queue.ephemeral() as q:
            if has_increase_timeout:
                if has_new_tunnel_params:
                    result = await runner.create_tunnel.spawn.aio(q=q, status_endpoint=status_endpoint, timeout=timeout, session_id=str(session_id))
                else:
                    result = await runner.create_tunnel.spawn.aio(q=q, status_endpoint=status_endpoint, timeout=timeout)
            else:
                result = await runner.create_tunnel.spawn.aio(q, status_endpoint)

            modal_function_id = result.object_id
            print("modal_function_id", modal_function_id)

            # gpuEvent = None
            # while gpuEvent is None:
            # async with get_db_context() as db:
            #     gpuEvent = cast(
            #         Optional[GPUEvent],
            #         (
            #             await db.execute(
            #                 (
            #                     select(GPUEvent)
            #                     .where(GPUEvent.session_id == str(session_id))
            #                     .where(GPUEvent.end_time.is_(None))
            #                     .apply_org_check(request)
            #                 )
            #             )
            #         ).scalar_one_or_none(),
            #     )

            # if gpuEvent is None:
            #     await asyncio.sleep(1)

            print("async_creation", status_queue)
            if status_queue is not None:
                await status_queue.put(modal_function_id)
            print("async_creation", modal_function_id)

            async with get_db_context() as db:
                result = await db.execute(
                    update(GPUEvent)
                    .where(GPUEvent.session_id == str(session_id))
                    .values(modal_function_id=modal_function_id, session_timeout=timeout)
                    .returning(GPUEvent)
                )
                await db.commit()
                gpuEvent = result.scalar_one()
                await db.refresh(gpuEvent)


            async def wait_for_url_from_queue():
                while True:
                    msg = await q.get.aio()
                    if msg.startswith("url:"):
                        url = msg[4:]
                        break
                    else:
                        logger.info(msg)
                    
                    print("async_creation", msg)

                async with get_db_context() as db:
                    result = await db.execute(
                        update(GPUEvent)
                        .where(GPUEvent.session_id == str(session_id))
                        .values(tunnel_url=url)
                        .returning(GPUEvent)
                    )
                    await db.commit()
                    gpuEvent = result.scalar_one()
                    print("gpuEvent", gpuEvent)
                    await db.refresh(gpuEvent)
                    return gpuEvent
                
            # async def wait_for_url_from_callback():
                # while True:
                #     async with get_db_context() as db:
                #         result = await db.execute(
                #             select(GPUEvent)
                #             .where(GPUEvent.session_id == session_id)
                #             .where(GPUEvent.end_time.is_(None))
                #         )
                #         gpuEvent = result.scalar_one_or_none()

                #         print("gpuEvent", gpuEvent.modal_function_id)
                        
                #         if gpuEvent.tunnel_url:
                #             logger.info(f"Session {session_id} tunnel URL updated")
                #             return gpuEvent
                    
                #     await asyncio.sleep(1)

            # done, pending = await asyncio.wait(
            #     [
            #         asyncio.create_task(wait_for_url_from_queue()),
            #         asyncio.create_task(wait_for_url_from_callback())
            #     ],
            #     return_when=asyncio.FIRST_COMPLETED
            # )

            # print("done", done.result())

            # # Cancel any pending tasks
            # for task in pending:
            #     task.cancel()

            # return done.result()

            result = await wait_for_url_from_queue()

            return result
    except Exception as e:
        send_log_entry(session_id, machine_id, str(e), "info")
        async with get_db_context() as db:
            await delete_session(request, str(session_id), wait_for_shutdown=True, db=db)
        raise e


class CreateSessionBody(BaseModel):
    machine_id: str
    gpu: Optional[MachineGPU] = Field(None, description="The GPU to use")
    timeout: Optional[int] = Field(None, description="The timeout in minutes")
    wait_for_server: bool = Field(
        False, description="Whether to create the session asynchronously"
    )


class CreateDynamicSessionBody(BaseModel):
    gpu: MachineGPU = Field("A10G", description="The GPU to use")
    machine_id: Optional[str] = Field(None, description="The machine id to use")
    machine_version_id: Optional[str] = Field(
        None, description="The machine version id to use"
    )
    timeout: Optional[int] = Field(None, description="The timeout in minutes")
    comfyui_hash: Optional[GitCommitHash] = Field(
        None, description="The comfyui hash to use"
    )
    dependencies: Optional[Union[List[str], DepsBody]] = Field(
        [],
        description="The dependencies to use, either as a DepsBody or a list of shorthand strings",
        examples=[
            [
                "Stability-AI/ComfyUI-SAI_API@1793086",
                "cubiq/ComfyUI_IPAdapter_plus@b188a6c",
            ]
        ],
    )
    wait_for_server: bool = Field(
        False, description="Whether to create the session asynchronously"
    )
    base_docker_image: Optional[str] = Field(
        None, description="The base docker image to use"
    )
    python_version: Optional[str] = Field(None, description="The python version to use")


async def ensure_session_creation_complete(task: asyncio.Task):
    try:
        await task
    except Exception as e:
        # Handle any exceptions that occurred during session creation
        logger.error(f"Session creation failed: {str(e)}")
        # You might want to update the session status in the database here


class IncreaseTimeoutBody(BaseModel):
    machine_id: str
    session_id: UUID
    timeout: int
    gpu: str


@router.post("/session/increase-timeout")
async def increase_timeout(
    request: Request, body: IncreaseTimeoutBody, db: AsyncSession = Depends(get_db)
):
    gpu_event = (
        await db.execute(
            select(GPUEvent)
            .where(GPUEvent.session_id == str(body.session_id))
            .where(GPUEvent.end_time.is_(None))
            .apply_org_check(request)
        )
    ).scalar_one_or_none()

    if gpu_event is None:
        raise HTTPException(status_code=404, detail="GPU event not found")
    
    machine = await db.execute(select(Machine.optimized_runner).where(Machine.id == body.machine_id))
    optimized_runner = machine.scalar_one()

    await increase_timeout_task(
        body.machine_id, body.session_id, body.timeout, body.gpu, optimized_runner
    )

    # in the database we save the timeout in minutes
    gpu_event.session_timeout = gpu_event.session_timeout + body.timeout
    await db.commit()

    return JSONResponse(
        status_code=200, content={"message": "Timeout increased successfully"}
    )


class IncreaseTimeoutBody2(BaseModel):
    minutes: int


@router.post("/session/{session_id}/increase-timeout")
async def increase_timeout_2(
    request: Request,
    session_id: str,
    body: IncreaseTimeoutBody2,
    db: AsyncSession = Depends(get_db),
):
    plan = request.state.current_user.get("plan")

    gpu_event = (
        await db.execute(
            select(GPUEvent)
            .where(GPUEvent.session_id == str(session_id))
            .where(GPUEvent.end_time.is_(None))
            .apply_org_check(request)
        )
    ).scalar_one_or_none()

    if gpu_event is None:
        raise HTTPException(status_code=404, detail="GPU event not found")

    # Retrieve the current timeout end time from Redis
    current_timeout_end_str = await redis.get(f"session:{session_id}:timeout_end")
    if not current_timeout_end_str:
        raise HTTPException(status_code=404, detail="Timeout end not found for session")

    if plan == "free":
        max_timeout_minutes = 30
        if not gpu_event.start_time:
            raise HTTPException(status_code=400, detail="Session has not started yet")

        # Calculate the new timeout end time
        current_timeout_end = datetime.fromisoformat(current_timeout_end_str)
        new_timeout_end = current_timeout_end + timedelta(minutes=body.minutes)

        # Calculate total session duration from start to new end time
        total_duration = (new_timeout_end - gpu_event.start_time).total_seconds() / 60
        elapsed_minutes = (
            datetime.utcnow() - gpu_event.start_time
        ).total_seconds() / 60

        if total_duration > max_timeout_minutes:
            raise HTTPException(
                status_code=400,
                detail=f"Free plan users are limited to {max_timeout_minutes} minutes total session time. "
                f"Session has been running for {int(elapsed_minutes)} minutes. "
                f"Maximum remaining time: {max(0, int(max_timeout_minutes - elapsed_minutes))} minutes",
            )
    else:
        # Calculate the new timeout end time for non-free plans
        current_timeout_end = datetime.fromisoformat(current_timeout_end_str)
        new_timeout_end = current_timeout_end + timedelta(minutes=body.minutes)

    # Update the timeout end time in Redis
    await redis.set(f"session:{session_id}:timeout_end", new_timeout_end.isoformat())

    return JSONResponse(
        status_code=200, content={"message": "Timeout increased successfully"}
    )


class CreateSessionResponse(BaseModel):
    session_id: UUID
    url: Optional[str] = None


# Create a new session for a machine, return the session id and url
@router.post(
    "/session",
    openapi_extra={
        "x-speakeasy-name-override": "create",
    },
)
async def create_session(
    request: Request,
    body: CreateSessionBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> CreateSessionResponse:
    org_id = request.state.current_user.get("org_id")
    user_id = request.state.current_user.get("user_id")
    plan = request.state.current_user.get("plan")

    if plan == "free":
        raise HTTPException(
            status_code=403,
            detail="Free plan users cannot create sessions, we are mitigating abuse, please wait",
        )

        max_concurrent_sessions = 1
        max_timeout_minutes = 30

        # Check timeout limit
        if body.timeout and body.timeout > max_timeout_minutes:
            raise HTTPException(
                status_code=400,
                detail=f"Free plan users are limited to {max_timeout_minutes} minutes timeout",
            )

        # find all gpu event on this account
        gpu_events = (
            (
                await db.execute(
                    select(GPUEvent)
                    .apply_org_check(request)
                    .where(GPUEvent.session_id.isnot(None))
                    .where(GPUEvent.end_time.is_(None))
                )
            )
            .scalars()
            .all()
        )

        if len(gpu_events) >= max_concurrent_sessions:
            raise HTTPException(
                status_code=400, detail="Free plan does not support concurrent sessions"
            )
    
    # Optimize by using COUNT directly in the database query
    max_concurrent_sessions = 5
    
    session_count_query = (
        select(func.count())
        .select_from(GPUEvent)
        .where(GPUEvent.machine_id == body.machine_id)
        .where(GPUEvent.session_id.isnot(None))
        .where(GPUEvent.end_time.is_(None))
    )
    
    # use func count cannot use apply_org_check
    if org_id:
        session_count_query = session_count_query.where(GPUEvent.org_id == org_id)
    else:
        session_count_query = session_count_query.where((GPUEvent.user_id == user_id) & (GPUEvent.org_id.is_(None)))
        
    session_count = await db.execute(session_count_query)
    session_count = session_count.scalar_one()

    if session_count >= max_concurrent_sessions:
        raise HTTPException(
            status_code=400, detail="You have reached the maximum number of concurrent sessions. Please close some sessions and try again."
        )

    # check if the user has reached the spend limit
    # exceed_spend_limit = await is_exceed_spend_limit(request, db)
    # if exceed_spend_limit:
    #     raise HTTPException(status_code=400, detail="Spend limit reached")

    machine_id = body.machine_id
    machine = cast(
        Optional[Machine],
        (
            await db.execute(
                (
                    select(Machine)
                    .where(Machine.id == machine_id)
                    .apply_org_check(request)
                )
            )
        ).scalar_one_or_none(),
    )

    if machine is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    if machine.type != "comfy-deploy-serverless":
        raise HTTPException(
            status_code=400,
            detail="Machine is not a Comfy Deploy Serverless machine",
        )

    if int(machine.machine_builder_version) < 4:
        raise HTTPException(
            status_code=400,
            detail="Machine builder version is not larger than 4",
        )

    session_id = uuid4()

    gpu_event = GPUEvent(
        id=uuid4(),
        user_id=user_id,
        org_id=org_id,
        machine_id=machine_id,
        machine_version_id=machine.machine_version_id,
        gpu=body.gpu.value if body.gpu is not None else machine.gpu,
        gpu_provider="modal",
        session_id=str(session_id),
        session_timeout=body.timeout or 15,
    )

    db.add(gpu_event)
    await db.commit()

    # Add the background task
    try:
        if not body.wait_for_server:
            print("async_creation")
            q = asyncio.Queue()

            task = asyncio.create_task(
                create_session_background_task(
                    machine_id,
                    session_id,
                    request,
                    body.timeout or 15,
                    body.gpu.value if body.gpu is not None else machine.gpu,
                    q,
                )
            )

            background_tasks.add_task(
                ensure_session_creation_complete,
                task,
            )

            try:
                modal_function_id = await asyncio.wait_for(q.get(), timeout=10.0)
                print("async_creation", "modal_function_id", modal_function_id)
            except asyncio.TimeoutError:
                print("Timed out waiting for modal_function_id")
                modal_function_id = None
        else:
            gpuEvent = await create_session_background_task(
                machine_id,
                session_id,
                request,
                body.timeout or 15,
                str(body.gpu.value if body.gpu is not None else machine.gpu),
            )

            return {
                "session_id": session_id,
                "url": gpuEvent.tunnel_url,
            }
    except Exception as e:
        logger.error(f"Session creation failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Session creation failed")

    return {
        "session_id": session_id,
    }


@beta_router.post(
    "/deps",
    openapi_extra={
        "x-speakeasy-name-override": "generateDockerSteps",
    },
)
async def convert_to_docker_steps(
    body: DepsBody,
):
    converted = generate_all_docker_commands(body)

    return JSONResponse(status_code=200, content=converted)


def extract_hash(dependency_string):
    parts = dependency_string.split("@")
    if len(parts) > 1:
        return parts[-1]
    return ""


def extract_url(dependency_string):
    parts = dependency_string.split("@")
    if len(parts) > 1:
        return "https://github.com/" + parts[0]
    return ""


def send_log_entry(
    session_id: UUID,
    machine_id: Optional[str],
    log_message: str,
    log_type: str = "info",
):
    data = [
        (
            uuid4(),
            session_id,
            None,
            machine_id,
            datetime.now(),
            log_type,
            log_message,
        )
    ]
    asyncio.create_task(insert_to_clickhouse("log_entries", data))


os.environ["MODAL_IMAGE_BUILDER_VERSION"] = "2024.04"


async def create_dynamic_sesssion_background_task(
    request: Request,
    gpu_event_id: str,
    session_id: UUID,
    body: CreateDynamicSessionBody,
    status_queue: Optional[any] = None,
):
    # app = modal.App(str(session_id))
    # if status_queue is not None:
    #     print("status_queue is not None")

    try:
        org_id = request.state.current_user.get("org_id")
        user_id = request.state.current_user.get("user_id")

        # Get docker configuration using the shared function
        async with get_db_context() as db:
            docker_config = await get_docker_commands_from_dynamic_session_body(
                request, body, db
            )

        gpu = (
            body.gpu.value if body.gpu is not None and body.gpu.value != "CPU" else None
        )

        shared_model_volume_name = os.environ.get("SHARED_MODEL_VOLUME_NAME")

        # session_manager = await modal.App.lookup.aio("session_manager")
        run_session = await modal.Function.lookup.aio("session_manager", "run_session")
        machine_version = docker_config.get("machine_version")
        send_log_entry(session_id, body.machine_id, "Starting session")
        result = await run_session.spawn.aio(
            user_id=user_id,
            org_id=org_id,
            session_id=session_id,
            machine_id=body.machine_id,
            machine_version_id=machine_version.get("id") if machine_version else None,
            docker_config=docker_config,
            gpu=gpu,
            timeout=6 * 60 * 60,  # 6 hours
            workdir="/comfyui",
            encrypted_ports=[8188],
            update_endpoint=session_callback_endpoint,
            comfyui_cmd=comfyui_cmd(
                cpu=True if body.gpu == "CPU" else False,
                install_latest_comfydeploy=True,
            ),
            shared_model_volume_name=shared_model_volume_name,
            tunnel_url_queue=status_queue,
        )
        return result.object_id

    except Exception as e:
        logger.error(f"Error creating dynamic session: {str(e)}")
        async with get_db_context() as db:
            await delete_session(request, str(session_id), db=db)
            
    return None


@beta_router.get(
    "/session/dynamic/docker-commands",
)
async def get_docker_commands_from_dynamic_session_body(
    request: Request,
    body: CreateDynamicSessionBody,
    # background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    try:
        machine_version: Optional[MachineVersion] = None
        machine: Optional[Machine] = None

        # Get machine and its version info
        modal_image_id = None
        if body.machine_id is not None:
            machine = await db.execute(
                select(Machine)
                .where(Machine.id == body.machine_id)
                .apply_org_check(request)
            )
            machine = machine.scalars().first()

            if not machine:
                raise HTTPException(status_code=404, detail="Machine not found")

            target_machine_version_id = (
                body.machine_version_id or machine.machine_version_id
            )

            # Get machine version if it exists
            if target_machine_version_id:
                machine_version = await db.execute(
                    select(MachineVersion).where(
                        MachineVersion.id == target_machine_version_id,
                        MachineVersion.machine_id == body.machine_id,
                    )
                )
                machine_version = machine_version.scalars().first()

                if machine_version and machine_version.modal_image_id:
                    # Use existing modal image ID from version
                    modal_image_id = machine_version.modal_image_id

            if not modal_image_id:
                logger.info("No dependencies, using machine current settings")
                body.dependencies = DepsBody(
                    comfyui_version=machine.comfyui_version or comfyui_hash,
                    docker_command_steps=machine.docker_command_steps,
                )

        logger.info("Dependencies configuration " + str(body.dependencies))

        if (
            not modal_image_id and body.dependencies is not None
        ) or body.machine_id is None:
            if body.dependencies is None:
                body.dependencies = []

            if isinstance(body.dependencies, list):
                # Handle shorthand dependencies
                deps_body = DepsBody(
                    comfyui_version=body.comfyui_hash or comfyui_hash,
                    docker_command_steps=DockerSteps(
                        steps=[
                            DockerStep(
                                type="custom-node",
                                data=CustomNode(
                                    install_type="git-clone",
                                    url=extract_url(dep),
                                    hash=extract_hash(dep),
                                    name=dep.split("/")[-1],
                                ),
                            )
                            for dep in body.dependencies
                        ]
                    ),
                )
                # We only install comfyui manager if this is a brand new machine
                converted = generate_all_docker_commands(
                    deps_body, include_comfyuimanager=body.machine_id is None
                )
            else:
                converted = generate_all_docker_commands(
                    body.dependencies, include_comfyuimanager=body.machine_id is None
                )

            python_version = "3.11"

            # Python version to override - first check body, then machine_version
            if body.python_version is not None:
                python_version = body.python_version
            elif machine_version is not None:
                python_version = machine_version.python_version

            # Base docker image to use - first check body, then machine_version, then fallback to debian slim
            base_docker_image = None
            if body.base_docker_image is not None:
                base_docker_image = body.base_docker_image
            elif (
                machine_version is not None
                and machine_version.base_docker_image is not None
            ):
                base_docker_image = machine_version.base_docker_image

            # Convert SQLAlchemy models to dictionaries
            machine_version_dict = None
            machine_dict = None

            if machine_version:
                machine_version_dict = {
                    "id": str(machine_version.id),
                    "version": machine_version.version,
                    "python_version": machine_version.python_version,
                    "base_docker_image": machine_version.base_docker_image,
                    "modal_image_id": machine_version.modal_image_id,
                }

            if machine:
                machine_dict = {
                    "id": str(machine.id),
                    "name": machine.name,
                    "type": machine.type,
                    "status": machine.status,
                }

            return {
                "docker_commands": converted.docker_commands,
                "python_version": python_version,
                "base_docker_image": base_docker_image,
                "modal_image_id": modal_image_id,
                "machine_version": machine_version_dict,
                "machine": machine_dict,
                "install_custom_node_with_gpu": machine_version.install_custom_node_with_gpu
                if machine_version
                else False,
            }
        else:
            if machine_version:
                machine_version_dict = {
                    "id": str(machine_version.id),
                    "version": machine_version.version,
                    "python_version": machine_version.python_version,
                    "base_docker_image": machine_version.base_docker_image,
                    "modal_image_id": machine_version.modal_image_id,
                }
            return {
                "modal_image_id": modal_image_id,
                "message": "Using existing modal image",
                "machine_version": machine_version_dict,
            }

    except Exception as e:
        logger.error(f"Error generating docker commands: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


class UpdateSessionCallbackBody(BaseModel):
    session_id: str
    sandbox_id: Optional[str] = None
    tunnel_url: str
    machine_version_id: Optional[str] = None


@beta_router.post(
    "/session/callback",
)
async def update_session_callback(
    request: Request,
    body: UpdateSessionCallbackBody,
) -> Dict[str, Any]:
    # We should do an auth check here
    async with get_db_context() as db:
        await db.execute(
            update(GPUEvent)
            .where(GPUEvent.session_id == body.session_id)
            .values(
                tunnel_url=body.tunnel_url,
                # Gonna override the modal_function_id with the sandbox_id
                modal_function_id=body.sandbox_id,
                start_time=datetime.now(),
                machine_version_id=body.machine_version_id,
            )
        )

    return {"success": True}


class UpdateSessionLogBody(BaseModel):
    session_id: str
    machine_id: Optional[str]
    log: str


@beta_router.post(
    "/session/callback/log",
)
async def update_session_log(
    request: Request,
    body: UpdateSessionLogBody,
) -> Dict[str, Any]:
    send_log_entry(body.session_id, body.machine_id, body.log)
    return {"success": True}


class SessionCheckTimeoutBody(BaseModel):
    session_id: str


@beta_router.post(
    "/session/callback/check-timeout",
)
async def session_check_timeout(
    request: Request,
    body: SessionCheckTimeoutBody,
) -> Dict[str, Any]:
    await check_and_close_sessions(request, body.session_id)
    return {"success": True}


@beta_router.post(
    "/session/dynamic",
    openapi_extra={
        "x-speakeasy-name-override": "createDynamic",
    },
)
async def create_dynamic_session(
    request: Request,
    body: CreateDynamicSessionBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> CreateSessionResponse:
    session_id = uuid4()

    # Validate free plan first before any other operations
    plan = request.state.current_user.get("plan")
    if plan == "free":
        raise HTTPException(
            status_code=403,
            detail="Free plan users cannot create sessions, we are mitigating abuse, please wait",
        )

        max_concurrent_sessions = 1
        max_timeout_minutes = 30

        if body.timeout and body.timeout > max_timeout_minutes:
            raise HTTPException(
                status_code=400,
                detail=f"Free plan users are limited to {max_timeout_minutes} minutes timeout",
            )

        gpu_events = (
            (
                await db.execute(
                    select(GPUEvent)
                    .apply_org_check(request)
                    .where(GPUEvent.session_id.isnot(None))
                    .where(GPUEvent.end_time.is_(None))
                )
            )
            .scalars()
            .all()
        )

        if len(gpu_events) >= max_concurrent_sessions:
            raise HTTPException(
                status_code=400, detail="Free plan does not support concurrent sessions"
            )

    # Create tasks for parallel execution
    async def create_gpu_event():
        org_id = request.state.current_user.get("org_id")
        user_id = request.state.current_user.get("user_id")
        gpu_event_id = str(uuid4())

        async with get_db_context() as db:
            new_gpu_event = GPUEvent(
                id=gpu_event_id,
                user_id=user_id,
                org_id=org_id,
                session_id=str(session_id),
                machine_id=body.machine_id,
                gpu=body.gpu.value if body.gpu is not None else "CPU",
                session_timeout=body.timeout or 15,
                gpu_provider="modal",
            )
            db.add(new_gpu_event)
            await db.commit()
            await db.refresh(new_gpu_event)
            return gpu_event_id

    async def set_timeout_redis():
        timeout_duration = body.timeout or 15
        timeout_end_time = datetime.utcnow() + timedelta(minutes=timeout_duration)
        await redis.set(
            f"session:{session_id}:timeout_end", timeout_end_time.isoformat()
        )

    # Run remaining setup tasks in parallel
    setup_tasks = [create_gpu_event(), set_timeout_redis()]

    try:
        results = await asyncio.gather(*setup_tasks)
        gpu_event_id = results[0]  # Get GPU event ID from create_gpu_event result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if body.wait_for_server:
        async with modal.Queue.ephemeral() as q:
            modal_function_run_id = await create_dynamic_sesssion_background_task(
                request,
                gpu_event_id,
                session_id,
                body,
                q,
            )

            if modal_function_run_id is not None:
                async with get_db_context() as db:
                    await db.execute(
                        update(GPUEvent)
                        .where(GPUEvent.session_id == str(session_id))
                        .values(
                            modal_function_id=modal_function_run_id,
                        )
                    )

            try:
                tunnel_url = await q.get.aio(timeout=300.0)
                if isinstance(tunnel_url, dict) and tunnel_url.get("error") is not None:
                    raise HTTPException(status_code=400, detail=tunnel_url.get("error"))
                return {
                    "session_id": session_id,
                    "url": tunnel_url,
                }
            except Exception as e:
                return {
                    "session_id": session_id,
                    "url": None,
                }
    else:
        modal_function_run_id = await create_dynamic_sesssion_background_task(
            request, gpu_event_id, session_id, body
        )
        
        if modal_function_run_id is not None:
            async with get_db_context() as db:
                await db.execute(
                    update(GPUEvent)
                    .where(GPUEvent.session_id == str(session_id))
                    .values(
                        modal_function_id=modal_function_run_id,
                    )
                )
        
        return {
            "session_id": session_id,
        }


class SnapshotSessionBody(BaseModel):
    machine_name: Optional[str] = None


# You can only snapshot a new machine
@router.post("/session/{session_id}/snapshot")
async def snapshot_session(
    request: Request,
    session_id: str,
    body: Optional[SnapshotSessionBody] = None,
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    current_user = request.state.current_user
    user_id = current_user["user_id"]
    org_id = current_user["org_id"] if "org_id" in current_user else None

    gpuEvent = cast(
        Optional[GPUEvent],
        (
            await db.execute(
                (
                    select(GPUEvent)
                    .where(GPUEvent.session_id == session_id)
                    .where(GPUEvent.end_time.is_(None))
                    .apply_org_check(request)
                )
            )
        ).scalar_one_or_none(),
    )

    if gpuEvent is None:
        raise HTTPException(
            status_code=404, detail="GPUEvent not found session_id: " + session_id
        )

    modal_function_id = gpuEvent.modal_function_id

    if modal_function_id is None:
        raise HTTPException(status_code=400, detail="Modal function id not found")

    if not modal_function_id.startswith("sb-"):
        raise HTTPException(
            status_code=400, detail="Modal function id is not a sandbox"
        )

    machine = None

    if (
        gpuEvent.machine_id is None
        and body is not None
        and body.machine_name is not None
    ):
        machine = Machine(
            id=uuid4(),
            type=MachineType.COMFY_DEPLOY_SERVERLESS,
            user_id=user_id,
            org_id=org_id,
            status="ready",
            endpoint="not-ready",
            created_at=func.now(),
            updated_at=func.now(),
            name=body.machine_name,
            gpu=gpuEvent.gpu,
            machine_builder_version="4",
            is_workspace=True,
        )
        db.add(machine)
        await db.flush()

        gpuEvent.machine_id = machine.id

    if gpuEvent.machine_id is None:
        raise HTTPException(status_code=400, detail="Machine id not found")

    # Get the sandbox and create snapshot
    sb = await modal.Sandbox.from_id.aio(modal_function_id)
    image = sb.snapshot_filesystem()
    image_id = image.object_id

    print("image_id", image_id, image)

    # Get current user info
    current_user = request.state.current_user
    user_id = current_user["user_id"]

    if machine is None:
        machine = await db.execute(
            select(Machine)
            .where(Machine.id == gpuEvent.machine_id)
            .apply_org_check(request)
        )
        machine = machine.scalar_one()

    # Get next version number
    current_version = await db.execute(
        select(func.max(MachineVersion.version)).where(
            MachineVersion.machine_id == machine.id
        )
    )
    next_version = (current_version.scalar() or 0) + 1

    # Create new version with image_id
    machine_version = MachineVersion(
        id=uuid4(),
        machine_id=machine.id,
        version=next_version,
        user_id=user_id,
        created_at=func.now(),
        updated_at=func.now(),
        modal_image_id=image_id,
        **{col: getattr(machine, col) for col in get_machine_columns().keys()},
    )
    db.add(machine_version)

    # Update machine with new version id
    machine.machine_version_id = machine_version.id
    machine.updated_at = func.now()

    gpuEvent.machine_version_id = machine_version.id

    await db.commit()
    await db.refresh(machine)
    await db.refresh(gpuEvent)
    await db.refresh(machine_version)

    await redeploy_machine(request, db, machine, machine_version, background_tasks)

    return JSONResponse(
        content={
            "message": "Session snapshot created successfully",
            "version_id": str(machine_version.id),
            "version": next_version,
            "image_id": image_id,
        }
    )


class DeleteSessionResponse(BaseModel):
    success: bool


# Delete a session by id
@router.delete(
    "/session/{session_id}",
    openapi_extra={
        "x-speakeasy-name-override": "cancel",
    },
)
async def delete_session(
    request: Request,
    session_id: str,
    wait_for_shutdown: bool = False,
    db: AsyncSession = Depends(get_db),
) -> DeleteSessionResponse:
    # If this is from our modal, we need to force close
    is_modal = request.state.current_user.get("modal", False)
    # print("is_modal", is_modal)
    
    gpuEvent = cast(
        Optional[GPUEvent],
        (
            await db.execute(
                (
                    select(GPUEvent)
                    .where(GPUEvent.session_id == str(session_id))
                    .where(GPUEvent.end_time.is_(None))
                    .apply_org_check(request)
                )
            )
        ).scalar_one_or_none(),
    )

    if gpuEvent is None:
        raise HTTPException(
            status_code=404,
            detail="GPUEvent not found session_id: "
            + session_id
            + ", the event might be already closed",
        )

    modal_function_id = gpuEvent.modal_function_id

    # Checking for a stuck case and modal_function_id is not set
    if (
        gpuEvent.start_time is None
        and gpuEvent.end_time is None
        and not modal_function_id
    ):
        # Check if the session has been stuck for over an hour
        time_difference = datetime.utcnow() - gpuEvent.created_at
        # logfire.info("time_difference", time_difference=time_difference.total_seconds())
        # If this is from our modal, we need to force close
        if time_difference.total_seconds() > 3600:  # 3600 seconds = 1 hour
            gpuEvent.start_time = gpuEvent.created_at
            gpuEvent.end_time = gpuEvent.created_at
            await db.commit()
            await db.refresh(gpuEvent)
            # We will eat this usage
            logfire.info(
                "Session stuck for over an hour, eating usage",
                session_id=session_id,
                possible_duration=time_difference.total_seconds(),
                gpu_type=gpuEvent.gpu,
            )
            return {"success": True}

    # The reason that we are skipping this check for modal, is because tere will be a case the sandbox is still building
    # and this will make sure it let it pass thru and contiune and turn off the session in our system
    # Still there could be a case when it really get stuck in buildng and modal continue to start it afterawrd..
    if modal_function_id is None and not is_modal:
        raise HTTPException(status_code=400, detail="Modal function id not found")

    is_sandbox = modal_function_id.startswith("sb-")

    try:
        if is_sandbox:
            await modal.Sandbox.from_id(modal_function_id).terminate.aio()
        else:
            call_graph = await modal.functions.FunctionCall.from_id(modal_function_id).get_call_graph.aio()
            call_status = call_graph[0].status if call_graph else None
     
            # If already terminated/failed/timed out, just update the database
            if call_status in [InputStatus.TERMINATED, InputStatus.FAILURE, InputStatus.TIMEOUT, InputStatus.INIT_FAILURE]:
                if gpuEvent.start_time is not None and gpuEvent.end_time is None:
                    gpuEvent.end_time = gpuEvent.start_time
                    await db.commit()
                    await db.refresh(gpuEvent)
                    logfire.error("Session end has some problem, force closing (reset to 0)", status=call_status.name)
            else:
                # Only attempt cancellation if not already terminated
                await modal.functions.FunctionCall.from_id(modal_function_id).cancel.aio()
    except Exception as e:
        logger.error(f"Error handling modal function {modal_function_id}: {str(e)}")
        
        # Try to handle gRPC-style exceptions by checking for status code attribute
        error_info = getattr(e, "args", None)
        if error_info and isinstance(error_info, tuple) and len(error_info) >= 1:
            # The first element might be the status enum
            status = error_info[0]
            status_code = getattr(status, "value", None)
            
            # Check if it's a NOT_FOUND status (5)
            if status_code == 5 or (isinstance(status, tuple) and status[0] == 5):
                if gpuEvent.start_time is not None and gpuEvent.end_time is None:
                    gpuEvent.end_time = gpuEvent.start_time
                    await db.commit()
                    await db.refresh(gpuEvent)
                    logfire.error("Session end has some problem, function ID might be expired", function_id=modal_function_id)

    # Update GPU event end time if is sandbox
    if is_sandbox:
        if gpuEvent.end_time is None and gpuEvent.start_time is not None:
            gpuEvent.end_time = datetime.now()
            await db.commit()
            await db.refresh(gpuEvent)

        # Send usage data to Autumn if its a sandbox, else will be handled in the lifecycle callback
        if gpuEvent.end_time is not None and gpuEvent.start_time is not None:
            await send_autumn_usage_event(
                customer_id=gpuEvent.org_id or gpuEvent.user_id,
                gpu_type=gpuEvent.gpu,
                start_time=gpuEvent.start_time,
                end_time=gpuEvent.end_time,
                environment=gpuEvent.environment,
                idempotency_key=str(gpuEvent.id),
            )
        else:
            logfire.error(f"Session {session_id} has no start or end time when closing")
        
    # Update GPU event end time
    # This cause probably they are closing the session before the build is complete
    if gpuEvent.end_time is None and gpuEvent.start_time is None:
        gpuEvent.start_time = datetime.now()
        gpuEvent.end_time = datetime.now()
        await db.commit()
        await db.refresh(gpuEvent)

    await redis.delete("session:" + session_id + ":timeout_end")

    if wait_for_shutdown:
        max_wait_time = 30  # Maximum wait time in seconds
        start_time = datetime.now()

        while True:
            # Check if we've exceeded max wait time
            if (datetime.now() - start_time).total_seconds() > max_wait_time:
                break

            # Refresh GPU event to get latest status
            await db.refresh(gpuEvent)

            if gpuEvent.end_time is not None:
                break

            await asyncio.sleep(1)

    return {"success": True}


async def check_and_close_sessions(request: Request, session_id: str):
    try:
        # while True:
        # Construct the specific Redis key for the session
        key = f"session:{session_id}:timeout_end"
        logger.info(f"Checking session {session_id}")

        # Retrieve the timeout end time from Redis
        timeout_end_str = await redis.get(key)
        if timeout_end_str is None:
            logger.info(f"No timeout end found for session {session_id}")
            return
            # break

        try:
            timeout_end = datetime.fromisoformat(timeout_end_str)
        except ValueError:
            logger.error(f"Invalid date format for session {session_id}")
            return
            # break

        if datetime.utcnow() > timeout_end:
            # Close the session
            async with get_db_context() as db:
                await delete_session(request, session_id, db=db)
            # Optionally, delete the key from Redis
            logger.info(f"Session {session_id} closed due to timeout")
            return {"message": "Session closed due to timeout", "continue": False}
            # break
        # else:
        #     await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"Error checking session {session_id}: {str(e)}")

