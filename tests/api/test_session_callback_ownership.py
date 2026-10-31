"""Tenant ownership check for POST /api/session/callback.

The DB session is mocked; we capture the SQL each call issues and assert the
scoped UPDATE runs first and that enforce/shadow mode behave as specified.
"""

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from api.routes import session as session_routes
from api.routes.session import UpdateSessionCallbackBody

pytestmark = pytest.mark.unit

BODY = UpdateSessionCallbackBody(session_id="sess_1", tunnel_url="https://evil.example")


def _sql(stmt):
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _request(user_id="user_1", org_id=None):
    current_user = {"user_id": user_id}
    if org_id:
        current_user["org_id"] = org_id
    return SimpleNamespace(state=SimpleNamespace(current_user=current_user))


def _run(monkeypatch, request, scoped_rowcount, row_exists):
    """Run the handler; return the list of SQL strings executed."""
    executed = []

    async def execute(stmt):
        executed.append(_sql(stmt))
        result = MagicMock()
        if len(executed) == 1:
            result.rowcount = scoped_rowcount
        result.first.return_value = ("gpu_event_id",) if row_exists else None
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)

    @contextlib.asynccontextmanager
    async def fake_ctx():
        yield db

    monkeypatch.setattr(session_routes, "get_db_context", fake_ctx)
    try:
        return executed, asyncio.run(session_routes.update_session_callback(request, BODY))
    except HTTPException as e:
        return executed, e


def test_owner_org_scoped_update_only(monkeypatch):
    executed, result = _run(monkeypatch, _request(org_id="org_1"), 1, True)
    assert result == {"success": True}
    assert len(executed) == 1
    assert executed[0].startswith("UPDATE comfyui_deploy.gpu_events")
    assert "session_id = 'sess_1'" in executed[0]
    assert "org_id = 'org_1'" in executed[0]


def test_owner_personal_scoped_update(monkeypatch):
    executed, _ = _run(monkeypatch, _request(), 1, True)
    assert "user_id = 'user_1'" in executed[0]
    assert "org_id IS NULL" in executed[0]


def test_unknown_session_no_fallback(monkeypatch):
    executed, result = _run(monkeypatch, _request(org_id="org_1"), 0, False)
    assert result == {"success": True}
    assert len(executed) == 2  # scoped update + existence check, nothing else
    assert executed[1].startswith("SELECT")


def test_mismatch_enforced_returns_404_without_update(monkeypatch):
    monkeypatch.setenv("SESSION_CALLBACK_ENFORCE_OWNERSHIP", "true")
    executed, result = _run(monkeypatch, _request(org_id="org_attacker"), 0, True)
    assert isinstance(result, HTTPException) and result.status_code == 404
    assert len(executed) == 2
    assert "org_id = 'org_attacker'" in executed[0]
    assert executed[1].startswith("SELECT")


def test_mismatch_shadow_logs_and_updates_unscoped(monkeypatch, caplog):
    monkeypatch.delenv("SESSION_CALLBACK_ENFORCE_OWNERSHIP", raising=False)
    with caplog.at_level("WARNING", logger=session_routes.logger.name):
        executed, result = _run(monkeypatch, _request(org_id="org_attacker"), 0, True)
    assert result == {"success": True}
    assert len(executed) == 3
    assert executed[2].startswith("UPDATE comfyui_deploy.gpu_events")
    assert "org_id" not in executed[2].split("WHERE")[1]
    assert "user_id" not in executed[2].split("WHERE")[1]
    assert "sess_1" in caplog.text and "org_attacker" in caplog.text


def test_token_without_user_id_is_treated_as_non_owner(monkeypatch):
    monkeypatch.setenv("SESSION_CALLBACK_ENFORCE_OWNERSHIP", "true")
    request = SimpleNamespace(state=SimpleNamespace(current_user={}))
    executed, result = _run(monkeypatch, request, 0, True)
    assert isinstance(result, HTTPException) and result.status_code == 404
    assert "false" in executed[0].lower()
