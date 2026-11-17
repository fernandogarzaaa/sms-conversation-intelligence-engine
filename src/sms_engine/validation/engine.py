"""The deterministic validator battery."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sms_engine.domain.context import ClientContext, RetrievedContext
from sms_engine.domain.enums import ValidationCode
from sms_engine.domain.policy import PolicyOutcome
from sms_engine.domain.state import ConversationState
from sms_engine.domain.validation import ValidationFinding, ValidationResult
from sms_engine.policy import actions as A

_MONEY = re.compile(
    r"(\$\s?\d[\d,]*(?:\.\d+)?|\b\d+\s?(?:dollars|bucks)\b|\b\d{1,3}\s?%|\bpercent\b|"
    r"\b(discount|coupon|promo code|voucher|rebate|cash ?back|% off|free (month|service|job))\b)",
    re.I,
)
_URL = re.compile(r"https?://([^\s/]+)[^\s]*", re.I)
_COMMITMENT = re.compile(
    r"\b(i('|\s?wi)ll (send|dispatch|have|get) (someone|a crew|a truck|somebody)|"
    r"we'?ll be there (today|tomorrow|at \d)|i('|\s?ha)ve (scheduled|booked|rebooked)|"
    r"i can (refund|comp|waive|discount|credit) (you|your)|"
    r"consider it (done|handled)|i'?ll take care of it personally)\b",
    re.I,
)
_UNSUPPORTED = re.compile(
    r"\b(guarantee[ds]?|best in town|cheapest|we beat any price|100% satisfaction|"
    r"fully bonded|we never|always on time|no one does it better)\b",
    re.I,
)
_FILLER = re.compile(
    r"(thanks for reaching out[.! ]*we appreciate your feedback|"
    r"we value your business|your satisfaction is our priority)",
    re.I,
)


@dataclass
class ValidationContext:
    client: ClientContext
    policy: PolicyOutcome
    state: ConversationState
    retrieved: RetrievedContext
    actions: list[str]          # actions the draft claims to perform
    intents: list[str]


class ResponseValidator:
    def validate(self, text: str, ctx: ValidationContext) -> ValidationResult:
        findings: list[ValidationFinding] = []

        def reject(code: ValidationCode, msg: str, span: str | None = None) -> None:
            findings.append(ValidationFinding(code=code, severity="reject", message=msg, span=span))

        def warn(code: ValidationCode, msg: str, span: str | None = None) -> None:
            findings.append(ValidationFinding(code=code, severity="warn", message=msg, span=span))

        stripped = (text or "").strip()

        # --- opt-out / full suppression: nothing may be sent -------------------
        if self.ctx_forbids_all_outbound(ctx):
            if stripped:
                reject(
                    ValidationCode.OPT_OUT_NOT_HONORED,
                    "Outbound is fully suppressed for this contact (opt-out / wrong number / "
                    "negative escalation); no message may be sent.",
                )
            return ValidationResult(passed=not stripped, findings=findings)

        # --- empty / length --------------------------------------------------
        if not stripped:
            reject(ValidationCode.EMPTY, "Generated response is empty.")
            return ValidationResult(passed=False, findings=findings)
        if len(stripped) > ctx.client.max_sms_chars:
            reject(
                ValidationCode.TOO_LONG,
                f"Response is {len(stripped)} chars, over the {ctx.client.max_sms_chars} limit.",
            )

        approved = " || ".join(ctx.client.approved_phrases).lower()

        # --- money terms ---------------------------------------------------------
        for m in _MONEY.finditer(stripped):
            span = m.group(0)
            if span.lower() in approved:
                continue
            reject(ValidationCode.MONEY_TERM, f"Response contains a money/discount term: '{span}'.", span)

        # --- URLs ----------------------------------------------------------------
        for m in _URL.finditer(stripped):
            domain = m.group(1).lower()
            if not any(domain == d or domain.endswith("." + d) for d in ctx.client.allowed_url_domains):
                reject(ValidationCode.DISALLOWED_URL, f"Response links to a non-allowlisted domain: '{domain}'.", domain)
            if m.group(0) in ctx.state.links_sent:
                reject(ValidationCode.REPEATS_PRESENTED_OFFER, f"Response resends a link already sent: '{m.group(0)}'.")

        # --- required links ----------------------------------------------------
        if A.REVIEW_REQUEST in ctx.actions and ctx.client.review_link and ctx.client.review_link not in stripped:
            reject(ValidationCode.MISSING_REQUIRED_LINK, "Review request without the review link.")
        if A.REFERRAL_LINK_SEND in ctx.actions:
            rp = ctx.client.referral_program
            if not rp or not _URL.search(stripped):
                reject(ValidationCode.MISSING_REQUIRED_LINK, "Referral-link action without a referral link in the text.")

        # --- forbidden phrases (client-configured) ----------------------------
        low = stripped.lower()
        for phrase in ctx.client.forbidden_claims:
            if phrase.lower() in low:
                reject(ValidationCode.FORBIDDEN_PHRASE, f"Response contains a client-forbidden phrase: '{phrase}'.", phrase)

        # --- unsupported claims ---------------------------------------------------
        for m in _UNSUPPORTED.finditer(stripped):
            if m.group(0).lower() in approved:
                continue
            reject(ValidationCode.UNSUPPORTED_CLAIM, f"Response makes an unsupported claim: '{m.group(0)}'.", m.group(0))

        # --- overreaching commitments -----------------------------------------
        for m in _COMMITMENT.finditer(stripped):
            reject(ValidationCode.OVERREACHING_COMMITMENT, f"Response over-commits to an action Agent can't guarantee: '{m.group(0)}'.", m.group(0))

        # --- policy-forbidden actions performed by the draft -----------------
        for act in ctx.actions:
            if ctx.policy.forbids(act):
                code = ValidationCode.REPEATS_PRESENTED_OFFER if act.startswith("repeat_offer:") else ValidationCode.FORBIDDEN_PHRASE
                if act == A.REFERRAL_ASK:
                    code = ValidationCode.REFERRAL_ASK_AFTER_COMMIT
                reject(code, f"Draft performs a policy-forbidden action: '{act}'.")

        # heuristic: a referral *ask* phrasing while referral_ask is forbidden
        if ctx.policy.forbids(A.REFERRAL_ASK) and re.search(
            r"\b(could you|would you|mind) (refer|recommend|passing|sharing)|"
            r"\brefer (a friend|someone|us)\b|\bknow anyone who\b|\bsend them our way\b",
            low,
        ):
            reject(ValidationCode.REFERRAL_ASK_AFTER_COMMIT, "Response asks for a referral, which policy forbids here.")

        # --- grounding for question answers ---------------------------------
        if A.ANSWER_QUESTION in ctx.actions and not ctx.retrieved.grounded:
            reject(ValidationCode.UNGROUNDED_ANSWER, "Response answers a question that is not grounded in an approved FAQ.")

        # --- brand voice (warn only) ---------------------------------------
        if stripped.count("!") > 1:
            warn(ValidationCode.BRAND_VOICE, "More than one exclamation mark.")
        if _FILLER.search(stripped):
            warn(ValidationCode.BRAND_VOICE, "Generic filler phrasing; response should reference the actual conversation.")
        if re.search(r"\b[A-Z]{4,}\b", stripped):
            warn(ValidationCode.BRAND_VOICE, "Contains SHOUTING (all-caps word).")

        passed = not any(f.severity == "reject" for f in findings)
        return ValidationResult(passed=passed, findings=findings)

    @staticmethod
    def ctx_forbids_all_outbound(ctx: ValidationContext) -> bool:
        return (
            ctx.policy.forbids(A.ANY_OUTBOUND)
            or ctx.state.opted_out
            or ctx.policy.routing.value in ("SUPPRESS", "ESCALATE")
        )
