"""Human-plausible outbound timing. Separate from the LLM. Deterministic under a
seeded RNG so tests can assert exact send times.

Rules (brief §14):
  - short message  -> short delay
  - complex message-> longer delay
  - question (auto)-> faster
  - night          -> queue for the morning window
  - jitter, never :00, never :30, no two identical timestamps
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sms_engine.infrastructure.rng import Rng

NIGHT_START = 21  # 21:00 local -> hold
MORNING_OPEN = time(7, 15)
MORNING_SPREAD_MIN = 150  # send between 07:15 and 09:45


@dataclass
class TimingDecision:
    send_after: datetime
    base_delay_seconds: int
    reason: str


class TimingEngine:
    def __init__(self, rng: Rng, *, tz_offset_hours: int = 0) -> None:
        self.rng = rng
        self.tz_offset = tz_offset_hours
        self._issued: set[str] = set()

    def _local(self, dt: datetime) -> datetime:
        return dt + timedelta(hours=self.tz_offset)

    def _from_local(self, dt: datetime) -> datetime:
        return dt - timedelta(hours=self.tz_offset)

    def complexity_bucket(self, *, char_len: int, intent_count: int, is_question: bool) -> str:
        if is_question and intent_count <= 1 and char_len < 140:
            return "fast"
        if char_len < 80 and intent_count <= 1:
            return "short"
        if char_len > 240 or intent_count >= 3:
            return "complex"
        return "medium"

    def schedule(
        self,
        now: datetime,
        *,
        char_len: int,
        intent_count: int,
        is_question: bool,
        salt: str = "",
    ) -> TimingDecision:
        bucket = self.complexity_bucket(
            char_len=char_len, intent_count=intent_count, is_question=is_question
        )
        r = self.rng.derived(f"timing:{salt}")
        base = {
            "fast": r.randint(35, 110),
            "short": r.randint(60, 200),
            "medium": r.randint(240, 560),
            "complex": r.randint(600, 1500),
        }[bucket]

        jitter = r.randint(-20, 40)
        target = now + timedelta(seconds=base + jitter)

        local = self._local(target)
        reason = f"{bucket} message, base {base}s"
        if local.hour >= NIGHT_START or local.time() < MORNING_OPEN:
            # roll forward to the next morning window (07:15 local or later)
            day = local.date()
            if local.hour >= NIGHT_START:
                day = day + timedelta(days=1)
            offset = r.randint(0, MORNING_SPREAD_MIN * 60)
            morning = datetime.combine(day, MORNING_OPEN) + timedelta(seconds=offset)
            target = self._from_local(morning)
            local = morning
            reason = f"{bucket} message held overnight -> morning window"

        target = self._dodge_forbidden_minutes(target, r)
        target = self._dedupe(target)
        return TimingDecision(send_after=target, base_delay_seconds=base, reason=reason)

    @staticmethod
    def _is_forbidden(dt: datetime) -> bool:
        # Never land on :00 or :30, and never on a whole minute: a wall of
        # identical-looking timestamps reads as automated.
        return dt.minute in (0, 30) or dt.second == 0

    def _dodge_forbidden_minutes(self, dt: datetime, r: Rng) -> datetime:
        # Loop, don't dodge once: a single +63..227s jump can land on :00/:30
        # or a whole minute again (seed 0, salt m19 landed on 15:02:00).
        while self._is_forbidden(dt):
            dt = dt + timedelta(seconds=r.randint(63, 227))
        return dt

    def _dedupe(self, dt: datetime) -> datetime:
        # A +7s dedupe step can also land on a forbidden minute, so the
        # forbidden check belongs inside this loop too.
        key = dt.isoformat(timespec="seconds")
        while key in self._issued or self._is_forbidden(dt):
            dt = dt + timedelta(seconds=7)
            key = dt.isoformat(timespec="seconds")
        self._issued.add(key)
        return dt
