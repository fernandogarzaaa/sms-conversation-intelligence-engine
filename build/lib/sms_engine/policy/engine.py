"""The deterministic policy engine.

`PolicyEngine.decide(...)` evaluates an ordered list of rules against the
classification, entities, and *prior* conversation state, and folds their hits
into a single `PolicyOutcome`. No randomness, no model calls.

Design bias (brief §5): when signals conflict or confidence is low, prefer
HUMAN_REVIEW / SUPPRESS over a dangerous automated send.
"""

from __future__ import annotations

from dataclasses import dataclass

from sms_engine.application.config import AutomationConfig
from sms_engine.domain.classification import Classification
from sms_engine.domain.context import ClientContext
from sms_engine.domain.entities import Entities
from sms_engine.domain.enums import (
    IntentType,
    RiskLevel,
    RoutingDecision,
    SuppressionReason,
)
from sms_engine.domain.policy import PolicyOutcome, PolicyRuleHit
from sms_engine.domain.state import ConversationState
from sms_engine.policy import actions as A

_ROUTING_PRECEDENCE = {
    RoutingDecision.AUTO_REPLY: 0,
    RoutingDecision.HUMAN_REVIEW: 1,
    RoutingDecision.SUPPRESS: 2,
    RoutingDecision.ESCALATE: 3,
}


@dataclass
class PolicyInput:
    classification: Classification
    entities: Entities
    state_before: ConversationState
    client: ClientContext


class PolicyEngine:
    def __init__(self, config: AutomationConfig | None = None) -> None:
        self.config = config or AutomationConfig.default()

    # -- public API -----------------------------------------------------------

    def decide(self, pin: PolicyInput) -> PolicyOutcome:
        hits: list[PolicyRuleHit] = []
        for rule in self._rules:
            hit = rule(self, pin)
            if hit:
                hits.append(hit)

        if not hits:
            # No rule matched at all -> unknown -> human review (false-negative bias).
            hits.append(
                PolicyRuleHit(
                    rule_id="R_DEFAULT_UNKNOWN",
                    description="No policy rule matched; defaulting to human review.",
                    routing=RoutingDecision.HUMAN_REVIEW,
                    risk=RiskLevel.MEDIUM,
                )
            )

        routing = max(
            (h.routing for h in hits if h.routing),
            key=lambda r: _ROUTING_PRECEDENCE[r],
            default=RoutingDecision.HUMAN_REVIEW,
        )
        risk = max(
            (h.risk for h in hits if h.risk),
            key=lambda r: r.rank,
            default=RiskLevel.LOW,
        )
        forbidden: list[str] = []
        for h in hits:
            for a in h.forbid_actions:
                if a not in forbidden:
                    forbidden.append(a)

        suppression_activate = None
        for h in hits:  # last writer wins, but opt-out beats everything
            if h.activate_suppression:
                suppression_activate = h.activate_suppression
        if any(
            h.activate_suppression == SuppressionReason.OPT_OUT for h in hits
        ):
            suppression_activate = SuppressionReason.OPT_OUT

        suppression_release = any(h.release_suppression for h in hits)

        primary = self._primary_intent(pin, hits)
        allowed = self._allowed_actions(routing, forbidden, pin, primary)
        requires_grounding = (
            routing == RoutingDecision.AUTO_REPLY
            and pin.classification.has(IntentType.QUESTION)
        )

        summary = "; ".join(f"[{h.rule_id}] {h.description}" for h in hits)

        return PolicyOutcome(
            routing=routing,
            risk=risk,
            forbidden_actions=forbidden,
            suppression_activate=suppression_activate,
            suppression_release=suppression_release,
            rule_hits=hits,
            summary=summary,
            requires_grounding=requires_grounding,
            primary_intent=primary,
            allowed_actions=allowed,
        )

    # -- helpers ------------------------------------------------------------------

    def _primary_intent(self, pin: PolicyInput, hits: list[PolicyRuleHit]) -> str | None:
        order = [
            IntentType.STOP_OPTOUT,
            IntentType.NEGATIVE,
            IntentType.WRONG_NUMBER,
            IntentType.MIXED_SENTIMENT,
            IntentType.OPEN_ISSUE,
            IntentType.IDENTITY_CORRECTION,
            IntentType.QUESTION,
            IntentType.VOLUNTEERED_FUTURE_INTENT,
            IntentType.REPEAT_BUSINESS_INTENT,
            IntentType.REFERRAL_INTENT,
            IntentType.INLINE_REFERRAL,
            IntentType.DEFERRAL,
            IntentType.PURE_POSITIVE,
            IntentType.UNRELATED,
        ]
        present = pin.classification.types()
        for it in order:
            if it in present:
                return it.value
        return None

    def _allowed_actions(
        self,
        routing: RoutingDecision,
        forbidden: list[str],
        pin: PolicyInput,
        primary: str | None,
    ) -> list[str]:
        if routing in (RoutingDecision.SUPPRESS, RoutingDecision.ESCALATE):
            return []
        allowed = [A.ACKNOWLEDGEMENT]
        if pin.classification.has(IntentType.QUESTION):
            allowed.append(A.ANSWER_QUESTION)
        if pin.classification.has(IntentType.IDENTITY_CORRECTION):
            allowed.append(A.CONFIRM_IDENTITY)
        return [a for a in allowed if a not in forbidden]

    # -- rules ------------------------------------------------------------------
    # Each rule returns a PolicyRuleHit or None. Order matters only for `summary`
    # readability; folding is precedence-based, not first-match.

    def _r_optout(self, pin: PolicyInput) -> PolicyRuleHit | None:
        if pin.classification.has(IntentType.STOP_OPTOUT) or pin.entities.opt_out_phrase:
            return PolicyRuleHit(
                rule_id="R_STOP_OPTOUT",
                description="Opt-out detected: immediate global suppression of all outbound.",
                routing=RoutingDecision.SUPPRESS,
                risk=RiskLevel.HIGH,
                activate_suppression=SuppressionReason.OPT_OUT,
                forbid_actions=[A.ANY_OUTBOUND, *sorted(A.CAMPAIGN_ACTIONS)],
            )
        return None

    def _r_already_opted_out(self, pin: PolicyInput) -> PolicyRuleHit | None:
        if pin.state_before.opted_out:
            return PolicyRuleHit(
                rule_id="R_ALREADY_OPTED_OUT",
                description="Contact previously opted out; suppress everything.",
                routing=RoutingDecision.SUPPRESS,
                risk=RiskLevel.HIGH,
                forbid_actions=[A.ANY_OUTBOUND, *sorted(A.CAMPAIGN_ACTIONS)],
            )
        return None

    def _r_negative(self, pin: PolicyInput) -> PolicyRuleHit | None:
        if pin.classification.has(IntentType.NEGATIVE):
            return PolicyRuleHit(
                rule_id="R_NEGATIVE",
                description="Negative sentiment: suppress campaign and escalate to a human.",
                routing=RoutingDecision.ESCALATE,
                risk=RiskLevel.CRITICAL,
                activate_suppression=SuppressionReason.COMPLAINT,
                forbid_actions=[A.ANY_OUTBOUND, *sorted(A.CAMPAIGN_ACTIONS)],
            )
        return None

    def _r_wrong_number(self, pin: PolicyInput) -> PolicyRuleHit | None:
        if pin.classification.has(IntentType.WRONG_NUMBER) or pin.entities.wrong_number_phrase:
            return PolicyRuleHit(
                rule_id="R_WRONG_NUMBER",
                description="Wrong-number signal: suppress campaign, human review to confirm.",
                routing=RoutingDecision.HUMAN_REVIEW,
                risk=RiskLevel.HIGH,
                activate_suppression=SuppressionReason.WRONG_NUMBER,
                forbid_actions=[A.ANY_OUTBOUND, *sorted(A.CAMPAIGN_ACTIONS)],
            )
        return None

    def _r_mixed_or_open_issue(self, pin: PolicyInput) -> PolicyRuleHit | None:
        c = pin.classification
        if c.has(IntentType.MIXED_SENTIMENT) or c.has(IntentType.OPEN_ISSUE):
            return PolicyRuleHit(
                rule_id="R_OPEN_ISSUE",
                description=(
                    "Unresolved issue / mixed sentiment: suppress campaign, human review. "
                    "No referral ask, no review request, no promo."
                ),
                routing=RoutingDecision.HUMAN_REVIEW,
                risk=RiskLevel.HIGH,
                activate_suppression=SuppressionReason.OPEN_ISSUE,
                forbid_actions=sorted(A.CAMPAIGN_ACTIONS),
            )
        return None

    def _r_state_has_open_issue(self, pin: PolicyInput) -> PolicyRuleHit | None:
        if pin.state_before.has_unresolved_issue:
            return PolicyRuleHit(
                rule_id="R_STATE_OPEN_ISSUE",
                description="Prior unresolved issue on file: campaign stays suppressed.",
                routing=RoutingDecision.HUMAN_REVIEW,
                risk=RiskLevel.HIGH,
                forbid_actions=sorted(A.CAMPAIGN_ACTIONS),
            )
        return None

    def _r_repeated_issues(self, pin: PolicyInput) -> PolicyRuleHit | None:
        c = pin.classification
        prior = sum(1 for i in pin.state_before.open_issues if not i.resolved)
        new = 1 if (c.has(IntentType.OPEN_ISSUE) or c.has(IntentType.MIXED_SENTIMENT)) else 0
        limit = self.config.escalate_on_open_issues
        if prior and prior + new >= limit:
            return PolicyRuleHit(
                rule_id="R_REPEATED_ISSUES",
                description=(
                    f"{prior + new} unresolved issues on this contact (limit {limit}): "
                    "escalate to a human, hold all automated outbound."
                ),
                routing=RoutingDecision.ESCALATE,
                risk=RiskLevel.CRITICAL,
                activate_suppression=SuppressionReason.ESCALATED,
                forbid_actions=[A.ANY_OUTBOUND, *sorted(A.CAMPAIGN_ACTIONS)],
            )
        return None

    def _r_identity_correction(self, pin: PolicyInput) -> PolicyRuleHit | None:
        if pin.classification.has(IntentType.IDENTITY_CORRECTION) or pin.entities.corrected_name:
            return PolicyRuleHit(
                rule_id="R_IDENTITY_CORRECTION",
                description="Identity correction: update identity, human review before any send.",
                routing=RoutingDecision.HUMAN_REVIEW,
                risk=RiskLevel.MEDIUM,
            )
        return None

    def _r_question(self, pin: PolicyInput) -> PolicyRuleHit | None:
        c = pin.classification
        if not c.has(IntentType.QUESTION):
            return None
        rule = self.config.rule_for("QUESTION")
        conf = c.confidence_of(IntentType.QUESTION)
        risky = c.types() & {
            IntentType.MIXED_SENTIMENT,
            IntentType.OPEN_ISSUE,
            IntentType.NEGATIVE,
            IntentType.IDENTITY_CORRECTION,
        }
        if rule.enabled and conf >= rule.min_confidence and not risky:
            return PolicyRuleHit(
                rule_id="R_QUESTION_AUTO_IF_GROUNDED",
                description=(
                    "Question with high confidence: auto-reply ONLY if answer is grounded "
                    "in an approved FAQ; otherwise the pipeline downgrades to human review."
                ),
                routing=RoutingDecision.AUTO_REPLY,
                risk=RiskLevel.MEDIUM,
            )
        return PolicyRuleHit(
            rule_id="R_QUESTION_REVIEW",
            description="Question: route to human review.",
            routing=RoutingDecision.HUMAN_REVIEW,
            risk=RiskLevel.MEDIUM,
        )

    def _r_pure_positive(self, pin: PolicyInput) -> PolicyRuleHit | None:
        c = pin.classification
        if not c.has(IntentType.PURE_POSITIVE):
            return None
        # any of these co-occurring intents means "not purely positive" for automation
        blockers = c.types() & {
            IntentType.MIXED_SENTIMENT,
            IntentType.OPEN_ISSUE,
            IntentType.NEGATIVE,
            IntentType.IDENTITY_CORRECTION,
            IntentType.WRONG_NUMBER,
            IntentType.STOP_OPTOUT,
            IntentType.QUESTION,
        }
        rule = self.config.rule_for("PURE_POSITIVE")
        conf = c.confidence_of(IntentType.PURE_POSITIVE)
        if blockers:
            return PolicyRuleHit(
                rule_id="R_POSITIVE_WITH_BLOCKER",
                description=f"Positive sentiment but co-occurring {sorted(b.value for b in blockers)}: human review.",
                routing=RoutingDecision.HUMAN_REVIEW,
                risk=RiskLevel.MEDIUM,
            )
        if rule.enabled and conf >= rule.min_confidence:
            return PolicyRuleHit(
                rule_id="R_PURE_POSITIVE_AUTO",
                description=f"Pure positive @ {conf:.2f} ≥ {rule.min_confidence:.2f}: auto-reply eligible.",
                routing=RoutingDecision.AUTO_REPLY,
                risk=RiskLevel.LOW,
            )
        return PolicyRuleHit(
            rule_id="R_PURE_POSITIVE_REVIEW",
            description="Pure positive but automation not enabled / confidence below threshold: human review.",
            routing=RoutingDecision.HUMAN_REVIEW,
            risk=RiskLevel.LOW,
        )

    def _r_low_confidence(self, pin: PolicyInput) -> PolicyRuleHit | None:
        c = pin.classification
        if not c.intents:
            return PolicyRuleHit(
                rule_id="R_NO_INTENT",
                description="Classifier returned no intent: human review.",
                routing=RoutingDecision.HUMAN_REVIEW,
                risk=RiskLevel.MEDIUM,
            )
        if c.top_confidence < self.config.global_confidence_floor:
            return PolicyRuleHit(
                rule_id="R_LOW_CONFIDENCE",
                description=(
                    f"Top intent confidence {c.top_confidence:.2f} below floor "
                    f"{self.config.global_confidence_floor:.2f}: human review."
                ),
                routing=RoutingDecision.HUMAN_REVIEW,
                risk=RiskLevel.MEDIUM,
            )
        return None

    def _r_unrelated(self, pin: PolicyInput) -> PolicyRuleHit | None:
        c = pin.classification
        if c.types() == {IntentType.UNRELATED}:
            return PolicyRuleHit(
                rule_id="R_UNRELATED",
                description="Unrelated message: human review, no automated campaign action.",
                routing=RoutingDecision.HUMAN_REVIEW,
                risk=RiskLevel.LOW,
                forbid_actions=sorted(A.CAMPAIGN_ACTIONS),
            )
        return None

    def _r_referral_already_committed(self, pin: PolicyInput) -> PolicyRuleHit | None:
        c = pin.classification
        committed = pin.state_before.referral_already_committed or c.has(
            IntentType.REFERRAL_INTENT
        ) or c.has(IntentType.INLINE_REFERRAL)
        if committed:
            return PolicyRuleHit(
                rule_id="R_NO_REPEAT_REFERRAL_ASK",
                description="Customer has expressed/again expressed referral intent: do not ask them to refer again.",
                forbid_actions=[A.REFERRAL_ASK],
            )
        return None

    def _r_no_repeat_offers(self, pin: PolicyInput) -> PolicyRuleHit | None:
        kinds = pin.state_before.offer_kinds_presented()
        if not kinds:
            return None
        return PolicyRuleHit(
            rule_id="R_NO_REPEAT_OFFERS",
            description=f"Offers already presented ({sorted(kinds)}): do not repeat them.",
            forbid_actions=[A.repeat_offer(k) for k in sorted(kinds)],
        )

    def _r_deferral(self, pin: PolicyInput) -> PolicyRuleHit | None:
        if pin.classification.has(IntentType.DEFERRAL):
            return PolicyRuleHit(
                rule_id="R_DEFERRAL",
                description="Customer asked to be contacted later: no ask now, human review.",
                routing=RoutingDecision.HUMAN_REVIEW,
                risk=RiskLevel.LOW,
                forbid_actions=sorted(A.CAMPAIGN_ACTIONS),
            )
        return None

    @property
    def _rules(self):
        return [
            self.__class__._r_optout,
            self.__class__._r_already_opted_out,
            self.__class__._r_negative,
            self.__class__._r_wrong_number,
            self.__class__._r_mixed_or_open_issue,
            self.__class__._r_state_has_open_issue,
            self.__class__._r_repeated_issues,
            self.__class__._r_identity_correction,
            self.__class__._r_deferral,
            self.__class__._r_question,
            self.__class__._r_pure_positive,
            self.__class__._r_unrelated,
            self.__class__._r_low_confidence,
            self.__class__._r_referral_already_committed,
            self.__class__._r_no_repeat_offers,
        ]


# ---------------------------------------------------------------------------
# Guards for messages that do not come from the inbound pipeline: text a human
# approved in the review queue, and campaign steps. Both are derived from the
# conversation state alone, deterministically.
# ---------------------------------------------------------------------------


def state_forbidden_actions(state: ConversationState) -> list[str]:
    """Actions the conversation state forbids for ANY outbound message."""
    forbidden: list[str] = []

    def add(*acts: str) -> None:
        for a in acts:
            if a not in forbidden:
                forbidden.append(a)

    if state.opted_out:
        add(A.ANY_OUTBOUND, *sorted(A.CAMPAIGN_ACTIONS), A.FOLLOWUP_OUTREACH)
    if state.suppression.active or state.has_unresolved_issue:
        add(*sorted(A.CAMPAIGN_ACTIONS))
    if state.referral_already_committed:
        add(A.REFERRAL_ASK)
    for kind in sorted(state.offer_kinds_presented()):
        add(A.repeat_offer(kind))
    return forbidden


def outbound_guard(state: ConversationState, *, risk: RiskLevel = RiskLevel.MEDIUM) -> PolicyOutcome:
    """The policy a human-approved or campaign message is validated against.

    Approval never bypasses the guardrails: an open issue still forbids a referral
    ask, an opt-out still forbids everything, a committed referral still forbids
    asking again.
    """
    forbidden = state_forbidden_actions(state)
    return PolicyOutcome(
        routing=RoutingDecision.HUMAN_REVIEW,
        risk=risk,
        forbidden_actions=forbidden,
        allowed_actions=[a for a in (A.ACKNOWLEDGEMENT,) if a not in forbidden],
        summary="[R_OUTBOUND_GUARD] state-derived guard for a non-pipeline outbound message",
        rule_hits=[
            PolicyRuleHit(
                rule_id="R_OUTBOUND_GUARD",
                description="Forbidden actions derived from suppression, open issues, referral status, and offers already presented.",
                forbid_actions=forbidden,
            )
        ],
    )


@dataclass
class CampaignCheck:
    allowed: bool
    kind: str               # the campaign kind that will actually be sent
    requested_kind: str
    reason: str
    actions: list[str]


CAMPAIGN_KINDS = ("review_request", "referral_ask", "referral_link")


def campaign_check(
    kind: str,
    state: ConversationState,
    client: ClientContext,
    config: AutomationConfig,
    *,
    has_open_review: bool = False,
) -> CampaignCheck:
    """Decide deterministically whether a campaign step may be sent now.

    The order mirrors the brief: suppression is global, open issues block every
    campaign action, nothing already presented is presented again, and a customer
    who already committed to refer gets their personal link instead of the pitch.
    """
    requested = kind
    if kind not in CAMPAIGN_KINDS:
        return CampaignCheck(False, kind, requested, f"unknown campaign kind {kind!r}", [])

    def blocked(reason: str) -> CampaignCheck:
        return CampaignCheck(False, kind, requested, reason, [])

    if not config.campaigns.enabled:
        return blocked("campaigns are disabled in the automation config")
    if state.opted_out:
        return blocked("contact opted out")
    if state.suppression.active:
        reason = state.suppression.reason.value if state.suppression.reason else "ACTIVE"
        return blocked(f"contact is suppressed ({reason})")
    if state.has_unresolved_issue:
        return blocked("an issue is still unresolved")
    if has_open_review:
        return blocked("a human review is open for this conversation")

    presented = state.offer_kinds_presented()

    if kind == "referral_ask" and state.referral_already_committed:
        # John: the customer already said they'll recommend us. Don't pitch the
        # terms again — send their own referral link instead (once).
        kind = "referral_link"

    if kind == "review_request":
        if not client.review_link:
            return blocked("client has no review link configured")
        if "review_link" in presented:
            return blocked("review link was already sent")
        return CampaignCheck(True, kind, requested, "review request allowed", [A.REVIEW_REQUEST])

    if not client.referral_program:
        return blocked("client has no referral program configured")

    if kind == "referral_ask":
        if "referral_terms" in presented:
            return blocked("referral terms were already presented")
        return CampaignCheck(
            True, kind, requested, "referral ask allowed", [A.REFERRAL_ASK, A.REFERRAL_LINK_SEND]
        )

    # referral_link
    if "referral_link" in presented:
        return blocked("referral link was already sent")
    reason = (
        "customer already committed to refer: sending their personal link instead of the pitch"
        if requested == "referral_ask"
        else "referral link allowed"
    )
    return CampaignCheck(True, kind, requested, reason, [A.REFERRAL_LINK_SEND])
