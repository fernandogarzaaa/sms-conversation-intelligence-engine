from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sms_engine.application.pipeline import InboundPipeline
from sms_engine.application.service import SmsEngineService
from sms_engine.application.wiring import deterministic_engine
from sms_engine.context.packs import default_context

START = datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
def engine():
    e = deterministic_engine(START, seed=42)
    e.clients.upsert(default_context())
    yield e
    e.close()


@pytest.fixture
def svc(engine) -> SmsEngineService:
    return SmsEngineService(engine)


@pytest.fixture
def pipe(engine) -> InboundPipeline:
    return InboundPipeline(engine)


@pytest.fixture
def client_ctx():
    return default_context()
