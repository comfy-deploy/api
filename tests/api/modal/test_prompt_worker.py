"""Regression tests for the optimized-cold-start ComfyUI prompt worker.

`_ComfyDeployRunner.prompt_worker` (src/api/modal/v4/my_app.py) is a fork of
ComfyUI's own `main.py:prompt_worker`, used only by
`ComfyDeployRunnerOptimizedImports` (the "Optimized Cold Start" machine flag),
which boots ComfyUI in-process instead of as a subprocess.

Because it is a fork, it drifts from upstream. It drifted badly enough to break:
ComfyUI v0.3.68 changed `PromptExecutor(server, cache_type, cache_size)` to
`cache_args` (a dict), and `execute_async` began dereferencing
`self.cache_args["ram"]`. The fork constructed `PromptExecutor(server)` with no
cache config, so `cache_args` stayed `None`. From v0.20.0 that dereference sits
*above* `execute_async`'s try/except, so it raised immediately after the
`execution_start` event, killed the daemon worker thread, and left every run
wedged in "running" forever with no error surfaced to the user.

These tests pin both halves of the fix:
  1. the executor is constructed with cache args the installed ComfyUI accepts;
  2. the worker survives *any* executor exception and always calls task_done,
     so a run can never silently hang again.

my_app.py can't be imported directly (it pulls in modal + a generated
`cd_config`), so the functions under test are extracted from its AST verbatim.
"""

import ast
import inspect
import logging
import queue
import sys
import threading
import types
from collections import namedtuple
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

MY_APP = Path(__file__).resolve().parents[3] / "src/api/modal/v4/my_app.py"


def _extract(names):
    """Pull functions out of _ComfyDeployRunner verbatim, without importing modal."""
    tree = ast.parse(MY_APP.read_text())
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "_ComfyDeployRunner"
    )
    funcs = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {f.name for f in funcs} == set(names), f"missing: {set(names) - {f.name for f in funcs}}"
    ns = {"logging": logging, "inspect": inspect, "gc": __import__("gc")}
    mod = ast.Module(body=funcs, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, str(MY_APP), "exec"), ns)
    return ns


_NS = _extract(["prompt_worker", "build_prompt_executor"])


class Runner:
    prompt_executor = None
    build_prompt_executor = staticmethod(_NS["build_prompt_executor"])
    prompt_worker = _NS["prompt_worker"]


ExecutionStatus = namedtuple("ExecutionStatus", ["status_str", "completed", "messages"])


class FakePromptQueue:
    """Mirrors execution.PromptQueue.task_done as of ComfyUI v0.30.x."""

    def __init__(self):
        self.items = queue.Queue()
        self.currently_running = {}
        self.history = {}
        self.done = threading.Event()

    def put(self, item):
        item_id = len(self.currently_running) + 1
        self.currently_running[item_id] = item
        self.items.put((item, item_id))

    def get(self, timeout=None):
        try:
            return self.items.get(timeout=0.5)
        except queue.Empty:
            return None

    def task_done(self, item_id, history_result, status, process_item=None):
        prompt = self.currently_running.pop(item_id)  # KeyError on double-done
        if process_item is not None:
            prompt = process_item(prompt)
        self.history[prompt[1]] = {
            "prompt": prompt, "outputs": {}, "status": status._asdict()
        }
        self.history[prompt[1]].update(history_result)  # TypeError if not a dict
        self.done.set()

    def get_flags(self):
        return {}


class FakePromptExecutor:
    """Signature and first statements copied from ComfyUI v0.30.x execution.py."""

    def __init__(self, server, cache_type=False, cache_args=None):
        self.cache_args = cache_args
        self.cache_type = cache_type
        self.server = server
        self.success = True
        self.status_messages = []
        self.seen_extra_data = None
        self.raise_on_execute = False

    def execute(self, prompt, prompt_id, extra_data, execute_outputs):
        self.seen_extra_data = extra_data
        # execute_async's first real statements, which sit ABOVE its try:
        int(self.cache_args["ram"] * (1024 ** 3))
        int(self.cache_args["ram_inactive"] * (1024 ** 3))
        if self.raise_on_execute:
            raise RuntimeError("boom inside executor")
        self.success = True
        self.history_result = {"outputs": {"9": "image.png"}, "meta": {}}


class RaisingPromptExecutor(FakePromptExecutor):
    # Keeps the real __init__ signature: build_prompt_executor introspects it,
    # so *args/**kwargs here would silently select the pre-v0.3.68 branch.
    def __init__(self, server, cache_type=False, cache_args=None):
        super().__init__(server, cache_type=cache_type, cache_args=cache_args)
        self.raise_on_execute = True


class LegacyPromptExecutor(FakePromptExecutor):
    """ComfyUI <= v0.3.67: cache_size, and no cache_args dereference."""

    def __init__(self, server, cache_type=False, cache_size=None):
        super().__init__(server, cache_type=cache_type, cache_args=None)

    def execute(self, prompt, prompt_id, extra_data, execute_outputs):
        self.seen_extra_data = extra_data
        self.success = True
        self.history_result = {"outputs": {}, "meta": {}}


class FakeServer:
    def __init__(self):
        self.client_id = "cid"
        self.last_prompt_id = None
        self.sent = []

    def send_sync(self, event, data, sid=None):
        self.sent.append((event, data))


class _FakeModelManagement:
    unload_all_models = cleanup_models = soft_empty_cache = staticmethod(lambda: None)


@pytest.fixture
def comfy_modules(monkeypatch):
    """Install fake `execution` / `comfy` modules for the worker's local imports."""

    def install(executor_cls):
        execution = types.ModuleType("execution")
        execution.PromptExecutor = executor_cls
        execution.CacheType = types.SimpleNamespace(
            CLASSIC=0, LRU=1, NONE=2, RAM_PRESSURE=3
        )
        execution.PromptQueue = types.SimpleNamespace(ExecutionStatus=ExecutionStatus)
        comfy = types.ModuleType("comfy")
        comfy.model_management = _FakeModelManagement
        monkeypatch.setitem(sys.modules, "execution", execution)
        monkeypatch.setitem(sys.modules, "comfy", comfy)

    return install


def drive(install, executor_cls, item, worker=None, timeout=10):
    install(executor_cls)
    q, server = FakePromptQueue(), FakeServer()
    q.put(item)
    target = worker or Runner().prompt_worker
    t = threading.Thread(target=target, args=(q, server), daemon=True)
    t.start()
    return q.done.wait(timeout=timeout), q, server, t


SENSITIVE = {"api_key_comfy_org": "sk-secret"}
ITEM6 = (0, "pid-1", {"1": {}}, {"client_id": "cid"}, ["9"], SENSITIVE)
ITEM5 = (0, "pid-2", {"1": {}}, {"client_id": "cid"}, ["9"])


class TestBuildPromptExecutor:
    def test_passes_cache_args_on_modern_comfyui(self, comfy_modules):
        comfy_modules(FakePromptExecutor)
        import execution

        e = Runner.build_prompt_executor(execution, FakeServer())
        assert e.cache_args == {"lru": 0, "ram": 0.0, "ram_inactive": 0.0}
        assert e.cache_type == execution.CacheType.CLASSIC

    def test_omits_cache_args_on_legacy_comfyui(self, comfy_modules):
        """<= v0.3.67 has no cache_args param; passing it would TypeError."""
        comfy_modules(LegacyPromptExecutor)
        import execution

        e = Runner.build_prompt_executor(execution, FakeServer())
        assert e.cache_args is None


class TestPromptWorker:
    def test_happy_path_merges_sensitive_and_strips_it_from_history(self, comfy_modules):
        ok, q, _server, t = drive(comfy_modules, FakePromptExecutor, ITEM6)
        assert ok, "task_done was never called - the run would hang"
        assert t.is_alive()
        # v0.3.68+ moved comfy.org auth out of extra_data into slot 5
        assert Runner.prompt_executor.seen_extra_data["api_key_comfy_org"] == "sk-secret"
        assert "api_key_comfy_org" not in ITEM6[3], "must not mutate the queued dict"
        assert len(q.history["pid-1"]["prompt"]) == 5, "sensitive slot leaked to history"
        assert q.history["pid-1"]["outputs"] == {"9": "image.png"}
        assert q.history["pid-1"]["status"]["status_str"] == "success"

    def test_executor_exception_fails_the_run_instead_of_hanging(self, comfy_modules):
        ok, q, server, t = drive(comfy_modules, RaisingPromptExecutor, ITEM6)
        assert ok, "task_done was never called - the run would hang"
        assert t.is_alive(), "worker thread died; every later run would hang too"
        assert q.history["pid-1"]["status"]["status_str"] == "error"
        assert q.history["pid-1"]["status"]["completed"] is False
        assert q.history["pid-1"]["outputs"] == {}, "stale outputs leaked on failure"

        errors = [d for e, d in server.sent if e == "execution_error"]
        assert errors, "client was never told the run failed"
        assert "boom inside executor" in errors[0]["exception_message"]

    def test_legacy_five_tuple_queue_item(self, comfy_modules):
        ok, _q, _server, _t = drive(comfy_modules, FakePromptExecutor, ITEM5)
        assert ok
        assert Runner.prompt_executor.seen_extra_data == {"client_id": "cid"}


class TestOriginalBugIsReproducible:
    """Guards the guard: proves these tests would have caught the original bug."""

    @staticmethod
    def _unguarded_worker(q, server):
        """The pre-fix worker: no cache_args, no try/except around execute."""
        import comfy  # noqa: F401
        import execution

        e = execution.PromptExecutor(server)
        while True:
            queue_item = q.get(timeout=1000.0)
            if queue_item is not None:
                item, item_id = queue_item
                server.last_prompt_id = item[1]
                e.execute(item[2], item[1], item[3], item[4])
                q.task_done(
                    item_id,
                    e.history_result,
                    status=execution.PromptQueue.ExecutionStatus(
                        status_str="success" if e.success else "error",
                        completed=e.success,
                        messages=e.status_messages,
                    ),
                )

    # The uncaught thread exception IS the bug: it goes to stderr, never to
    # logging, which is why nothing showed up in the user's machine logs.
    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_unguarded_worker_wedges_the_run(self, comfy_modules):
        ok, q, server, t = drive(
            comfy_modules, FakePromptExecutor, ITEM6,
            worker=self._unguarded_worker, timeout=3,
        )
        assert not ok, "expected task_done never to fire with cache_args=None"
        assert not t.is_alive(), "expected the worker thread to die"
        # This is exactly what the user saw: stuck in 'running', no error.
        assert 1 in q.currently_running
        assert not any(e == "execution_error" for e, _ in server.sent)
