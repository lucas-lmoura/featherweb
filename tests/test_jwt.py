"""JSON Web Tokens, and the attacks RFC 8725 exists to prevent.

The interesting tests here are the ones that must *fail*: ``alg: none``, an
algorithm the caller never allowed, a token verified with the wrong key, and
the algorithm-confusion attack where an RS256 token is presented for HS256
verification using the public key as the shared secret.
"""

from __future__ import annotations

import time
from base64 import urlsafe_b64encode
from typing import Any

import pytest

from featherweb._compat import json_dumps
from featherweb.auth.jwt import (
    ExpiredToken,
    InvalidToken,
    JWTAuth,
    JWTError,
    decode,
    encode,
)

SECRET = "a shared secret of respectable length"


def segment(value: Any) -> str:
    return urlsafe_b64encode(json_dumps(value)).rstrip(b"=").decode("ascii")


def forge(header: dict[str, Any], payload: dict[str, Any], signature: str = "AAAA") -> str:
    """A token nobody signed, to see what the decoder makes of it."""
    return f"{segment(header)}.{segment(payload)}.{signature}"


# -- round trips ------------------------------------------------------------


def test_a_token_round_trips() -> None:
    token = encode({"sub": "ada"}, SECRET)
    assert decode(token, SECRET, algorithms=["HS256"]) == {"sub": "ada"}


@pytest.mark.parametrize("algorithm", ["HS256", "HS384", "HS512"])
def test_every_hmac_algorithm_round_trips(algorithm: str) -> None:
    token = encode({"sub": "ada"}, SECRET, algorithm=algorithm)
    assert decode(token, SECRET, algorithms=[algorithm])["sub"] == "ada"


def test_a_token_has_three_parts() -> None:
    assert encode({"sub": "ada"}, SECRET).count(".") == 2


def test_the_header_names_the_algorithm() -> None:
    from base64 import urlsafe_b64decode

    from featherweb._compat import json_loads

    head = encode({"sub": "ada"}, SECRET, algorithm="HS384").split(".")[0]
    header = json_loads(urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    assert header["alg"] == "HS384"
    assert header["typ"] == "JWT"


def test_extra_headers_are_carried() -> None:
    from base64 import urlsafe_b64decode

    from featherweb._compat import json_loads

    head = encode({"sub": "a"}, SECRET, headers={"kid": "key-1"}).split(".")[0]
    assert json_loads(urlsafe_b64decode(head + "=" * (-len(head) % 4)))["kid"] == "key-1"


# -- the algorithm allow-list -----------------------------------------------


def test_alg_none_is_never_accepted() -> None:
    """RFC 8725 §3.2, the original JWT catastrophe."""
    token = forge({"alg": "none", "typ": "JWT"}, {"sub": "mallory"}, signature="x")
    with pytest.raises(JWTError):
        decode(token, SECRET, algorithms=["HS256"])


@pytest.mark.parametrize("asked", [["none"], ["None"], ["NONE"], ["HS256", "none"]])
def test_asking_for_none_is_refused_outright(asked: list[str]) -> None:
    """Even a caller who explicitly wants it does not get it."""
    with pytest.raises(JWTError, match="never accepted"):
        decode(encode({"sub": "a"}, SECRET), SECRET, algorithms=asked)


def test_an_algorithm_outside_the_allow_list_is_refused() -> None:
    token = encode({"sub": "ada"}, SECRET, algorithm="HS512")
    with pytest.raises(InvalidToken, match="not accepted here"):
        decode(token, SECRET, algorithms=["HS256"])


def test_the_token_does_not_get_to_choose_the_algorithm() -> None:
    """A token claiming HS512 is not verified as HS512 unless we allowed it."""
    forged = forge({"alg": "HS512"}, {"sub": "mallory"})
    with pytest.raises(InvalidToken, match="not accepted here"):
        decode(forged, SECRET, algorithms=["HS256"])


def test_an_empty_allow_list_is_refused() -> None:
    with pytest.raises(JWTError, match="at least one algorithm"):
        decode(encode({"sub": "a"}, SECRET), SECRET, algorithms=[])


def test_an_unknown_algorithm_is_refused() -> None:
    with pytest.raises(JWTError, match="unsupported"):
        decode(encode({"sub": "a"}, SECRET), SECRET, algorithms=["HS999"])


def test_a_header_without_an_algorithm_is_refused() -> None:
    token = forge({"typ": "JWT"}, {"sub": "mallory"})
    with pytest.raises(InvalidToken, match="no algorithm"):
        decode(token, SECRET, algorithms=["HS256"])


# -- signatures -------------------------------------------------------------


def test_the_wrong_key_does_not_verify() -> None:
    token = encode({"sub": "ada"}, SECRET)
    with pytest.raises(InvalidToken, match="signature"):
        decode(token, "some other secret", algorithms=["HS256"])


def test_a_tampered_payload_does_not_verify() -> None:
    header, _, signature = encode({"sub": "ada"}, SECRET).split(".")
    forged = f"{header}.{segment({'sub': 'root'})}.{signature}"
    with pytest.raises(InvalidToken, match="signature"):
        decode(forged, SECRET, algorithms=["HS256"])


def test_a_token_with_no_signature_does_not_verify() -> None:
    header, payload, _ = encode({"sub": "ada"}, SECRET).split(".")
    with pytest.raises(InvalidToken):
        decode(f"{header}.{payload}.", SECRET, algorithms=["HS256"])


@pytest.mark.parametrize("token", ["", "a", "a.b", "a.b.c.d", "....", "not a token at all"])
def test_a_malformed_token_is_refused(token: str) -> None:
    with pytest.raises(InvalidToken):
        decode(token, SECRET, algorithms=["HS256"])


def test_a_header_that_is_not_json_is_refused() -> None:
    with pytest.raises(InvalidToken, match="not valid JSON"):
        decode("bm90anNvbg.bm90anNvbg.AAAA", SECRET, algorithms=["HS256"])


def test_a_payload_that_is_not_an_object_is_refused() -> None:
    token = encode({"sub": "a"}, SECRET)
    header = token.split(".")[0]
    forged = f"{header}.{segment([1, 2, 3])}.AAAA"
    with pytest.raises(InvalidToken):
        decode(forged, SECRET, algorithms=["HS256"])


# -- the time claims --------------------------------------------------------


def test_an_expired_token_is_refused() -> None:
    token = encode({"sub": "ada", "exp": time.time() - 10}, SECRET)
    with pytest.raises(ExpiredToken, match="expired"):
        decode(token, SECRET, algorithms=["HS256"])


def test_leeway_forgives_a_small_clock_difference() -> None:
    token = encode({"sub": "ada", "exp": time.time() - 5}, SECRET)
    assert decode(token, SECRET, algorithms=["HS256"], leeway=30)["sub"] == "ada"


def test_a_token_that_is_not_valid_yet_is_refused() -> None:
    token = encode({"sub": "ada", "nbf": time.time() + 600}, SECRET)
    with pytest.raises(ExpiredToken, match="not valid yet"):
        decode(token, SECRET, algorithms=["HS256"])


def test_a_token_issued_in_the_future_is_refused() -> None:
    token = encode({"sub": "ada", "iat": time.time() + 600}, SECRET)
    with pytest.raises(ExpiredToken, match="issued in the future"):
        decode(token, SECRET, algorithms=["HS256"])


def test_expiry_can_be_switched_off_deliberately() -> None:
    token = encode({"sub": "ada", "exp": time.time() - 10}, SECRET)
    assert decode(token, SECRET, algorithms=["HS256"], verify_exp=False)["sub"] == "ada"


def test_a_token_without_an_expiry_is_accepted() -> None:
    assert decode(encode({"sub": "a"}, SECRET), SECRET, algorithms=["HS256"])["sub"] == "a"


@pytest.mark.parametrize("value", ["soon", True, None, [1]])
def test_a_time_claim_that_is_not_a_number_is_refused(value: Any) -> None:
    token = encode({"sub": "ada", "exp": value}, SECRET)
    if value is None:  # an absent claim, which is allowed
        assert decode(token, SECRET, algorithms=["HS256"])["sub"] == "ada"
        return
    with pytest.raises(InvalidToken, match="must be a number"):
        decode(token, SECRET, algorithms=["HS256"])


# -- issuer and audience ----------------------------------------------------


def test_the_issuer_has_to_match_when_asked_for() -> None:
    token = encode({"sub": "a", "iss": "them"}, SECRET)
    with pytest.raises(InvalidToken, match="issued by someone else"):
        decode(token, SECRET, algorithms=["HS256"], issuer="us")


def test_a_matching_issuer_passes() -> None:
    token = encode({"sub": "a", "iss": "us"}, SECRET)
    assert decode(token, SECRET, algorithms=["HS256"], issuer="us")["iss"] == "us"


def test_the_audience_has_to_match_when_asked_for() -> None:
    token = encode({"sub": "a", "aud": "other-service"}, SECRET)
    with pytest.raises(InvalidToken, match="different audience"):
        decode(token, SECRET, algorithms=["HS256"], audience="this-service")


def test_an_audience_list_matches_any_member() -> None:
    token = encode({"sub": "a", "aud": ["one", "this-service"]}, SECRET)
    assert decode(token, SECRET, algorithms=["HS256"], audience="this-service")["sub"] == "a"


def test_a_missing_audience_fails_when_one_is_required() -> None:
    with pytest.raises(InvalidToken, match="different audience"):
        decode(encode({"sub": "a"}, SECRET), SECRET, algorithms=["HS256"], audience="wanted")


# -- asymmetric algorithms --------------------------------------------------

cryptography = pytest.importorskip("cryptography", reason="the crypto extra is not installed")


def key_pair(kind: str) -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    key: Any
    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if kind == "rsa"
        else ec.generate_private_key(ec.SECP256R1())
    )
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public


@pytest.mark.parametrize(("kind", "algorithm"), [("rsa", "RS256"), ("ec", "ES256")])
def test_an_asymmetric_token_round_trips(kind: str, algorithm: str) -> None:
    private, public = key_pair(kind)
    token = encode({"sub": "ada"}, private, algorithm=algorithm)
    assert decode(token, public, algorithms=[algorithm])["sub"] == "ada"


@pytest.mark.parametrize(("kind", "algorithm"), [("rsa", "RS256"), ("ec", "ES256")])
def test_the_algorithm_confusion_attack_is_refused(kind: str, algorithm: str) -> None:
    """The classic: present an RS256 token for HS256 verification, using the
    public key — which the attacker has — as the shared secret. It fails
    because the allow-list, not the token, decides how to verify."""
    private, public = key_pair(kind)
    token = encode({"sub": "mallory"}, private, algorithm=algorithm)
    with pytest.raises(InvalidToken, match="not accepted here"):
        decode(token, public, algorithms=["HS256"])


@pytest.mark.parametrize(("kind", "algorithm"), [("rsa", "RS256"), ("ec", "ES256")])
def test_another_key_does_not_verify(kind: str, algorithm: str) -> None:
    private, _ = key_pair(kind)
    _, other_public = key_pair(kind)
    token = encode({"sub": "ada"}, private, algorithm=algorithm)
    with pytest.raises(InvalidToken, match="signature"):
        decode(token, other_public, algorithms=[algorithm])


def test_a_verification_key_that_is_not_a_key_is_refused() -> None:
    private, _ = key_pair("rsa")
    token = encode({"sub": "ada"}, private, algorithm="RS256")
    with pytest.raises(JWTError, match="PEM public key"):
        decode(token, "not a key at all", algorithms=["RS256"])


# -- the strategy -----------------------------------------------------------


class FakeRequest:
    """Only what JWTAuth reads."""

    def __init__(self, authorization: str | None = None) -> None:
        self.headers = {"authorization": authorization} if authorization else {}


def test_issued_tokens_identify_their_subject() -> None:
    auth = JWTAuth(SECRET)
    identity = auth.identify(FakeRequest(f"Bearer {auth.issue('ada', roles=['admin'])}"))
    assert identity is not None
    assert (identity.id, identity.roles) == ("ada", frozenset({"admin"}))


def test_no_header_is_simply_anonymous() -> None:
    assert JWTAuth(SECRET).identify(FakeRequest()) is None


@pytest.mark.parametrize("header", ["", "Basic abc", "Bearer", "Bearer    "])
def test_a_header_without_a_bearer_token_is_anonymous(header: str) -> None:
    assert JWTAuth(SECRET).identify(FakeRequest(header or None)) is None


def test_a_bad_token_is_401_rather_than_anonymous() -> None:
    """The caller tried to authenticate and failed; saying so beats ignoring it."""
    from featherweb.auth.guards import Unauthenticated

    with pytest.raises(Unauthenticated):
        JWTAuth(SECRET).identify(FakeRequest("Bearer not.a.token"))


def test_an_issued_token_carries_an_expiry() -> None:
    auth = JWTAuth(SECRET)
    claims = decode(auth.issue("ada", expires_in=60), SECRET, algorithms=["HS256"])
    assert claims["exp"] > time.time()


def test_a_token_can_be_issued_without_an_expiry() -> None:
    auth = JWTAuth(SECRET)
    assert "exp" not in decode(auth.issue("a", expires_in=None), SECRET, algorithms=["HS256"])


def test_the_issuer_and_audience_are_stamped_on_issued_tokens() -> None:
    auth = JWTAuth(SECRET, issuer="us", audience="them")
    identity = auth.identify(FakeRequest(f"Bearer {auth.issue('ada')}"))
    assert identity is not None
    assert identity.claims["iss"] == "us"


def test_extra_claims_survive_the_round_trip() -> None:
    auth = JWTAuth(SECRET)
    identity = auth.identify(FakeRequest(f"Bearer {auth.issue('ada', team='core')}"))
    assert identity is not None
    assert identity.claims["team"] == "core"


def test_a_token_without_a_subject_is_refused() -> None:
    from featherweb.auth.guards import Unauthenticated

    auth = JWTAuth(SECRET)
    token = encode({"team": "core"}, SECRET)
    with pytest.raises(Unauthenticated, match="no subject"):
        auth.identify(FakeRequest(f"Bearer {token}"))


def test_the_allow_list_is_checked_at_startup() -> None:
    with pytest.raises(JWTError, match="never accepted"):
        JWTAuth(SECRET, algorithms=["none"])


def test_a_single_role_string_becomes_a_set() -> None:
    auth = JWTAuth(SECRET)
    token = encode({"sub": "a", "roles": "admin"}, SECRET)
    identity = auth.identify(FakeRequest(f"Bearer {token}"))
    assert identity is not None
    assert identity.roles == frozenset({"admin"})
