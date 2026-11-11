"""A webhook refused by the SSRF guard is reported once (no traceback, no raise), and
Autumn's 202 "event_received" counts as success."""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from api.routes import internal
from api.utils import autumn as autumn_mod
from api.utils.ssrf import SSRFBlockedError

pytestmark = pytest.mark.unit

RUN = {"id": "r1", "workflow_id": "w1", "webhook": "http://localhost:9/x",
       "status": "running", "live_status": None, "progress": 0.1}


def test_blocked_webhook_returns_blocked_and_logs_for_customer(monkeypatch):
    monkeypatch.setattr(internal, "retry_fetch", AsyncMock(side_effect=SSRFBlockedError("blocked hostname localhost")))
    log = AsyncMock()
    monkeypatch.setattr(internal, "insert_log_entry_to_redis", log)

    async def go():
        out = await internal.send_webhook(workflow_run=RUN, updated_at=datetime.now(), run_id="r1")
        await asyncio.sleep(0)  # let the log task run
        return out

    out = asyncio.run(go())
    assert out["status"] == "blocked"
    run_id, entries = log.await_args.args
    assert run_id == "r1" and json.loads(entries[0]["logs"])["status"] == "blocked"


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body
    async def text(self):
        return self._body
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, resp):
        self.resp = resp
    def request(self, **kw):
        return self.resp
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


@pytest.mark.parametrize("status,ok", [(200, True), (202, True), (409, False), (500, False)])
def test_autumn_2xx_is_success(monkeypatch, status, ok):
    body = '{"id":"placeholder","code":"event_received"}' if status < 300 else '{"code":"err"}'
    monkeypatch.setattr(autumn_mod.aiohttp, "ClientSession", lambda: _Session(_Resp(status, body)))
    client = autumn_mod.AutumnClient(api_key="test")
    assert (asyncio.run(client._make_request("POST", "events", data={})) is not None) is ok
