"""Outbound SMS transports.

The engine decides *what* to send and *when*; a transport only delivers it.

    SmsTransport (Protocol)
    ├── LogTransport     default — records the message locally, nothing leaves the machine
    └── TwilioTransport  Twilio Programmable Messaging REST API

Also provides `verify_twilio_signature`, used by the inbound webhook.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("sms_engine.transport")


class TransportError(RuntimeError):
    """Delivery failed. `retryable` tells the send worker whether to try again."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class OutboundSms:
    conversation_id: str
    message_id: str
    client_id: str
    to: str | None
    from_: str | None
    body: str


@dataclass
class SendReceipt:
    provider_message_id: str | None
    status: str
    transport: str
    raw: dict = field(default_factory=dict)


class SmsTransport(Protocol):
    name: str

    def send(self, sms: OutboundSms) -> SendReceipt: ...


class LogTransport:
    """Development transport: logs and remembers messages; sends nothing."""

    name = "log"

    def __init__(self) -> None:
        self.sent: list[OutboundSms] = []

    def send(self, sms: OutboundSms) -> SendReceipt:
        self.sent.append(sms)
        log.info(
            "outbound sms (log transport, not delivered)",
            extra={"conversation_id": sms.conversation_id, "message_id": sms.message_id,
                   "chars": len(sms.body)},
        )
        return SendReceipt(provider_message_id=None, status="logged", transport=self.name)


class FailingTransport:
    """Test helper: fails the first `failures` sends, then succeeds."""

    name = "failing"

    def __init__(self, failures: int = 1, *, retryable: bool = True) -> None:
        self.failures = failures
        self.retryable = retryable
        self.calls = 0
        self.sent: list[OutboundSms] = []

    def send(self, sms: OutboundSms) -> SendReceipt:
        self.calls += 1
        if self.calls <= self.failures:
            raise TransportError("simulated provider outage", retryable=self.retryable)
        self.sent.append(sms)
        return SendReceipt(provider_message_id=f"FAKE{self.calls:04d}", status="queued", transport=self.name)


class TwilioTransport:
    """Send through Twilio's Messages API. Requires `httpx`."""

    name = "twilio"
    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    def __init__(self, account_sid: str, auth_token: str, *, timeout: float = 10.0, client=None) -> None:
        if not account_sid or not auth_token:
            raise ValueError("TwilioTransport needs TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.timeout = timeout
        self._client = client

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def send(self, sms: OutboundSms) -> SendReceipt:
        if not sms.to:
            raise TransportError("conversation has no phone number on file", retryable=False)
        if not sms.from_:
            raise TransportError("client has no sms_from_number configured", retryable=False)
        try:
            resp = self._http().post(
                self.API.format(sid=self.account_sid),
                data={"To": sms.to, "From": sms.from_, "Body": sms.body},
                auth=(self.account_sid, self.auth_token),
            )
        except Exception as exc:  # network / timeout
            raise TransportError(f"twilio request failed: {exc}") from exc
        if resp.status_code >= 400:
            retryable = resp.status_code == 429 or resp.status_code >= 500
            raise TransportError(
                f"twilio returned {resp.status_code}: {resp.text[:200]}", retryable=retryable
            )
        data = resp.json()
        return SendReceipt(
            provider_message_id=data.get("sid"),
            status=data.get("status", "queued"),
            transport=self.name,
            raw={k: data.get(k) for k in ("sid", "status", "num_segments", "error_code")},
        )


def build_transport(name: str, *, account_sid: str = "", auth_token: str = "") -> SmsTransport:
    name = (name or "log").lower()
    if name == "log":
        return LogTransport()
    if name == "twilio":
        return TwilioTransport(account_sid, auth_token)
    raise ValueError(f"unknown SMS transport {name!r} (expected 'log' or 'twilio')")


def twilio_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """Compute Twilio's X-Twilio-Signature for a form-encoded webhook request."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_twilio_signature(auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)
