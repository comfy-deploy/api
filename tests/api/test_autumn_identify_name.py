"""Autumn identify must not crash for Clerk users missing a last name or email.

A crash here 500s /api/autumn/customers, so the frontend shows a $0 balance.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api import autumn_mount

pytestmark = pytest.mark.unit

# Bypass the Redis-backed cache decorator.
get_name = autumn_mount.get_customer_clerk_name_cached.__wrapped__


def _patch_clerk(monkeypatch, user):
    clerk = MagicMock()
    clerk.users.get_async = AsyncMock(return_value=user)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=clerk)
    ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(autumn_mount, "Clerk", lambda **_: ctx)


def _user(first, last, emails):
    return SimpleNamespace(
        first_name=first,
        last_name=last,
        email_addresses=[SimpleNamespace(email_address=e) for e in emails],
    )


@pytest.mark.parametrize(
    "first,last,emails,expected",
    [
        ("Ada", "Lovelace", ["ada@example.com"], {"email": "ada@example.com", "name": "Ada Lovelace"}),
        ("Ada", None, ["ada@example.com"], {"email": "ada@example.com", "name": "Ada"}),
        (None, None, ["ada@example.com"], {"email": "ada@example.com", "name": "ada@example.com"}),
        (None, None, [], {"email": None, "name": "user_1"}),
    ],
)
def test_user_name_fallbacks(monkeypatch, first, last, emails, expected):
    _patch_clerk(monkeypatch, _user(first, last, emails))
    assert asyncio.run(get_name("user_1")) == expected
