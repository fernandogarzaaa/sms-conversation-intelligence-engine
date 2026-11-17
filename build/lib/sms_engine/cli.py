"""`sms-engine` command-line interface.

    sms-engine serve [--host 127.0.0.1] [--port 8099] [--reload]
    sms-engine worker [--interval 15]
    sms-engine demo [maya john ...]
    sms-engine seed-db [--path sms_engine.db]
    sms-engine eval [--mismatches] [--json]
    sms-engine ingest-client data/clients/acme_raw.json
    sms-engine check-config [config/automation.toml]
    sms-engine migrate

Configuration comes from environment variables (see `.env.example`).
"""

from __future__ import annotations

import argparse
import json
import sys


def _settings():
    from sms_engine.application.settings import Settings

    return Settings.from_env()


def _engine(settings):
    from sms_engine.ai.factory import provider_from_env
    from sms_engine.application.wiring import build_engine
    from sms_engine.infrastructure.transport import build_transport

    return build_engine(
        db_path=settings.db_path,
        provider=provider_from_env(),
        config=settings.automation_config(),
        default_timezone=settings.default_timezone,
        transport=build_transport(settings.transport, account_sid=settings.twilio_account_sid,
                                  auth_token=settings.twilio_auth_token),
    )


def cmd_serve(a) -> int:
    import uvicorn

    uvicorn.run("sms_engine.api.app:get_app", factory=True, host=a.host, port=a.port, reload=a.reload,
                proxy_headers=True, forwarded_allow_ips=a.forwarded_allow_ips)
    return 0


def cmd_worker(a) -> int:
    from sms_engine.application.service import SmsEngineService
    from sms_engine.application.worker import BackgroundWorker
    from sms_engine.infrastructure.logging_setup import configure_logging

    s = _settings()
    configure_logging(s.log_level, json_lines=s.log_json)
    engine = _engine(s)
    try:
        worker = BackgroundWorker(SmsEngineService(engine), a.interval)
        if a.once:
            out = worker.run_once()
            print(json.dumps({"sent": len(out["sent"]), "followups": len(out["followups"]),
                              "error": out.get("error")}))
            return 1 if out.get("error") else 0
        print(f"worker running every {a.interval}s against {s.db_path} (Ctrl+C to stop)")
        worker.run_forever()
        return 0
    finally:
        engine.close()


def cmd_demo(a) -> int:
    from sms_engine.demo import run_console

    return run_console(a.names)


def cmd_seed_db(a) -> int:
    from sms_engine.demo import seed_db

    seed_db(a.path)
    return 0


def cmd_eval(a) -> int:
    from sms_engine.ai import MultiIntentClassifier
    from sms_engine.ai.factory import provider_from_env
    from sms_engine.eval.evaluator import evaluate_classifier

    ev = evaluate_classifier(MultiIntentClassifier(provider_from_env()))
    summary = ev.summary()
    if a.json:
        if a.mismatches:
            summary["mismatches"] = ev.mismatches
        print(json.dumps(summary, indent=2))
        return 0
    print(f"model={summary['model']}  n={summary['n']}")
    print(f"exact match {summary['exact_match_rate']:.3f}   salient recall {summary['salient_recall']:.3f}   "
          f"macro F1 {summary['macro_f1']:.3f}")
    print(f"\n{'intent':<28}{'support':>8}{'prec':>8}{'recall':>8}{'f1':>8}")
    for k, s in summary["per_intent"].items():
        print(f"{k:<28}{s['support']:>8}{s['precision']:>8.3f}{s['recall']:>8.3f}{s['f1']:>8.3f}")
    if a.mismatches and ev.mismatches:
        print(f"\n{len(ev.mismatches)} mismatches:")
        for m in ev.mismatches:
            print(f"  {m['id']}: {m['text']}\n      expected {m['expected']}  predicted {m['predicted']}")
    return 0


def cmd_ingest_client(a) -> int:
    from sms_engine.context.packs import load_client_context

    s = _settings()
    engine = _engine(s)
    ctx = load_client_context(a.path)
    engine.clients.upsert(ctx)
    engine.close()
    print(f"ingested client {ctx.client_id!r} ({ctx.name}) into {s.db_path}: "
          f"{len(ctx.faq)} FAQ items, {sum(f.approved for f in ctx.faq)} approved")
    return 0


def cmd_check_config(a) -> int:
    from sms_engine.application.config import AutomationConfig

    path = a.path or _settings().automation_config_path
    if not path:
        print("no automation config file (set SMS_ENGINE_AUTOMATION_CONFIG); built-in defaults apply")
        cfg = AutomationConfig.default()
    else:
        cfg = AutomationConfig.from_toml(path)
        print(f"{path}: OK")
    enabled = [k for k, r in cfg.intents.items() if r.enabled]
    print(f"auto-send enabled for: {', '.join(enabled) or 'nothing (everything goes to a human)'}")
    print(f"confidence floor {cfg.global_confidence_floor}, escalate at {cfg.escalate_on_open_issues} open issues, "
          f"campaigns {'on' if cfg.campaigns.enabled else 'off'}"
          f"{' (review required)' if cfg.campaigns.require_review else ''}")
    return 0


def cmd_migrate(_a) -> int:
    from sms_engine.infrastructure.db import Database

    s = _settings()
    db = Database(s.db_path)
    print(f"{s.db_path}: schema version {db.version}")
    db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sms-engine", description="SMS Conversation Intelligence Engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run the API and review dashboard")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8099)
    sp.add_argument("--reload", action="store_true")
    sp.add_argument("--forwarded-allow-ips", default="127.0.0.1")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("worker", help="run the send-queue / follow-up worker")
    sp.add_argument("--interval", type=float, default=15.0)
    sp.add_argument("--once", action="store_true", help="run one pass and exit (for cron)")
    sp.set_defaults(fn=cmd_worker)

    sp = sub.add_parser("demo", help="run the scenario walk-through in the console")
    sp.add_argument("names", nargs="*", help="scenario keys or names, e.g. maya john")
    sp.set_defaults(fn=cmd_demo)

    sp = sub.add_parser("seed-db", help="write a demo database for the dashboard")
    sp.add_argument("--path", default="sms_engine.db")
    sp.set_defaults(fn=cmd_seed_db)

    sp = sub.add_parser("eval", help="score the configured provider on the labelled dataset")
    sp.add_argument("--mismatches", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_eval)

    sp = sub.add_parser("ingest-client", help="ingest a client context pack (raw JSON)")
    sp.add_argument("path")
    sp.set_defaults(fn=cmd_ingest_client)

    sp = sub.add_parser("check-config", help="validate an automation config TOML")
    sp.add_argument("path", nargs="?")
    sp.set_defaults(fn=cmd_check_config)

    sp = sub.add_parser("migrate", help="create or upgrade the database schema")
    sp.set_defaults(fn=cmd_migrate)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
