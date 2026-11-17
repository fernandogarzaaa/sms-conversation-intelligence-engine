"""Load raw client-source files from `data/clients/*_raw.json` and ingest them."""

from __future__ import annotations

import json
from pathlib import Path

from sms_engine.context.ingestion import ContextIngestor, RawClientSources
from sms_engine.domain.context import ClientContext

_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "clients"


def load_raw(path: str | Path) -> RawClientSources:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return RawClientSources(
        client_id=d["client_id"],
        name=d["name"],
        website_text=d.get("website_text", ""),
        faq=d.get("faq", []),
        config=d.get("config", {}),
        sample_replies=d.get("sample_replies", []),
        sources=d.get("sources", []),
    )


def load_client_context(path: str | Path) -> ClientContext:
    return ContextIngestor().ingest(load_raw(path))


def all_seed_contexts(data_dir: Path = _DATA_DIR) -> list[ClientContext]:
    return [load_client_context(p) for p in sorted(data_dir.glob("*_raw.json"))]


def default_context() -> ClientContext:
    return load_client_context(_DATA_DIR / "greenline_raw.json")
