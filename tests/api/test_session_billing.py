"""Session billing: sessions must stop at their timeout, extensions must be persisted,
and a late or bogus gpu_end must never bill past the session's timeout.

Redis, the DB and Autumn are mocked; handlers are called directly.
"""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes import internal
from api.routes import session as session_routes
from api.routes.session import IncreaseTimeoutBody2

pytestmark = pytest.mark.unit

NOW = datetime.now(timezone.utc)


def _db(row=None):
    result = MagicMock()
    result.scalar_one.return_value = row
    result.scalar_one_or_none.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _patch_db_context(monkeypatch, db):
    @contextlib.asynccontextmanager
    async def fake_ctx():
        yield db

    monkeypatch.setattr(session_routes, "get_db_context", fake_ctx)


def _session_event(started_minutes_ago, timeout_minutes):
    return SimpleNamespace(
        start_time=NOW - timedelta(minutes=started_minutes_ago),
        created_at=NOW - timedelta(minutes=started_minutes_ago + 1),
        session_timeout=timeout_minutes,
        end_time=None,
    )


# ------------------------------------------------- check-timeout closes sessions


def _check(monkeypatch, redis_value, event=None, redis_configured=True):
    redis = None
    if redis_configured:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=redis_value)
    monkeypatch.setattr(session_routes, "redis", redis)
    _patch_db_context(monkeypatch, _db(event))
    delete = AsyncMock()
    monkeypatch.setattr(session_routes, "delete_session", delete)
    result = asyncio.run(session_routes.check_and_close_sessions(SimpleNamespace(), "sess_1"))
    return delete, result


def test_redis_deadline_passed_closes_session(monkeypatch):
    past = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    delete, result = _check(monkeypatch, past)
    delete.assert_awaited_once()
    assert result["continue"] is False


def test_redis_deadline_not_reached_keeps_session(monkeypatch):
    future = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
    delete, _ = _check(monkeypatch, future)
    delete.assert_not_awaited()


@pytest.mark.parametrize("redis_configured", [True, False])
def test_missing_redis_deadline_falls_back_to_db(monkeypatch, redis_configured):
    # Before the fix a missing key meant "skip", so the session ran until Modal's max (~24h).
    delete, result = _check(monkeypatch, None, _session_event(30, 15), redis_configured)
    delete.assert_awaited_once()
    assert result["continue"] is False


def test_missing_redis_deadline_db_not_expired_keeps_session(monkeypatch):
    delete, _ = _check(monkeypatch, None, _session_event(5, 15))
    delete.assert_not_awaited()


def test_missing_redis_deadline_and_no_db_row_does_nothing(monkeypatch):
    delete, _ = _check(monkeypatch, None, None)
    delete.assert_not_awaited()


# ------------------------------------------------- extensions are persisted


def test_increase_timeout_v2_persists_session_timeout(monkeypatch):
    event = _session_event(10, 15)
    db = _db(event)
    redis = MagicMock()
    redis.get = AsyncMock(return_value=(datetime.utcnow() + timedelta(minutes=5)).isoformat())
    redis.set = AsyncMock()
    monkeypatch.setattr(session_routes, "redis", redis)
    request = SimpleNamespace(state=SimpleNamespace(current_user={"user_id": "u1", "plan": "business"}))

    asyncio.run(session_routes.increase_timeout_2(request, "sess_1", IncreaseTimeoutBody2(minutes=20), db))

    redis.set.assert_awaited_once()
    assert event.session_timeout == 35  # billing cap and DB fallback both need the extended value
    db.commit.assert_awaited()


# ------------------------------------------------- gpu_end can't bill past the timeout


def _gpu_end(monkeypatch, event, reported_end):
    monkeypatch.setattr(internal, "parse_jwt", AsyncMock(return_value={"user_id": "u1", "token_type": "machine"}))
    monkeypatch.setattr(internal, "track_gpu_concurrency_change", AsyncMock())
    charge = AsyncMock()
    monkeypatch.setattr(internal, "send_autumn_usage_event", charge)
    db = _db(event)
    data = {"eventType": "gpu_end", "event_id": "evt_1", "timestamp": reported_end.isoformat()}
    request = SimpleNamespace(headers={"authorization": "Bearer t"})
    asyncio.run(internal.create_gpu_event(request, data, db))
    return charge


def _end_event(session_id, start, timeout, reported_end):
    # The UPDATE ... RETURNING row already carries the reported end_time.
    return SimpleNamespace(
        id="evt_1", user_id="u1", org_id=None, gpu="A10G", environment=None,
        session_id=session_id, session_timeout=timeout, start_time=start, end_time=reported_end,
    )


def test_session_gpu_end_is_capped_at_timeout_plus_grace(monkeypatch):
    start = NOW - timedelta(hours=24)
    reported = NOW
    event = _end_event("sess_1", start, 15, reported)
    charge = _gpu_end(monkeypatch, event, reported)
    billed_end = charge.await_args.kwargs["end_time"]
    assert billed_end == start + timedelta(minutes=15) + internal.SESSION_BILLING_GRACE
    assert event.end_time == billed_end  # stored value matches what was billed


def test_session_gpu_end_within_timeout_is_unchanged(monkeypatch):
    start = NOW - timedelta(minutes=10)
    event = _end_event("sess_1", start, 15, NOW)
    charge = _gpu_end(monkeypatch, event, NOW)
    assert charge.await_args.kwargs["end_time"] == NOW


def test_run_gpu_end_is_not_capped(monkeypatch):
    # Run containers have no session timeout; long jobs are real work.
    start = NOW - timedelta(hours=6)
    event = _end_event(None, start, None, NOW)
    charge = _gpu_end(monkeypatch, event, NOW)
    assert charge.await_args.kwargs["end_time"] == NOW


def test_gpu_end_before_start_bills_zero_not_negative(monkeypatch):
    start = NOW
    reported = NOW - timedelta(minutes=3)
    event = _end_event(None, start, None, reported)
    charge = _gpu_end(monkeypatch, event, reported)
    assert charge.await_args.kwargs["end_time"] == start


# ------------------------------------------------- real DB shape: naive UTC datetimes
# Staging showed the DB returns these naive; turning one aware made the Autumn charge
# do naive - aware, raise, and drop the charge.


def test_naive_db_times_capped_end_stays_naive(monkeypatch):
    start = datetime.utcnow() - timedelta(hours=24)
    reported = datetime.utcnow()
    event = _end_event("sess_1", start, 15, reported)
    charge = _gpu_end(monkeypatch, event, reported)
    billed_end = charge.await_args.kwargs["end_time"]
    assert billed_end.tzinfo is None
    assert billed_end == start + timedelta(minutes=15) + internal.SESSION_BILLING_GRACE
    assert billed_end - charge.await_args.kwargs["start_time"] > timedelta(0)  # must not raise


def test_naive_db_times_unchanged_end_is_left_alone(monkeypatch):
    start = datetime.utcnow() - timedelta(minutes=5)
    reported = datetime.utcnow()
    event = _end_event("sess_1", start, 15, reported)
    db_updates_before = event.end_time
    charge = _gpu_end(monkeypatch, event, reported)
    assert charge.await_args.kwargs["end_time"] is db_updates_before
