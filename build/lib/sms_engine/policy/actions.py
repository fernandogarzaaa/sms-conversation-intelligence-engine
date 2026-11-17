"""Vocabulary of downstream actions the policy engine can allow or forbid.

Keeping these as named constants means validators and the response planner check
the *same* strings the policy engine emits.
"""

ANY_OUTBOUND = "any_outbound"          # full suppression: send nothing at all
REFERRAL_ASK = "referral_ask"          # asking the customer to refer someone
REVIEW_REQUEST = "review_request"      # asking for a public review
PROMOTIONAL_MESSAGE = "promotional_message"
REFERRAL_LINK_SEND = "referral_link_send"
DISCOUNT_OFFER = "discount_offer"
ACKNOWLEDGEMENT = "acknowledgement"    # a plain contextual reply, no ask
ANSWER_QUESTION = "answer_question"
CONFIRM_IDENTITY = "confirm_identity"
FOLLOWUP_OUTREACH = "followup_outreach"  # a human-approved check-in on a volunteered need

CAMPAIGN_ACTIONS = {
    REFERRAL_ASK,
    REVIEW_REQUEST,
    PROMOTIONAL_MESSAGE,
    REFERRAL_LINK_SEND,
    DISCOUNT_OFFER,
}


def repeat_offer(kind: str) -> str:
    return f"repeat_offer:{kind}"


# Offer kinds recorded on the conversation when a campaign action is sent, so the
# same thing is never presented twice (R_NO_REPEAT_OFFERS).
OFFER_KIND_FOR_ACTION = {
    REVIEW_REQUEST: "review_link",
    REFERRAL_ASK: "referral_terms",
    REFERRAL_LINK_SEND: "referral_link",
}
