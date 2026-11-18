"""Unit: background worker loop and the Twilio transport (offline, mocked HTTP)."""

from __future__ import annotations

import time

import httpx
import pytest

from sms_engine.application.worker import BackgroundWorker
from sms_engine.infrastructure.transport import (
    OutboundSms,
    TransportError,
    TwilioTransport,
    build_transport,
    twilio_signature,
    verify_twilio_signature,
)


def _sms(**kw) -> OutboundSms:
    base = dict(conversation_id="c", message_id="m", client_id="greenline",
                to="+15125550111", from_="+15125550100", body="Hello")
    base.update(kw)
    return OutboundSms(**base)


def _twilio(handler) -> TwilioTransport:
    return TwilioTransport("AC123", "secret", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_twilio_send_posts_the_right_request():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.content.decode()
        return httpx.Response(201, json={"sid": "SM9", "status": "queued", "num_segments": "1"})

    rec = _twilio(handler).send(_sms())
    assert rec.provider_message_id == "SM9" and rec.status == "queued"
    assert seen["url"].endswith("/Accounts/AC123/Messages.json")
    assert seen["auth"].startswith("Basic ")
    assert "To=%2B15125550111" in seen["body"] and "Body=Hello" in seen["body"]


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (400, False), (401, False)])
def test_twilio_errors_are_classified(status, retryable):
    t = _twilio(lambda req: httpx.Response(status, json={"message": "nope"}))
    with pytest.raises(TransportError) as exc:
        t.send(_sms())
    assert exc.value.retryable is retryable


def test_twilio_network_error_is_retryable():
    def handler(req):
        raise httpx.ConnectError("down")

    with pytest.raises(TransportError) as exc:
        _twilio(handler).send(_sms())
    assert exc.value.retryable


def test_twilio_without_phone_numbers_is_not_retryable():
    t = _twilio(lambda req: httpx.Response(201, json={}))
    for sms in (_sms(to=None), _sms(from_=None)):
        with pytest.raises(TransportError) as exc:
            t.send(sms)
        assert not exc.value.retryable


def test_build_transport():
    assert build_transport("log").name == "log"
    assert build_transport("twilio", account_sid="A", auth_token="B").name == "twilio"
    with pytest.raises(ValueError):
        build_transport("twilio")
    with pytest.raises(ValueError):
        build_transport("carrier-pigeon")


def test_signature_round_trip():
    params = {"Body": "hi", "From": "+1", "MessageSid": "SM1"}
    sig = twilio_signature("tok", "https://x.example/webhooks/twilio/sms", params)
    assert verify_twilio_signature("tok", "https://x.example/webhooks/twilio/sms", params, sig)
    assert not verify_twilio_signature("tok", "https://x.example/other", params, sig)
    assert not verify_twilio_signature("tok", "https://x.example/webhooks/twilio/sms", params, "")


class _Svc:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

        class _SW:
            def recover_stuck(self_inner):
                return []

        self.send_worker = _SW()

    def run_worker_pass(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("db locked")
        return {"sent": [], "followups": []}


def test_worker_runs_in_the_background_and_stops():
    svc = _Svc()
    w = BackgroundWorker(svc, 0.01)
    w.start()
    deadline = time.time() + 2
    while svc.calls < 3 and time.time() < deadline:
        time.sleep(0.01)
    w.stop()
    assert svc.calls >= 3 and not w.running and w.passes >= 3


def test_worker_survives_a_failing_pass():
    w = BackgroundWorker(_Svc(fail=True), 1)
    out = w.run_once()
    assert "db locked" in out["error"] and w.last_error


def test_worker_interval_must_be_positive():
    with pytest.raises(ValueError):
        BackgroundWorker(_Svc(), 0)
