"""Turn human-review outcomes into per-intent automation eligibility.

No arbitrary graduation is hardcoded. An intent becomes *eligible* for auto-send
only when, over a configurable minimum sample of human decisions, the approval
rate (approved without edits) clears a configurable bar. This function reports
eligibility; flipping `AutomationConfig` is still a deliberate operator action.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sms_engine.application.config import AutomationConfig
from sms_engine.infrastructure.db import Database


@dataclass
class AutomationEligibility:
    intent: str
    sample: int
    approvals_no_edit: int
    approvals_incl_edit: int
    rejects: int
    approval_rate_no_edit: float
    currently_enabled: bool
    min_sample: int
    min_approval: float
    eligible: bool
    recommendation: str


def compute_eligibility(
    db: Database, config: AutomationConfig | None = None
) -> list[AutomationEligibility]:
    config = config or AutomationConfig.default()
    c = db.conn

    # join each decided review to the primary_intent recorded on its trace
    rows = c.execute(
        """
        SELECT r.action AS action, t.json AS tjson
        FROM reviews r
        JOIN traces t
          ON t.conversation_id = r.conversation_id AND t.message_id = r.message_id
        WHERE r.status = 'DECIDED' AND COALESCE(r.kind, 'REPLY') = 'REPLY'
        """
    ).fetchall()

    buckets: dict[str, dict] = {}
    for row in rows:
        t = json.loads(row["tjson"])
        intent = (t.get("policy", {}) or {}).get("primary_intent") or "UNKNOWN"
        b = buckets.setdefault(
            intent, {"sample": 0, "approve_no_edit": 0, "approve_edit": 0, "reject": 0}
        )
        b["sample"] += 1
        if row["action"] == "APPROVE":
            b["approve_no_edit"] += 1
        elif row["action"] == "EDIT_AND_SEND":
            b["approve_edit"] += 1
        elif row["action"] == "REJECT":
            b["reject"] += 1

    out: list[AutomationEligibility] = []
    for intent, b in sorted(buckets.items()):
        rule = config.rule_for(intent)
        sample = b["sample"]
        approvals_no_edit = b["approve_no_edit"]
        rate = approvals_no_edit / sample if sample else 0.0
        eligible = (
            sample >= rule.graduation_min_sample and rate >= rule.graduation_min_approval
        )
        if rule.enabled:
            rec = "already enabled"
        elif eligible:
            rec = f"ELIGIBLE: enable auto-send for {intent} (rate {rate:.0%} over {sample})"
        elif sample < rule.graduation_min_sample:
            rec = f"insufficient sample ({sample}/{rule.graduation_min_sample})"
        else:
            rec = f"approval rate {rate:.0%} below {rule.graduation_min_approval:.0%} bar"
        out.append(
            AutomationEligibility(
                intent=intent,
                sample=sample,
                approvals_no_edit=approvals_no_edit,
                approvals_incl_edit=approvals_no_edit + b["approve_edit"],
                rejects=b["reject"],
                approval_rate_no_edit=round(rate, 3),
                currently_enabled=rule.enabled,
                min_sample=rule.graduation_min_sample,
                min_approval=rule.graduation_min_approval,
                eligible=eligible,
                recommendation=rec,
            )
        )
    return out
