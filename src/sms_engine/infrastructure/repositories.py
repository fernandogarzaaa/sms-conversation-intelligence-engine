"""Repositories over the SQLite database. Thin mappers, no business logic."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from sms_engine.domain.context import ClientContext
from sms_engine.domain.events import Event
from sms_engine.domain.enums import EventType
from sms_engine.domain.messages import Direction, Message
from sms_engine.domain.planning import PlannedResponse
from sms_engine.domain.trace import DecisionTrace


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class ClientRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
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
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self, id: str, contact_id: str, client_id: str, created_at: datetime, phone: str | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO conversations(id, contact_id, client_id, phone, created_at) "
            "VALUES(?,?,?,?,?)",
            (id, contact_id, client_id, phone, created_at.isoformat()),
        )
        self.conn.commit()

    def get(self, id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM conversations WHERE id=?", (id,)).fetchone()

    def find_by_contact(self, contact_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM conversations WHERE contact_id=? ORDER BY created_at LIMIT 1",
            (contact_id,),
        ).fetchone()

    def all_ids(self) -> list[str]:
        return [r["id"] for r in self.conn.execute("SELECT id FROM conversations ORDER BY created_at")]


class MessageRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, m: Message) -> None:
        self.conn.execute(
            "INSERT INTO messages(id, conversation_id, direction, raw_text, created_at, "
            "variant_id, human_edited, meta_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                m.id,
                m.conversation_id,
                m.direction.value,
                m.raw_text,
                m.created_at.isoformat(),
                m.variant_id,
                int(m.human_edited),
                json.dumps(m.meta),
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


class EventRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
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
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save(self, p: PlannedResponse) -> None:
        self.conn.execute(
            "INSERT INTO planned_responses(id, conversation_id, contact_id, in_reply_to, "
            "built_at_version, text, variant_id, validation_json, send_after, created_at, "
            "status, model_used, generation_latency_ms, actions_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, text=excluded.text, "
            "validation_json=excluded.validation_json, send_after=excluded.send_after, "
            "variant_id=excluded.variant_id, actions_json=excluded.actions_json",
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
        )

    def get(self, id: str) -> PlannedResponse | None:
        r = self.conn.execute("SELECT * FROM planned_responses WHERE id=?", (id,)).fetchone()
        return self._row(r) if r else None

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


class ReviewRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
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

    def open_queue(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reviews WHERE status='OPEN' ORDER BY "
            "CASE risk_level WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 "
            "WHEN 'MEDIUM' THEN 2 ELSE 3 END, created_at",
        ).fetchall()

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
    def __init__(self, conn: sqlite3.Connection) -> None:
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
    def __init__(self, conn: sqlite3.Connection) -> None:
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
