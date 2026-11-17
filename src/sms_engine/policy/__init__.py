"""Deterministic policy & suppression engine.

Pure functions over domain objects. This layer — never the model — decides
routing, risk, suppression, and which downstream actions are permitted.
"""

from sms_engine.policy.engine import PolicyEngine, PolicyInput
from sms_engine.policy import actions

__all__ = ["PolicyEngine", "PolicyInput", "actions"]
