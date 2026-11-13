"""create_deployment must tolerate duplicate workflow + environment rows."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes import deployments

pytestmark = pytest.mark.unit


class _Stop(Exception):
    pass


def test_existing_deployment_lookup_is_limited_to_one_row():
    user = {"user_id": "user_1", "token_type": "user", "org_id": "org_1"}
    request = SimpleNamespace(state=SimpleNamespace(current_user=user))
    data = MagicMock(
        deployment_id=None,
        machine_id="m1",
        machine_version_id=None,
        workflow_id="w1",
        environment="production",
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=_Stop())
    db.rollback = AsyncMock()

    with pytest.raises(Exception):
        asyncio.run(deployments.create_deployment(request, data, db=db))

    stmt = db.execute.await_args_list[0].args[0]
    assert stmt._limit_clause is not None
