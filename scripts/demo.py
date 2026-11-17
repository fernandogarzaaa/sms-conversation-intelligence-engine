"""End-to-end console demo.

  python scripts/demo.py            # run all scenarios A-G with decision traces
  python scripts/demo.py --seed-db  # also write ./sms_engine.db for the web UI
  python scripts/demo.py maya john # run only named scenarios

Deterministic: FixedClock + seeded RNG. No network. Mock provider.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

# make stdout UTF-8 on Windows terminals
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sms_engine.application.pipeline import InboundPipeline
from sms_engine.application.service import SmsEngineService
from sms_engine.application.wiring import build_engine, deterministic_engine
from sms_engine.context.packs import all_seed_contexts, default_context
from sms_engine.scenarios import SCENARIOS, run_scenario

DIV = "─" * 78


def _print_step(scn_key, s):
    print(f"\n{DIV}\n  SCENARIO {scn_key} · step {s['step']}")
    print(f'  customer: "{s["text"]}"')
    print(f"  intents : {', '.join(s['intents'])}")
    print(f"  routing : {s['routing']}")
    r = s["result"]
    if r.planned_response:
        print(f'  queued  : "{r.planned_response.text}"  (send_after {r.planned_response.send_after})')
    elif r.draft_text:
        print(f'  draft   : "{r.draft_text}"  (held for human review)')
    else:
        print("  outbound: (none)")
    print("\n  decision trace:")
    for line in r.trace.render().splitlines():
        print(f"    {line}")
    print("\n  checks:")
    for name, ok in s["checks"].items():
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}")


def run_console(names: list[str]) -> int:
    engine = deterministic_engine(datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc), seed=42)
    engine.clients.upsert(default_context())
    pipe = InboundPipeline(engine)

    selected = [s for s in SCENARIOS if not names or s.key.lower() in names or s.contact_id.split("_")[-1] in names]
    total_checks = passed_checks = 0
    for scn in selected:
        for s in run_scenario(pipe, scn):
            _print_step(scn.key, s)
            total_checks += len(s["checks"])
            passed_checks += sum(s["checks"].values())

    print(f"\n{DIV}\n  {passed_checks}/{total_checks} scenario checks passed")
    print(f"  metrics: {engine.db and __import__('json').dumps(SmsEngineService(engine).operational_metrics(), default=str)[:400]}")
    return 0 if passed_checks == total_checks else 1


def seed_db(path: str = "sms_engine.db") -> None:
    import os

    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except OSError:
            pass
    engine = build_engine(db_path=path, seed=42)
    for ctx in all_seed_contexts():
        engine.clients.upsert(ctx)
    svc = SmsEngineService(engine)
    pipe = InboundPipeline(engine)
    for scn in SCENARIOS:
        run_scenario(pipe, scn)
    # a couple of approvals so the learning-loop table has data
    for r in svc.review_queue()[:2]:
        try:
            svc.decide_review(r["id"], action="APPROVE", reviewer="demo")
        except Exception:
            pass
    engine.close()
    print(f"seeded {path} — run:  uvicorn sms_engine.api.app:get_app --factory --reload")


if __name__ == "__main__":
    args = [a.lower() for a in sys.argv[1:]]
    if "--seed-db" in args:
        args.remove("--seed-db")
        seed_db()
    raise SystemExit(run_console(args))
