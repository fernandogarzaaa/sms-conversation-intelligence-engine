"""The canonical demo scenarios (brief §16). Used by the e2e tests and the demo.

Each scenario is a list of steps; a step is an inbound message plus assertions
expressed as plain predicates over the PipelineResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from sms_engine.application.pipeline import PipelineResult
from sms_engine.domain.enums import IntentType, RoutingDecision

Check = Callable[[PipelineResult], bool]


@dataclass
class Step:
    text: str
    checks: dict[str, Check] = field(default_factory=dict)


@dataclass
class Scenario:
    key: str
    title: str
    contact_id: str
    steps: list[Step]


def _has(*types: IntentType) -> Check:
    return lambda r: set(types) <= r.classification.types()


def _route(d: RoutingDecision) -> Check:
    return lambda r: r.routing == d


def _no_send() -> Check:
    return lambda r: r.planned_response is None


def _forbids(action: str) -> Check:
    return lambda r: r.policy.forbids(action)


SCENARIOS: list[Scenario] = [
    Scenario(
        "A", "Pure positive -> auto-reply eligible", "scen_a",
        [Step("Everything went great, thanks!", {
            "intent PURE_POSITIVE": _has(IntentType.PURE_POSITIVE),
            "routes AUTO_REPLY": _route(RoutingDecision.AUTO_REPLY),
            "response is planned + validated": lambda r: r.planned_response and r.planned_response.validation.passed,
        })],
    ),
    Scenario(
        "B", "Maya — mixed sentiment + open issue -> suppress + human review", "scen_maya",
        [Step("The crew was fantastic but there was a wasp nest so they couldn't clear the grill area.", {
            "intents MIXED_SENTIMENT + OPEN_ISSUE": _has(IntentType.MIXED_SENTIMENT, IntentType.OPEN_ISSUE),
            "routes HUMAN_REVIEW": _route(RoutingDecision.HUMAN_REVIEW),
            "campaign suppressed": lambda r: r.state_after.suppression.active,
            "NO referral ask": _forbids("referral_ask"),
            "NO review request": _forbids("review_request"),
            "no automated message sent": _no_send(),
            "issue captured with location": lambda r: r.entities.issue and r.entities.issue_location,
        })],
    ),
    Scenario(
        "C", "John — identity correction + positive + referral", "scen_john",
        [Step("This is actually John. Everything went great. I'll definitely recommend you.", {
            "3 intents": _has(IntentType.IDENTITY_CORRECTION, IntentType.PURE_POSITIVE, IntentType.REFERRAL_INTENT),
            "routes HUMAN_REVIEW (identity)": _route(RoutingDecision.HUMAN_REVIEW),
            "CRM identity updated to John": lambda r: r.state_after.identity.name == "John",
            "referral marked committed": lambda r: r.state_after.referral_status.value in ("COMMITTED", "COMPLETED"),
            "does NOT ask for a referral again": _forbids("referral_ask"),
            "draft references the conversation": lambda r: r.draft_text and "John" in r.draft_text,
        })],
    ),
    Scenario(
        "D", "Question -> grounded FAQ answer is eligible", "scen_q",
        [Step("Do you guys move pianos?", {
            "intent QUESTION": _has(IntentType.QUESTION),
            "answer is grounded in an approved FAQ": lambda r: r.retrieved.grounded,
            "routes AUTO_REPLY": _route(RoutingDecision.AUTO_REPLY),
            "answer mentions pianos": lambda r: r.planned_response and "piano" in r.planned_response.text.lower(),
        }),
         Step("How much would that run me?", {
            "pricing question is not grounded": lambda r: not r.retrieved.grounded,
            "routes HUMAN_REVIEW": _route(RoutingDecision.HUMAN_REVIEW),
         })],
    ),
    Scenario(
        "E", "Opt out -> immediate global suppression", "scen_stop",
        [Step("Please stop texting me.", {
            "intent STOP_OPTOUT": _has(IntentType.STOP_OPTOUT),
            "routes SUPPRESS": _route(RoutingDecision.SUPPRESS),
            "contact opted out": lambda r: r.state_after.opted_out,
            "nothing sent": _no_send(),
        }),
         Step("Actually the move was great, thanks!", {
            "still suppressed after a positive message": _route(RoutingDecision.SUPPRESS),
            "still nothing sent": _no_send(),
         })],
    ),
    Scenario(
        "F", "Volunteered future intent -> follow-up task", "scen_future",
        [Step("My daughter is moving next spring. I'll probably need help.", {
            "intent VOLUNTEERED_FUTURE_INTENT": _has(IntentType.VOLUNTEERED_FUTURE_INTENT),
            "extracts who/what/when": lambda r: r.entities.future_person and r.entities.future_timeframe,
            "a follow-up task is scheduled": lambda r: len(r.followup_ids) == 1,
        })],
    ),
    Scenario(
        "G", "Race condition -> queued response invalidated", "scen_race",
        [Step("Everything went great, thanks!", {
            "first response is queued": lambda r: r.planned_response is not None,
        }),
         Step("Wait — the crew left a dresser in the driveway.", {
            "second message routes to review": _route(RoutingDecision.HUMAN_REVIEW),
            "prior queued response was invalidated": lambda r: any(
                s.stage == "QUEUE_INVALIDATION" for s in r.trace.steps
            ),
            "state recomputed with the new issue": lambda r: r.state_after.has_unresolved_issue,
         })],
    ),
]


def run_scenario(pipeline, scenario: Scenario, *, client_id: str = "greenline") -> list[dict]:
    """Execute a scenario; return per-step results with check pass/fail."""
    out = []
    for i, step in enumerate(scenario.steps, start=1):
        r = pipeline.handle_inbound(client_id=client_id, contact_id=scenario.contact_id, text=step.text)
        checks = {name: bool(fn(r)) for name, fn in step.checks.items()}
        out.append({
            "step": i,
            "text": step.text,
            "routing": r.routing.value,
            "intents": sorted(x.type.value for x in r.classification.intents),
            "checks": checks,
            "all_passed": all(checks.values()),
            "result": r,
        })
    return out
