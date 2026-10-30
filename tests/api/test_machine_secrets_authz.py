"""Tenant scoping for the machine secret routes in src/api/routes/machines.py.

`GET /machine/{machine_id}/secrets/linked` returns secrets with DECRYPTED
values, and `/secrets/unlinked` + `PATCH /machine/secret` act on a machine
looked up by id. All three used to load the machine with no org/user check, so
any authenticated user who knew a machine UUID could read (or link secrets
into) another tenant's machine.

These tests capture the SQL each handler issues for the machine lookup and
assert it carries the caller's tenant condition. No database needed.

Lives in tests/api/ (not tests/api/routes/) because that conftest requires a
real DATABASE_URL.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from api.routes import machines
from api.routes.machines import UpdateMachineWithSecretInput

pytestmark = pytest.mark.unit

USER_ID = "user_attacker"
ORG_ID = "org_attacker"


def _request(org_id):
    current_user = {"user_id": USER_ID}
    if org_id:
        current_user["org_id"] = org_id
    return SimpleNamespace(state=SimpleNamespace(current_user=current_user))


def _db():
    """AsyncSession stand-in: every query finds nothing, statements are captured."""
    db = MagicMock()
    db.statements = []

    async def execute(stmt):
        db.statements.append(stmt)
        result = MagicMock()
        result.scalars.return_value.first.return_value = None
        result.scalars.return_value.all.return_value = []
        return result

    db.execute = execute
    db.rollback = AsyncMock()
    return db


def _sql(stmt):
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _call(handler_name, request, db, machine_id):
    handler = getattr(machines, handler_name)
    if handler_name == "update_machine_with_secret":
        body = UpdateMachineWithSecretInput(machine_id=machine_id, secret_id=uuid.uuid4())
        coro = handler(request=request, request_body=body, db=db)
    else:
        coro = handler(request=request, machine_id=machine_id, db=db)
    with pytest.raises(HTTPException):
        asyncio.run(coro)


HANDLERS = [
    "get_all_linked_machine_secrets",
    "get_all_unlinked_machine_secrets",
    "update_machine_with_secret",
]


@pytest.mark.parametrize("handler_name", HANDLERS)
def test_machine_lookup_scoped_to_org(handler_name):
    db = _db()
    machine_id = uuid.uuid4()
    _call(handler_name, _request(ORG_ID), db, machine_id)

    sql = _sql(db.statements[0])
    assert "FROM comfyui_deploy.machines" in sql
    assert str(machine_id) in sql
    assert f"comfyui_deploy.machines.org_id = '{ORG_ID}'" in sql


@pytest.mark.parametrize("handler_name", HANDLERS)
def test_machine_lookup_scoped_to_personal_user(handler_name):
    db = _db()
    machine_id = uuid.uuid4()
    _call(handler_name, _request(None), db, machine_id)

    sql = _sql(db.statements[0])
    assert "FROM comfyui_deploy.machines" in sql
    assert f"comfyui_deploy.machines.user_id = '{USER_ID}'" in sql
    assert "comfyui_deploy.machines.org_id IS NULL" in sql


@pytest.mark.parametrize("handler_name", HANDLERS)
def test_foreign_machine_stops_before_touching_secrets(handler_name):
    """Machine lookup misses (foreign tenant) -> nothing else is queried."""
    db = _db()
    _call(handler_name, _request(ORG_ID), db, uuid.uuid4())
    assert len(db.statements) == 1
