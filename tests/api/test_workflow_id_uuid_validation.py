"""Malformed workflow ids must be rejected as 422, not reach the DB (500)."""

import inspect
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from api.routes import workflow

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "fn",
    [
        workflow.get_versions,
        workflow.get_workflow,
        workflow.get_workflow_version,
        workflow.get_deployments,
    ],
)
def test_workflow_id_param_is_uuid(fn):
    ann = inspect.signature(fn).parameters["workflow_id"].annotation
    assert ann is UUID
    adapter = TypeAdapter(ann)
    with pytest.raises(ValidationError):
        adapter.validate_python("27e866af-4aa5-40c9-b1c0-abbcf0e4")
    adapter.validate_python("27e866af-4aa5-40c9-b1c0-abbcf0e4a1b2")
