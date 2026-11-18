"""Operational metrics computed from the live database (brief §13, §22)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sms_engine.infrastructure.db import Database


@dataclass
class OperationalMetrics:
    messages_processed: int = 0
    inbound_messages: int = 0
    outbound_messages: int = 0
    auto_planned: int = 0
    human_review: int = 0
    suppressed: int = 0
    escalated: int = 0
    automation_rate: float = 0.0
    human_review_rate: float = 0.0
    suppression_rate: float = 0.0
    approval_rate: float = 0.0
    edit_rate: float = 0.0
    reject_rate: float = 0.0
    reviews_open: int = 0
    reviews_decided: int = 0
    avg_classification_confidence: float = 0.0
    avg_pipeline_latency_ms: float = 0.0
    responses_invalidated: int = 0
    reviews_superseded: int = 0
    processing_failures: int = 0
    outbound_failed: int = 0
    campaigns_triggered: int = 0
    campaigns_allowed: int = 0
    campaigns_blocked: int = 0
    campaign_block_reasons: dict = field(default_factory=dict)
    planned_by_status: dict = field(default_factory=dict)
    followups_by_status: dict = field(default_factory=dict)
    followups_scheduled: int = 0
    routing_breakdown: dict = field(default_factory=dict)
    by_intent: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


class MetricsService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def compute(self) -> OperationalMetrics:
        c = self.db.conn
        m = OperationalMetrics()

        m.inbound_messages = c.execute(
            "SELECT COUNT(*) n FROM messages WHERE direction='INBOUND'"
        ).fetchone()["n"]
        m.outbound_messages = c.execute(
            "SELECT COUNT(*) n FROM messages WHERE direction='OUTBOUND'"
        ).fetchone()["n"]
        m.messages_processed = m.inbound_messages

        traces = [json.loads(r["json"]) for r in c.execute("SELECT json FROM traces").fetchall()]
        if traces:
            confs = []
            lats = []
            for t in traces:
                cls = t.get("classification", {})
                ints = cls.get("intents", [])
                if ints:
                    confs.append(max(i["confidence"] for i in ints))
                lats.append(t.get("latency_ms", 0.0))
                r = t.get("routing", "")
                m.routing_breakdown[r] = m.routing_breakdown.get(r, 0) + 1
                prim = (t.get("policy", {}) or {}).get("primary_intent")
                if prim:
                    b = m.by_intent.setdefault(prim, {"count": 0, "AUTO_REPLY": 0, "HUMAN_REVIEW": 0, "SUPPRESS": 0, "ESCALATE": 0})
                    b["count"] += 1
                    b[r] = b.get(r, 0) + 1
            m.avg_classification_confidence = round(sum(confs) / len(confs), 3) if confs else 0.0
            m.avg_pipeline_latency_ms = round(sum(lats) / len(lats), 2) if lats else 0.0

        m.auto_planned = m.routing_breakdown.get("AUTO_REPLY", 0)
        m.human_review = m.routing_breakdown.get("HUMAN_REVIEW", 0)
        m.suppressed = m.routing_breakdown.get("SUPPRESS", 0)
        m.escalated = m.routing_breakdown.get("ESCALATE", 0)
        total = sum(m.routing_breakdown.values()) or 1
        m.automation_rate = round(m.auto_planned / total, 3)
        m.human_review_rate = round((m.human_review + m.escalated) / total, 3)
        m.suppression_rate = round((m.suppressed + m.escalated) / total, 3)

        m.responses_invalidated = c.execute(
            "SELECT COUNT(*) n FROM events WHERE type='RESPONSE_INVALIDATED'"
        ).fetchone()["n"]
        m.followups_scheduled = c.execute("SELECT COUNT(*) n FROM followups").fetchone()["n"]
        m.followups_by_status = {
            r["status"]: r["n"]
            for r in c.execute("SELECT status, COUNT(*) n FROM followups GROUP BY status").fetchall()
        }
        m.planned_by_status = {
            r["status"]: r["n"]
            for r in c.execute("SELECT status, COUNT(*) n FROM planned_responses GROUP BY status").fetchall()
        }
        ev_counts = {
            r["type"]: r["n"]
            for r in c.execute("SELECT type, COUNT(*) n FROM events GROUP BY type").fetchall()
        }
        m.reviews_superseded = ev_counts.get("REVIEW_SUPERSEDED", 0)
        m.processing_failures = ev_counts.get("PROCESSING_FAILED", 0)
        m.outbound_failed = ev_counts.get("RESPONSE_FAILED", 0)
        for r in c.execute("SELECT payload_json FROM events WHERE type='CAMPAIGN_TRIGGERED'").fetchall():
            pl = json.loads(r["payload_json"] or "{}")
            if "allowed" not in pl:
                continue  # a bare trigger from an external system, not a decision
            m.campaigns_triggered += 1
            if pl.get("allowed"):
                m.campaigns_allowed += 1
            else:
                m.campaigns_blocked += 1
                reason = str(pl.get("reason", "unknown"))
                m.campaign_block_reasons[reason] = m.campaign_block_reasons.get(reason, 0) + 1

        m.reviews_open = c.execute("SELECT COUNT(*) n FROM reviews WHERE status='OPEN'").fetchone()["n"]
        decided = c.execute("SELECT action FROM reviews WHERE status='DECIDED'").fetchall()
        m.reviews_decided = len(decided)
        if decided:
            acts = [r["action"] for r in decided]
            approve = sum(a in ("APPROVE", "EDIT_AND_SEND") for a in acts)
            edits = sum(a == "EDIT_AND_SEND" for a in acts)
            rejects = sum(a == "REJECT" for a in acts)
            m.approval_rate = round(approve / len(acts), 3)
            m.edit_rate = round(edits / len(acts), 3)
            m.reject_rate = round(rejects / len(acts), 3)

        return m
