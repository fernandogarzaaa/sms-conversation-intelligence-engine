"""Repositories over the SQLite database. Thin mappers, no business logic."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from sms_engine.domain.context import ClientContext
from sms_engine.domain.enums import EventType
from sms_engine.domain.events import Event
from sms_engine.domain.messages import Direction, Message
from sms_engine.domain.planning import PlannedResponse
from sms_engine.domain.trace import DecisionTrace


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


# Repositories accept either a raw sqlite3.Connection or the thread-safe
# `SafeConnection` facade from `infrastructure.db`; both expose execute/commit.
Conn = Any


class ClientRepo:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def upsert(self, ctx: ClientContext) -> None:
        self.conn.execute(
            "INSERT INTO clients(client_id, json) VALUES(?, ?) "
            "ON CONFLICT(client_id) DO UPDATE SET json=excluded.json",
            (ctx.client_id, ctx.model_dump_json()),
        )
        self.conn.commit()

    def get(self, client_id: str) -> ClientContext | None:
        row = self.conn.execute(
            "SELECT json FROM clients WHERE client_id=?", (client_id,)
        ).fetchone()
        return ClientContext.model_validate_json(row["json"]) if row else None

    def all(self) -> list[ClientContext]:
        rows = self.conn.execute("SELECT json FROM clients ORDER BY client_id").fetchall()
        return [ClientContext.model_validate_json(r["json"]) for r in rows]


class ConversationRepo:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def create(
        self,
        id: str,
        contact_id: str,
        client_id: str,
        created_at: datetime,
        phone: str | None = None,
        neighborhood: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO conversations(id, contact_id, client_id, phone, created_at, neighborhood) "
            "VALUES(?,?,?,?,?,?)",
            (id, contact_id, client_id, phone, created_at.isoformat(), neighborhood),
        )
        self.conn.commit()

    def get(self, id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM conversations WHERE id=?", (id,)).fetchone()

    def find_by_contact(self, contact_id: str, client_id: str | None = None) -> sqlite3.Row | None:
        if client_id is None:
            return self.conn.execute(
                "SELECT * FROM conversations WHERE contact_id=? ORDER BY created_at LIMIT 1",
                (contact_id,),
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM conversations WHERE contact_id=? AND client_id=? ORDER BY created_at LIMIT 1",
            (contact_id, client_id),
        ).fetchone()

    def find_by_phone(self, phone: str, client_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM conversations WHERE phone=? AND client_id=? ORDER BY created_at LIMIT 1",
            (phone, client_id),
        ).fetchone()

    def update_contact_details(
        self, id: str, *, phone: str | None = None, neighborhood: str | None = None
    ) -> None:
        if phone:
            self.conn.execute(
                "UPDATE conversations SET phone=? WHERE id=? AND (phone IS NULL OR phone='')",
                (phone, id),
            )
        if neighborhood:
            self.conn.execute("UPDATE conversations SET neighborhood=? WHERE id=?", (neighborhood, id))
        self.conn.commit()

    def all_ids(self, *, client_id: str | None = None, limit: int | None = None, offset: int = 0) -> list[str]:
        sql = "SELECT id FROM conversations"
        params: list = []
        if client_id:
            sql += " WHERE client_id=?"
            params.append(client_id)
        sql += " ORDER BY created_at, id"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [int(limit), int(offset)]
        return [r["id"] for r in self.conn.execute(sql, params)]

    def count(self, *, client_id: str | None = None) -> int:
        if client_id:
            r = self.conn.execute("SELECT COUNT(*) n FROM conversations WHERE client_id=?", (client_id,))
        else:
            r = self.conn.execute("SELECT COUNT(*) n FROM conversations")
        return int(r.fetchone()["n"])


class MessageRepo:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def add(self, m: Message) -> None:
        self.conn.execute(
            "INSERT INTO messages(id, conversation_id, direction, raw_text, created_at, "
            "variant_id, human_edited, meta_json, provider_message_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                m.id,
                m.conversation_id,
                m.direction.value,
                m.raw_text,
                m.created_at.isoformat(),
                m.variant_id,
                int(m.human_edited),
                json.dumps(m.meta),
                m.provider_message_id,
            ),
        )
        self.conn.commit()

    def _row_to_msg(self, r: sqlite3.Row) -> Message:
        return Message(
            id=r["id"],
            conversation_id=r["conversation_id"],
            direction=Direction(r["direction"]),
            raw_text=r["raw_text"],
            created_at=_dt(r["created_at"]),
            variant_id=r["variant_id"],
            human_edited=bool(r["human_edited"]),
            provider_message_id=r["provider_message_id"],
            meta=json.loads(r["meta_json"]),
        )

    def list_for_conversation(self, conversation_id: str) -> list[Message]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()
        return [self._row_to_msg(r) for r in rows]

    def get(self, message_id: str) -> Message | None:
        r = self.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return self._row_to_msg(r) if r else None

    def get_by_provider_id(self, provider_message_id: str) -> Message | None:
        r = self.conn.execute(
            "SELECT * FROM messages WHERE provider_message_id=?", (provider_message_id,)
        ).fetchone()
        return self._row_to_msg(r) if r else None

    def update_meta(self, message_id: str, meta: dict) -> None:
        self.conn.execute(
            "UPDATE messages SET meta_json=? WHERE id=?", (json.dumps(meta), message_id)
        )
        self.conn.commit()

    def count(self, direction: str | None = None) -> int:
        if direction:
            r = self.conn.execute("SELECT COUNT(*) n FROM messages WHERE direction=?", (direction,))
        else:
            r = self.conn.execute("SELECT COUNT(*) n FROM messages")
        return int(r.fetchone()["n"])


class EventRepo:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def current_version(self, conversation_id: str) -> int:
        r = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS v FROM events WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return int(r["v"])

    def input_version(self, conversation_id: str) -> int:
        """Seq of the most recent inbound message event. A planned response is
        stale iff a newer inbound message exists (brief §8) — outbound/bookkeeping
        events like RESPONSE_PLANNED must NOT invalidate a response by themselves.
        """
        r = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS v FROM events "
            "WHERE conversation_id=? AND type='SMS_RECEIVED'",
            (conversation_id,),
        ).fetchone()
        return int(r["v"])

    def append(
        self,
        *,
        id: str,
        conversation_id: str,
        contact_id: str,
        type: EventType,
        created_at: datetime,
        payload: dict | None = None,
        caused_by: str | None = None,
    ) -> Event:
        # seq is assigned here, atomically per conversation.
        cur = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS nxt FROM events WHERE conversation_id=?",
            (conversation_id,),
        )
        seq = int(cur.fetchone()["nxt"])
        ev = Event(
            id=id,
            conversation_id=conversation_id,
            contact_id=contact_id,
            type=type,
            seq=seq,
            created_at=created_at,
            payload=payload or {},
            caused_by=caused_by,
        )
        self.conn.execute(
            "INSERT INTO events(id, conversation_id, contact_id, type, seq, created_at, "
            "payload_json, caused_by) VALUES(?,?,?,?,?,?,?,?)",
            (
                ev.id,
                ev.conversation_id,
                ev.contact_id,
                ev.type.value,
                ev.seq,
                ev.created_at.isoformat(),
                json.dumps(ev.payload),
                ev.caused_by,
            ),
        )
        self.conn.commit()
        return ev

    def list_for_conversation(self, conversation_id: str) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE conversation_id=? ORDER BY seq",
            (conversation_id,),
        ).fetchall()
        return [
            Event(
                id=r["id"],
                conversation_id=r["conversation_id"],
                contact_id=r["contact_id"],
                type=EventType(r["type"]),
                seq=r["seq"],
                created_at=_dt(r["created_at"]),
                payload=json.loads(r["payload_json"]),
                caused_by=r["caused_by"],
            )
            for r in rows
        ]

    def all(self, limit: int = 500) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY created_at DESC, seq DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            Event(
                id=r["id"],
                conversation_id=r["conversation_id"],
                contact_id=r["contact_id"],
                type=EventType(r["type"]),
                seq=r["seq"],
                created_at=_dt(r["created_at"]),
                payload=json.loads(r["payload_json"]),
                caused_by=r["caused_by"],
            )
            for r in rows
        ]


class PlannedResponseRepo:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def save(self, p: PlannedResponse) -> None:
        self.conn.execute(
            "INSERT INTO planned_responses(id, conversation_id, contact_id, in_reply_to, "
            "built_at_version, text, variant_id, validation_json, send_after, created_at, "
            "status, model_used, generation_latency_ms, actions_json, kind, send_attempts, last_error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, text=excluded.text, "
            "validation_json=excluded.validation_json, send_after=excluded.send_after, "
            "variant_id=excluded.variant_id, actions_json=excluded.actions_json, "
            "send_attempts=excluded.send_attempts, last_error=excluded.last_error",
            (
                p.id,
                p.conversation_id,
                p.contact_id,
                p.in_reply_to,
                p.built_at_version,
                p.text,
                p.variant_id,
                p.validation.model_dump_json(),
                p.send_after.isoformat() if p.send_after else None,
                p.created_at.isoformat(),
                p.status,
                p.model_used,
                p.generation_latency_ms,
                json.dumps(p.actions),
                p.kind,
                p.send_attempts,
                p.last_error,
            ),
        )
        self.conn.commit()

    def _row(self, r: sqlite3.Row) -> PlannedResponse:
        from sms_engine.domain.validation import ValidationResult

        return PlannedResponse(
            id=r["id"],
            conversation_id=r["conversation_id"],
            contact_id=r["contact_id"],
            in_reply_to=r["in_reply_to"],
            built_at_version=r["built_at_version"],
            text=r["text"],
            variant_id=r["variant_id"],
            validation=ValidationResult.model_validate_json(r["validation_json"]),
            send_after=_dt(r["send_after"]) if r["send_after"] else None,
            created_at=_dt(r["created_at"]),
            status=r["status"],
            model_used=r["model_used"],
            generation_latency_ms=r["generation_latency_ms"],
            actions=json.loads(r["actions_json"]),
            kind=r["kind"] or "REPLY",
            send_attempts=int(r["send_attempts"] or 0),
            last_error=r["last_error"],
        )

    def get(self, id: str) -> PlannedResponse | None:
        r = self.conn.execute("SELECT * FROM planned_responses WHERE id=?", (id,)).fetchone()
        return self._row(r) if r else None

    def for_conversation(self, conversation_id: str) -> list[PlannedResponse]:
        rows = self.conn.execute(
            "SELECT * FROM planned_responses WHERE conversation_id=? ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def for_reply_to(self, message_id: str) -> list[PlannedResponse]:
        rows = self.conn.execute(
            "SELECT * FROM planned_responses WHERE in_reply_to=? ORDER BY created_at, id", (message_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def stuck_sending(self, older_than: datetime) -> list[PlannedResponse]:
        rows = self.conn.execute(
            "SELECT * FROM planned_responses WHERE status='SENDING' AND send_after <= ?",
            (older_than.isoformat(),),
        ).fetchall()
        return [self._row(r) for r in rows]

    def open_for_conversation(self, conversation_id: str) -> list[PlannedResponse]:
        rows = self.conn.execute(
            "SELECT * FROM planned_responses WHERE conversation_id=? AND status IN "
            "('PLANNED','PENDING_REVIEW') ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def due(self, now: datetime) -> list[PlannedResponse]:
        rows = self.conn.execute(
            "SELECT * FROM planned_responses WHERE status='PLANNED' AND send_after IS NOT NULL "
            "AND send_after <= ? ORDER BY send_after",
            (now.isoformat(),),
        ).fetchall()
        return [self._row(r) for r in rows]

    def set_status(self, id: str, status: str) -> None:
        self.conn.execute("UPDATE planned_responses SET status=? WHERE id=?", (status, id))
        self.conn.commit()

    def record_send_failure(
        self, id: str, *, error: str, retry_at: datetime | None, give_up: bool
    ) -> None:
        self.conn.execute(
            "UPDATE planned_responses SET send_attempts=send_attempts+1, last_error=?, "
            "status=?, send_after=COALESCE(?, send_after) WHERE id=?",
            (error[:500], "FAILED" if give_up else "PLANNED",
             retry_at.isoformat() if retry_at else None, id),
        )
        self.conn.commit()

    def send_after_taken(self, when: datetime) -> bool:
        """True if any still-planned response is already scheduled at this exact second."""
        key = when.isoformat(timespec="seconds")
        r = self.conn.execute(
            "SELECT 1 FROM planned_responses WHERE status='PLANNED' AND substr(send_after,1,19)=? LIMIT 1",
            (key[:19],),
        ).fetchone()
        return r is not None

    def variant_history(self, contact_id: str, limit: int = 10) -> list[str]:
        """Variant ids used for this contact, oldest first (planned or sent, not discarded)."""
        rows = self.conn.execute(
            "SELECT variant_id FROM planned_responses WHERE contact_id=? AND variant_id IS NOT NULL "
            "AND status IN ('PLANNED','SENT') ORDER BY created_at DESC, id DESC LIMIT ?",
            (contact_id, limit),
        ).fetchall()
        return [r["variant_id"] for r in reversed(rows)]

    def neighborhood_variants(self, neighborhood: str, kind: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT p.variant_id AS v FROM planned_responses p "
            "JOIN conversations c ON c.id = p.conversation_id "
            "WHERE c.neighborhood=? AND p.variant_id LIKE ? AND p.status IN ('PLANNED','SENT')",
            (neighborhood, f"{kind}:%"),
        ).fetchall()
        return [r["v"] for r in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) n FROM planned_responses GROUP BY status"
        ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def list_recent(self, limit: int = 50) -> list[PlannedResponse]:
        rows = self.conn.execute(
            "SELECT * FROM planned_responses ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row(r) for r in rows]


class ReviewRepo:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def create(self, row: dict) -> None:
        cols = ",".join(row.keys())
        ph = ",".join("?" for _ in row)
        self.conn.execute(
            f"INSERT INTO reviews({cols}) VALUES({ph})", tuple(row.values())
        )
        self.conn.commit()

    def get(self, id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM reviews WHERE id=?", (id,)).fetchone()

    def for_message(self, message_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reviews WHERE message_id=? ORDER BY created_at, id", (message_id,)
        ).fetchall()

    def open_queue(self, *, client_id: str | None = None) -> list[sqlite3.Row]:
        order = (
            " ORDER BY CASE r.risk_level WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 "
            "WHEN 'MEDIUM' THEN 2 ELSE 3 END, r.created_at, r.id"
        )
        if client_id:
            return self.conn.execute(
                "SELECT r.* FROM reviews r JOIN conversations c ON c.id = r.conversation_id "
                "WHERE r.status='OPEN' AND c.client_id=?" + order,
                (client_id,),
            ).fetchall()
        return self.conn.execute("SELECT r.* FROM reviews r WHERE r.status='OPEN'" + order).fetchall()

    def open_for_conversation(self, conversation_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reviews WHERE conversation_id=? AND status='OPEN' ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()

    def for_conversation(self, conversation_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reviews WHERE conversation_id=? ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()

    def open_counts_by_conversation(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT conversation_id, COUNT(*) n FROM reviews WHERE status='OPEN' GROUP BY conversation_id"
        ).fetchall()
        return {r["conversation_id"]: int(r["n"]) for r in rows}

    def supersede(self, id: str, *, at: datetime, by_message_id: str) -> None:
        """Close an open review because a newer inbound message made its draft stale."""
        self.conn.execute(
            "UPDATE reviews SET status='SUPERSEDED', decided_at=?, notes=? WHERE id=? AND status='OPEN'",
            (at.isoformat(), f"superseded by newer inbound message {by_message_id}", id),
        )
        self.conn.commit()

    def decided(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reviews WHERE status='DECIDED' ORDER BY decided_at"
        ).fetchall()

    def decide(
        self,
        id: str,
        *,
        action: str,
        decided_at: datetime,
        human_edits: str | None,
        final_response: str | None,
        reviewer: str | None,
        notes: str | None,
    ) -> None:
        self.conn.execute(
            "UPDATE reviews SET status='DECIDED', action=?, decided_at=?, human_edits=?, "
            "final_response=?, reviewer=?, notes=? WHERE id=?",
            (action, decided_at.isoformat(), human_edits, final_response, reviewer, notes, id),
        )
        self.conn.commit()

    def all(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reviews ORDER BY created_at").fetchall()


class TraceRepo:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def save(self, t: DecisionTrace) -> None:
        tid = f"trace_{t.conversation_id}_{t.message_id}"
        self.conn.execute(
            "INSERT INTO traces(id, conversation_id, message_id, created_at, json) "
            "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET json=excluded.json",
            (tid, t.conversation_id, t.message_id, t.created_at.isoformat(), t.model_dump_json()),
        )
        self.conn.commit()

    def for_message(self, conversation_id: str, message_id: str) -> DecisionTrace | None:
        r = self.conn.execute(
            "SELECT json FROM traces WHERE conversation_id=? AND message_id=?",
            (conversation_id, message_id),
        ).fetchone()
        return DecisionTrace.model_validate_json(r["json"]) if r else None

    def for_conversation(self, conversation_id: str) -> list[DecisionTrace]:
        rows = self.conn.execute(
            "SELECT json FROM traces WHERE conversation_id=? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        return [DecisionTrace.model_validate_json(r["json"]) for r in rows]

    def all(self) -> list[DecisionTrace]:
        rows = self.conn.execute("SELECT json FROM traces ORDER BY created_at").fetchall()
        return [DecisionTrace.model_validate_json(r["json"]) for r in rows]


class FollowupRepo:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def add(
        self,
        *,
        id: str,
        conversation_id: str,
        contact_id: str,
        due_at: datetime,
        reason: str,
        payload: dict,
        created_at: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT INTO followups(id, conversation_id, contact_id, due_at, reason, "
            "payload_json, status, created_at) VALUES(?,?,?,?,?,?,'SCHEDULED',?)",
            (
                id,
                conversation_id,
                contact_id,
                due_at.isoformat(),
                reason,
                json.dumps(payload),
                created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def for_conversation(self, conversation_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM followups WHERE conversation_id=? ORDER BY due_at",
            (conversation_id,),
        ).fetchall()

    def all(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM followups ORDER BY due_at").fetchall()

    def get(self, id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM followups WHERE id=?", (id,)).fetchone()

    def due(self, now: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM followups WHERE status='SCHEDULED' AND due_at <= ? ORDER BY due_at, id",
            (now.isoformat(),),
        ).fetchall()

    def set_status(
        self, id: str, status: str, *, at: datetime, review_id: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE followups SET status=?, updated_at=?, review_id=COALESCE(?, review_id) WHERE id=?",
            (status, at.isoformat(), review_id, id),
        )
        self.conn.commit()
