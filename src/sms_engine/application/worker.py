"""Background worker: periodically drains the send queue and due follow-ups.

Runs in-process (a daemon thread started by the API when
SMS_ENGINE_WORKER_INTERVAL > 0) or standalone via `sms-engine worker`. Either
way it is just a loop around `SmsEngineService.run_worker_pass`, so tests and
the CLI exercise exactly the same code.
"""

from __future__ import annotations

import logging
import threading

from sms_engine.application.service import SmsEngineService

log = logging.getLogger("sms_engine.worker")


class BackgroundWorker:
    def __init__(self, svc: SmsEngineService, interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        self.svc = svc
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.passes = 0
        self.last_error: str | None = None

    def run_once(self) -> dict:
        try:
            out = self.svc.run_worker_pass()
            self.passes += 1
            self.last_error = None
            if out["sent"] or out["followups"]:
                log.info("worker pass", extra={"sent": len(out["sent"]), "followups": len(out["followups"])})
            return out
        except Exception as exc:  # never let the loop die
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("worker pass failed")
            return {"sent": [], "followups": [], "error": self.last_error}

    def _loop(self) -> None:
        recovered = self.svc.send_worker.recover_stuck()
        if recovered:
            log.warning("marked interrupted sends as failed", extra={"ids": recovered})
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sms-engine-worker", daemon=True)
        self._thread.start()
        log.info("background worker started", extra={"interval_seconds": self.interval})

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_forever(self) -> None:  # pragma: no cover - CLI entry
        self.start()
        try:
            while self.running:
                self._thread.join(1.0)  # type: ignore[union-attr]
        except KeyboardInterrupt:
            self.stop()
