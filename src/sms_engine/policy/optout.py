"""Deterministic opt-out detection — a backstop that does not depend on any model.

Honouring an opt-out is a legal obligation (TCPA / CTIA carrier rules), so the
system never relies on the classifier alone. The pipeline runs this check on
every inbound message; when it fires, the policy engine's R_STOP_OPTOUT rule
fires too, even if the model missed it or the provider is down.

Deliberately narrow: the standard carrier keywords when they are the whole
message, plus explicit "stop texting me"-type phrases. A bare "stop" inside a
sentence ("they didn't stop working all day") is not an opt-out.
"""

from __future__ import annotations

import re

# CTIA standard opt-out keywords, matched only as the entire message
_KEYWORD_ONLY = re.compile(
    r"^\s*(stop|stopall|stop all|unsubscribe|cancel|end|quit|opt[\s-]?out|revoke|remove)\s*[.!]*\s*$",
    re.I,
)
_PHRASES = re.compile(
    r"\b(stop (texting|messaging|contacting|sending|txting)( me| us)?|"
    r"(do ?n'?t|do not|never) (text|message|contact|sms) (me|us)( again)?|"
    r"no more (texts?|messages?|msgs?)|"
    r"unsubscribe( me)?|opt (me )?out|remove me( from)?|take me off( your| the)? (list|texts?)|"
    r"lose my number|leave me alone)\b",
    re.I,
)


def detect_opt_out(text: str | None) -> str | None:
    """Return the matched opt-out phrase, or None."""
    t = text or ""
    m = _KEYWORD_ONLY.match(t)
    if m:
        return m.group(1)
    m = _PHRASES.search(t)
    return m.group(0) if m else None
