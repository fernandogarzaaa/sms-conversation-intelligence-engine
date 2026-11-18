"""The send worker: drains due planned responses through the SMS transport.

Three guards run at send time, in addition to the planning-time checks:

1. **Version guard** (SendQueue.due): a newer inbound message makes the plan stale.
2. **State guard**: the conversation state is rebuilt. An opt-out cancels
   everything; a suppression activated after planning cancels automated
   messages; an open issue cancels campaign and follow-up messages.
3. **Quiet-hours guard**: if the worker is catching up after downtime and the
   recipient's local time is outside the send window, the message is moved to
   the next morning window instead of going out at night.

Delivery is check -> claim -> send -> finalise, one message at a time. The
checks run immediately before each claim, so an opt-out that arrives while an
earlier message is in flight still stops the next one. The claim is a
conditional PLANNED -> SENDING update inside a `BEGIN IMMEDIATE` transaction,
committed before the network call: the database lock is never held across I/O,
and two workers (threads or processes) can never send the same message. Transport failures are retried with
exponential backoff up to `sending.max_attempts`, then marked FAILED.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sms_engine.application.state_builder import build_state
from sms_engine.application.wiring import Engine
from sms_engine.domain.enums import EventType, ReferralStatus
from sms_engine.domain.messages import Direction, Message
from sms_engine.domain.planning import PlannedResponse
from sms_engine.domain.state import ConversationState
from sms_engine.infrastructure.transport import OutboundSms, TransportError
from sms_engine.policy import actions as A

log = logging.getLogger("sms_engine.send_worker")


class SendWorker:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def process_due(self, now: datetime | None = None) -> list[Message]:
        e = self.e
        now = now or e.clock.now()
        due_ids = [p.id for p in e.planned.due(now)]

        sent: list[Message] = []
        for pid in due_ids:
            # Re-check and claim each message on its own, immediately before it is
            # delivered: a STOP or a new message that arrives while an earlier
            # message is in flight must still stop this one.
            claimed = self._check_and_claim(pid, now)
            if claimed is None:
                continue
            out = self._deliver(claimed, now)
            if out is not None:
                sent.append(out)
        return sent

    def _check_and_claim(self, pid: str, now: datetime) -> PlannedResponse | None:
        e = self.e
        with e.db.transaction():
            p = e.planned.get(pid)
            if p is None or p.status != "PLANNED" or (p.send_after and p.send_after > now):
                return None  # another worker took it, or it was cancelled / rescheduled
            current = e.events.input_version(p.conversation_id)
            if p.built_at_version < current:
                e.planned.set_status(p.id, "INVALIDATED")
                self._event(p, EventType.RESPONSE_INVALIDATED, now,
                            {"planned_response_id": p.id, "reason": "stale at send time",
                             "built_at_version": p.built_at_version, "current_version": current})
                return None
            state = self._state(p.conversation_id)
            blocked = self._blocked_reason(p, state)
            if blocked:
                self._cancel(p, now, blocked)
                return None
            client = e.clients.get(state.client_id)
            tz = e.client_timezone(client)
            if not e.timing.in_window(now, tz):
                nxt = e.timing.schedule(
                    now, char_len=0, intent_count=1, is_question=False,
                    salt=f"requeue:{p.id}:{p.send_attempts}", tz=tz,
                    min_delay_seconds=0,
                ).send_after
                if not e.timing.in_window(nxt, tz):  # pragma: no cover - defensive
                    nxt = now + timedelta(hours=1)
                p.send_after = nxt
                e.planned.save(p)
                return None
            if not e.planned.claim(p.id):
                return None
            p.status = "SENDING"
            return p

    def recover_stuck(self, now: datetime | None = None, *, older_than_minutes: int = 15) -> list[str]:
        """Mark responses stuck in SENDING (worker died mid-send) as FAILED.

        At-most-once delivery: we do not know whether the provider accepted the
        message, so we never retry it automatically. A person can resend.
        """
        e = self.e
        now = now or e.clock.now()
        ids: list[str] = []
        with e.db.transaction():
            for p in e.planned.stuck_sending(now - timedelta(minutes=older_than_minutes)):
                e.planned.record_send_failure(p.id, error="interrupted while sending", retry_at=None, give_up=True)
                self._event(p, EventType.RESPONSE_FAILED, now, {"planned_response_id": p.id,
                                                                "reason": "interrupted while sending"})
                ids.append(p.id)
        return ids

    # -- internals ------------------------------------------------------------------

    def _state(self, conversation_id: str) -> ConversationState:
        e = self.e
        row = e.conversations.get(conversation_id)
        return build_state(
            contact_id=row["contact_id"], conversation_id=conversation_id, client_id=row["client_id"],
            events=e.events.list_for_conversation(conversation_id),
            messages=e.messages.list_for_conversation(conversation_id),
        )

    @staticmethod
    def _blocked_reason(p: PlannedResponse, state: ConversationState) -> str | None:
        if state.opted_out:
            return "contact opted out before send time"
        if state.suppression.active and p.model_used != "human":
            reason = state.suppression.reason.value if state.suppression.reason else "ACTIVE"
            return f"contact suppressed ({reason}) after this response was planned"
        if p.kind in ("CAMPAIGN", "FOLLOWUP") and state.has_unresolved_issue:
            return "an issue is open; campaign/follow-up messages are held"
        if p.kind == "CAMPAIGN":
            presented = state.offer_kinds_presented()
            repeated = sorted(A.OFFER_KIND_FOR_ACTION[a] for a in p.actions
                              if a in A.OFFER_KIND_FOR_ACTION and A.OFFER_KIND_FOR_ACTION[a] in presented)
            if repeated:
                return f"already sent to this contact ({', '.join(repeated)})"
        return None

    def _cancel(self, p: PlannedResponse, now: datetime, reason: str) -> None:
        self.e.planned.set_status(p.id, "CANCELLED")
        self._event(p, EventType.RESPONSE_INVALIDATED, now,
                    {"planned_response_id": p.id, "reason": reason, "at": "send time"})
        log.info("planned response cancelled at send time",
                 extra={"planned_response_id": p.id, "reason": reason})

    def _deliver(self, p: PlannedResponse, now: datetime) -> Message | None:
        e = self.e
        conv = e.conversations.get(p.conversation_id)
        client = e.clients.get(conv["client_id"]) if conv else None
        msg_id = e.ids.new("msg")
        sms = OutboundSms(
            conversation_id=p.conversation_id,
            message_id=msg_id,
            client_id=conv["client_id"] if conv else "",
            to=conv["phone"] if conv else None,
            from_=(client.sms_from_number or (client.phone_numbers[0] if client.phone_numbers else None))
            if client else None,
            body=p.text,
        )
        try:
            receipt = e.transport.send(sms)
        except Exception as exc:
            retryable = getattr(exc, "retryable", True) if isinstance(exc, TransportError) else True
            self._fail(p, now, str(exc), retryable)
            return None

        out = Message(
            id=msg_id,
            conversation_id=p.conversation_id,
            direction=Direction.OUTBOUND,
            raw_text=p.text,
            created_at=now,
            variant_id=p.variant_id,
            human_edited=p.model_used == "human",
            provider_message_id=receipt.provider_message_id,
            meta={"transport": receipt.transport, "delivery_status": receipt.status,
                  "planned_response_id": p.id, "kind": p.kind},
        )
        links = _find_urls(p.text)
        offers = []
        for a in p.actions:
            if a.startswith("repeat_offer:"):
                offers.append({"kind": a.split(":", 1)[1], "detail": p.text[:60]})
            elif a in A.OFFER_KIND_FOR_ACTION:
                offers.append({"kind": A.OFFER_KIND_FOR_ACTION[a], "detail": p.text[:60],
                               "presented_at": now.isoformat()})
        extra: dict = {}
        if A.REFERRAL_ASK in p.actions:
            extra["referral_status"] = ReferralStatus.ASKED.value
        if p.kind == "CAMPAIGN":
            extra["campaign_status"] = "ACTIVE"
        with e.db.transaction():
            e.messages.add(out)
            e.queue.mark_sent(p, out.id, now, offers=offers, links=links, extra=extra)
        return out

    def _fail(self, p: PlannedResponse, now: datetime, error: str, retryable: bool) -> None:
        e = self.e
        cfg = e.config.sending
        attempts = p.send_attempts + 1
        give_up = (not retryable) or attempts >= cfg.max_attempts
        retry_at = None if give_up else now + timedelta(seconds=cfg.retry_backoff_seconds * 2 ** (attempts - 1))
        with e.db.transaction():
            e.planned.record_send_failure(p.id, error=error, retry_at=retry_at, give_up=give_up)
            if give_up:
                self._event(p, EventType.RESPONSE_FAILED, now,
                            {"planned_response_id": p.id, "error": error[:300], "attempts": attempts})
        log.warning("outbound send failed",
                    extra={"planned_response_id": p.id, "attempts": attempts, "give_up": give_up,
                           "error": error[:200]})

    def _event(self, p: PlannedResponse, etype: EventType, now: datetime, payload: dict) -> None:
        self.e.events.append(
            id=self.e.ids.new("evt"), conversation_id=p.conversation_id, contact_id=p.contact_id,
            type=etype, created_at=now, payload=payload, caused_by=p.in_reply_to,
        )


def _find_urls(text: str) -> list[str]:
    import re

    return re.findall(r"https?://[^\s)]+", text or "")
