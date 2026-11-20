import re

from app.core.security import redact

_FORBIDDEN = re.compile(r"\b(upi pin|cvv|password|card number|one[- ]time password|your otp|tell me the otp)\b", re.I)
_REFUND = re.compile(r"\brefund (?:has been|is being|was) (?:initiated|processed|started|issued)\b", re.I)
# Actions the backend policy reserves for humans or forbids: the model may never promise or offer them itself.
_NOT_ALLOWED = re.compile(
    r"\b(?:i(?:'ll| will| can| have)|let me|would you like me to)\s+(?:\w+\s+){0,2}"
    r"(?:unblock|re-?activate|replace|waive|close your|increase your|raise your|refund|reverse|transfer (?:the )?money)\b", re.I)
_REF = re.compile(r"\bKV-\d{8}-\d{6}\b")
_TXN = re.compile(r"\bTXN-[A-Z]+-\d+\b")
_AMOUNT = re.compile(r"(\d[\d,]*)\s*rupees", re.I)
_CARD_END = re.compile(r"\bending(?: in| with)?\s+(\d{4})\b", re.I)

SAFE_FALLBACK = "I can't confirm that right now. I can transfer you to a human agent if you like."


def grounded(reply: str, tool_outputs: str, user_text: str) -> bool:
    """True only if every id/amount in the reply appears in verified tool output (or the customer's own words)."""
    if _FORBIDDEN.search(reply) or _REFUND.search(reply) or _NOT_ALLOWED.search(reply):
        return False
    if redact(reply) != reply:  # reply contains a phone number, email, long account/UTR number or spoken secret
        return False
    for ident in _REF.findall(reply) + _TXN.findall(reply):
        if ident not in tool_outputs:
            return False
    for tail in _CARD_END.findall(reply):  # card / account last-four must come from a tool, never from the model
        if tail not in tool_outputs:
            return False
    for amt in _AMOUNT.findall(reply):
        if amt not in tool_outputs and amt not in user_text:
            return False
    return True
