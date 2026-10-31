"""SSRF guard (api.utils.ssrf) and its wiring into the run-webhook sender. No real network.

Tests are sync + asyncio.run, like the rest of tests/api (pytest-asyncio auto mode isn't active).
"""

import asyncio
import logging
import socket
from types import SimpleNamespace

import pytest

from api.routes import utils as route_utils
from api.utils import ssrf

pytestmark = pytest.mark.unit

_real_getaddrinfo = socket.getaddrinfo

FAKE_DNS = {
    "example.com": ["93.184.216.34"],
    "good.com": ["93.184.216.34"],
    "mixed.example": ["93.184.216.34", "10.0.0.5"],
    "v6.example": ["2606:4700::1111"],
    "rebind-internal.example": ["169.254.169.254"],
}


def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host in FAKE_DNS:
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in FAKE_DNS[host]
        ]
    # Numeric hosts (incl. 2130706433, 0x7f000001, ::1) go through the real
    # resolver with AI_NUMERICHOST, which never touches the network.
    return _real_getaddrinfo(host, port, family, type, proto, socket.AI_NUMERICHOST)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.delenv("SSRF_GUARD_ENFORCE", raising=False)


@pytest.mark.parametrize(
    "url, allowed",
    [
        ("https://example.com/hook?token=secret", True),
        ("http://example.com:8080/x", True),
        ("https://v6.example/", True),
        ("http://127.0.0.1/", False),
        ("http://localhost:8000/", False),
        ("http://foo.localhost/", False),
        ("http://10.1.2.3/", False),
        ("http://192.168.1.1/", False),
        ("http://172.16.0.1/", False),
        ("http://100.64.0.1/", False),  # CGNAT
        ("http://169.254.169.254/latest/meta-data/", False),
        ("http://metadata.google.internal/computeMetadata/v1/", False),
        ("http://METADATA.google.internal./", False),
        ("http://[::1]/", False),
        ("http://[::ffff:127.0.0.1]/", False),
        ("http://[::ffff:169.254.169.254]/", False),
        ("http://[fd00::1]/", False),  # ULA
        ("http://[fe80::1]/", False),  # IPv6 link-local
        ("http://[64:ff9b::a9fe:a9fe]/", False),  # NAT64 -> 169.254.169.254
        ("http://224.0.0.1/", False),  # multicast
        ("http://0.0.0.0/", False),
        ("http://2130706433/", False),  # decimal 127.0.0.1
        ("http://0x7f000001/", False),  # hex 127.0.0.1
        ("http://mixed.example/", False),  # one public + one private address
        ("http://rebind-internal.example/", False),
        ("ftp://example.com/file", False),
        ("file:///etc/passwd", False),
        ("http://good.com@127.0.0.1/", False),  # userinfo trick
        ("http://does-not-resolve.invalid/", False),
        ("http://[::1/", False),  # malformed
        ("not a url", False),
    ],
)
def test_check_url(url, allowed):
    reason = asyncio.run(ssrf.check_url(url))
    assert (reason is None) is allowed, reason


def test_shadow_mode_logs_host_only_and_allows(caplog):
    with caplog.at_level(logging.WARNING, logger="api.utils.ssrf"):
        assert asyncio.run(ssrf.guard("http://user:pw@127.0.0.1/x?token=secret", "t")) is None
    assert "would block" in caplog.text and "127.0.0.1" in caplog.text
    assert "secret" not in caplog.text and "pw" not in caplog.text


def test_enforce_mode_returns_reason(monkeypatch):
    monkeypatch.setenv("SSRF_GUARD_ENFORCE", "true")
    assert asyncio.run(ssrf.guard("http://169.254.169.254/", "t"))
    assert asyncio.run(ssrf.guard("https://example.com/", "t")) is None


# --- run-webhook sender wiring ------------------------------------------------


class FakeSession:
    """Stands in for aiohttp.ClientSession; records what fetch_with_timeout did."""

    calls = []

    def __init__(self, **kwargs):
        FakeSession.calls.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def request(self, method, url, **kw):
        response = SimpleNamespace(ok=True, status=200, history=(), url=url)

        class Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *a):
                return False

        return Ctx()


@pytest.fixture
def fake_session(monkeypatch):
    FakeSession.calls = []
    monkeypatch.setattr(route_utils.aiohttp, "ClientSession", FakeSession)
    return FakeSession


def test_webhook_shadow_mode_sends_as_before(fake_session):
    resp = asyncio.run(route_utils.retry_fetch("http://127.0.0.1/hook", {"method": "POST", "json": {}}))
    assert resp.status == 200
    assert fake_session.calls == [{"trace_configs": None}]  # original session, no hooks


def test_webhook_enforce_mode_refuses_without_retry(fake_session, monkeypatch):
    monkeypatch.setenv("SSRF_GUARD_ENFORCE", "true")
    with pytest.raises(ssrf.SSRFBlockedError):
        asyncio.run(route_utils.retry_fetch("http://169.254.169.254/", {"method": "POST", "json": {}}))
    assert fake_session.calls == []


def test_webhook_enforce_mode_allows_public_with_redirect_hook(fake_session, monkeypatch):
    monkeypatch.setenv("SSRF_GUARD_ENFORCE", "true")
    resp = asyncio.run(route_utils.retry_fetch("https://example.com/hook", {"method": "POST", "json": {}}))
    assert resp.status == 200
    (trace,) = fake_session.calls[0]["trace_configs"]
    assert len(trace.on_request_redirect) == 1


def _redirect(url, location):
    return SimpleNamespace(url=url, response=SimpleNamespace(headers={"Location": location}))


def test_redirect_hook_blocks_private_hop_and_hop_limit(monkeypatch):
    monkeypatch.setenv("SSRF_GUARD_ENFORCE", "true")
    hook = ssrf.redirect_trace_config("t").on_request_redirect[0]

    ctx = SimpleNamespace()
    asyncio.run(hook(None, ctx, _redirect("https://example.com/a", "/b")))  # relative, public
    with pytest.raises(ssrf.SSRFBlockedError):
        asyncio.run(hook(None, ctx, _redirect("https://example.com/a", "http://169.254.169.254/")))

    ctx = SimpleNamespace()
    for _ in range(ssrf.MAX_REDIRECTS):
        asyncio.run(hook(None, ctx, _redirect("https://example.com/a", "https://good.com/")))
    with pytest.raises(ssrf.SSRFBlockedError):
        asyncio.run(hook(None, ctx, _redirect("https://example.com/a", "https://good.com/")))


def test_shadow_redirect_logging(caplog):
    response = SimpleNamespace(
        history=[SimpleNamespace(url="https://example.com/a")],
        url="http://10.0.0.1/internal",
    )
    with caplog.at_level(logging.WARNING, logger="api.utils.ssrf"):
        asyncio.run(ssrf.log_redirect_hops(response, "run_webhook"))
    assert "would block run_webhook" in caplog.text and "10.0.0.1" in caplog.text
