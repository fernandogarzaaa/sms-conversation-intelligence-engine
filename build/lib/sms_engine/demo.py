"""End-to-end demonstration: the console walk-through and the demo database.

    sms-engine demo                 # scenarios A-G with decision traces
    sms-engine demo maya john       # only the named scenarios
    sms-engine seed-db              # write ./sms_engine.db for the dashboard

Deterministic: FixedClock + seeded RNG + MockProvider. No network.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from sms_engine.application.pipeline import InboundPipeline
from sms_engine.application.service import SmsEngineService
from sms_engine.application.wiring import build_engine, deterministic_engine
from sms_engine.context.packs import all_seed_contexts, default_context
from sms_engine.infrastructure.clock import FixedClock
from sms_engine.scenarios import SCENARIOS, run_scenario

DIV = "─" * 78
DEMO_START = datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc)


def _print_step(scn_key, s, out=print):
    out(f"\n{DIV}\n  SCENARIO {scn_key} · step {s['step']}")
    out(f'  customer: "{s["text"]}"')
    out(f"  intents : {', '.join(s['intents'])}")
    out(f"  routing : {s['routing']}")
    r = s["result"]
    if r.planned_response:
        out(f'  queued  : "{r.planned_response.text}"  (send_after {r.planned_response.send_after})')
    elif r.draft_text:
        out(f'  draft   : "{r.draft_text}"  (held for human review)')
    else:
        out("  outbound: (none)")
    out("\n  decision trace:")
    for line in r.trace.render().splitlines():
        out(f"    {line}")
    out("\n  checks:")
    for name, ok in s["checks"].items():
        out(f"    [{'PASS' if ok else 'FAIL'}] {name}")


def run_console(names: list[str] | None = None, *, provider=None, out=print) -> int:
    names = [n.lower() for n in (names or [])]
    engine = deterministic_engine(DEMO_START, seed=42, provider=provider)
    engine.clients.upsert(default_context())
    pipe = InboundPipeline(engine)

    selected = [s for s in SCENARIOS if not names or s.key.lower() in names or s.contact_id.split("_")[-1] in names]
    if not selected:
        out(f"no scenario matches {names}; known: " + ", ".join(
            f"{s.key} ({s.contact_id.split('_')[-1]})" for s in SCENARIOS))
        return 2
    total_checks = passed_checks = 0
    for scn in selected:
        for s in run_scenario(pipe, scn):
            _print_step(scn.key, s, out)
            total_checks += len(s["checks"])
            passed_checks += sum(s["checks"].values())

    out(f"\n{DIV}\n  {passed_checks}/{total_checks} scenario checks passed")
    metrics = SmsEngineService(engine).operational_metrics()
    keep = ("messages_processed", "automation_rate", "human_review_rate", "suppression_rate",
            "responses_invalidated", "reviews_superseded", "followups_scheduled")
    out("  metrics: " + json.dumps({k: metrics[k] for k in keep}))
    engine.close()
    return 0 if passed_checks == total_checks else 1


def seed_db(path: str = "sms_engine.db", *, out=print) -> dict:
    """Build a demo database that shows every flow: replies, reviews, campaigns, sends."""
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except OSError:
            pass
    clock = FixedClock(DEMO_START)
    engine = build_engine(db_path=path, seed=42, clock=clock)
    for ctx in all_seed_contexts():
        engine.clients.upsert(ctx)
    svc = SmsEngineService(engine)
    pipe = InboundPipeline(engine)
    for scn in SCENARIOS:
        run_scenario(pipe, scn)
        clock.advance(minutes=3)

    # John: approve the identity-correction reply, then ask the campaign system
    # for a referral ask. He already committed, so he gets his own link instead.
    john = engine.conversations.find_by_contact("scen_john", "greenline")
    for r in svc.review_queue():
        if r["conversation_id"] == john["id"]:
            svc.decide_review(r["id"], action="APPROVE", reviewer="demo")
    clock.advance(minutes=20)
    svc.run_worker_pass()
    svc.trigger_campaign(john["id"], "referral_ask", source="demo")

    # Scenario A (pure positive): reply goes out, then a review request.
    a = engine.conversations.find_by_contact("scen_a", "greenline")
    svc.trigger_campaign(a["id"], "review_request", source="demo")

    # Maya: the campaign system tries a review request; suppression blocks it.
    maya = engine.conversations.find_by_contact("scen_maya", "greenline")
    svc.trigger_campaign(maya["id"], "review_request", source="demo")

    # Let the queue drain inside the send window.
    clock.advance(hours=1)
    sent = svc.run_worker_pass()
    summary = {
        "path": path,
        "conversations": engine.conversations.count(),
        "open_reviews": len(svc.review_queue()),
        "sent": len(sent["sent"]),
    }
    engine.close()
    out(f"seeded {path}: {summary['conversations']} conversations, {summary['open_reviews']} open reviews, "
        f"{summary['sent']} messages sent through the log transport")
    out("start the dashboard:  sms-engine serve   (then open http://127.0.0.1:8099)")
    return summary


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - script entry
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    args = [a.lower() for a in (sys.argv[1:] if argv is None else argv)]
    if "--seed-db" in args:
        args.remove("--seed-db")
        seed_db()
    return run_console(args)
