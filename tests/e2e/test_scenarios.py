"""End-to-end: run every canonical scenario A–G and assert all checks pass."""

from __future__ import annotations

import pytest

from sms_engine.scenarios import SCENARIOS, run_scenario


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
def test_scenario(pipe, scenario):
    steps = run_scenario(pipe, scenario)
    failures = []
    for s in steps:
        for name, ok in s["checks"].items():
            if not ok:
                failures.append(f"[{scenario.key} step {s['step']}] {name}  (routing={s['routing']}, intents={s['intents']})")
    assert not failures, "\n".join(failures)


def test_all_scenarios_together_share_no_state_leak(pipe):
    # each scenario uses its own contact_id; running all in one engine must not cross-contaminate
    for scenario in SCENARIOS:
        steps = run_scenario(pipe, scenario)
        assert all(s["all_passed"] for s in steps), scenario.key
