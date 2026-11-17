"""Human-plausible outbound timing. Separate from the LLM. Deterministic under a
seeded RNG so tests can assert exact send times.

Rules (brief §14):
  - short message  -> short delay
  - complex message-> longer delay
  - question (auto)-> faster
  - outside the send window (quiet hours) -> queue for the next morning window
  - jitter, never :00, never :30, no two identical timestamps

Quiet hours are evaluated in the *recipient's* local time. The default send
window is 08:15–20:30, inside the US TCPA 8am–9pm window with a safety margin;
both ends are configurable. A client can set an IANA timezone (e.g.
"America/Chicago") so daylight-saving changes are handled; otherwise a fixed UTC
offset is used.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sms_engine.infrastructure.rng import Rng

DEFAULT_WINDOW_START = time(8, 15)
DEFAULT_WINDOW_END = time(20, 30)
MORNING_SPREAD_MIN = 150  # spread morning sends over 150 minutes after the window opens

# Backwards-compatible names (older code/tests referenced these).
MORNING_OPEN = DEFAULT_WINDOW_START
NIGHT_START = 21


@dataclass
class TimingDecision:
    send_after: datetime
    base_delay_seconds: int
    reason: str


def resolve_tz(name: str | None, fallback_offset_hours: int = 0) -> timezone | ZoneInfo:
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone(timedelta(hours=fallback_offset_hours))


class TimingEngine:
    def __init__(
        self,
        rng: Rng,
        *,
        tz_offset_hours: int = 0,
        tz: str | None = None,
        window_start: time = DEFAULT_WINDOW_START,
        window_end: time = DEFAULT_WINDOW_END,
        is_taken: Callable[[datetime], bool] | None = None,
    ) -> None:
        if window_end <= window_start:
            raise ValueError("send window end must be after its start")
        self.rng = rng
        self.tz_offset = tz_offset_hours
        self.default_tz = tz
        self.window_start = window_start
        self.window_end = window_end
        # optional persistent check (e.g. the planned_responses table) so de-dupe
        # survives restarts and works across workers
        self.is_taken = is_taken
        self._issued: set[str] = set()

    # -- tz helpers --------------------------------------------------------------

    def _zone(self, tz: str | None):
        return resolve_tz(tz or self.default_tz, self.tz_offset)

    def _local(self, dt: datetime, tz: str | None = None) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self._zone(tz))

    @staticmethod
    def _to_utc(local: datetime) -> datetime:
        return local.astimezone(timezone.utc)

    def in_window(self, dt: datetime, tz: str | None = None) -> bool:
        t = self._local(dt, tz).time()
        return self.window_start <= t < self.window_end

    # -- policy --------------------------------------------------------------------

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
        tz: str | None = None,
        min_delay_seconds: int | None = None,
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
        if min_delay_seconds is not None:
            base = max(base, min_delay_seconds)

        jitter = r.randint(-20, 40)
        target = now + timedelta(seconds=base + jitter)
        reason = f"{bucket} message, base {base}s"

        if not self.in_window(target, tz):
            target = self._next_morning(target, r, tz)
            reason = f"{bucket} message held for quiet hours -> morning window"

        target = self._dodge_forbidden_minutes(target, r)
        target = self._dedupe(target)
        return TimingDecision(send_after=target, base_delay_seconds=base, reason=reason)

    def _next_morning(self, target: datetime, r: Rng, tz: str | None) -> datetime:
        zone = self._zone(tz)
        local = self._local(target, tz)
        day = local.date()
        if local.time() >= self.window_end:
            day = day + timedelta(days=1)
        opens = datetime.combine(day, self.window_start, tzinfo=zone)
        window_minutes = (
            datetime.combine(day, self.window_end) - datetime.combine(day, self.window_start)
        ).total_seconds() / 60
        spread = int(min(MORNING_SPREAD_MIN, max(1, window_minutes - 1)) * 60)
        morning = opens + timedelta(seconds=r.randint(0, spread))
        return self._to_utc(morning)

    def _dodge_forbidden_minutes(self, dt: datetime, r: Rng) -> datetime:
        # never land exactly on :00 or :30, and never on a whole minute (avoids
        # a wall of identical-looking timestamps)
        if dt.minute in (0, 30) or dt.second == 0:
            dt = dt + timedelta(seconds=r.randint(63, 227))
        return dt

    def _dedupe(self, dt: datetime) -> datetime:
        while True:
            key = dt.isoformat(timespec="seconds")
            taken = key in self._issued or (self.is_taken is not None and self.is_taken(dt))
            if not taken:
                break
            dt = dt + timedelta(seconds=7)
        if len(self._issued) > 50_000:  # bounded memory in long-lived processes
            self._issued.clear()
        self._issued.add(dt.isoformat(timespec="seconds"))
        return dt
