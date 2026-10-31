"""Security regression tests for the machine builder (src/api/modal/builder.py).

1. build_logic splices the machine config into my_app.py as Python source and
   runs `modal deploy` on the API host. The config contains user-controlled
   strings (prestart_command, extra_args, machine name, ...). They used to be
   interpolated as '{value}', so a value containing a quote could inject code
   that ran on the API host. render_config_source must emit pure data.
2. The /machine/create, keep-warm and cancel-function HTTP routes are internal
   operations and must be restricted to the admin org.
"""

import ast
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.modal import builder
from api.modal.builder import render_config_source

pytestmark = pytest.mark.unit

INJECTION = "x' + str(__import__('os').system('touch /tmp/pwned')) + '"


def base_config(**overrides):
    config = {
        "name": "my machine",
        "version_id": "7b0c1c0e-9b8a-4f4e-8f3e-2f9d4d1c0a11",
        "deploy_test": "False",
        "gpu": "A10G",
        "pip": ["numpy==1.26.4", "opencv-python"],
        "run_timeout": 300,
        "idle_timeout": 60,
        "legacy_mode": "False",
        "base_docker_image": "",
        "python_version": "3.11",
        "prestart_command": "pip install -U some-package && echo ready",
        "extra_args": "--fast --preview-method auto",
        "secrets": {"HF_HOME": "/models/hf", "API_KEY": "abc123"},
        "cpu_request": 2.5,
        "memory_limit": None,
        "models_to_cache": [],
        "enable_gpu_memory_snapshot": False,
        "docker_commands": [["RUN apt-get update", "RUN echo 'hi'"]],
        "deps": {"comfyui": "abc123", "git_custom_nodes": {"https://github.com/a/b": {"hash": "def"}}},
    }
    config.update(overrides)
    return config


def old_render(config):
    """The pre-fix renderer, kept here to prove output parity for normal values."""
    lines = ["config = {"]
    for key, value in config.items():
        if isinstance(value, str):
            lines.append(f"    '{key}': '{value}',")
        else:
            lines.append(f"    '{key}': {value},")
    lines.append("}")
    return "\n".join(lines)


def evaluate(source):
    """Assert the source is exactly one `config = <literal>` and return the literal."""
    tree = ast.parse(source)
    assert len(tree.body) == 1
    assign = tree.body[0]
    assert isinstance(assign, ast.Assign) and assign.targets[0].id == "config"
    return ast.literal_eval(assign.value)


def test_normal_config_round_trips():
    config = base_config()
    assert evaluate(render_config_source(config)) == config


def test_output_matches_old_renderer_for_normal_values():
    # Existing machines (no quotes/backslashes in string fields) must build
    # exactly as before.
    config = base_config()
    assert evaluate(render_config_source(config)) == evaluate(old_render(config))


@pytest.mark.parametrize(
    "field", ["prestart_command", "extra_args", "name", "base_docker_image", "python_version"]
)
def test_injection_payload_stays_a_string(field):
    config = base_config(**{field: INJECTION})
    source = render_config_source(config)
    assert evaluate(source)[field] == INJECTION
    assert "__import__" not in [
        n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)
    ]


@pytest.mark.parametrize(
    "value",
    ["Bob's machine", 'say "hi"', "C:\\path\\to\\x", "line1\nline2", "tab\there", "'''", "\\'", "ünïcödé 🚀"],
)
def test_awkward_strings_are_preserved_exactly(value):
    config = base_config(prestart_command=value, name=value)
    result = evaluate(render_config_source(config))
    assert result["prestart_command"] == value
    assert result["name"] == value


@pytest.mark.parametrize("value", [object(), float("nan"), float("inf")])
def test_non_literal_values_are_refused(value):
    with pytest.raises((ValueError, SyntaxError)):
        render_config_source(base_config(deps=value))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_ORG_ID", "org_admin")
    app = FastAPI()
    current_user = {}

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.current_user = dict(current_user)
        return await call_next(request)

    app.include_router(builder.router, prefix="/api")
    test_client = TestClient(app)
    test_client.current_user = current_user
    return test_client


ADMIN_ROUTES = [
    ("/api/machine/modal/some-app/keep-warm", {"warm_pool_size": 1}),
    ("/api/machine/modal/cancel-function", {"function_id": "fc-123"}),
    ("/api/machine/create", {}),
]


@pytest.mark.parametrize("path,body", ADMIN_ROUTES)
@pytest.mark.parametrize(
    "user",
    [{"user_id": "user_1"}, {"user_id": "user_1", "org_id": "org_customer"}],
)
def test_internal_routes_reject_non_admins(client, path, body, user):
    client.current_user.update(user)
    with patch.object(builder.modal.Cls, "lookup", side_effect=AssertionError("must not run")), \
         patch.object(builder.modal.functions.FunctionCall, "from_id", side_effect=AssertionError("must not run")), \
         patch.object(builder, "build_logic", side_effect=AssertionError("must not run")):
        response = client.post(path, json=body)
    assert response.status_code == 403


def test_admin_org_passes_the_guard(client):
    client.current_user.update({"user_id": "user_1", "org_id": "org_admin"})
    with patch.object(builder.modal.functions.FunctionCall, "from_id") as from_id:
        response = client.post("/api/machine/modal/cancel-function", json={"function_id": "fc-123"})
    assert response.status_code == 200
    from_id.assert_called_once_with("fc-123")


def test_guard_fails_closed_without_admin_org_config(client, monkeypatch):
    monkeypatch.delenv("ADMIN_ORG_ID")
    client.current_user.update({"user_id": "user_1", "org_id": "org_admin"})
    response = client.post("/api/machine/modal/cancel-function", json={"function_id": "fc-123"})
    assert response.status_code == 500


def test_direct_python_calls_still_work():
    # machines.py / deployments.py call set_machine_always_on directly; the HTTP
    # guard must not change the function's signature.
    with patch.object(builder.modal.Cls, "lookup") as lookup:
        assert builder.set_machine_always_on("some-app", builder.KeepWarmBody(warm_pool_size=0)) == {"status": "success"}
    lookup.assert_called_once_with("some-app", "ComfyDeployRunner")
