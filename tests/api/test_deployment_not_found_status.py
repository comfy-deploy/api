"""Missing deployments must return 404, not be rewrapped as 500."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.routes import deployments
from api.routes.deployments import DeploymentUpdate

pytestmark = pytest.mark.unit


def _request():
    user = {"user_id": "user_1", "token_type": "user", "org_id": "org_1"}
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


def _empty_db():
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.rollback = AsyncMock()
    return db


def test_update_missing_deployment_is_404():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            deployments.update_deployment(
                _request(), "missing", DeploymentUpdate(), db=_empty_db()
            )
        )
    assert exc.value.status_code == 404


def test_get_missing_deployment_is_404():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(deployments.get_deployment(_request(), "missing", db=_empty_db()))
    assert exc.value.status_code == 404
