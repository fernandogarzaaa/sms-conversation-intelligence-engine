"""Unit: human-like timing engine, deterministic under a seeded RNG (brief §14)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sms_engine.application.timing import TimingEngine
from sms_engine.infrastructure.rng import Rng


def _eng(seed=7):
    return TimingEngine(Rng(seed), tz_offset_hours=-5)


def test_deterministic_under_same_seed():
    now = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)  # 13:00 local
    a = _eng().schedule(now, char_len=20, intent_count=1, is_question=False, salt="m1").send_after
    b = _eng().schedule(now, char_len=20, intent_count=1, is_question=False, salt="m1").send_after
    assert a == b


def test_short_message_faster_than_complex():
    now = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)
    short = _eng().schedule(now, char_len=15, intent_count=1, is_question=False, salt="s")
    complex_ = _eng().schedule(now, char_len=400, intent_count=4, is_question=False, salt="c")
    assert short.base_delay_seconds < complex_.base_delay_seconds


def test_question_gets_fast_bucket():
    now = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)
    d = _eng().schedule(now, char_len=30, intent_count=1, is_question=True, salt="q")
    assert d.base_delay_seconds <= 120


def test_night_message_is_held_to_morning():
    now = datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc)  # 23:00 local previous day
    d = _eng().schedule(now, char_len=20, intent_count=1, is_question=False, salt="n")
    local = d.send_after.astimezone(timezone.utc)  # stored in UTC
    local_hour = (d.send_after.hour - 5) % 24
    assert 7 <= local_hour <= 10
    assert "morning" in d.reason


def test_never_lands_on_00_or_30_minute_or_whole_minute():
    eng = _eng()
    now = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    for i in range(40):
        d = eng.schedule(now, char_len=10 + i, intent_count=1, is_question=False, salt=f"m{i}")
        assert d.send_after.minute not in (0, 30)
        assert d.send_after.second != 0


def test_no_two_identical_timestamps():
    eng = _eng()
    now = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    seen = set()
    for i in range(50):
        d = eng.schedule(now, char_len=12, intent_count=1, is_question=False, salt=f"x{i}")
        key = d.send_after.isoformat(timespec="seconds")
        assert key not in seen
        seen.add(key)


def test_never_lands_on_forbidden_minute_across_many_seeds():
    # Regression: the old dodge ran once, and seed 0 / salt m19 landed on
    # 15:02:00 exactly. Sweep 200 seeds x 50 schedules: none may land on
    # :00, :30, or a whole minute.
    now = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    for seed in range(200):
        eng = TimingEngine(Rng(seed), tz_offset_hours=-5)
        for i in range(50):
            d = eng.schedule(now, char_len=10 + i, intent_count=1, is_question=False, salt=f"m{i}")
            assert d.send_after.minute not in (0, 30), (seed, i, d.send_after)
            assert d.send_after.second != 0, (seed, i, d.send_after)


def test_message_at_0705_local_is_held_to_morning_window():
    # Regression: the night-hold check compared only the hour, so 07:05
    # local (before the 07:15 window opens) was sent immediately.
    eng = _eng()
    now = datetime(2026, 8, 20, 12, 5, tzinfo=timezone.utc)  # 07:05 local (-5)
    d = eng.schedule(now, char_len=20, intent_count=1, is_question=False, salt="early")
    local = d.send_after + timedelta(hours=-5)
    assert local.hour >= 7 and (local.hour, local.minute) >= (7, 15)
    assert "morning" in d.reason
