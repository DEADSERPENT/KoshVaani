import pytest

from app.core.crypto import FieldCipher, generate_key


def test_round_trip_and_ciphertext_is_randomised():
    c = FieldCipher(generate_key())
    a, b = c.encrypt("Aarav Mehta", "customer.name"), c.encrypt("Aarav Mehta", "customer.name")
    assert a != b and "Aarav" not in a
    assert c.decrypt(a, "customer.name") == "Aarav Mehta"


def test_value_is_bound_to_its_field_and_key():
    c = FieldCipher(generate_key())
    token = c.encrypt("+919900000101", "customer.phone")
    with pytest.raises(ValueError):
        c.decrypt(token, "customer.name")  # cannot be moved to another field
    with pytest.raises(ValueError):
        FieldCipher(generate_key()).decrypt(token, "customer.phone")  # wrong key


def test_tampering_is_detected():
    c = FieldCipher(generate_key())
    t = c.encrypt("secret", "f")
    with pytest.raises(ValueError):
        c.decrypt(t[:-2] + ("AA" if not t.endswith("AA") else "BB"), "f")


def test_blind_index_is_stable_keyed_and_field_scoped():
    c = FieldCipher(generate_key())
    assert c.blind_index("+919900000101", "p") == c.blind_index("+919900000101", "p")
    assert c.blind_index("+919900000101", "p") != c.blind_index("+919900000102", "p")
    assert c.blind_index("x", "p") != c.blind_index("x", "q")
    assert c.blind_index("x", "p") != FieldCipher(generate_key()).blind_index("x", "p")


def test_bad_key_length_rejected():
    with pytest.raises(ValueError):
        FieldCipher("c2hvcnQ")
