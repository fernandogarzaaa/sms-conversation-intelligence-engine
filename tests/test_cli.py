"""CLI: the commands an operator runs work end to end (offline)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sms_engine.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_check_config(capsys):
    assert main(["check-config", str(ROOT / "config" / "automation.example.toml")]) == 0
    assert "PURE_POSITIVE" in capsys.readouterr().out


def test_demo_all_scenarios_pass(capsys):
    assert main(["demo"]) == 0
    assert "scenario checks passed" in capsys.readouterr().out


def test_demo_unknown_scenario_is_an_error(capsys):
    assert main(["demo", "nobody"]) == 2


def test_seed_db_writes_every_flow(tmp_path, capsys):
    path = tmp_path / "demo.db"
    assert main(["seed-db", "--path", str(path)]) == 0
    c = sqlite3.connect(path)
    kinds = {r[0] for r in c.execute("SELECT kind FROM planned_responses WHERE status='SENT'")}
    assert {"REPLY", "CAMPAIGN"} <= kinds
    blocked = c.execute(
        "SELECT COUNT(*) FROM events WHERE type='CAMPAIGN_TRIGGERED' AND json_extract(payload_json,'$.allowed')=0"
    ).fetchone()[0]
    assert blocked >= 1


def test_migrate_and_ingest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SMS_ENGINE_DB", str(tmp_path / "x.db"))
    monkeypatch.delenv("SMS_ENGINE_PROVIDER", raising=False)
    assert main(["migrate"]) == 0
    assert main(["ingest-client", str(ROOT / "data" / "clients" / "greenline_raw.json")]) == 0
    assert "greenline" in capsys.readouterr().out


def test_eval_json(capsys, monkeypatch):
    monkeypatch.delenv("SMS_ENGINE_PROVIDER", raising=False)
    assert main(["eval", "--json"]) == 0
    assert '"exact_match_rate"' in capsys.readouterr().out


def test_worker_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SMS_ENGINE_DB", str(tmp_path / "w.db"))
    monkeypatch.delenv("SMS_ENGINE_PROVIDER", raising=False)
    assert main(["worker", "--once"]) == 0
    assert '"sent": 0' in capsys.readouterr().out
