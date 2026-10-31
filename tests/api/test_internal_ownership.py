"""Tenant ownership checks for POST /api/gpu_event and POST /api/update-run.

DB / token parsing are mocked; handlers are called directly. Shadow mode (flag
off) must warn and keep today's behaviour, enforce mode (flag "true") rejects.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from api.routes import internal
from api.routes.internal import UpdateRunBody

pytestmark = pytest.mark.unit

GPU_FLAG = "GPU_EVENT_ENFORCE_OWNERSHIP"
RUN_FLAG = "UPDATE_RUN_ENFORCE_OWNERSHIP"


def _caller(user_id="user_a", org_id=None, token_type="machine"):
    caller = {"user_id": user_id, "token_type": token_type}
    if org_id:
        caller["org_id"] = org_id
    return caller


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "tenant mismatch" in r.getMessage()]


@pytest.fixture(autouse=True)
def _fresh_warning_dedupe():
    internal._cross_tenant_warned.clear()


# ---------------------------------------------------------------- gpu_event


def _db(scalar=None):
    result = MagicMock()
    result.scalar_one.return_value = scalar
    result.scalar_one_or_none.return_value = scalar
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _gpu_event(monkeypatch, caller, data, db):
    monkeypatch.setattr(internal, "parse_jwt", AsyncMock(return_value=caller))
    monkeypatch.setattr(internal, "track_gpu_concurrency_change", AsyncMock())
    monkeypatch.setattr(internal, "send_autumn_usage_event", AsyncMock())
    request = SimpleNamespace(headers={"authorization": "Bearer s3cr3t-token-value"})
    return asyncio.run(internal.create_gpu_event(request, data, db))


def _event(user_id, org_id):
    return SimpleNamespace(
        id="evt_1", user_id=user_id, org_id=org_id, session_id=None, gpu="A10G",
        start_time=None, end_time=None, environment=None,
    )


END = {"eventType": "gpu_end", "event_id": "evt_1", "timestamp": "2026-01-01T00:00:00+00:00"}


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer not-a-jwt"}])
def test_gpu_event_invalid_or_missing_token_is_401(headers):
    request = SimpleNamespace(headers=headers)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(internal.create_gpu_event(request, END, _db()))
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "caller,event",
    [
        (_caller("user_a"), _event("user_a", None)),  # personal owner
        (_caller("user_b", "org_1"), _event("user_a", "org_1")),  # other member, same org
    ],
)
def test_gpu_end_owner_allowed(monkeypatch, caplog, caller, event):
    monkeypatch.setenv(GPU_FLAG, "true")
    db = _db(event)
    assert _gpu_event(monkeypatch, caller, END, db) == {"event_id": "evt_1"}
    db.commit.assert_awaited_once()
    assert not _warnings(caplog)


@pytest.mark.parametrize(
    "caller,event",
    [
        (_caller("user_a"), _event("user_b", None)),  # other personal user
        (_caller("user_a"), _event("user_a", "org_1")),  # personal token, org row
        (_caller("user_a", "org_1"), _event("user_a", "org_2")),  # other org
    ],
)
def test_gpu_end_other_tenant(monkeypatch, caplog, caller, event):
    monkeypatch.delenv(GPU_FLAG, raising=False)
    db = _db(event)
    assert _gpu_event(monkeypatch, caller, END, db) == {"event_id": "evt_1"}
    db.commit.assert_awaited_once()
    assert len(_warnings(caplog)) == 1
    assert "s3cr3t" not in _warnings(caplog)[0].getMessage()

    caplog.clear()
    monkeypatch.setenv(GPU_FLAG, "true")
    db = _db(event)
    resp = _gpu_event(monkeypatch, caller, END, db)
    assert isinstance(resp, JSONResponse) and resp.status_code == 403
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    internal.send_autumn_usage_event.assert_not_awaited()
    assert len(_warnings(caplog)) == 1


START = {"eventType": "gpu_start", "machine_id": "m_1", "timestamp": "2026-01-01T00:00:00+00:00"}


def test_gpu_start_own_machine_allowed(monkeypatch, caplog):
    monkeypatch.setenv(GPU_FLAG, "true")
    db = _db(SimpleNamespace(user_id="user_a", org_id="org_1"))
    resp = _gpu_event(monkeypatch, _caller("user_b", "org_1"), START, db)
    assert "event_id" in resp
    db.add.assert_called_once()
    assert not _warnings(caplog)


def test_gpu_start_body_ids_fall_back_to_token(monkeypatch, caplog):
    # Today's containers send user_id/org_id = None and an unknown machine -> token identity.
    monkeypatch.setenv(GPU_FLAG, "true")
    db = _db(None)
    data = {**START, "user_id": None, "org_id": None}
    assert "event_id" in _gpu_event(monkeypatch, _caller("user_a", "org_1"), data, db)
    created = db.add.call_args.args[0]
    assert (created.user_id, created.org_id) == ("user_a", "org_1")
    assert not _warnings(caplog)


@pytest.mark.parametrize(
    "caller,machine,body",
    [
        # someone else's machine_id
        (_caller("user_a"), SimpleNamespace(user_id="user_b", org_id=None), {}),
        (_caller("user_a", "org_1"), SimpleNamespace(user_id="user_b", org_id="org_2"), {}),
        # unknown machine, victim ids in the body
        (_caller("user_a"), None, {"user_id": "user_b"}),
        (_caller("user_a"), None, {"org_id": "org_2"}),
        (_caller("user_a", "org_1"), None, {"user_id": "user_a", "org_id": "org_2"}),
    ],
)
def test_gpu_start_other_tenant(monkeypatch, caplog, caller, machine, body):
    data = {**START, **body}
    monkeypatch.delenv(GPU_FLAG, raising=False)
    db = _db(machine)
    assert "event_id" in _gpu_event(monkeypatch, caller, data, db)
    db.add.assert_called_once()  # shadow: created exactly as today
    assert len(_warnings(caplog)) == 1

    caplog.clear()
    monkeypatch.setenv(GPU_FLAG, "true")
    db = _db(machine)
    resp = _gpu_event(monkeypatch, caller, data, db)
    assert isinstance(resp, JSONResponse) and resp.status_code == 403
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


def test_gpu_start_session_other_tenant(monkeypatch, caplog):
    data = {**START, "session_id": "sess_1"}
    event = _event("user_b", "org_2")
    monkeypatch.delenv(GPU_FLAG, raising=False)
    assert _gpu_event(monkeypatch, _caller("user_a", "org_1"), data, _db(event)) == {"event_id": "evt_1"}
    assert len(_warnings(caplog)) == 1

    monkeypatch.setenv(GPU_FLAG, "true")
    db = _db(_event("user_b", "org_2"))
    resp = _gpu_event(monkeypatch, _caller("user_a", "org_1"), data, db)
    assert isinstance(resp, JSONResponse) and resp.status_code == 403
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------- update-run


def _update_run(monkeypatch, caller, run):
    monkeypatch.setattr(internal, "get_cached_workflow_run", AsyncMock(return_value=run))
    request = SimpleNamespace(state=SimpleNamespace(current_user=caller))
    # ws_event short-circuits right after the ownership check: no further DB access.
    body = UpdateRunBody(run_id="run_1", ws_event={"x": 1})
    return asyncio.run(internal.update_run(request, body, BackgroundTasks(), MagicMock()))


def _run(user_id, org_id):
    return SimpleNamespace(user_id=user_id, org_id=org_id, machine_id="m_1", deployment_id=None)


@pytest.mark.parametrize(
    "caller,run",
    [
        (_caller("user_a"), _run("user_a", None)),
        (_caller("user_b", "org_1"), _run("user_a", "org_1")),
        (_caller("user_a", token_type="user"), _run("user_a", None)),
        (_caller("user_a"), None),  # unknown run: nothing to compare, unchanged
    ],
)
def test_update_run_owner_allowed(monkeypatch, caplog, caller, run):
    monkeypatch.setenv(RUN_FLAG, "true")
    assert _update_run(monkeypatch, caller, run) == {"status": "success"}
    assert not _warnings(caplog)


@pytest.mark.parametrize(
    "caller,run",
    [
        (_caller("user_a"), _run("user_b", None)),
        (_caller("user_a"), _run("user_a", "org_1")),
        (_caller("user_a", "org_1"), _run("user_a", None)),
        (_caller("user_a", "org_1"), _run("user_b", "org_2")),
    ],
)
def test_update_run_other_tenant(monkeypatch, caplog, caller, run):
    monkeypatch.delenv(RUN_FLAG, raising=False)
    assert _update_run(monkeypatch, caller, run) == {"status": "success"}
    assert len(_warnings(caplog)) == 1
    # update-run fires per log line: the same mismatch is logged once, not every call
    assert _update_run(monkeypatch, caller, run) == {"status": "success"}
    assert len(_warnings(caplog)) == 1

    caplog.clear()
    monkeypatch.setenv(RUN_FLAG, "true")
    with pytest.raises(HTTPException) as exc:
        _update_run(monkeypatch, caller, run)
    assert exc.value.status_code == 403
    assert len(_warnings(caplog)) == 1
