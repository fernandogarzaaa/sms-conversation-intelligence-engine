"""Deterministic, rule-based provider. No network, no randomness.

This is honest about what it is: a transparent keyword/regex model that stands in
for an LLM so the *system* (state, policy, validation, review, eval) can be
built, tested, and demoed end-to-end without an external dependency. A real
provider implements the same `LLMProvider` interface and the rest of the system
does not change.
"""

from __future__ import annotations

import re

from sms_engine.ai.provider import GenerateSpec, LLMProvider, ProviderResponse

# --- lexicons -----------------------------------------------------------------

POSITIVE = re.compile(
    r"\b(great|fantastic|awesome|amazing|excellent|perfect|wonderful|"
    r"thanks?|thank you|happy|pleased|loved?|love it|good job|well done|"
    r"professional|on time|smooth|impressed|impressive|appreciate|no complaints|"
    r"5 stars|five stars|top notch|stellar|nice|friendly|polite|courteous|"
    r"helpful|respectful|pleasant|painless|careful|kind|hard[- ]?working|"
    r"pretty good|decent|solid|with care|took care of|all set)\b",
    re.I,
)
# statements that a prior issue is now resolved -> read as positive, not an issue
RESOLUTION = re.compile(
    r"\b(fixed( now)?|all good now|sorted( out)?( now)?|resolved|taken care of( now)?|"
    r"came back and (took|got|finished|cleared)|all set now|handled now)\b",
    re.I,
)
# greeting / smalltalk that is the whole message
GREETING = re.compile(
    r"^\s*(happy (friday|monday|holidays|new year)|good (morning|evening|afternoon)|"
    r"hi|hey|hello|yo|sup|howdy)\W*$",
    re.I,
)
# negated positives ("not great", "not bad") -> neutral, strip before positive test
NEGATED_POS = re.compile(
    r"\bnot (that )?(great|good|bad|amazing|happy|impressed|pleased|wonderful|perfect)\b",
    re.I,
)
# positive idioms that contain a negation word (must not read as an issue)
POSITIVE_IDIOM = re.compile(
    r"(could ?n'?t (have asked for|be|have been|have been happier|ask for) "
    r"(a )?(better|nicer|happier|more)|"
    r"no complaints|nothing (bad|negative) to say|can'?t complain|"
    r"couldn t (have asked|be|have been))",
    re.I,
)
STRONG_NEGATIVE = re.compile(
    r"\b(terrible|awful|horrible|worst|furious|angry|unacceptable|disgusting|"
    r"never again|rude|disrespectful|damaged|scam|ripoff|rip off|overcharged|"
    r"threat(en|ening)?|lawyer|sue you|report you|disappointed|disappointing|"
    r"waste of money|nightmare|left trash|trash everywhere|careless)\b",
    re.I,
)
CONTRAST = re.compile(
    r"\b(but|however|although|though|except|aside from|other than|"
    r"one (small )?thing|only (issue|problem|complaint))\b",
    re.I,
)
ISSUE_MARKERS = re.compile(
    r"\b(could ?n'?t|can ?not|cannot|did ?n'?t|were ?n'?t|was ?n'?t|is ?n'?t|"
    r"unable to|couldn t|wasn t|didn t|isn t|"
    r"still (there|here|left|full|down there|in the|on the|waiting)|"
    r"left (a |an |the |some |my |our |behind|it )|left in the|"
    r"not (done|finished|removed|cleared|picked up|hauled|taken)|"
    r"(job|work) (is ?n'?t|not) (done|finished)|"
    r"prevented|blocked|never showed|no ?show|missed the|forgot|skipped|"
    r"cracked|broke|broken|shattered|scratch(ed)?|scuff(ed)?|dent(ed)?|"
    r"chipped|torn|stained?|spill(ed)?|damaged|"
    r"waiting on|come back for|haven'?t (come|been back)|debris|"
    r"still needs|needs to come back)\b",
    re.I,
)
HAZARD = re.compile(r"\b(wasp|hornet|bee|nest|snake|dog|flood(ed|ing)?|ice|locked)\b", re.I)
OPTOUT = re.compile(
    r"(\bstop\b|\bstopall\b|\bunsubscribe\b|\bopt[\s-]?out\b|\bremove me\b|"
    r"\bquit\b|\bcancel\b(?!\s+my\s+job)|leave me alone|lose my number|"
    r"do ?n'?t (text|contact|message|call) me|no more (texts?|messages?|msgs?)|"
    r"take me off|stop messaging)",
    re.I,
)
WRONG_NUMBER = re.compile(
    r"(wrong number|you have the wrong(?! name| person)|who is this|who'?s this|"
    r"i think you (have|got) the wrong|do ?n'?t know (who|what) (you|this)|"
    r"never (used|hired|called|contacted) you|did ?n'?t (hire|use|order|book)|"
    r"you'?ve got the wrong(?! name| person))",
    re.I,
)
IDENTITY = re.compile(
    r"(this is actually|it'?s actually|actually,? this is|my name is|"
    r"i'?m not [A-Z]\w+|wrong name|it'?s \w+,? not \w+|"
    r"you'?(ve| have) got the wrong name|you keep calling me \w+|"
    r"call me (?!back\b|later\b|soon\b|now\b|tomorrow\b|when\b)[A-Z]\w+|"
    r"this is (her|his) (husband|wife|son|daughter|partner)|"
    r"wrong person, this is)",
    re.I,
)
NAME_AFTER = re.compile(
    r"(?i:this is actually|it'?s actually|actually,? this is|my name is|call me|"
    r"you keep calling me|wrong person,? this is(?: the)?|this is)\s+([A-Z][a-z]+)",
)
NAME_NOT_NAME = re.compile(r"\b(?:i'?m|it'?s|this is)\s+([A-Z][a-z]+),?\s+not\s+([A-Z][a-z]+)", re.I)
# authoritative "the right name is X"
RIGHT_NAME_PAT = re.compile(
    r"(?i:my name is|this is actually|it'?s actually|actually,? this is|"
    r"call me|name's)\s+([A-Z][a-z]+)"
)
# "you're using the wrong name X"
WRONG_NAME_PAT = re.compile(
    r"(?i:you keep calling me|you'?ve been calling me|stop calling me|"
    r"quit calling me)\s+([A-Z][a-z]+)"
)
QUESTION_LEAD = re.compile(
    r"^\s*(do|does|did|can|could|would|will|is|are|was|were|how|what|when|where|why|who|any)\b(?!['’]|n['’]?t\b)",
    re.I,
)
# leads strong enough to mean "question" even without a trailing "?"
STRONG_Q_LEAD = re.compile(
    r"^\s*(do|does|did|how|what|when|where|why|who|can|could)\b(?!['’]|n['’]?t\b)",
    re.I,
)
REFERRAL = re.compile(
    r"\b(recommend|refer(red|ring)?|referral|"
    r"(tell(ing)?|told) (my|our|the|people|everyone|folks|friends|two|three|a few|some|couple)|"
    r"spread the word|pass(ed|ing)? (along|on) your (name|info|number)|"
    r"give (your|out your|her|him|them) (name|number|info)|"
    r"let (people|everyone) know|shout you out|would recommend|"
    r"gave (her|him|them) your (number|info|name))\b",
    re.I,
)
INLINE_REFERRAL = re.compile(
    r"(my (friend|neighbou?r|coworker|colleague|sister|brother|cousin)\s+[A-Z][a-z]+\s+(needs?|is looking|wants?|could use)|"
    r"here'?s (a|another) (number|contact)|"
    r"(you should |go )?call my (friend|coworker|colleague|neighbou?r|brother|sister|cousin)\s+[A-Z][a-z]+|"
    r"gave (her|him|them) your (number|info))",
    re.I,
)
REPEAT_BUSINESS = re.compile(
    r"\b(use (you|your service) again|call you (again|back)|hire you again|"
    r"book (you )?again|next time|next move|more service|second load|"
    r"need (you|your help) again|be (a )?repeat customer|come back to you|"
    r"again in the future|sav(e|ing) your number|keep your number)\b",
    re.I,
)
FUTURE = re.compile(
    r"\b(next (spring|summer|fall|autumn|winter|month|year|week)|"
    r"in (a|\d+|a couple|a few|several)\s+(few )?(days?|weeks?|months?)|"
    r"couple (of )?months?|"
    r"(moving|move|relocating) (next|in|this coming)|planning to move|"
    r"buying a (house|place|home)|"
    r"will (probably )?need|might need|gonna need|going to need|down the road|"
    r"later this year|early next year|this (summer|spring|fall|winter))\b",
    re.I,
)
RELATION = re.compile(
    r"\b(daughter|son|mother|mom|father|dad|parents?|sister|brother|friend|"
    r"neighbou?r|coworker|colleague|aunt|uncle|cousin|grandmother|grandfather|"
    r"in[- ]?law|roommate)\b",
    re.I,
)
TIMEFRAME = re.compile(
    r"\b(spring|summer|fall|autumn|winter|january|february|march|april|may|june|"
    r"july|august|september|october|november|december|next month|next year|"
    r"this year|next week|end of (the )?(month|year))\b",
    re.I,
)
SERVICE_WORD = re.compile(r"\b(mov(e|ing)|relocat(e|ing|ion)|junk|haul(ing)?|pack(ing)?|declutter)\b", re.I)
DEFERRAL = re.compile(
    r"\b(not (right )?now|later|busy (right now|at the moment)|check back|"
    r"reach out (later|next \w+)|contact me (later|next \w+)|another time|"
    r"in a bit|give me (a|some) time)\b",
    re.I,
)
CREW_TARGET = re.compile(r"\b(crew|team|guys|movers?|gentlemen|workers?|staff|driver)\b", re.I)
LOCATION = re.compile(
    r"\b(?:near|by|around|in|at|behind|next to)\s+the\s+([a-z][a-z ]{2,20}?)\b"
    r"|(grill area|back yard|backyard|garage|basement|attic|patio|shed|driveway|kitchen)",
    re.I,
)
PROMPT_INJECTION = re.compile(
    r"(ignore (all |any |the )?(previous|prior|above) (instructions?|prompts?)|"
    r"disregard (your|the) (rules?|instructions?)|you are now|new instructions?:|"
    r"system prompt|act as|pretend to be|jailbreak|reveal your prompt|"
    r"print your (system )?prompt|\bDAN\b)",
    re.I,
)


def _clip(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "..."


def _clean_issue(s: str) -> str:
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"^\s*(wait|so|and|but|oh|also|hey|um|well|actually)\b[\s,.-]*", "", s, flags=re.I)
    s = re.sub(r"^\s*(there (was|were)|that|the fact that)\s+", "", s, flags=re.I).strip()
    s = s.strip(" .,-")
    if not re.match(r"^(the|a|an|that|those|this|some|my|our)\b", s, re.I):
        s = ("the issue with " if len(s.split()) > 4 else "the ") + s
    return s


class MockProvider(LLMProvider):
    name = "mock"

    # -- classification ------------------------------------------------------

    def classify_intents(self, text: str, state_summary: dict) -> ProviderResponse:
        t = text or ""
        intents: list[dict] = []

        def add(kind: str, conf: float, why: str) -> None:
            intents.append({"type": kind, "confidence": round(conf, 2), "rationale": why})

        t_np = NEGATED_POS.sub(" ", t)  # drop "not great" etc. before the positive test
        pos_idiom = bool(POSITIVE_IDIOM.search(t))
        resolved = bool(RESOLUTION.search(t))
        greeting_only = bool(GREETING.search(t))
        pos_hits = [m.group(0).lower() for m in POSITIVE.finditer(t_np)]
        # "happy to" / "glad to" is an offer framing, not praise of the job
        praise_hits = [p for p in pos_hits if not re.search(rf"\b{re.escape(p)}\s+to\b", t_np, re.I)]
        has_pos = (bool(praise_hits) or pos_idiom or resolved) and not greeting_only
        has_offer_pos = bool(pos_hits)  # weaker: enough to co-occur with REFERRAL etc.
        has_neg = bool(STRONG_NEGATIVE.search(t))
        has_contrast = bool(CONTRAST.search(t))
        has_issue = bool(ISSUE_MARKERS.search(t)) or bool(HAZARD.search(t) and SERVICE_WORD.search(t))
        # an explicit "but ... <hazard>" reads as an issue even without an action verb
        if has_contrast and HAZARD.search(t):
            has_issue = True
        # a purely positive idiom ("couldn't have asked for better") is not an issue
        # unless there is ALSO an explicit contrast clause
        if pos_idiom and not has_contrast:
            has_issue = bool(ISSUE_MARKERS.search(re.sub(POSITIVE_IDIOM, "", t)))
        # a stated resolution clears the issue reading
        if resolved and has_pos and not has_contrast:
            has_issue = False

        injected = bool(PROMPT_INJECTION.search(t))
        hard_signal = False  # a signal that should suppress the weak QUESTION fallback

        if greeting_only:
            add("UNRELATED", 0.6, "greeting / smalltalk only")
            hard_signal = True

        if OPTOUT.search(t):
            add("STOP_OPTOUT", 0.98, "matched opt-out phrase")
            hard_signal = True
        if WRONG_NUMBER.search(t):
            add("WRONG_NUMBER", 0.9, "matched wrong-number phrase")
            hard_signal = True
        if (IDENTITY.search(t) or NAME_NOT_NAME.search(t)) and not WRONG_NUMBER.search(t):
            add("IDENTITY_CORRECTION", 0.95, "customer stated a name correction")
            hard_signal = True
        ends_q = t.rstrip().endswith("?")
        q_lead = bool(QUESTION_LEAD.search(t))
        strong_q = bool(STRONG_Q_LEAD.search(t))
        is_question = (not injected and not OPTOUT.search(t)) and (
            (ends_q and (q_lead or "?" == t.strip()[-1:] and len(t) < 200))
            or strong_q
        )
        # a direct question ending in "?" is a request, not a report of an issue —
        # unless it also carries past-tense/leftover problem words
        if is_question and ends_q and not re.search(
            r"\b(still|left|forgot|skipped|did ?n'?t|could ?n'?t|was ?n'?t|is ?n'?t|"
            r"cracked|broke|broken|scratch|damaged|scuff|dent|waiting)\b", t, re.I
        ):
            has_issue = False

        if has_neg and not has_pos:
            add("NEGATIVE", 0.92, "strong negative language, no positive markers")
            hard_signal = True
        if has_neg and has_pos and not has_contrast:
            add("MIXED_SENTIMENT", 0.8, "contradictory positive and negative statements")
            hard_signal = True
        if has_issue and not (has_neg and not has_pos):
            add("OPEN_ISSUE", 0.93, "unresolved-work markers present")
            hard_signal = True
        if has_pos and (has_contrast or has_issue) and not has_neg:
            add("MIXED_SENTIMENT", 0.96, "positive sentiment alongside a contrast/issue")
            hard_signal = True
        if has_pos and not has_contrast and not has_issue and not has_neg:
            add("PURE_POSITIVE", 0.96, "positive markers, no contrast/issue/negation")
            hard_signal = True
        if is_question:
            add("QUESTION", 0.9, "interrogative form")
        if REFERRAL.search(t) or INLINE_REFERRAL.search(t):
            add("REFERRAL_INTENT", 0.9, "explicit intent to recommend/refer")
            hard_signal = True
        if INLINE_REFERRAL.search(t):
            add("INLINE_REFERRAL", 0.82, "a concrete referral appears in the message")
            hard_signal = True
        if REPEAT_BUSINESS.search(t):
            add("REPEAT_BUSINESS_INTENT", 0.85, "intent to use the service again")
            hard_signal = True
        if FUTURE.search(t) and (RELATION.search(t) or SERVICE_WORD.search(t)) and not INLINE_REFERRAL.search(t):
            add("VOLUNTEERED_FUTURE_INTENT", 0.85, "future service need volunteered")
            hard_signal = True
        if DEFERRAL.search(t) and not has_pos and not has_issue and "VOLUNTEERED_FUTURE_INTENT" not in {i["type"] for i in intents}:
            add("DEFERRAL", 0.78, "asked to be contacted later")
            hard_signal = True

        if injected:
            add("UNRELATED", 0.7, "off-topic instruction / injection content present")

        if not intents:
            add("UNRELATED", 0.55, "no recognizable service intent")

        # de-dup keep highest confidence
        best: dict[str, dict] = {}
        for it in intents:
            k = it["type"]
            if k not in best or it["confidence"] > best[k]["confidence"]:
                best[k] = it
        # cross-intent cleanup
        if ("STOP_OPTOUT" in best or "WRONG_NUMBER" in best or "NEGATIVE" in best) and "QUESTION" in best:
            del best["QUESTION"]
        if "NEGATIVE" in best and "OPEN_ISSUE" in best:
            del best["OPEN_ISSUE"]  # a complaint already implies an issue
        if "WRONG_NUMBER" in best and "OPEN_ISSUE" in best:
            del best["OPEN_ISSUE"]
        if "DEFERRAL" in best and "QUESTION" in best:
            del best["QUESTION"]
        if "INLINE_REFERRAL" in best and "VOLUNTEERED_FUTURE_INTENT" in best:
            del best["VOLUNTEERED_FUTURE_INTENT"]
        if ("UNRELATED" in best and len(best) > 1
                and best["UNRELATED"]["confidence"] < 0.6 and not injected):
            del best["UNRELATED"]
        payload = {
            "intents": sorted(best.values(), key=lambda x: -x["confidence"]),
            "entities": {},
            "flags": {"possible_prompt_injection": injected},
        }
        raw = f"MOCK_CLASSIFY::{payload}"
        return ProviderResponse(payload=payload, raw=raw, model=self.name, latency_ms=3.0)

    # -- entity extraction -------------------------------------------------------

    def extract_entities(self, text: str, intents: list[str]) -> ProviderResponse:
        t = text or ""
        e: dict = {}

        if "IDENTITY_CORRECTION" in intents:
            m = NAME_NOT_NAME.search(t)
            if m:
                e["corrected_name"] = m.group(1).title()
                e["stated_wrong_name"] = m.group(2).title()
            else:
                wrong = WRONG_NAME_PAT.search(t)
                right = RIGHT_NAME_PAT.search(t)
                if right:
                    e["corrected_name"] = right.group(1).title()
                if wrong:
                    e["stated_wrong_name"] = wrong.group(1).title()
                if not right and not wrong:
                    m = NAME_AFTER.search(t)
                    if m:
                        e["corrected_name"] = m.group(1).title()

        if "OPEN_ISSUE" in intents or "MIXED_SENTIMENT" in intents:
            clause = None
            cm = CONTRAST.search(t)
            if cm:
                clause = t[cm.end():].strip(" .,-")
            if not clause:
                im = ISSUE_MARKERS.search(t)
                if im:
                    # start at the last sentence boundary before the marker
                    head = t[: im.start()]
                    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
                    start = cut + 2 if cut >= 0 else max(0, im.start() - 48)
                    clause = t[start:].strip(" .,-")
            if clause:
                clause = clause.replace("—", "-").replace("–", "-")
                clause = re.sub(r"^\s*(wait|so|and|but|oh|also|hey|um|well|actually)\b[\s,.-]*", "", clause, flags=re.I)
                e["issue"] = _clip(clause.strip(" .,-"), 140)
            loc = LOCATION.search(t)
            if loc:
                e["issue_location"] = (loc.group(1) or loc.group(2) or "").strip()

        pos_target = CREW_TARGET.search(t)
        if pos_target and POSITIVE.search(t):
            e["sentiment_positive_target"] = pos_target.group(0).lower()

        if "VOLUNTEERED_FUTURE_INTENT" in intents:
            r = RELATION.search(t)
            if r:
                e["future_person"] = r.group(0).lower()
            s = SERVICE_WORD.search(t)
            if s:
                e["future_service"] = s.group(0).lower()
            tf = TIMEFRAME.search(t)
            if tf:
                e["future_timeframe"] = tf.group(0).lower()

        if "REFERRAL_INTENT" in intents or "INLINE_REFERRAL" in intents:
            e["referral_mentioned"] = True
        if "REPEAT_BUSINESS_INTENT" in intents:
            e["repeat_business_mentioned"] = True

        mo = OPTOUT.search(t)
        if mo:
            e["opt_out_phrase"] = mo.group(0)
        mw = WRONG_NUMBER.search(t)
        if mw:
            e["wrong_number_phrase"] = mw.group(0)

        if "QUESTION" in intents:
            q = re.search(r"([^.?!]*\?)", t)
            e["question_text"] = (q.group(1).strip() if q else _clip(t, 140))

        e["urls"] = re.findall(r"https?://[^\s)]+", t)
        e["money_terms"] = re.findall(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d+\s?(?:dollars|percent|%)\b", t, re.I)

        raw = f"MOCK_EXTRACT::{e}"
        return ProviderResponse(payload=e, raw=raw, model=self.name, latency_ms=2.0)

    # -- response generation --------------------------------------------------

    def generate_reply(self, spec: GenerateSpec) -> ProviderResponse:
        name = spec.contact_name
        greet = f"{name}, " if name else ""
        actions: list[str] = []
        parts: list[str] = []

        intents = set(spec.intents)
        ent = spec.entities or {}
        rc = spec.retrieved_context or {}

        answered = False
        if "answer_question" in spec.allowed_actions and rc.get("grounded"):
            faq = (rc.get("faq_hits") or [{}])[0]
            ans = faq.get("answer", "").strip()
            if ans:
                parts.append(ans)
                actions.append("answer_question")
                answered = True

        if "confirm_identity" in spec.allowed_actions and (
            "IDENTITY_CORRECTION" in intents or ent.get("corrected_name")
        ):
            cn = ent.get("corrected_name")
            if cn:
                parts.append(f"Thanks {cn}, I've updated your name on our end.")
            else:
                parts.append("Thanks for the correction - I've updated your name on our end.")
            actions.append("confirm_identity")

        if "acknowledgement" in spec.allowed_actions and not answered:
            target = ent.get("sentiment_positive_target") or "crew"
            if target and not target.startswith(("the ", "your ", "our ")):
                target = "the " + target
            g = greet.capitalize() if greet else ""
            if "PURE_POSITIVE" in intents:
                parts.append(f"{g}glad {target} took care of everything." if g
                             else f"Glad {target} took care of everything.")
            elif "OPEN_ISSUE" in intents or "MIXED_SENTIMENT" in intents:
                issue = _clean_issue(ent.get("issue") or "the issue you mentioned")
                parts.append(
                    f"{g}thanks for the kind words about {target}. "
                    f"I've flagged {issue} for the team to sort out." if g
                    else f"Thanks for the kind words about {target}. "
                         f"I've flagged {issue} for the team to sort out."
                )
            else:
                parts.append(f"{g}thanks for the note - I've passed it along to the team." if g
                             else "Thanks for the note - I've passed it along to the team.")
            actions.append("acknowledgement")

        if "REFERRAL_INTENT" in intents and "referral_ask" not in spec.forbidden_actions:
            # customer already offered — thank, do NOT ask again
            parts.append("Really appreciate you spreading the word.")
        elif "REFERRAL_INTENT" in intents:
            parts.append("Really appreciate you spreading the word.")

        if "REPEAT_BUSINESS_INTENT" in intents:
            parts.append("We'd be glad to help again whenever you need it.")

        if "VOLUNTEERED_FUTURE_INTENT" in intents:
            who = ent.get("future_person")
            tf = ent.get("future_timeframe")
            tail = "Just reach out when the time comes"
            if who and tf:
                tail = f"Happy to help {who} out when {tf} comes around"
            elif who:
                tail = f"Happy to help {who} out when the time comes"
            parts.append(tail + ".")

        text = " ".join(p.strip() for p in parts if p.strip()).strip()
        text = re.sub(r"\s+", " ", text)
        if len(text) > spec.max_chars:
            text = text[: spec.max_chars - 1].rstrip() + "…"

        payload = {"text": text, "actions": actions or ["acknowledgement"]}
        raw = f"MOCK_GENERATE::{payload}"
        return ProviderResponse(payload=payload, raw=raw, model=self.name, latency_ms=6.0)
