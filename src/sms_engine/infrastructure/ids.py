"""Id generation. Deterministic mode for tests, random mode for real runs."""

from __future__ import annotations

import uuid


class IdFactory:
    def __init__(self, deterministic: bool = False, prefix_seed: str = "t") -> None:
        self._deterministic = deterministic
        self._seed = prefix_seed
        self._counters: dict[str, int] = {}

    def new(self, kind: str) -> str:
        if self._deterministic:
            n = self._counters.get(kind, 0) + 1
            self._counters[kind] = n
            return f"{kind}_{self._seed}{n:04d}"
        return f"{kind}_{uuid.uuid4().hex[:12]}"
