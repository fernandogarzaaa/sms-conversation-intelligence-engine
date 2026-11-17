"""Unit: AutomationConfig loads from config/automation.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from sms_engine.application.config import AutomationConfig

REPO = Path(__file__).resolve().parents[2]
TOML = REPO / "config" / "automation.toml"


def test_toml_file_exists_and_matches_defaults():
    assert TOML.is_file()
    loaded = AutomationConfig.from_toml(TOML)
    assert loaded == AutomationConfig.default()


def test_toml_enables_exactly_positive_and_question():
    loaded = AutomationConfig.from_toml(TOML)
    enabled = {k for k, v in loaded.intents.items() if v.enabled}
    assert enabled == {"PURE_POSITIVE", "QUESTION"}


def test_missing_toml_file_fails_loudly():
    with pytest.raises(FileNotFoundError):
        AutomationConfig.from_toml(REPO / "config" / "does-not-exist.toml")
