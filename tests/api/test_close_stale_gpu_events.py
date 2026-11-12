"""Stale GPU events (gpu_end never arrived) are closed and billed like a normal gpu_end:
never more than the customer could have used, never left open forever.

DB, Autumn and Modal are mocked; the admin handler is called directly."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes import admin
from api.routes.internal import SESSION_BILLING_GRACE, stale_event_end

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def _ev(hours_ago, session_timeout=None, session=False, fn=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        start_time=(NOW - timedelta(hours=hours_ago)).replace(tzinfo=None),  # DB hands back naive
        session_id="sess_1" if session else None,
        session_timeout=session_timeout,
        modal_function_id=fn,
        machine_id=uuid.uuid4(),
        org_id="org_1",
        user_id="user_1",
        gpu="A10G",
        environment="production",
    )


# ---------------------------------------------------------------- stale_event_end


def test_session_billed_to_its_deadline_once_overdue():
    ev = _ev(3, session_timeout=30, session=True)
    start = NOW - timedelta(hours=3)
    assert stale_event_end(ev, None, 0, NOW) == start + timedelta(minutes=30) + SESSION_BILLING_GRACE


def test_session_left_alone_until_deadline_plus_margin():
    assert stale_event_end(_ev(1, session_timeout=30, session=True), None, 0, NOW) is None


def test_session_without_timeout_closed_at_zero_after_24h():
    ev = _ev(25, session=True)
    assert stale_event_end(ev, None, 0, NOW) == NOW - timedelta(hours=25)
    assert stale_event_end(_ev(5, session=True), None, 0, NOW) is None


def test_run_container_billed_to_last_run_plus_idle_timeout():
    ev = _ev(20)
    last_run = NOW - timedelta(hours=15)
    assert stale_event_end(ev, last_run, 60, NOW) == last_run + timedelta(seconds=60)


def test_run_container_with_recent_activity_is_still_alive():
    assert stale_event_end(_ev(20), NOW - timedelta(hours=2), 60, NOW) is None


def test_run_container_that_never_ran_bills_nothing():
    ev = _ev(13)
    assert stale_event_end(ev, None, 60, NOW) == NOW - timedelta(hours=13)


# ---------------------------------------------------------------- endpoint


def _request(org_id="org_admin"):
    return SimpleNamespace(state=SimpleNamespace(current_user={"user_id": "u", "org_id": org_id}))


def _db(events, last_run=None, idle=60, won=True):
    """execute() answers, in order: events list, then per run event (last_run, idle), then UPDATE."""
    db = MagicMock()
    db.commit = AsyncMock()

    async def execute(stmt):
        r = MagicMock()
        sql = str(stmt)
        if sql.startswith("UPDATE"):
            r.scalar_one_or_none.return_value = "id" if won else None
        elif "max(" in sql:
            r.scalar.return_value = last_run
        elif "idle_timeout" in sql:
            r.scalar.return_value = idle
        else:
            r.scalars.return_value.all.return_value = events
        return r

    db.execute = AsyncMock(side_effect=execute)
    return db


@pytest.fixture
def mocks(monkeypatch):
    monkeypatch.setenv("ADMIN_ORG_ID", "org_admin")
    monkeypatch.setattr(admin, "datetime", SimpleNamespace(now=lambda tz=None: NOW))
    charge = AsyncMock(return_value=True)
    track = AsyncMock()
    monkeypatch.setattr(admin, "send_autumn_usage_event", charge)
    monkeypatch.setattr(admin, "track_gpu_concurrency_change", track)
    fc = MagicMock()
    fc.cancel.aio = AsyncMock()
    monkeypatch.setattr(admin.modal.functions.FunctionCall, "from_id", MagicMock(return_value=fc))
    return SimpleNamespace(charge=charge, track=track, cancel=fc.cancel.aio)


def _run(db, dry_run=False, org="org_admin"):
    return asyncio.run(admin.close_stale_gpu_events(_request(org), dry_run=dry_run, db=db))


def test_non_admin_refused(mocks):
    with pytest.raises(Exception):
        _run(_db([]), org="org_customer")


def test_dry_run_reports_but_changes_nothing(mocks):
    db = _db([_ev(3, session_timeout=30, session=True)])
    out = _run(db, dry_run=True)
    assert out["closed"] == 1 and out["billed_seconds"] == 35 * 60
    db.commit.assert_not_called()
    mocks.charge.assert_not_called()


def test_overdue_session_closed_billed_and_stopped(mocks):
    ev = _ev(3, session_timeout=30, session=True, fn="fc-123")
    out = _run(_db([ev]))
    assert out["billed_seconds"] == 35 * 60
    kw = mocks.charge.call_args.kwargs
    assert kw["idempotency_key"] == str(ev.id)  # same key as gpu_end -> no double charge
    assert (kw["end_time"] - kw["start_time"]) == timedelta(minutes=35)
    mocks.track.assert_awaited_once_with(customer_id="org_1", increment=-1, gpu_event_id=str(ev.id))
    mocks.cancel.assert_awaited_once()


def test_dead_run_container_billed_from_its_runs(mocks):
    last_run = NOW - timedelta(hours=14)
    _run(_db([_ev(20)], last_run=last_run, idle=120))
    kw = mocks.charge.call_args.kwargs
    assert kw["end_time"] == last_run + timedelta(seconds=120)


def test_late_gpu_end_wins_and_nothing_is_billed(mocks):
    out = _run(_db([_ev(3, session_timeout=30, session=True)], won=False))
    assert out["events"][0]["skipped"] == "gpu_end arrived" and out["billed_seconds"] == 0
    mocks.charge.assert_not_called()
    mocks.track.assert_not_called()


def test_still_running_events_untouched(mocks):
    out = _run(_db([_ev(1, session_timeout=30, session=True)]))
    assert out["closed"] == 0
    mocks.charge.assert_not_called()
