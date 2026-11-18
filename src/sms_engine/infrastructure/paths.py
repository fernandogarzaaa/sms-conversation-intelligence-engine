"""Locate the repository's `data/` directory without assuming an editable install.

Resolution order: explicit environment variable -> the source checkout this
package lives in -> `./data` under the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[3]


def data_dir() -> Path:
    env = os.environ.get("SMS_ENGINE_DATA_DIR")
    if env:
        return Path(env)
    candidate = _PKG_ROOT / "data"
    if candidate.is_dir():
        return candidate
    return Path.cwd() / "data"


def clients_dir() -> Path:
    env = os.environ.get("SMS_ENGINE_CLIENTS_DIR")
    return Path(env) if env else data_dir() / "clients"


def seed_dataset_path() -> Path:
    env = os.environ.get("SMS_ENGINE_SEED_PATH")
    return Path(env) if env else data_dir() / "seed" / "inbound_messages.jsonl"
