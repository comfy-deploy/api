"""run() inside _create_run must read user_settings from the enclosing scope.

Assigning it anywhere in run() makes it local to the whole function, so the
private-S3 presigning block raised UnboundLocalError on every run.
"""

import types

import pytest

from api.routes import run as run_module

pytestmark = pytest.mark.unit


def test_inner_run_closes_over_user_settings():
    inner = next(
        c for c in run_module._create_run.__code__.co_consts
        if isinstance(c, types.CodeType) and c.co_name == "run"
    )
    assert "user_settings" in inner.co_freevars
    assert "user_settings" not in inner.co_varnames
