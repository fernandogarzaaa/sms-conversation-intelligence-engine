"""Unit: storage (migrations, transactions, thread safety), idempotency, fail-safe."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from sms_engine.domain.enums import EventType, RoutingDecision
from sms_engine.infrastructure.db import SCHEMA_V1, SCHEMA_VERSION, Database


def test_fresh_database_is_at_latest_schema_version():
    db = Database(":memory:")
    assert db.version == SCHEMA_VERSION
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "provider_message_id" in cols


def test_v1_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(SCHEMA_V1)
    raw.execute(
        "INSERT INTO conversations(id, contact_id, client_id, phone, created_at) "
        "VALUES('conv_old','c1','greenline',NULL,'2026-01-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    db = Database(path)
    assert db.version == SCHEMA_VERSION
    row = db.conn.execute("SELECT * FROM conversations WHERE id='conv_old'").fetchone()
    assert row["contact_id"] == "c1" and row["neighborhood"] is None
    db.close()
    # re-opening is a no-op
    assert Database(path).version == SCHEMA_VERSION


def test_transaction_rolls_back_everything_on_error():
    db = Database(":memory:")
    db.conn.execute("CREATE TABLE t(x INTEGER)")
    db.conn.commit()
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.conn.execute("INSERT INTO t VALUES (1)")
            db.conn.commit()  # inside a transaction this must NOT commit
            with db.transaction():  # nested
                db.conn.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError("boom")
    assert db.conn.execute("SELECT COUNT(*) n FROM t").fetchone()["n"] == 0
    with db.transaction():
        db.conn.execute("INSERT INTO t VALUES (3)")
    assert db.conn.execute("SELECT COUNT(*) n FROM t").fetchone()["n"] == 1


def test_concurrent_inbound_messages_never_collide(engine, pipe):
    """Many threads, two contacts: event seqs stay unique and ordered, nothing raises."""
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            pipe.handle_inbound(client_id="greenline", contact_id=f"conc{i % 2}",
                                text="Everything went great, thanks!" if i % 3 else "Do you move pianos?")
        except BaseException as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for cid in ("conc0", "conc1"):
        conv = engine.conversations.find_by_contact(cid, "greenline")
        seqs = [e.seq for e in engine.events.list_for_conversation(conv["id"])]
        assert seqs == list(range(1, len(seqs) + 1))
        # every message got exactly one trace
        msgs = [m for m in engine.messages.list_for_conversation(conv["id"]) if m.direction.value == "INBOUND"]
        assert len(msgs) == 8
        assert all(engine.traces.for_message(conv["id"], m.id) for m in msgs)
        # at most one live planned reply per conversation (the others were invalidated)
        live = [p for p in engine.planned.for_conversation(conv["id"]) if p.status == "PLANNED"]
        assert len(live) <= 1


def test_same_provider_message_id_is_processed_once(engine, pipe):
    r1 = pipe.handle_inbound(client_id="greenline", contact_id="idem", text="Everything went great, thanks!",
                             provider_message_id="SM123")
    r2 = pipe.handle_inbound(client_id="greenline", contact_id="idem", text="Everything went great, thanks!",
                             provider_message_id="SM123")
    assert not r1.duplicate and r2.duplicate
    assert r2.message.id == r1.message.id
    assert r2.routing == r1.routing
    assert r2.planned_response and r2.planned_response.id == r1.planned_response.id
    inbound = [m for m in engine.messages.list_for_conversation(r1.conversation_id) if m.direction.value == "INBOUND"]
    assert len(inbound) == 1
    # the replay did not invalidate the reply it is replaying
    assert engine.planned.get(r1.planned_response.id).status == "PLANNED"


def test_pipeline_crash_fails_safe_to_human_review(engine, pipe, monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr(engine.policy, "decide", explode)
    r = pipe.handle_inbound(client_id="greenline", contact_id="crash", text="Everything went great, thanks!")
    assert r.error and "policy store unavailable" in r.error
    assert r.routing == RoutingDecision.HUMAN_REVIEW
    assert r.planned_response is None
    review = engine.reviews.get(r.review_id)
    assert review["status"] == "OPEN" and review["risk_level"] == "HIGH"
    types = [e.type for e in engine.events.list_for_conversation(r.conversation_id)]
    # the message itself was kept, the half-finished analysis was rolled back
    assert EventType.SMS_RECEIVED in types and EventType.PROCESSING_FAILED in types
    assert EventType.CLASSIFICATION_COMPLETED not in types
    assert engine.messages.get(r.message.id) is not None


def test_overlong_inbound_text_is_truncated(pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="long", text="great " * 2000)
    assert len(r.message.raw_text) == 2000


def test_unknown_client_is_rejected(pipe):
    with pytest.raises(ValueError):
        pipe.handle_inbound(client_id="nope", contact_id="x", text="hi")
