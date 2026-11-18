"""Unit: safety does not depend on the model (opt-out backstop, degraded mode)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sms_engine.ai.mock_provider import MockProvider
from sms_engine.ai.provider import LLMProvider, ProviderResponse
from sms_engine.application.pipeline import InboundPipeline
from sms_engine.application.variants import VariantPool, _redundant
from sms_engine.application.wiring import deterministic_engine
from sms_engine.context.packs import default_context
from sms_engine.domain.enums import RoutingDecision
from sms_engine.infrastructure.rng import Rng
from sms_engine.policy.optout import detect_opt_out

START = datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc)


class BlindModel(LLMProvider):
    """A model that always calls everything pure positive and extracts nothing."""

    name = "blind"

    def generate_reply(self, spec):
        return MockProvider().generate_reply(spec)

    def classify_intents(self, text, state_summary):
        return ProviderResponse(payload={"intents": [{"type": "PURE_POSITIVE", "confidence": 0.99}]},
                                raw="", model=self.name)

    def extract_entities(self, text, intents):
        return ProviderResponse(payload={}, raw="", model=self.name)


class DownModel(LLMProvider):
    name = "down"

    def classify_intents(self, text, state_summary):
        raise TimeoutError("provider timed out")

    def extract_entities(self, text, intents):
        raise TimeoutError("provider timed out")

    def generate_reply(self, spec):
        raise TimeoutError("provider timed out")


def _pipe(provider):
    e = deterministic_engine(START, seed=1, provider=provider)
    e.clients.upsert(default_context())
    return e, InboundPipeline(e)


@pytest.mark.parametrize("text", [
    "STOP", "unsubscribe", "Stop texting me please", "Thanks, but please don't text me again.",
    "take me off your list", "opt out",
])
def test_opt_out_is_honoured_even_when_the_model_misses_it(text):
    _, pipe = _pipe(BlindModel())
    r = pipe.handle_inbound(client_id="greenline", contact_id="blind", text=text)
    assert r.routing == RoutingDecision.SUPPRESS
    assert r.state_after.opted_out
    assert any(s.stage == "OPT_OUT_BACKSTOP" for s in r.trace.steps)


@pytest.mark.parametrize("text", [
    "The crew didn't stop working all day, great job",
    "Can you stop by Tuesday to grab the couch?",
    "Everything went great, thanks!",
])
def test_backstop_does_not_fire_on_ordinary_sentences(text):
    assert detect_opt_out(text) is None


def test_provider_outage_uses_rules_fallback_and_never_auto_sends():
    e, pipe = _pipe(DownModel())
    r = pipe.handle_inbound(client_id="greenline", contact_id="down", text="Everything went great, thanks!")
    assert r.error is None  # the pipeline itself did not crash
    assert "fallback" in r.classification.model_used
    assert r.routing == RoutingDecision.HUMAN_REVIEW
    assert any(h.rule_id == "R_PROVIDER_DEGRADED" for h in r.policy.rule_hits)
    assert r.planned_response is None and r.review_id

    stop = pipe.handle_inbound(client_id="greenline", contact_id="down2", text="please stop texting me")
    assert stop.routing == RoutingDecision.SUPPRESS and stop.state_after.opted_out


def test_variants_do_not_double_up_thanks():
    ack = "Thanks for the note - I've passed it along to the team."
    assert _redundant("{ack} Thanks for taking a second to let us know.", ack)
    assert _redundant("{ack} Appreciate the note.", ack)
    assert not _redundant("{ack} Good to hear.", ack)
    pool = VariantPool(rng=Rng(5))
    for i in range(10):
        sel = pool.select("acknowledgement", contact_id=f"c{i}", ack_text=ack)
        assert not _redundant(sel.template, ack)
