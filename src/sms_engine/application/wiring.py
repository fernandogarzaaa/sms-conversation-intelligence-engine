"""Composition root: build a fully-wired engine from a few knobs.

Tests and the demo call `build_engine(...)` with a FixedClock + seeded RNG to get
a deterministic system; the API builds one with SystemClock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sms_engine.ai import (
    EntityExtractor,
    MockProvider,
    MultiIntentClassifier,
    ResponseGenerator,
)
from sms_engine.ai.provider import LLMProvider
from sms_engine.application.config import AutomationConfig
from sms_engine.application.timing import TimingEngine
from sms_engine.application.variants import VariantPool
from sms_engine.application.send_queue import SendQueue
from sms_engine.context.retrieval import ContextRetriever
from sms_engine.infrastructure.clock import Clock, FixedClock, SystemClock
from sms_engine.infrastructure.db import Database
from sms_engine.infrastructure.ids import IdFactory
from sms_engine.infrastructure.rng import Rng
from sms_engine.infrastructure.repositories import (
    ClientRepo,
    ConversationRepo,
    EventRepo,
    FollowupRepo,
    MessageRepo,
    PlannedResponseRepo,
    ReviewRepo,
    TraceRepo,
)
from sms_engine.policy.engine import PolicyEngine
from sms_engine.validation.engine import ResponseValidator


@dataclass
class Engine:
    db: Database
    clock: Clock
    rng: Rng
    ids: IdFactory
    config: AutomationConfig

    clients: ClientRepo
    conversations: ConversationRepo
    messages: MessageRepo
    events: EventRepo
    planned: PlannedResponseRepo
    reviews: ReviewRepo
    traces: TraceRepo
    followups: FollowupRepo

    classifier: MultiIntentClassifier
    extractor: EntityExtractor
    generator: ResponseGenerator
    retriever: ContextRetriever
    policy: PolicyEngine
    validator: ResponseValidator
    timing: TimingEngine
    variants: VariantPool
    queue: SendQueue

    def close(self) -> None:
        self.db.close()


def build_engine(
    *,
    db_path: str = ":memory:",
    clock: Clock | None = None,
    seed: int | str = 1234,
    deterministic_ids: bool = False,
    provider: LLMProvider | None = None,
    config: AutomationConfig | None = None,
    tz_offset_hours: int = -5,
) -> Engine:
    db = Database(db_path)
    clock = clock or SystemClock()
    rng = Rng(seed)
    ids = IdFactory(deterministic=deterministic_ids)
    config = config or AutomationConfig.default()
    provider = provider or MockProvider()

    conn = db.conn
    events = EventRepo(conn)
    planned = PlannedResponseRepo(conn)

    return Engine(
        db=db,
        clock=clock,
        rng=rng,
        ids=ids,
        config=config,
        clients=ClientRepo(conn),
        conversations=ConversationRepo(conn),
        messages=MessageRepo(conn),
        events=events,
        planned=planned,
        reviews=ReviewRepo(conn),
        traces=TraceRepo(conn),
        followups=FollowupRepo(conn),
        classifier=MultiIntentClassifier(provider),
        extractor=EntityExtractor(provider),
        generator=ResponseGenerator(provider),
        retriever=ContextRetriever(),
        policy=PolicyEngine(config),
        validator=ResponseValidator(),
        timing=TimingEngine(rng, tz_offset_hours=tz_offset_hours),
        variants=VariantPool(rng=rng),
        queue=SendQueue(planned=planned, events=events, ids=ids),
    )


def deterministic_engine(start: datetime, **kw) -> Engine:
    return build_engine(clock=FixedClock(start), deterministic_ids=True, **kw)
