"""Unit: automation config loading, env settings, timezone-aware quiet hours."""

from __future__ import annotations

from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from sms_engine.application.config import AutomationConfig
from sms_engine.application.settings import Settings
from sms_engine.application.timing import TimingEngine
from sms_engine.infrastructure.rng import Rng

ROOT = Path(__file__).resolve().parents[2]


def test_example_toml_loads_and_matches_defaults():
    cfg = AutomationConfig.from_toml(ROOT / "config" / "automation.example.toml")
    default = AutomationConfig.default()
    assert cfg.rule_for("PURE_POSITIVE").enabled and cfg.rule_for("QUESTION").enabled
    assert cfg.global_confidence_floor == default.global_confidence_floor
    assert cfg.escalate_on_open_issues == 2
    assert cfg.sending.max_attempts == 3 and cfg.followups.delay_days == 30


def test_toml_can_turn_automation_off(tmp_path):
    p = tmp_path / "a.toml"
    p.write_text('global_confidence_floor = 0.7\n[intents.QUESTION]\nenabled = false\n[campaigns]\nrequire_review = true\n')
    cfg = AutomationConfig.from_toml(p)
    assert not cfg.rule_for("PURE_POSITIVE").enabled  # not listed -> never auto-sent
    assert not cfg.rule_for("QUESTION").enabled
    assert cfg.campaigns.require_review


def test_invalid_toml_values_are_rejected(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("global_confidence_floor = 1.5\n")
    with pytest.raises(ValidationError):
        AutomationConfig.from_toml(p)


def test_settings_from_env():
    s = Settings.from_env({
        "SMS_ENGINE_API_KEYS": "k1, k2",
        "SMS_ENGINE_UI_PASSWORD": "pw",
        "SMS_ENGINE_WORKER_INTERVAL": "5",
        "SMS_ENGINE_TRANSPORT": "TWILIO",
        "SMS_ENGINE_PUBLIC_URL": "https://sms.example.com/",
        "SMS_ENGINE_SEED_CLIENTS": "false",
    })
    assert s.api_keys == ["k1", "k2"] and s.auth_enabled and s.ui_auth_enabled
    assert s.worker_interval_seconds == 5 and s.transport == "twilio"
    assert s.public_base_url == "https://sms.example.com" and s.seed_clients is False


def test_settings_defaults_are_safe_for_local_dev():
    s = Settings.from_env({})
    assert not s.auth_enabled and s.transport == "log" and s.worker_interval_seconds == 0


def _eng(**kw):
    return TimingEngine(Rng(3), **kw)


def test_quiet_hours_follow_daylight_saving():
    """22:00 local in January (UTC-6) and July (UTC-5) are both held to the morning."""
    eng = _eng(tz="America/Chicago")
    for now in (datetime(2026, 1, 15, 4, 0, tzinfo=timezone.utc), datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc)):
        d = eng.schedule(now, char_len=20, intent_count=1, is_question=False, salt=str(now))
        local = eng._local(d.send_after)
        assert time(8, 15) <= local.time() <= time(10, 46), local
        assert "quiet hours" in d.reason


def test_send_window_never_opens_before_8am():
    eng = _eng(tz="America/Chicago")
    early = datetime(2026, 8, 20, 12, 1, tzinfo=timezone.utc)  # 07:01 local
    for i in range(30):
        d = eng.schedule(early, char_len=10, intent_count=1, is_question=False, salt=f"e{i}")
        assert eng._local(d.send_after).time() >= time(8, 15)


def test_evening_message_after_window_end_waits_for_next_day():
    eng = _eng(tz="America/Chicago")
    now = datetime(2026, 8, 21, 1, 45, tzinfo=timezone.utc)  # 20:45 local
    d = eng.schedule(now, char_len=10, intent_count=1, is_question=True, salt="eve")
    local = eng._local(d.send_after)
    assert local.date().day == 21 and local.time() >= time(8, 15)


def test_custom_window_and_persistent_dedupe():
    taken = set()
    eng = _eng(tz="UTC", window_start=time(9, 0), window_end=time(17, 0),
               is_taken=lambda dt: dt.isoformat(timespec="seconds") in taken)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    first = eng.schedule(now, char_len=10, intent_count=1, is_question=False, salt="same").send_after
    taken.add(first.isoformat(timespec="seconds"))
    fresh = _eng(tz="UTC", window_start=time(9, 0), window_end=time(17, 0),
                 is_taken=lambda dt: dt.isoformat(timespec="seconds") in taken)
    second = fresh.schedule(now, char_len=10, intent_count=1, is_question=False, salt="same").send_after
    assert second != first  # a restarted process still avoids the taken second


def test_invalid_window_is_rejected():
    with pytest.raises(ValueError):
        _eng(window_start=time(20, 0), window_end=time(8, 0))
