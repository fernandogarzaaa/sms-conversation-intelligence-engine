"""Regression tests for findings from the PR #1 automated review."""

from __future__ import annotations

from datetime import datetime, timezone

from sms_engine.application.campaigns import CampaignService
from sms_engine.application.pipeline import InboundPipeline
from sms_engine.application.send_worker import SendWorker
from sms_engine.application.wiring import build_engine, deterministic_engine
from sms_engine.context.packs import default_context
from sms_engine.infrastructure.clock import FixedClock
from sms_engine.infrastructure.transport import LogTransport

START = datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc)
POS = "Everything went great, thanks!"


class StopDuringFirstSend(LogTransport):
    """While the first message is in flight, the *other* customer texts STOP."""

    def __init__(self) -> None:
        super().__init__()
        self.pipe: InboundPipeline | None = None
        self.stop_contact: str | None = None

    def send(self, sms):
        if self.stop_contact and not self.sent:
            self.pipe.handle_inbound(client_id="greenline", contact_id=self.stop_contact, text="STOP")
        return super().send(sms)


def test_stop_during_an_earlier_delivery_cancels_a_later_due_message():
    t = StopDuringFirstSend()
    e = deterministic_engine(START, seed=42, transport=t)
    e.clients.upsert(default_context())
    pipe = InboundPipeline(e)
    r1 = pipe.handle_inbound(client_id="greenline", contact_id="first", text=POS)
    e.clock.advance(minutes=5)
    r2 = pipe.handle_inbound(client_id="greenline", contact_id="second", text=POS)
    assert r1.planned_response.send_after < r2.planned_response.send_after
    t.pipe, t.stop_contact = pipe, "second"
    e.clock.advance(hours=1)
    sent = SendWorker(e).process_due(e.clock.now())
    assert [m.conversation_id for m in sent] == [r1.conversation_id]
    assert e.planned.get(r2.planned_response.id).status in ("INVALIDATED", "CANCELLED")


def test_send_claim_is_atomic_across_connections(tmp_path):
    path = str(tmp_path / "shared.db")
    a = build_engine(db_path=path, clock=FixedClock(START), deterministic_ids=True)
    a.clients.upsert(default_context())
    r = InboundPipeline(a).handle_inbound(client_id="greenline", contact_id="dup", text=POS)
    b = build_engine(db_path=path, clock=FixedClock(START))
    pid = r.planned_response.id
    # both workers saw the row as PLANNED; only one may win the claim
    assert a.planned.claim(pid) is True
    assert b.planned.claim(pid) is False
    a.close()
    b.close()


def test_two_worker_processes_send_a_message_once(tmp_path):
    path = str(tmp_path / "shared2.db")
    ta, tb = LogTransport(), LogTransport()
    a = build_engine(db_path=path, clock=FixedClock(START), deterministic_ids=True, transport=ta)
    a.clients.upsert(default_context())
    InboundPipeline(a).handle_inbound(client_id="greenline", contact_id="once", text=POS)
    b = build_engine(db_path=path, clock=FixedClock(START), transport=tb)
    later = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
    SendWorker(a).process_due(later)
    SendWorker(b).process_due(later)
    assert len(ta.sent) + len(tb.sent) == 1
    a.close()
    b.close()


def test_same_campaign_step_is_not_queued_twice(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="twice_camp", text=POS)
    engine.clock.advance(hours=1)
    SendWorker(engine).process_due(engine.clock.now())
    camps = CampaignService(engine)
    first = camps.trigger(r.conversation_id, "review_request")
    second = camps.trigger(r.conversation_id, "review_request")
    assert first.allowed and not second.allowed
    assert "already queued" in second.reason
    engine.clock.advance(hours=1)
    sent = SendWorker(engine).process_due(engine.clock.now())
    assert len([m for m in sent if "review" in m.raw_text]) == 1


def test_referral_ask_is_not_queued_while_its_link_is_pending(engine, pipe):
    r = pipe.handle_inbound(client_id="greenline", contact_id="ref_pend", text=POS)
    engine.clock.advance(hours=1)
    SendWorker(engine).process_due(engine.clock.now())
    camps = CampaignService(engine)
    assert camps.trigger(r.conversation_id, "referral_link").allowed
    assert not camps.trigger(r.conversation_id, "referral_ask").allowed
