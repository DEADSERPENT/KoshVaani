"""Deterministic spoken-number / date / yes-no parsing. The LLM never supplies final amounts or IDs."""
import re
from datetime import date, timedelta

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_SCALES = {"hundred": 100, "thousand": 1000, "lakh": 100_000, "lakhs": 100_000}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
             "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
             "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
             "nineteenth": 19, "twentieth": 20, "thirtieth": 30}
_MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}

_DIGIT_AMOUNT = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d{1,2}))?\s*(?:rupees?|rs\.?|inr|/-)?", re.I)


def _words_to_int(tokens: list[str]) -> int | None:
    total, current, seen = 0, 0, False
    for t in tokens:
        if t in _UNITS:
            current += _UNITS[t]; seen = True
        elif t in _TENS:
            current += _TENS[t]; seen = True
        elif t == "hundred":
            current = max(current, 1) * 100; seen = True
        elif t in _SCALES:
            total += max(current, 1) * _SCALES[t]; current = 0; seen = True
        elif t == "and":
            continue
        else:
            return None
    return total + current if seen else None


def parse_amount_paise(text: str) -> int | None:
    """Returns integer paise, or None if no amount (or an ambiguous one) is present."""
    t = text.lower().replace("-", " ")
    found: set[int] = set()
    for m in _DIGIT_AMOUNT.finditer(t):
        raw, frac = m.group(1), m.group(2)
        has_marker = bool(m.group(0).strip().startswith(("₹", "rs", "inr")) or re.search(r"rupee|rs|inr|/-", m.group(0)))
        digits = raw.replace(",", "")
        if len(digits) > 8:
            continue  # phone / UTR / account numbers are never amounts (max here is 99,99,999 rupees)
        if not has_marker and len(digits) < 3:
            continue  # bare small numbers are dates/ordinals, not amounts
        if re.match(r"^\s*(?:st|nd|rd|th)\b", t[m.end(1):]):
            continue
        found.add(int(raw.replace(",", "")) * 100 + (int((frac or "0").ljust(2, "0")) if frac else 0))
    words = re.findall(r"[a-z]+", t)
    i = 0
    while i < len(words):
        j = i
        while j < len(words) and (words[j] in _UNITS or words[j] in _TENS or words[j] in _SCALES or words[j] == "and"):
            j += 1
        run = [w for w in words[i:j]]
        while run and run[-1] == "and":
            run.pop()
        if run and run[0] != "and":
            val = _words_to_int(run)
            if val and (val >= 100 or "rupee" in t or "rupees" in t):
                found.add(val * 100)
        i = max(j, i + 1)
    return found.pop() if len(found) == 1 else None


def parse_date(text: str, today: date) -> date | None:
    t = text.lower()
    if "day before yesterday" in t:
        return today - timedelta(days=2)
    if "yesterday" in t:
        return today - timedelta(days=1)
    if re.search(r"\btoday\b|\bthis morning\b", t):
        return today
    month = next((m for m in _MONTHS if re.search(rf"\b{m}\b", t)), None)
    if not month:
        return None
    day = None
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", t):
        day = int(m.group(1)); break
    if day is None:
        for w in re.findall(r"[a-z]+", t):
            if w in _ORDINALS:
                day = _ORDINALS[w]; break
    if day is None:
        return None
    try:
        d = date(today.year, _MONTHS[month], day)
    except ValueError:
        return None
    return d if d <= today else date(today.year - 1, _MONTHS[month], day)


_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|correct|confirm(?:ed)?|go ahead|please do|do it|register it|raise it|that is right|that's right)\b", re.I)
_NO = re.compile(r"\b(no|nope|don'?t|do not|not now|cancel|stop|never ?mind|wrong)\b", re.I)


def is_affirmative(text: str) -> bool:
    return bool(_YES.search(text)) and not _NO.search(text)


def is_negative(text: str) -> bool:
    return bool(_NO.search(text))


def parse_choice(text: str, n: int) -> int | None:
    """Pick 1..n from 'the first one', 'second', 'number 2'."""
    t = text.lower()
    for w, v in _ORDINALS.items():
        if re.search(rf"\b{w}\b", t) and 1 <= v <= n:
            return v - 1
    m = re.search(r"\b(?:number|option|no\.?)\s*(\d)\b", t)
    if m and 1 <= int(m.group(1)) <= n:
        return int(m.group(1)) - 1
    return None


def extract_dispute_reference(text: str) -> str | None:
    m = re.search(r"\bKV[-\s]?(\d{8})[-\s]?(\d{6})\b", text, re.I)
    return f"KV-{m.group(1)}-{m.group(2)}" if m else None


def wants_human(text: str) -> bool:
    return bool(re.search(r"\b(human|real person|agent|representative|operator|supervisor)\b", text, re.I)) and \
        not re.search(r"\b(not|don'?t)\b.{0,12}\b(human|agent)\b", text, re.I)


_CARD_UNBLOCK = re.compile(r"\bun-?block\b|\b(replacement|new|re-?issue|re-?issued)\b[^.?!]*\bcard\b|\bcard\b[^.?!]*\b(replace|replaced|re-?issue)\b", re.I)
_CARD_STATUS = re.compile(r"\bcard status\b|\b(is|are)\b[^.?!]*\bcards?\b[^.?!]*\b(active|blocked|working|activated)\b", re.I)
_CARD_BLOCK = re.compile(
    r"\b(block|freeze|hotlist|deactivate|disable|stop)\b[^.?!]*\bcards?\b"
    r"|\bcards?\b[^.?!]*\b(lost|stolen|misplaced|missing|block|freeze|compromised|misused|skimmed)\b"
    r"|\b(lost|stolen|misplaced|lose|losing)\b[^.?!]*\bcards?\b", re.I)
_BALANCE = re.compile(r"\bbalance\b|how much (?:money|do i have|is (?:there|left))|available (?:funds|amount|money)", re.I)
_STATEMENT = re.compile(r"\b(?:mini[- ]?statement|statement|passbook)\b|\b(?:last|recent|latest)\b(?: \w+)? transactions?\b", re.I)
_LOAN = re.compile(r"\b(loan|emi|e\.m\.i|instal?lments?)\b", re.I)
_FRAUD = re.compile(r"\b(fraud|unauthori[sz]ed|misus\w*|someone (?:is )?us\w+|did ?n'?t make|not me|skimm\w*|compromised)\b", re.I)


def classify_block_reason(text: str) -> str:
    """Why the customer wants a card blocked; fraud outranks stolen/lost so the human follow-up is flagged."""
    if _FRAUD.search(text):
        return "SUSPECTED_FRAUD"
    if re.search(r"\bstol[ae]n?\b|\bsnatch\w*|\brobbed\b", text, re.I):
        return "STOLEN"
    if re.search(r"\b(lost|lose|losing|misplaced|missing|cannot find|can'?t find)\b", text, re.I):
        return "LOST"
    return "OTHER"


def pick_card(text: str, card_ids: list[str]) -> str | None:
    """Choose one of the listed cards from 'the debit one', 'credit card', 'the first one'. None if unclear."""
    t = text.lower()
    wants_debit, wants_credit = bool(re.search(r"\bdebit\b|\batm\b", t)), bool(re.search(r"\bcredit\b", t))
    if wants_debit != wants_credit:
        suffix = "-D" if wants_debit else "-C"
        hits = [c for c in card_ids if c.endswith(suffix)]
        return hits[0] if len(hits) == 1 else None
    idx = parse_choice(text, len(card_ids))
    return card_ids[idx] if idx is not None else None


# Requests the policy reserves for a human agent: moving money, changing limits, closing accounts, waivers/prepayment.
_RESTRICTED = re.compile(
    r"\b(?:transfer|send|move|wire)\b[^.?!]{0,40}\b(?:money|rupees|rs\.?|\d)"
    r"|\b(?:increase|raise|change|reduce|lower)\b[^.?!]{0,25}\blimit\b"
    r"|\bclose\b[^.?!]{0,25}\baccount\b|\bwaive|\bwaiver\b|\bprepay|\bforeclos", re.I)
_UPI_ISSUE = re.compile(r"(not (?:yet )?(?:been )?(?:received|credited|arrived)|(?:hasn'?t|haven'?t|didn'?t|did not) (?:been )?(?:receive|received|arrive|arrived|get|got)|nothing (?:has )?arrived|never (?:arrived|received|got)|deducted|debited|money left|failed|stuck|pending|complain|dispute|raise)")


def classify_intent(text: str) -> str:
    t = text.lower()
    if extract_dispute_reference(text) or re.search(r"\b(complaint|dispute|ticket|case)\b.*\b(status|happen|update)\b|\bmy complaint\b", t):
        return "DISPUTE_TRACKING"
    if wants_human(text):
        return "HUMAN_HANDOFF"
    if _CARD_UNBLOCK.search(text):
        return "CARD_UNBLOCK"
    if _CARD_STATUS.search(text):
        return "CARD_STATUS"
    if _CARD_BLOCK.search(text):
        return "CARD_BLOCK"
    if _RESTRICTED.search(text) and not _UPI_ISSUE.search(t):  # a failed transfer is a payment problem, not a request
        return "RESTRICTED_REQUEST"
    if _LOAN.search(text):
        return "LOAN_ENQUIRY"
    if _BALANCE.search(text):
        return "BALANCE_ENQUIRY"
    if _STATEMENT.search(text):
        return "MINI_STATEMENT"
    if _UPI_ISSUE.search(t):
        return "UPI_PAYMENT_ISSUE"
    if re.search(r"\b(check|status|went through|go through|success)", t) or "upi" in t or "payment" in t:
        return "TRANSACTION_STATUS"
    return "UNKNOWN"
