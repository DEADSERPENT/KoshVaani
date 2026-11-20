from datetime import date

import pytest

from app.agents.response_guard import grounded
from app.voice import transcript_normalizer as tn

TODAY = date(2026, 11, 12)


@pytest.mark.parametrize("text,paise", [
    ("I sent four thousand two hundred and fifty rupees yesterday", 425000),
    ("it was ₹4,250", 425000),
    ("Rs 4250 was deducted", 425000),
    ("1500 rupees", 150000),
    ("one thousand five hundred rupees", 150000),
    ("two lakh rupees", 20_000_000),
    ("₹499.50", 49950),
])
def test_amounts(text, paise):
    assert tn.parse_amount_paise(text) == paise


@pytest.mark.parametrize("text", ["on the 11th of November", "I need help", "sent 500 or 5000 rupees", "my number is 9900000101", "UTR 123456789012"])
def test_no_or_ambiguous_amount(text):
    assert tn.parse_amount_paise(text) is None


@pytest.mark.parametrize("text,d", [
    ("yesterday", date(2026, 11, 11)),
    ("today", date(2026, 11, 12)),
    ("on the eleventh of November", date(2026, 11, 11)),
    ("on 11 november", date(2026, 11, 11)),
    ("on the 5th of December", date(2025, 12, 5)),  # future month rolls back a year
])
def test_dates(text, d):
    assert tn.parse_date(text, TODAY) == d


def test_yes_no():
    assert tn.is_affirmative("Yes, please raise the complaint")
    assert not tn.is_affirmative("yes... no wait, cancel")
    assert tn.is_negative("no thanks")
    assert not tn.is_affirmative("what happens next")


def test_choice_and_reference():
    assert tn.parse_choice("the second one", 2) == 1
    assert tn.parse_choice("the third one", 2) is None
    assert tn.extract_dispute_reference("my reference is KV 20261110 000001") == "KV-20261110-000001"


def test_guard_blocks_ungrounded_ids_and_secrets():
    assert not grounded("Your reference is KV-20261112-000009", "{}", "")
    assert grounded("Your reference is KV-20261112-000009", "KV-20261112-000009", "")
    assert not grounded("Please tell me your UPI PIN", "", "")
    assert not grounded("Your refund has been initiated", "", "")
    assert not grounded("That was 9,999 rupees", "4,250 rupees", "")
