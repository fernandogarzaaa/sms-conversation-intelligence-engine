"""Runtime settings, read from environment variables.

Every setting has a safe local-development default, so `sms-engine serve` works
with no configuration at all. Production deployments set the auth, transport,
and provider variables (see `.env.example`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sms_engine.application.config import AutomationConfig


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _list(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


@dataclass
class Settings:
    db_path: str = "sms_engine.db"

    # --- auth -------------------------------------------------------------------
    # API keys accepted on the JSON API (X-API-Key header or Bearer token).
    # Empty = auth disabled (local development only; a warning is logged).
    api_keys: list[str] = field(default_factory=list)
    # HTTP Basic credentials for the review dashboard. Empty password = no auth.
    ui_user: str = "reviewer"
    ui_password: str = ""

    # --- config / data ---------------------------------------------------------
    automation_config_path: str | None = None
    clients_dir: str | None = None
    seed_clients: bool = True
    default_timezone: str | None = "America/Chicago"

    # --- worker ------------------------------------------------------------------
    # seconds between background send-queue / follow-up passes; 0 disables it
    worker_interval_seconds: float = 0.0

    # --- SMS transport ---------------------------------------------------------
    transport: str = "log"  # log | twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    # verify X-Twilio-Signature on the inbound webhook (on whenever a token is set)
    twilio_validate_signatures: bool = True
    # externally visible base URL, used to rebuild the signed URL behind a proxy
    public_base_url: str = ""

    # --- observability ---------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def ui_auth_enabled(self) -> bool:
        return bool(self.ui_password)

    def automation_config(self) -> AutomationConfig:
        if self.automation_config_path:
            return AutomationConfig.from_toml(self.automation_config_path)
        return AutomationConfig.default()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        auto = env.get("SMS_ENGINE_AUTOMATION_CONFIG") or None
        if auto is None and Path("config/automation.toml").is_file():
            auto = "config/automation.toml"
        return cls(
            db_path=env.get("SMS_ENGINE_DB", "sms_engine.db"),
            api_keys=_list(env.get("SMS_ENGINE_API_KEYS")),
            ui_user=env.get("SMS_ENGINE_UI_USER", "reviewer"),
            ui_password=env.get("SMS_ENGINE_UI_PASSWORD", ""),
            automation_config_path=auto,
            clients_dir=env.get("SMS_ENGINE_CLIENTS_DIR") or None,
            seed_clients=_bool(env.get("SMS_ENGINE_SEED_CLIENTS"), True),
            default_timezone=env.get("SMS_ENGINE_DEFAULT_TZ", "America/Chicago") or None,
            worker_interval_seconds=float(env.get("SMS_ENGINE_WORKER_INTERVAL", "0") or 0),
            transport=(env.get("SMS_ENGINE_TRANSPORT", "log") or "log").lower(),
            twilio_account_sid=env.get("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=env.get("TWILIO_AUTH_TOKEN", ""),
            twilio_validate_signatures=_bool(env.get("TWILIO_VALIDATE_SIGNATURES"), True),
            public_base_url=env.get("SMS_ENGINE_PUBLIC_URL", "").rstrip("/"),
            log_level=env.get("SMS_ENGINE_LOG_LEVEL", "INFO").upper(),
            log_json=_bool(env.get("SMS_ENGINE_LOG_JSON"), False),
        )
