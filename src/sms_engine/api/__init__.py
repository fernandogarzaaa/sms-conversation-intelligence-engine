"""HTTP API (FastAPI). Thin handlers over `application.service.SmsEngineService`."""

from sms_engine.api.app import create_app

__all__ = ["create_app"]
