"""Outbound variant pool.

Real phrasing variety (no artificial typos). Two customers who might know each
other should not get identical wording, and one contact should not see the same
variant twice in a row. Selection is deterministic given (contact_id,
neighborhood, recent history) via a seeded RNG.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sms_engine.infrastructure.rng import Rng

# kind -> list of interchangeable templates. `{ack}` is filled from the generated
# contextual sentence; templates only vary the *framing*, never invent facts.
VARIANTS: dict[str, list[str]] = {
    "acknowledgement": [
        "{ack}",
        "{ack} Thanks for taking a second to let us know.",
        "{ack} Appreciate the note.",
        "Hey — {ack_lower}",
        "{ack} Good to hear.",
    ],
    "answer_question": [
        "{ack}",
        "{ack} Happy to help.",
        "Good question — {ack_lower}",
        "{ack} Let me know if that covers it.",
        "{ack} Hope that helps.",
    ],
    "campaign": [
        "{ack}",
    ],
    "confirm_identity": [
        "{ack}",
        "{ack} Sorry about the mix-up.",
        "{ack} Thanks for catching that.",
        "{ack} Got it updated.",
        "{ack} That's fixed now.",
    ],
}


@dataclass
class VariantSelection:
    variant_id: str
    template: str


@dataclass
class VariantPool:
    rng: Rng
    # contact_id -> ordered list of variant_ids already used
    _history: dict[str, list[str]] = field(default_factory=dict)
    # neighborhood -> multiset of variant indices used
    _neighborhood: dict[str, list[int]] = field(default_factory=dict)

    def select(
        self,
        kind: str,
        *,
        contact_id: str,
        neighborhood: str | None = None,
        history: list[str] | None = None,
        neighborhood_used: list[str] | None = None,
        ack_text: str | None = None,
    ) -> VariantSelection:
        """Pick a variant. `history` / `neighborhood_used` (variant ids) let the
        caller supply persisted usage, so selection survives restarts and is the
        same across workers; when omitted, in-process memory is used."""
        pool = VARIANTS.get(kind) or VARIANTS["acknowledgement"]
        if kind not in VARIANTS:
            kind = "acknowledgement"
        n = len(pool)
        contact_hist = list(history) if history is not None else self._history.get(contact_id, [])
        recent = set(contact_hist[-2:])

        r = self.rng.derived(f"variant:{kind}:{contact_id}:{neighborhood or '-'}:{len(contact_hist)}")
        order = list(range(n))
        r.shuffle(order)

        if neighborhood_used is not None:
            nb_used = [int(v.split(":", 1)[1]) for v in neighborhood_used
                       if v.startswith(f"{kind}:") and v.split(":", 1)[1].isdigit()]
        else:
            nb_used = self._neighborhood.get(neighborhood or "", [])

        def cost(idx: int) -> tuple:
            vid = f"{kind}:{idx}"
            return (_redundant(pool[idx], ack_text), vid in recent, nb_used.count(idx))

        order.sort(key=cost)
        chosen = order[0]
        vid = f"{kind}:{chosen}"

        self._history.setdefault(contact_id, []).append(vid)
        if neighborhood:
            self._neighborhood.setdefault(neighborhood, []).append(chosen)
        return VariantSelection(variant_id=vid, template=pool[chosen])

    @staticmethod
    def render(template: str, ack: str) -> str:  # noqa: D401
        ack = ack.strip()
        ack_lower = (ack[:1].lower() + ack[1:]) if ack else ack
        out = template.replace("{ack_lower}", ack_lower).replace("{ack}", ack)
        return " ".join(out.split()).strip()


_GRATITUDE = re.compile(r"\b(thank|thanks|appreciate)", re.I)
_GREETING = re.compile(r"^\s*(hey|hi|hello)\b", re.I)


def _redundant(template: str, ack: str | None) -> bool:
    """True if the framing repeats what the generated sentence already says
    (a second "thanks", or "Hey —" in front of a sentence that greets by name)."""
    if not ack:
        return False
    framing = template.replace("{ack_lower}", "").replace("{ack}", "")
    if _GRATITUDE.search(framing) and _GRATITUDE.search(ack):
        return True
    if _GREETING.search(framing) and (_GREETING.search(ack) or re.match(r"^\s*[A-Z][a-z]+,", ack)):
        return True
    return False
