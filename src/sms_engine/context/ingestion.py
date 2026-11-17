"""Ingestion pipeline: raw client sources -> structured ClientContext.

Deterministic and dumb on purpose. It parses a small set of source shapes:
  - website_text : free text; we pull hours / service-area lines heuristically
  - faq          : list of {question, answer, approved?}
  - config       : explicit structured fields (authoritative, overrides heuristics)
  - sample_replies : approved phrases to seed brand voice / approved_phrases

No model is involved; nothing is invented. Fields the sources don't provide stay
empty, and the validator treats "not in context" as "not allowed to claim".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sms_engine.domain.context import ClientContext, FAQItem, ReferralProgram


@dataclass
class RawClientSources:
    client_id: str
    name: str
    website_text: str = ""
    faq: list[dict] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    sample_replies: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


_HOURS_RE = re.compile(r"(hours?|open)\s*[:\-]?\s*(.+)", re.I)
_AREA_RE = re.compile(r"(serv(e|ing|ice area)|areas? served)\s*[:\-]?\s*(.+)", re.I)


class ContextIngestor:
    def ingest(self, raw: RawClientSources) -> ClientContext:
        cfg = raw.config or {}

        hours = cfg.get("hours", "")
        service_area = list(cfg.get("service_area", []))
        if not hours or not service_area:
            for line in raw.website_text.splitlines():
                if not hours:
                    m = _HOURS_RE.search(line)
                    if m:
                        hours = m.group(2).strip()
                if not service_area:
                    m = _AREA_RE.search(line)
                    if m:
                        service_area = [s.strip() for s in re.split(r",|;|/| and ", m.group(3)) if s.strip()]

        faq_items = [
            FAQItem(
                id=item.get("id") or f"faq_{i+1}",
                question=item["question"].strip(),
                answer=item["answer"].strip(),
                tags=[w.lower() for w in re.findall(r"[a-zA-Z]+", item["question"])],
                approved=bool(item.get("approved", True)),
            )
            for i, item in enumerate(raw.faq)
        ]

        rp = None
        if cfg.get("referral_program"):
            r = cfg["referral_program"]
            rp = ReferralProgram(
                description=r.get("description", ""),
                reward_text=r.get("reward_text", "a thank-you"),
                referral_link_template=r.get("referral_link_template", "{referral_base}/{contact_id}"),
                terms_summary=r.get("terms_summary", ""),
            )

        approved_phrases = list(cfg.get("approved_phrases", []))
        for rpl in raw.sample_replies:
            rpl = rpl.strip()
            if rpl and rpl not in approved_phrases:
                approved_phrases.append(rpl)

        return ClientContext(
            client_id=raw.client_id,
            name=raw.name,
            brand_voice=cfg.get(
                "brand_voice",
                "Warm, concise, professional. First person as the persona. No hype, no emojis.",
            ),
            persona_name=cfg.get("persona_name", "Agent"),
            services=list(cfg.get("services", [])),
            service_area=service_area,
            hours=hours,
            booking_policy=cfg.get("booking_policy", ""),
            referral_program=rp,
            review_link=cfg.get("review_link", ""),
            crew_names=list(cfg.get("crew_names", [])),
            faq=faq_items,
            escalation_rules=list(cfg.get("escalation_rules", [])),
            forbidden_claims=list(cfg.get("forbidden_claims", [])),
            approved_phrases=approved_phrases,
            allowed_url_domains=list(cfg.get("allowed_url_domains", [])),
            max_sms_chars=int(cfg.get("max_sms_chars", 320)),
            sources=list(raw.sources) or ["config"],
            synthetic=bool(cfg.get("synthetic", True)),
        )
