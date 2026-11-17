"""Context retrieval: pick the client-context subset relevant to one message.

Deterministic keyword overlap scoring over FAQ items. `grounded` is True only
when an *approved* FAQ item clears the score threshold — that flag is what lets
the policy engine's "answer a question automatically" path actually fire.
"""

from __future__ import annotations

import re

from sms_engine.domain.classification import Classification
from sms_engine.domain.context import ClientContext, RetrievedContext
from sms_engine.domain.entities import Entities
from sms_engine.domain.state import ConversationState

_STOP = {
    "the", "a", "an", "do", "does", "did", "you", "your", "guys", "i", "we", "is",
    "are", "can", "could", "would", "will", "to", "of", "for", "and", "or", "my",
    "me", "it", "that", "this", "with", "have", "any", "on", "in", "at", "be",
}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if w not in _STOP and len(w) > 2}


class ContextRetriever:
    def __init__(self, score_threshold: float = 0.34) -> None:
        self.score_threshold = score_threshold

    def retrieve(
        self,
        *,
        message_text: str,
        classification: Classification,
        entities: Entities,
        state: ConversationState,
        client: ClientContext,
    ) -> RetrievedContext:
        q_tokens = _tokens(message_text)
        hits = []
        for item in client.faq:
            it_tokens = _tokens(item.question) | set(item.tags)
            if not it_tokens:
                continue
            overlap = len(q_tokens & it_tokens)
            score = overlap / max(1, len(it_tokens))
            if overlap >= 2 or score >= self.score_threshold:
                hits.append((score, item))
        hits.sort(key=lambda x: -x[0])
        faq_hits = [h[1] for h in hits[:3]]

        grounded = any(h.approved for h in faq_hits) and classification.has_question_like()

        facts_used = [f"{f.key}={f.value}" for f in state.facts[-5:]]

        snippets = []
        if client.hours:
            snippets.append(f"hours: {client.hours}")
        if client.service_area:
            snippets.append("service_area: " + ", ".join(client.service_area))
        if client.referral_program:
            snippets.append("referral_reward: " + client.referral_program.reward_text)

        retrieved_ids = [h.id for h in faq_hits]
        return RetrievedContext(
            client_id=client.client_id,
            faq_hits=faq_hits,
            facts_used=facts_used,
            snippets=snippets,
            retrieved_ids=retrieved_ids,
            grounded=bool(grounded),
        )
