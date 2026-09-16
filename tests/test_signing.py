"""Signed values: what verifies, what does not, and what expires.

The whole value of a signed cookie is that a client holding it cannot change
it, so most of this file is about changed values being refused.
"""

from __future__ import annotations

import time

import pytest

from featherweb.auth.signing import BadSignature, SignatureExpired, Signer

SECRET = "a secret long enough to look like one"


def test_a_signed_value_comes_back() -> None:
    signer = Signer(SECRET)
    assert signer.unsign(signer.sign(b"hello")) == b"hello"


def test_text_is_accepted_and_returned_as_bytes() -> None:
    signer = Signer(SECRET)
    assert signer.unsign(signer.sign("hello")) == b"hello"


def test_an_empty_value_still_signs() -> None:
    signer = Signer(SECRET)
    assert signer.unsign(signer.sign(b"")) == b""


def test_the_signed_form_is_url_safe() -> None:
    """It goes in a cookie, so it cannot contain anything needing escaping."""
    signed = Signer(SECRET).sign(b"\xff\xfe binary and spaces \x00")
    assert all(character.isalnum() or character in "-_." for character in signed)


def test_a_different_secret_does_not_verify() -> None:
    signed = Signer(SECRET).sign(b"hello")
    with pytest.raises(BadSignature):
        Signer("a different secret entirely").unsign(signed)


@pytest.mark.parametrize("position", [0, 5, 20, 41])
def test_a_tampered_signature_is_refused(position: int) -> None:
    signer = Signer(SECRET)
    payload, stamp, signature = signer.sign(b"hello").split(".")
    flipped = "b" if signature[position] != "b" else "c"
    broken = signature[:position] + flipped + signature[position + 1 :]
    with pytest.raises(BadSignature):
        signer.unsign(f"{payload}.{stamp}.{broken}")


def test_a_signature_of_the_wrong_length_is_refused() -> None:
    signer = Signer(SECRET)
    payload, stamp, signature = signer.sign(b"hello").split(".")
    for broken in (signature[:-4], signature + "AAAA", ""):
        with pytest.raises(BadSignature):
            signer.unsign(f"{payload}.{stamp}.{broken}")


def test_the_last_base64_character_carries_no_extra_bits() -> None:
    """Unpadded base64 is malleable in its final character.

    A 32-byte digest is 43 base64 characters, and the last one only carries two
    significant bits, so several spellings decode to the same signature. That is
    not a forgery — the bytes compared are identical either way — but it means a
    test cannot assume every changed character changes the signature.
    """
    from base64 import urlsafe_b64decode

    signature = Signer(SECRET).sign(b"hello").split(".")[2]
    variants = {
        urlsafe_b64decode(signature[:-1] + character + "=" * (-len(signature) % 4))
        for character in "ABCD"
    }
    assert len(variants) < 4


def test_a_tampered_payload_is_refused() -> None:
    """The point of the exercise: the value cannot be edited in transit."""
    signer = Signer(SECRET)
    _, stamp, signature = signer.sign(b"user=ada").split(".")
    from base64 import urlsafe_b64encode

    forged = urlsafe_b64encode(b"user=root").rstrip(b"=").decode()
    with pytest.raises(BadSignature):
        signer.unsign(f"{forged}.{stamp}.{signature}")


@pytest.mark.parametrize("value", ["", "nodots", "one.dot", "...", "a.b.!!!"])
def test_something_that_is_not_a_signed_value_is_refused(value: str) -> None:
    with pytest.raises(BadSignature):
        Signer(SECRET).unsign(value)


# -- purpose separation -----------------------------------------------------


def test_a_value_signed_for_one_purpose_does_not_verify_for_another() -> None:
    """Same secret, different purpose: the derived keys differ."""
    signed = Signer(SECRET, purpose="session").sign(b"hello")
    with pytest.raises(BadSignature):
        Signer(SECRET, purpose="password-reset").unsign(signed)


def test_the_same_purpose_verifies() -> None:
    signed = Signer(SECRET, purpose="session").sign(b"hello")
    assert Signer(SECRET, purpose="session").unsign(signed) == b"hello"


# -- key rotation -----------------------------------------------------------


def test_an_old_key_still_verifies_after_rotation() -> None:
    old = Signer("the old secret")
    rotated = Signer(["the new secret", "the old secret"])
    assert rotated.unsign(old.sign(b"hello")) == b"hello"


def test_new_values_are_signed_with_the_first_key() -> None:
    rotated = Signer(["the new secret", "the old secret"])
    assert Signer("the new secret").unsign(rotated.sign(b"hello")) == b"hello"


def test_a_key_dropped_from_the_list_stops_verifying() -> None:
    signed = Signer("the old secret").sign(b"hello")
    with pytest.raises(BadSignature):
        Signer(["the new secret"]).unsign(signed)


def test_a_signer_needs_a_secret() -> None:
    with pytest.raises(ValueError, match="at least one secret"):
        Signer([])


def test_an_empty_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        Signer(b"")


# -- age --------------------------------------------------------------------


def test_a_fresh_value_is_within_any_age() -> None:
    signer = Signer(SECRET)
    assert signer.unsign(signer.sign(b"hello"), max_age=60) == b"hello"


def test_an_old_value_is_expired() -> None:
    signer = Signer(SECRET)
    signed = signer.sign(b"hello", timestamp=int(time.time()) - 500)
    with pytest.raises(SignatureExpired) as info:
        signer.unsign(signed, max_age=100)
    assert info.value.max_age == 100


def test_an_expired_signature_is_still_a_bad_signature() -> None:
    """So a caller can catch one exception and mean 'do not trust this'."""
    assert issubclass(SignatureExpired, BadSignature)


def test_without_a_max_age_an_old_value_still_verifies() -> None:
    signer = Signer(SECRET)
    signed = signer.sign(b"hello", timestamp=int(time.time()) - 10_000)
    assert signer.unsign(signed) == b"hello"


def test_a_value_dated_in_the_future_is_refused() -> None:
    signer = Signer(SECRET)
    signed = signer.sign(b"hello", timestamp=int(time.time()) + 10_000)
    with pytest.raises(BadSignature, match="future"):
        signer.unsign(signed)


def test_an_absurdly_old_value_is_refused_even_without_a_max_age() -> None:
    signer = Signer(SECRET)
    signed = signer.sign(b"hello", timestamp=0)
    with pytest.raises(SignatureExpired):
        signer.unsign(signed)


def test_a_tampered_timestamp_breaks_the_signature() -> None:
    """The timestamp is signed too, so it cannot be moved forward."""
    signer = Signer(SECRET)
    payload, _, signature = signer.sign(b"hello", timestamp=1).split(".")
    from base64 import urlsafe_b64encode

    forged = urlsafe_b64encode(str(int(time.time())).encode()).rstrip(b"=").decode()
    with pytest.raises(BadSignature):
        signer.unsign(f"{payload}.{forged}.{signature}")


def test_the_repr_does_not_leak_the_secret() -> None:
    assert SECRET not in repr(Signer(SECRET))
