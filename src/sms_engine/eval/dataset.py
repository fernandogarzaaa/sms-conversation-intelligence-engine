"""Load the synthetic labelled dataset."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parents[3] / "data" / "seed" / "inbound_messages.jsonl"


@dataclass
class SeedRow:
    id: str
    text: str
    expected_intents: list[str]
    notes: str = ""
    synthetic: bool = True


def load_seed(path: str | Path = _DEFAULT) -> list[SeedRow]:
    rows: list[SeedRow] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        rows.append(
            SeedRow(
                id=d["id"],
                text=d["text"],
                expected_intents=list(d.get("expected_intents", [])),
                notes=d.get("notes", ""),
                synthetic=bool(d.get("synthetic", True)),
            )
        )
    return rows
