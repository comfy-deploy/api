"""Svix signature verification for POST /api/clerk/webhook.

Calls the handler directly with a raw starlette Request; the registry is
mocked, so no database is needed.
"""

import asyncio
import base64
import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from api.routes import clerk_webhook
from api.routes.clerk_webhook import verify_svix_signature

pytestmark = pytest.mark.unit

SECRET = "whsec_" + base64.b64encode(b"test-signing-key-32-bytes-long!!").decode()
NOW = 1_800_000_000
BODY = json.dumps({"data": {"id": "user_1"}, "type": "user.created", "timestamp": 1}).encode()


def _sign(body, secret=SECRET, msg_id="msg_1", ts=NOW):
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(sig).decode()


def _headers(body=BODY, **kw):
    return {"svix-id": "msg_1", "svix-timestamp": str(NOW), "svix-signature": _sign(body, **kw)}


@pytest.fixture(autouse=True)
def frozen_time(monkeypatch):
    monkeypatch.setattr(clerk_webhook.time, "time", lambda: NOW)


def test_svix_docs_test_vector(monkeypatch):
    # Example from https://docs.svix.com/receiving/verifying-payloads/how-manual
    monkeypatch.setattr(clerk_webhook.time, "time", lambda: 1731705121)
    headers = {
        "svix-id": "msg_loFOjxBNrRLzqYUf",
        "svix-timestamp": "1731705121",
        "svix-signature": "v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0=",
    }
    body = b'{"event_type":"ping","data":{"success":true}}'
    assert verify_svix_signature("whsec_plJ3nmyCDGBKInavdOK15jsl", headers, body) is None


def test_valid_signature():
    assert verify_svix_signature(SECRET, _headers(), BODY) is None


def test_tampered_body():
    assert verify_svix_signature(SECRET, _headers(), BODY + b" ") == "no matching v1 signature"


def test_wrong_secret():
    other = "whsec_" + base64.b64encode(b"another-key").decode()
    assert verify_svix_signature(SECRET, _headers(secret=other), BODY) == "no matching v1 signature"


@pytest.mark.parametrize("ts", [NOW - 301, NOW + 301])
def test_timestamp_outside_tolerance(ts):
    headers = _headers(ts=ts)
    headers["svix-timestamp"] = str(ts)
    assert verify_svix_signature(SECRET, headers, BODY) == "timestamp outside tolerance"


def test_timestamp_within_tolerance():
    headers = _headers(ts=NOW - 299)
    headers["svix-timestamp"] = str(NOW - 299)
    assert verify_svix_signature(SECRET, headers, BODY) is None


@pytest.mark.parametrize("missing", ["svix-id", "svix-timestamp", "svix-signature"])
def test_missing_header(missing):
    headers = _headers()
    del headers[missing]
    assert verify_svix_signature(SECRET, headers, BODY) == "missing svix headers"


def test_missing_secret():
    assert verify_svix_signature(None, _headers(), BODY) == "CLERK_WEBHOOK_SECRET not configured"


def test_non_numeric_timestamp():
    headers = _headers()
    headers["svix-timestamp"] = "abc"
    assert verify_svix_signature(SECRET, headers, BODY) == "invalid svix-timestamp"


def test_multiple_signatures_one_valid():
    headers = _headers()
    headers["svix-signature"] = "v1,bm9wZQ== v2,abc " + headers["svix-signature"]
    assert verify_svix_signature(SECRET, headers, BODY) is None


def test_valid_hmac_under_non_v1_version_rejected():
    headers = _headers()
    headers["svix-signature"] = headers["svix-signature"].replace("v1,", "v2,")
    assert verify_svix_signature(SECRET, headers, BODY) == "no matching v1 signature"


# --- handler: enforce on/off -------------------------------------------------


def _request(body, headers):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/clerk/webhook",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope, receive)


@pytest.fixture
def registry(monkeypatch):
    mock = AsyncMock(return_value="handled")
    monkeypatch.setattr(clerk_webhook.clerk_registry, "handle_webhook", mock)
    return mock


def _call(body, headers):
    return asyncio.run(clerk_webhook.handle_clerk_webhook(_request(body, headers), db=object()))


@pytest.mark.parametrize("enforce", ["true", "false"])
def test_handler_valid_signature_processed(monkeypatch, registry, enforce):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("CLERK_WEBHOOK_ENFORCE_SIGNATURE", enforce)
    assert _call(BODY, _headers()) == "handled"
    registry.assert_awaited_once()
    assert registry.await_args.kwargs["event_type"] == "user.created"
    assert registry.await_args.kwargs["data"] == {"id": "user_1"}


def test_handler_enforce_rejects_forged(monkeypatch, registry):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("CLERK_WEBHOOK_ENFORCE_SIGNATURE", "true")
    with pytest.raises(HTTPException) as exc:
        _call(BODY + b" ", _headers())
    assert exc.value.status_code == 401
    registry.assert_not_awaited()


def test_handler_enforce_rejects_when_secret_missing(monkeypatch, registry):
    monkeypatch.delenv("CLERK_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("CLERK_WEBHOOK_ENFORCE_SIGNATURE", "true")
    with pytest.raises(HTTPException) as exc:
        _call(BODY, _headers())
    assert exc.value.status_code == 401
    registry.assert_not_awaited()


def test_handler_shadow_mode_logs_and_processes(monkeypatch, registry, caplog):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("CLERK_WEBHOOK_ENFORCE_SIGNATURE", raising=False)
    with caplog.at_level("WARNING", logger=clerk_webhook.logger.name):
        assert _call(BODY, {}) == "handled"
    registry.assert_awaited_once()
    assert "missing svix headers" in caplog.text
    assert SECRET not in caplog.text


def test_handler_invalid_body_is_422(monkeypatch, registry):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("CLERK_WEBHOOK_ENFORCE_SIGNATURE", "true")
    body = b'{"type": "user.created"}'
    with pytest.raises(RequestValidationError) as exc:
        _call(body, _headers(body))
    assert exc.value.errors()[0]["loc"][0] == "body"
    registry.assert_not_awaited()
