"""Domain layer: pure data structures. No IO, no framework, no decisions.

Everything here is a dataclass / pydantic model plus enums. This layer is
imported by every other layer and imports nothing from the project.
"""

from sms_engine.domain.classification import Classification, IntentPrediction
from sms_engine.domain.context import (
    ClientContext,
    FAQItem,
    ReferralProgram,
    RetrievedContext,
)
from sms_engine.domain.entities import Entities
from sms_engine.domain.enums import (
    CampaignStatus,
    EventType,
    IntentType,
    ReferralStatus,
    ReviewAction,
    RiskLevel,
    RoutingDecision,
    SuppressionReason,
    ValidationCode,
)
from sms_engine.domain.events import Event
from sms_engine.domain.messages import Direction, Message
from sms_engine.domain.planning import PlannedResponse
from sms_engine.domain.policy import PolicyOutcome, PolicyRuleHit
from sms_engine.domain.state import (
    ConversationState,
    Fact,
    FutureIntent,
    Identity,
    IntentRecord,
    Offer,
    OpenIssue,
    Suppression,
)
from sms_engine.domain.trace import DecisionTrace, TraceStep
from sms_engine.domain.validation import ValidationFinding, ValidationResult

__all__ = [
    "EventType",
    "IntentType",
    "RiskLevel",
    "RoutingDecision",
    "ReviewAction",
    "SuppressionReason",
    "ValidationCode",
    "CampaignStatus",
    "ReferralStatus",
    "Direction",
    "Message",
    "ConversationState",
    "Fact",
    "FutureIntent",
    "Identity",
    "IntentRecord",
    "Offer",
    "OpenIssue",
    "Suppression",
    "Classification",
    "IntentPrediction",
    "Entities",
    "Event",
    "PolicyOutcome",
    "PolicyRuleHit",
    "ClientContext",
    "FAQItem",
    "ReferralProgram",
    "RetrievedContext",
    "ValidationFinding",
    "ValidationResult",
    "PlannedResponse",
    "DecisionTrace",
    "TraceStep",
]
