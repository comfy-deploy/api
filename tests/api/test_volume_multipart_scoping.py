"""Tenant scoping of client-supplied S3 keys on /volume/file/* multipart routes.

S3 helpers are mocked; we check that only keys under temp-uploads/{owner}/ reach
them when VOLUME_UPLOAD_ENFORCE_OWNERSHIP is on, and that shadow mode only warns.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.routes import volumes
from api.routes.types import (
    AbortMultipartUploadRequest,
    CompleteMultipartUploadRequest,
    GeneratePartUploadUrlRequest,
    InitiateMultipartUploadRequest,
)
from api.routes.volumes import AddModelRequest

pytestmark = pytest.mark.unit

OWN_KEY = "temp-uploads/org_1/0b6f/model.safetensors"
FOREIGN_KEYS = [
    "temp-uploads/org_2/0b6f/model.safetensors",
    "temp-uploads/org_1x/0b6f/model.safetensors",  # prefix-of-another-id
    "temp-uploads/org_1/../org_2/0b6f/model.safetensors",
    "models/anything.safetensors",
]


def _request(user_id="user_1", org_id="org_1"):
    current_user = {"user_id": user_id}
    if org_id:
        current_user["org_id"] = org_id
    return SimpleNamespace(state=SimpleNamespace(current_user=current_user))


@pytest.fixture
def s3(monkeypatch):
    mocks = SimpleNamespace(
        initiate=AsyncMock(return_value="upload-123"),
        part=AsyncMock(return_value="https://s3.example/presigned"),
        complete=AsyncMock(return_value={}),
        abort=AsyncMock(return_value={}),
    )
    monkeypatch.setattr(volumes, "initiate_multipart_upload", mocks.initiate)
    monkeypatch.setattr(volumes, "generate_part_upload_url", mocks.part)
    monkeypatch.setattr(volumes, "complete_multipart_upload", mocks.complete)
    monkeypatch.setattr(volumes, "abort_multipart_upload", mocks.abort)
    return mocks


# The exact JSON bodies the ComfyUI plugin proxy forwards (custom_routes.py).
def _plugin_calls(key):
    return [
        ("part", volumes.generate_part_upload_url_route,
         GeneratePartUploadUrlRequest.model_validate({"uploadId": "upload-123", "key": key, "partNumber": 1})),
        ("complete", volumes.complete_multipart_upload_route,
         CompleteMultipartUploadRequest.model_validate(
             {"uploadId": "upload-123", "key": key, "parts": [{"partNumber": 1, "eTag": "abc"}]})),
        ("abort", volumes.abort_multipart_upload_route,
         AbortMultipartUploadRequest.model_validate({"uploadId": "upload-123", "key": key})),
    ]


def _call(route, request, body):
    try:
        return asyncio.run(route(request, body, db=None))
    except HTTPException as e:
        return e


def test_initiate_key_round_trips_under_enforce(monkeypatch, s3):
    monkeypatch.setenv("VOLUME_UPLOAD_ENFORCE_OWNERSHIP", "true")
    for request in (_request(), _request(org_id=None)):
        init = asyncio.run(volumes.initiate_multipart_upload_route(
            request, InitiateMultipartUploadRequest(filename="m.safetensors", contentType="x", size=10), db=None))
        for name, route, body in _plugin_calls(init["key"]):
            assert not isinstance(_call(route, request, body), HTTPException), name
    assert s3.part.await_count == s3.complete.await_count == s3.abort.await_count == 2


@pytest.mark.parametrize("key", FOREIGN_KEYS)
def test_foreign_key_rejected_when_enforced(monkeypatch, s3, key):
    monkeypatch.setenv("VOLUME_UPLOAD_ENFORCE_OWNERSHIP", "true")
    for name, route, body in _plugin_calls(key):
        result = _call(route, _request(), body)
        assert isinstance(result, HTTPException) and result.status_code == 403, name
    s3.part.assert_not_awaited()
    s3.complete.assert_not_awaited()
    s3.abort.assert_not_awaited()


@pytest.mark.parametrize("flag", [None, "false"])
def test_foreign_key_warned_but_allowed_in_shadow(monkeypatch, s3, caplog, flag):
    if flag is None:
        monkeypatch.delenv("VOLUME_UPLOAD_ENFORCE_OWNERSHIP", raising=False)
    else:
        monkeypatch.setenv("VOLUME_UPLOAD_ENFORCE_OWNERSHIP", flag)
    with caplog.at_level(logging.WARNING, logger=volumes.logger.name):
        for name, route, body in _plugin_calls(FOREIGN_KEYS[0]):
            assert not isinstance(_call(route, _request(), body), HTTPException), name
    s3.part.assert_awaited_once()
    assert s3.part.await_args.args[2:] == (FOREIGN_KEYS[0], "upload-123", 1)
    s3.complete.assert_awaited_once()
    s3.abort.assert_awaited_once()
    mismatches = [r for r in caplog.records if "ownership mismatch" in r.getMessage()]
    assert len(mismatches) == 3
    assert "org_id=org_1" in mismatches[0].getMessage()


def test_own_key_no_warning(monkeypatch, s3, caplog):
    monkeypatch.delenv("VOLUME_UPLOAD_ENFORCE_OWNERSHIP", raising=False)
    with caplog.at_level(logging.WARNING, logger=volumes.logger.name):
        for name, route, body in _plugin_calls(OWN_KEY):
            assert not isinstance(_call(route, _request(), body), HTTPException), name
    assert not [r for r in caplog.records if "ownership" in r.getMessage()]


def test_add_model_temp_upload_foreign_key_rejected_when_enforced(monkeypatch):
    monkeypatch.setenv("VOLUME_UPLOAD_ENFORCE_OWNERSHIP", "true")
    get_s3_config = AsyncMock()
    monkeypatch.setattr(volumes, "get_s3_config", get_s3_config)
    body = AddModelRequest.model_validate({
        "source": "link", "folderPath": "checkpoints", "filename": "m.safetensors",
        "downloadLink": "https://x", "isTemporaryUpload": True, "s3ObjectKey": FOREIGN_KEYS[0],
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(volumes.add_model(_request(), body, background_tasks=None, db=None))
    assert exc.value.status_code == 403
    get_s3_config.assert_not_awaited()
