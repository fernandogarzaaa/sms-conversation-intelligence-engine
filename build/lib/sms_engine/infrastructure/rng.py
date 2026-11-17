"""Seeded RNG wrapper.

The timing engine and variant selector take an `Rng`, never the global `random`
module, so a fixed seed makes their output reproducible.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


class Rng:
    def __init__(self, seed: int | str = 0) -> None:
        self._seed = seed
        self._r = random.Random(seed)

    def reseed(self, seed: int | str) -> None:
        self._seed = seed
        self._r = random.Random(seed)

    def derived(self, salt: str) -> Rng:
        """A child RNG deterministically derived from this one + a salt string."""
        return Rng(f"{self._seed}:{salt}")

    def randint(self, lo: int, hi: int) -> int:
        return self._r.randint(lo, hi)

    def uniform(self, lo: float, hi: float) -> float:
        return self._r.uniform(lo, hi)

    def choice(self, seq: Sequence[T]) -> T:
        return self._r.choice(list(seq))

    def shuffle(self, seq: list) -> None:
        self._r.shuffle(seq)

    def random(self) -> float:
        return self._r.random()
