"""Phase 2 small authz fixes: comfy proxy header redaction, session log ownership,
runs count tenant scope, deployment create/update ownership, machine error-start-logs.

DB, Redis and logfire are mocked; nothing touches the network.
"""

import asyncio
import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from starlette.requests import Request

from api.routes import comfy_proxy, deployments, machines, runs
from api.routes import session as session_routes
from api.routes.deployments import DeploymentCreate, DeploymentUpdate
from api.routes.machines import ErrorLogEntry
from api.routes.session import UpdateSessionLogBody

pytestmark = pytest.mark.unit


def _sql(stmt):
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _request(user_id="user_1", org_id=None):
    current_user = {"user_id": user_id, "token_type": "user"}
    if org_id:
        current_user["org_id"] = org_id
    return SimpleNamespace(state=SimpleNamespace(current_user=current_user))


def _fake_db_ctx(rows):
    """get_db_context replacement: each execute() returns the next row via .first()."""
    executed = []
    rows = list(rows)

    async def execute(stmt):
        executed.append(_sql(stmt))
        result = MagicMock()
        result.first.return_value = rows.pop(0) if rows else None
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)

    @contextlib.asynccontextmanager
    async def ctx():
        yield db

    return ctx, executed


# --- 1. comfy proxy header redaction -------------------------------------------------


def test_redact_headers_masks_credentials_only():
    out = comfy_proxy._redact_headers(
        {
            "Authorization": "Bearer user-jwt",
            "Cookie": "__session=abc",
            "x-api-key": "comfy-key",
            "X-Auth-Token": "t",
            "x-client-secret": "s",
            "set-cookie": "a=b",
            "content-type": "application/json",
            "user-agent": "ua",
        }
    )
    assert out["content-type"] == "application/json" and out["user-agent"] == "ua"
    for k in ("Authorization", "Cookie", "x-api-key", "X-Auth-Token", "x-client-secret", "set-cookie"):
        assert out[k] == "[REDACTED]"


def test_proxy_never_logs_secrets_but_still_forwards_them(monkeypatch):
    monkeypatch.setattr(comfy_proxy, "check_enterprise_plan", lambda request: None)
    monkeypatch.setattr(comfy_proxy, "COMFY_API_KEY", "server-comfy-key")
    fake_logfire = MagicMock()
    monkeypatch.setattr(comfy_proxy, "logfire", fake_logfire)

    sent = {}
    response = SimpleNamespace(
        status_code=200,
        headers={"content-type": "application/json", "set-cookie": "resp-cookie-secret"},
        content=b"{}",
    )

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, **kwargs):
            sent.update(kwargs)
            return response

    monkeypatch.setattr(comfy_proxy.httpx, "AsyncClient", FakeClient)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/comfy-org/nodes",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer user-jwt-secret"),
            (b"cookie", b"__session=cookie-secret"),
            (b"host", b"api.example"),
        ],
    }
    asyncio.run(comfy_proxy.proxy_to_comfy_org(Request(scope), "nodes", db=None))

    logged = repr(fake_logfire.mock_calls)
    for secret in ("user-jwt-secret", "cookie-secret", "server-comfy-key", "resp-cookie-secret"):
        assert secret not in logged
    assert "[REDACTED]" in logged
    # Proxy behaviour unchanged: upstream still gets the real headers.
    assert sent["headers"]["authorization"] == "Bearer user-jwt-secret"
    assert sent["headers"]["x-api-key"] == "server-comfy-key"


# --- 2. session log ownership --------------------------------------------------------

LOG_BODY = UpdateSessionLogBody(session_id="sess_log_1", machine_id="m1", log="hello")


def _run_session_log(monkeypatch, request, owner_row):
    session_routes._session_owner_cache.clear()
    ctx, executed = _fake_db_ctx([owner_row])
    monkeypatch.setattr(session_routes, "get_db_context", ctx)
    sent = MagicMock()
    monkeypatch.setattr(session_routes, "send_log_entry", sent)
    try:
        return asyncio.run(session_routes.update_session_log(request, LOG_BODY)), sent, executed
    except HTTPException as e:
        return e, sent, executed


def test_session_log_owner_allowed_and_cached(monkeypatch):
    result, sent, executed = _run_session_log(monkeypatch, _request(org_id="org_1"), ("user_x", "org_1"))
    assert result == {"success": True}
    sent.assert_called_once_with("sess_log_1", "m1", "hello")
    assert "session_id = 'sess_log_1'" in executed[0]
    # Second call is served from the TTL cache: no further DB query.
    asyncio.run(session_routes.update_session_log(_request(org_id="org_1"), LOG_BODY))
    assert len(executed) == 1 and sent.call_count == 2


def test_session_log_personal_owner_allowed(monkeypatch):
    result, sent, _ = _run_session_log(monkeypatch, _request(), ("user_1", None))
    assert result == {"success": True} and sent.called


def test_session_log_foreign_shadow_warns_and_logs(monkeypatch, caplog):
    monkeypatch.delenv("SESSION_LOG_ENFORCE_OWNERSHIP", raising=False)
    with caplog.at_level("WARNING", logger=session_routes.logger.name):
        result, sent, _ = _run_session_log(monkeypatch, _request(org_id="org_attacker"), ("u", "org_1"))
    assert result == {"success": True} and sent.called
    assert "sess_log_1" in caplog.text and "org_attacker" in caplog.text


def test_session_log_foreign_enforced_rejected(monkeypatch):
    monkeypatch.setenv("SESSION_LOG_ENFORCE_OWNERSHIP", "true")
    result, sent, _ = _run_session_log(monkeypatch, _request(org_id="org_attacker"), ("u", "org_1"))
    assert isinstance(result, HTTPException) and result.status_code == 404
    sent.assert_not_called()


def test_session_log_personal_caller_cannot_write_org_session(monkeypatch):
    monkeypatch.setenv("SESSION_LOG_ENFORCE_OWNERSHIP", "true")
    result, sent, _ = _run_session_log(monkeypatch, _request(user_id="u"), ("u", "org_1"))
    assert isinstance(result, HTTPException) and not sent.called


def test_session_log_unknown_session_passes_through_uncached(monkeypatch):
    monkeypatch.setenv("SESSION_LOG_ENFORCE_OWNERSHIP", "true")
    result, sent, _ = _run_session_log(monkeypatch, _request(org_id="org_1"), None)
    assert result == {"success": True} and sent.called
    assert "sess_log_1" not in session_routes._session_owner_cache


# --- 3. runs count tenant scope ------------------------------------------------------


def _count_sql(monkeypatch, request, **kwargs):
    captured = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def scalar(self, stmt):
            captured.append(_sql(stmt))
            return 7

    monkeypatch.setattr(runs, "AsyncSessionLocal", FakeSession)
    count = asyncio.run(runs.get_runs_count(request, **kwargs))
    return count, captured[0]


def test_count_runs_scoped_to_org(monkeypatch):
    count, sql = _count_sql(monkeypatch, _request(org_id="org_1"), filters={"status": "success"})
    assert count == 7
    assert "workflow_runs.org_id = 'org_1'" in sql
    assert "workflow_runs.status = 'success'" in sql


def test_count_runs_scoped_to_personal_user(monkeypatch):
    _, sql = _count_sql(monkeypatch, _request(user_id="user_1"))
    assert "workflow_runs.user_id = 'user_1'" in sql
    assert "workflow_runs.org_id IS NULL" in sql


def test_count_runs_endpoint_passes_request(monkeypatch):
    _count_sql(monkeypatch, _request(org_id="org_1"))  # installs FakeSession
    captured = []
    real = runs.get_runs_count

    async def spy(request, **kwargs):
        captured.append(request)
        return await real(request, **kwargs)

    monkeypatch.setattr(runs, "get_runs_count", spy)
    req = _request(org_id="org_1")
    resp = asyncio.run(
        runs.count_runs(
            req, start_time=None, end_time=None, gpu=None, status=None, origin=None,
            workflow_id=None, machine_id=None, deployment_id=None,
        )
    )
    assert resp.count == 7 and captured == [req]


# --- 4. deployments create/update ownership ------------------------------------------


def _machine(org_id="org_1", user_id="u1"):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=user_id, org_id=org_id, machine_version_id=None
    )


class _DeployDB:
    """AsyncSession stub answering by the entity being selected."""

    def __init__(self, machine, workflow, wv_owner, existing=None):
        self.machine, self.workflow, self.wv_owner, self.existing = machine, workflow, wv_owner, existing
        self.added = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.refresh = AsyncMock()

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        name = stmt.column_descriptions[0]["name"]
        r = MagicMock()
        if entity is deployments.Deployment:
            r.scalar_one_or_none.return_value = self.existing
        elif entity is deployments.Machine:
            r.scalar_one_or_none.return_value = self.machine
        elif entity is deployments.Workflow and name == "Workflow":
            r.scalar_one_or_none.return_value = self.workflow
        elif entity is deployments.Workflow:  # (user_id, org_id) for a workflow version
            r.first.return_value = self.wv_owner
        else:
            raise AssertionError(f"unexpected query: {stmt}")
        return r

    def add(self, obj):
        self.added.append(obj)


async def _passthrough(deployment, *a, **k):
    return deployment


def _create(monkeypatch, db, org_id="org_1"):
    monkeypatch.setattr(deployments, "update_deployment_with_machine", _passthrough)
    body = DeploymentCreate(
        workflow_version_id=str(uuid.uuid4()), workflow_id=str(uuid.uuid4()),
        machine_id=str(uuid.uuid4()), environment="production",
    )
    req = _request(user_id="u1", org_id=org_id)
    try:
        return asyncio.run(deployments.create_deployment(req, body, db=db))
    except HTTPException as e:
        return e


def _workflow(org_id="org_1"):
    return SimpleNamespace(id=uuid.uuid4(), user_id="u1", org_id=org_id, description="d", name="w")


def test_create_deployment_owner_allowed(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ENFORCE_OWNERSHIP", "true")
    db = _DeployDB(_machine(), _workflow(), ("u1", "org_1"))
    monkeypatch.setattr(deployments.Deployment, "to_dict", lambda self: {"ok": True}, raising=False)
    assert _create(monkeypatch, db) == {"ok": True}
    assert len(db.added) == 1


@pytest.mark.parametrize(
    "machine_org,workflow_org,wv_org,reason",
    [
        ("org_evil", "org_1", "org_1", "Machine not found"),
        ("org_1", "org_evil", "org_1", "Workflow not found"),
        ("org_1", "org_1", "org_evil", "Workflow version not found"),
    ],
)
def test_create_deployment_foreign_enforced_rejected(monkeypatch, machine_org, workflow_org, wv_org, reason):
    monkeypatch.setenv("DEPLOYMENT_ENFORCE_OWNERSHIP", "true")
    db = _DeployDB(_machine(machine_org), _workflow(workflow_org), ("u9", wv_org))
    result = _create(monkeypatch, db)
    # create_deployment wraps every exception as 500 (pre-existing); the cause is "not found".
    assert isinstance(result, HTTPException) and result.detail.endswith(reason)
    assert db.added == [] and db.commit.await_count == 0


def test_create_deployment_foreign_shadow_warns_and_allows(monkeypatch, caplog):
    monkeypatch.delenv("DEPLOYMENT_ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.setattr(deployments.Deployment, "to_dict", lambda self: {"ok": True}, raising=False)
    db = _DeployDB(_machine("org_evil"), _workflow("org_evil"), ("u9", "org_evil"))
    with caplog.at_level("WARNING", logger=deployments.logger.name):
        assert _create(monkeypatch, db) == {"ok": True}
    for what in ("machine", "workflow ", "workflow_version"):
        assert f"Deployment {what}" in caplog.text


def _update(monkeypatch, db, body):
    monkeypatch.setattr(deployments, "update_deployment_with_machine", _passthrough)
    try:
        return asyncio.run(deployments.update_deployment(_request(org_id="org_1"), "dep", body, db=db))
    except HTTPException as e:
        return e


def test_update_deployment_foreign_machine_enforced_rejected(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ENFORCE_OWNERSHIP", "true")
    existing = SimpleNamespace(machine_id=uuid.uuid4(), workflow_version_id=None)
    db = _DeployDB(_machine("org_evil"), None, None, existing=existing)
    result = _update(monkeypatch, db, DeploymentUpdate(machine_id=str(uuid.uuid4())))
    assert isinstance(result, HTTPException) and db.commit.await_count == 0


def test_update_deployment_foreign_workflow_version_enforced_rejected(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ENFORCE_OWNERSHIP", "true")
    existing = SimpleNamespace(machine_id=None, workflow_version_id=None)
    db = _DeployDB(None, None, ("u9", "org_evil"), existing=existing)
    result = _update(monkeypatch, db, DeploymentUpdate(workflow_version_id=str(uuid.uuid4())))
    assert isinstance(result, HTTPException) and existing.workflow_version_id is None


def test_update_deployment_keeps_existing_machine_without_check(monkeypatch):
    """Settings-only update on a deployment whose stored machine is foreign: untouched."""
    monkeypatch.setenv("DEPLOYMENT_ENFORCE_OWNERSHIP", "true")
    existing = SimpleNamespace(machine_id=uuid.uuid4(), workflow_version_id=None)
    existing.to_dict = lambda: {"ok": True}
    db = _DeployDB(_machine("org_other"), None, None, existing=existing)
    assert _update(monkeypatch, db, DeploymentUpdate(concurrency_limit=2)) == {"ok": True}


# --- 5. machine-version error-start-logs ---------------------------------------------

VERSION_ID = uuid.uuid4()


def _redis(monkeypatch, stored):
    fake = MagicMock()
    fake.get = AsyncMock(return_value=stored)
    fake.set = AsyncMock()
    fake.expire = AsyncMock()
    monkeypatch.setattr(machines, "redis", fake)
    return fake


def _get_logs(monkeypatch, request, owner_row):
    _redis(monkeypatch, '[{"timestamp": 1.0, "logs": "boom"}]')
    ctx, executed = _fake_db_ctx([owner_row])
    monkeypatch.setattr(machines, "get_db_context", ctx)
    resp = asyncio.run(machines.get_error_logs(request, str(VERSION_ID)))
    return resp.body, executed


def test_get_error_logs_owner_sees_logs(monkeypatch):
    body, executed = _get_logs(monkeypatch, _request(org_id="org_1"), ("u", "org_1"))
    assert b"boom" in body
    assert len(executed) == 1 and str(VERSION_ID) in executed[0]
    assert "JOIN comfyui_deploy.machine_versions" in executed[0]


@pytest.mark.parametrize("owner_row", [("u", "org_evil"), None])
def test_get_error_logs_foreign_or_unknown_gets_empty(monkeypatch, owner_row):
    body, _ = _get_logs(monkeypatch, _request(org_id="org_1"), owner_row)
    assert body == b'{"logs":[]}'


def test_get_error_logs_no_logs_skips_db(monkeypatch):
    _redis(monkeypatch, None)
    ctx, executed = _fake_db_ctx([])
    monkeypatch.setattr(machines, "get_db_context", ctx)
    resp = asyncio.run(machines.get_error_logs(_request(), str(VERSION_ID)))
    assert resp.body == b'{"logs":[]}' and executed == []


def _post_logs(monkeypatch, request, owner_row):
    fake = _redis(monkeypatch, None)
    ctx, _ = _fake_db_ctx([owner_row])
    monkeypatch.setattr(machines, "get_db_context", ctx)
    try:
        asyncio.run(machines.add_error_logs(request, VERSION_ID, [ErrorLogEntry(timestamp=1.0, message="x")]))
    except HTTPException as e:
        return e, fake
    return None, fake


def test_post_error_logs_owner_machine_token_allowed(monkeypatch):
    req = _request(user_id="u", org_id="org_1")
    req.state.current_user["token_type"] = "machine"
    err, fake = _post_logs(monkeypatch, req, ("u", "org_1"))
    assert err is None and fake.set.await_count == 1


def test_post_error_logs_foreign_shadow_warns_and_stores(monkeypatch, caplog):
    monkeypatch.delenv("ERROR_LOGS_ENFORCE_OWNERSHIP", raising=False)
    with caplog.at_level("WARNING", logger=machines.logger.name):
        err, fake = _post_logs(monkeypatch, _request(org_id="org_evil"), ("u", "org_1"))
    assert err is None and fake.set.await_count == 1
    assert str(VERSION_ID) in caplog.text and "org_evil" in caplog.text


def test_post_error_logs_foreign_enforced_rejected(monkeypatch):
    monkeypatch.setenv("ERROR_LOGS_ENFORCE_OWNERSHIP", "true")
    err, fake = _post_logs(monkeypatch, _request(org_id="org_evil"), ("u", "org_1"))
    assert isinstance(err, HTTPException) and err.status_code == 404
    assert fake.set.await_count == 0
