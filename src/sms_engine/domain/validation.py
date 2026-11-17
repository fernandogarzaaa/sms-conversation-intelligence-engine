"""Deterministic validator output."""

from __future__ import annotations

from pydantic import BaseModel, Field

from sms_engine.domain.enums import ValidationCode


class ValidationFinding(BaseModel):
    code: ValidationCode
    severity: str  # "reject" | "warn"
    message: str
    span: str | None = None  # the offending substring, if any


class ValidationResult(BaseModel):
    passed: bool
    findings: list[ValidationFinding] = Field(default_factory=list)

    @property
    def rejects(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity == "reject"]

    @property
    def warnings(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity == "warn"]

    @classmethod
    def ok(cls) -> ValidationResult:
        return cls(passed=True, findings=[])
