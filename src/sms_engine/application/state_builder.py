"""The conversation state machine.

`build_state` is a pure reducer: ConversationState = fold(apply, events), with
message rows supplying bodies. It is called on every inbound message so a
late-arriving message is never applied on top of a stale snapshot — state is
always recomputed from the full, ordered event log.
"""

from __future__ import annotations

from datetime import datetime

from sms_engine.domain.enums import (
    CampaignStatus,
    EventType,
    IntentType,
    ReferralStatus,
    SuppressionReason,
)
from sms_engine.domain.events import Event
from sms_engine.domain.messages import Direction, Message
from sms_engine.domain.state import (
    ConversationState,
    Fact,
    FutureIntent,
    Identity,
    IntentRecord,
    Offer,
    OpenIssue,
    Suppression,
)


def _parse_dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(v)


def build_state(
    *,
    contact_id: str,
    conversation_id: str,
    client_id: str,
    events: list[Event],
    messages: list[Message],
) -> ConversationState:
    st = ConversationState(
        contact_id=contact_id,
        conversation_id=conversation_id,
        client_id=client_id,
    )
    msg_by_id = {m.id: m for m in messages}

    for ev in sorted(events, key=lambda e: e.seq):
        _apply(st, ev, msg_by_id)
        st.version = ev.seq

    return st


def _apply(st: ConversationState, ev: Event, msg_by_id: dict[str, Message]) -> None:
    p = ev.payload
    t = ev.type

    if t == EventType.JOB_COMPLETED:
        st.campaign_status = CampaignStatus.PENDING

    elif t == EventType.CAMPAIGN_TRIGGERED:
        if st.campaign_status in (CampaignStatus.PENDING,):
            st.campaign_status = CampaignStatus.ACTIVE

    elif t == EventType.SMS_RECEIVED:
        mid = p.get("message_id")
        if mid and mid not in st.received_messages:
            st.received_messages.append(mid)
        m = msg_by_id.get(mid)
        st.last_interaction = m.created_at if m else ev.created_at

    elif t == EventType.RESPONSE_SENT:
        mid = p.get("message_id")
        if mid and mid not in st.sent_messages:
            st.sent_messages.append(mid)
        for link in p.get("links_sent", []):
            if link not in st.links_sent:
                st.links_sent.append(link)
        for off in p.get("offers_presented", []):
            st.offers_presented.append(
                Offer(
                    kind=off["kind"],
                    detail=off.get("detail", ""),
                    presented_at=_parse_dt(off.get("presented_at")) or ev.created_at,
                    source_message_id=mid,
                )
            )
        m = msg_by_id.get(mid)
        st.last_interaction = m.created_at if m else ev.created_at

    elif t == EventType.CLASSIFICATION_COMPLETED:
        src = p.get("source_message_id", "")
        ts = _parse_dt(p.get("timestamp")) or ev.created_at
        for it in p.get("intents", []):
            st.intents.append(
                IntentRecord(
                    type=IntentType(it["type"]),
                    confidence=float(it["confidence"]),
                    source_message_id=src,
                    timestamp=ts,
                )
            )

    elif t == EventType.STATE_UPDATED:
        _apply_state_delta(st, p, ev.created_at)

    elif t == EventType.SUPPRESSION_ACTIVATED:
        st.suppression = Suppression(
            active=True,
            reason=SuppressionReason(p["reason"]),
            since=_parse_dt(p.get("since")) or ev.created_at,
            note=p.get("note"),
        )
        if st.suppression.reason == SuppressionReason.OPT_OUT:
            st.campaign_status = CampaignStatus.OPTED_OUT
        else:
            st.campaign_status = CampaignStatus.SUPPRESSED

    elif t == EventType.SUPPRESSION_RELEASED:
        st.suppression = Suppression(
            active=False,
            reason=None,
            released_at=_parse_dt(p.get("released_at")) or ev.created_at,
            note=p.get("note"),
        )
        if st.campaign_status == CampaignStatus.SUPPRESSED:
            st.campaign_status = CampaignStatus.ACTIVE

    elif t == EventType.ESCALATED:
        # escalation implies a hold on outbound campaign messaging
        if not st.suppression.active:
            st.suppression = Suppression(
                active=True,
                reason=SuppressionReason.ESCALATED,
                since=ev.created_at,
                note=p.get("note"),
            )
            st.campaign_status = CampaignStatus.SUPPRESSED

    elif t == EventType.SCHEDULED_FOLLOWUP:
        fid = p.get("future_intent_id")
        for fi in st.future_intents:
            if fi.id == fid:
                fi.followup_scheduled_for = _parse_dt(p.get("due_at"))


def _apply_state_delta(st: ConversationState, p: dict, when: datetime) -> None:
    # identity correction
    ident = p.get("identity")
    if ident:
        new_name = ident.get("name")
        if new_name and new_name != st.identity.name:
            st.identity = Identity(
                name=new_name,
                previous_name=st.identity.name,
                verified=bool(ident.get("verified", True)),
                source_message_id=ident.get("source_message_id"),
                updated_at=when,
            )

    for f in p.get("facts", []):
        st.facts.append(
            Fact(
                key=f["key"],
                value=str(f["value"]),
                source_message_id=f["source_message_id"],
                timestamp=_parse_dt(f.get("timestamp")) or when,
                confidence=float(f.get("confidence", 0.8)),
            )
        )

    for oi in p.get("open_issues", []):
        st.open_issues.append(
            OpenIssue(
                id=oi["id"],
                description=oi["description"],
                source_message_id=oi["source_message_id"],
                opened_at=_parse_dt(oi.get("opened_at")) or when,
            )
        )

    for rid in p.get("resolve_issues", []):
        for oi in st.open_issues:
            if oi.id == rid and not oi.resolved:
                oi.resolved = True
                oi.resolved_at = when

    for fi in p.get("future_intents", []):
        st.future_intents.append(
            FutureIntent(
                id=fi["id"],
                description=fi["description"],
                who=fi.get("who"),
                what=fi.get("what"),
                when_text=fi.get("when_text"),
                source_message_id=fi["source_message_id"],
                created_at=_parse_dt(fi.get("created_at")) or when,
            )
        )

    for off in p.get("offers_presented", []):
        st.offers_presented.append(
            Offer(
                kind=off["kind"],
                detail=off.get("detail", ""),
                presented_at=_parse_dt(off.get("presented_at")) or when,
                source_message_id=off.get("source_message_id"),
            )
        )

    for link in p.get("links_sent", []):
        if link not in st.links_sent:
            st.links_sent.append(link)

    if "referral_status" in p and p["referral_status"]:
        st.referral_status = ReferralStatus(p["referral_status"])

    if "campaign_status" in p and p["campaign_status"]:
        st.campaign_status = CampaignStatus(p["campaign_status"])
