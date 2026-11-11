"""Admin-only plugin update: non-admins are refused, dry run touches nothing,
and a real run calls the existing update as the machine owner, then restores
the admin identity. No database needed."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from api.routes import admin, machines

pytestmark = pytest.mark.unit

MACHINE = SimpleNamespace(id=uuid.uuid4(), name="m1", user_id="user_owner", org_id="org_owner")


def _request(org_id, sid="sess_1"):
    user = {"user_id": "user_admin", "org_id": org_id}
    if sid:
        user["sid"] = sid
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


def _db():
    db = MagicMock()

    async def execute(stmt):
        r = MagicMock()
        r.scalars.return_value.first.return_value = MACHINE
        return r

    db.execute = execute
    return db


def _run(req, dry_run):
    body = admin.UpdatePluginRequest(machine_ids=[MACHINE.id], dry_run=dry_run)
    return asyncio.run(admin.admin_update_machine_plugin(req, body, MagicMock(), _db()))


def test_non_admin_refused(monkeypatch):
    monkeypatch.setenv("ADMIN_ORG_ID", "org_admin")
    with pytest.raises(HTTPException) as e:
        _run(_request("org_someone"), dry_run=False)
    assert e.value.status_code == 403


def test_admin_api_key_refused(monkeypatch):
    monkeypatch.setenv("ADMIN_ORG_ID", "org_admin")
    with pytest.raises(HTTPException) as e:
        _run(_request("org_admin", sid=None), dry_run=False)
    assert e.value.status_code == 403


def test_dry_run_does_not_update(monkeypatch):
    monkeypatch.setenv("ADMIN_ORG_ID", "org_admin")
    called = []

    async def fake_update(**kw):
        called.append(kw)

    monkeypatch.setattr(machines, "update_machine_custom_nodes", fake_update)
    out = _run(_request("org_admin"), dry_run=True)
    assert out["results"][0]["status"] == "would_update"
    assert called == []


def test_update_runs_as_owner_then_restores_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_ORG_ID", "org_admin")
    seen = []

    async def fake_update(request, **kw):
        seen.append(dict(request.state.current_user))

    monkeypatch.setattr(machines, "update_machine_custom_nodes", fake_update)
    req = _request("org_admin")
    out = _run(req, dry_run=False)
    assert out["results"][0]["status"] == "rebuilding"
    assert seen == [{"user_id": "user_owner", "org_id": "org_owner"}]
    assert req.state.current_user["org_id"] == "org_admin" and req.state.current_user["sid"] == "sess_1"
