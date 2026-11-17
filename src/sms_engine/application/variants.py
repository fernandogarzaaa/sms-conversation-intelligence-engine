"""Outbound variant pool.

Real phrasing variety (no artificial typos). Two customers who might know each
other should not get identical wording, and one contact should not see the same
variant twice in a row. Selection is deterministic given (contact_id,
neighborhood, recent history) via a seeded RNG.
"""

from __future__ import annotations

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

    def select(self, kind: str, *, contact_id: str, neighborhood: str | None = None) -> VariantSelection:
        pool = VARIANTS.get(kind) or VARIANTS["acknowledgement"]
        n = len(pool)
        recent = set(self._history.get(contact_id, [])[-2:])

        r = self.rng.derived(f"variant:{kind}:{contact_id}:{neighborhood or '-'}:{len(self._history.get(contact_id, []))}")
        order = list(range(n))
        r.shuffle(order)

        nb_used = self._neighborhood.get(neighborhood or "", [])
        def cost(idx: int) -> tuple:
            vid = f"{kind}:{idx}"
            return (vid in recent, nb_used.count(idx))

        order.sort(key=cost)
        chosen = order[0]
        vid = f"{kind}:{chosen}"

        self._history.setdefault(contact_id, []).append(vid)
        if neighborhood:
            self._neighborhood.setdefault(neighborhood, []).append(chosen)
        return VariantSelection(variant_id=vid, template=pool[chosen])

    @staticmethod
    def render(template: str, ack: str) -> str:
        ack = ack.strip()
        ack_lower = (ack[:1].lower() + ack[1:]) if ack else ack
        out = template.replace("{ack_lower}", ack_lower).replace("{ack}", ack)
        return " ".join(out.split()).strip()
