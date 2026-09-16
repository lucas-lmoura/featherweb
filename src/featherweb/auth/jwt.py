"""JSON Web Tokens, following RFC 8725 (the JWT best-practice RFC).

The whole point of that RFC is that the obvious implementation is unsafe, so the
rules it exists for are the rules this module keeps:

* **The caller names the algorithms.** ``decode`` takes an allow-list and the
  token's own ``alg`` is only ever checked *against* it. A token never gets to
  say how it should be verified — that is the flaw behind the classic
  "RS256 token verified as HS256 with the public key as the secret" attack.
* **``alg: none`` is never accepted**, whatever the allow-list says.
* **Signature first.** Nothing in the payload — not ``exp``, not ``iss`` — is
  read until the signature has been verified, because until then the payload is
  just something a stranger sent.
* **Constant-time comparison** for the HMAC signatures, as everywhere else here.

HS256/384/512 need nothing but the standard library. RS256 and ES256 need the
optional ``cryptography`` extra, and say so plainly if it is missing.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

__all__ = [
    "ExpiredToken",
    "InvalidToken",
    "JWTAuth",
    "JWTError",
    "decode",
    "encode",
]

#: What we can verify with the standard library alone.
_HMAC_ALGORITHMS: Final[dict[str, str]] = {
    "HS256": "sha256",
    "HS384": "sha384",
    "HS512": "sha512",
}
#: What needs the ``cryptography`` extra.
_ASYMMETRIC_ALGORITHMS: Final = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})
SUPPORTED_ALGORITHMS: Final = frozenset(_HMAC_ALGORITHMS) | _ASYMMETRIC_ALGORITHMS


class JWTError(Exception):
    """Anything wrong with a token."""


class InvalidToken(JWTError):
    """The token is malformed, or its signature does not check out."""


class ExpiredToken(JWTError):
    """The signature was good but the token is outside its validity window."""


def encode(
    payload: Mapping[str, Any],
    key: str | bytes,
    *,
    algorithm: str = "HS256",
    headers: Mapping[str, Any] | None = None,
) -> str:
    """Sign ``payload`` into a compact JWT."""
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise JWTError(f"unsupported algorithm {algorithm!r}")
    header: dict[str, Any] = {**(headers or {}), "alg": algorithm, "typ": "JWT"}
    signing_input = _segment(header) + b"." + _segment(dict(payload))
    return (signing_input + b"." + _b64(_sign(signing_input, key, algorithm))).decode("ascii")


def decode(
    token: str | bytes,
    key: str | bytes,
    *,
    algorithms: Sequence[str],
    audience: str | None = None,
    issuer: str | None = None,
    leeway: float = 0.0,
    verify_exp: bool = True,
) -> dict[str, Any]:
    """Verify ``token`` and return its claims.

    ``algorithms`` is required and is the only thing that decides how the
    signature is checked.
    """
    allowed = _allow_list(algorithms)
    header_segment, payload_segment, signature_segment = _split(token)
    header = _read_json(header_segment, "header")
    algorithm = header.get("alg")
    if not isinstance(algorithm, str):
        raise InvalidToken("the token header has no algorithm")
    if algorithm not in allowed:
        # Not "unsupported": the point is that this caller did not ask for it.
        raise InvalidToken(f"algorithm {algorithm!r} is not accepted here")

    signing_input = header_segment + b"." + payload_segment
    try:
        signature = _unb64(signature_segment)
    except ValueError:
        raise InvalidToken("the signature is not valid base64") from None
    if not _verify(signing_input, signature, key, algorithm):
        raise InvalidToken("the signature does not match")

    claims = _read_json(payload_segment, "payload")
    _check_claims(claims, audience=audience, issuer=issuer, leeway=leeway, verify_exp=verify_exp)
    return claims


def _allow_list(algorithms: Sequence[str]) -> frozenset[str]:
    """The algorithms this call accepts, with the dangerous ones taken out."""
    if not algorithms:
        raise JWTError("decode() needs at least one algorithm to accept")
    allowed = {algorithm.upper() for algorithm in algorithms}
    if allowed & {"NONE", ""}:
        # RFC 8725 §3.2. Not negotiable, even if the caller asked for it.
        raise JWTError("the 'none' algorithm is never accepted")
    unknown = allowed - SUPPORTED_ALGORITHMS
    if unknown:
        raise JWTError(f"unsupported algorithms: {', '.join(sorted(unknown))}")
    return frozenset(allowed)


def _split(token: str | bytes) -> tuple[bytes, bytes, bytes]:
    raw = token.encode("ascii", "replace") if isinstance(token, str) else bytes(token)
    parts = raw.split(b".")
    if len(parts) != 3:
        raise InvalidToken("a JWT has three dot-separated parts")
    if not all(parts):
        raise InvalidToken("a JWT cannot have an empty part")
    return parts[0], parts[1], parts[2]


def _check_claims(
    claims: dict[str, Any],
    *,
    audience: str | None,
    issuer: str | None,
    leeway: float,
    verify_exp: bool,
) -> None:
    """The registered claims of RFC 7519 §4.1, with ``leeway`` for clock skew."""
    import time

    now = time.time()
    if verify_exp:
        expires = _numeric(claims, "exp")
        if expires is not None and now > expires + leeway:
            raise ExpiredToken("the token has expired")
        not_before = _numeric(claims, "nbf")
        if not_before is not None and now < not_before - leeway:
            raise ExpiredToken("the token is not valid yet")
        issued = _numeric(claims, "iat")
        if issued is not None and now < issued - leeway:
            raise ExpiredToken("the token was issued in the future")
    if issuer is not None and claims.get("iss") != issuer:
        raise InvalidToken("the token was issued by someone else")
    if audience is not None and not _audience_matches(claims.get("aud"), audience):
        raise InvalidToken("the token is for a different audience")


def _audience_matches(claim: Any, expected: str) -> bool:
    """``aud`` is a string or a list of them, per RFC 7519 §4.1.3."""
    if isinstance(claim, str):
        return claim == expected
    if isinstance(claim, list):
        return any(item == expected for item in cast(list[Any], claim))
    return False


def _numeric(claims: Mapping[str, Any], name: str) -> float | None:
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidToken(f"the {name!r} claim must be a number")
    return float(value)


# -- signing ----------------------------------------------------------------


def _sign(signing_input: bytes, key: str | bytes, algorithm: str) -> bytes:
    digest = _HMAC_ALGORITHMS.get(algorithm)
    if digest is not None:
        return hmac.new(_as_bytes(key), signing_input, digest).digest()
    return _asymmetric_sign(signing_input, key, algorithm)


def _verify(signing_input: bytes, signature: bytes, key: str | bytes, algorithm: str) -> bool:
    digest = _HMAC_ALGORITHMS.get(algorithm)
    if digest is not None:
        expected = hmac.new(_as_bytes(key), signing_input, digest).digest()
        return hmac.compare_digest(expected, signature)
    return _asymmetric_verify(signing_input, signature, key, algorithm)


def _crypto(algorithm: str) -> Any:
    """The ``cryptography`` pieces for ``algorithm``, or a clear error."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, padding
    except ImportError:
        raise JWTError(
            f"{algorithm} needs the optional 'cryptography' extra; "
            f"install featherweb[crypto], or use HS256"
        ) from None
    hash_name = {"256": hashes.SHA256, "384": hashes.SHA384, "512": hashes.SHA512}[algorithm[2:]]
    return hashes, serialization, ec, padding, hash_name()


def _asymmetric_sign(signing_input: bytes, key: str | bytes, algorithm: str) -> bytes:
    _, serialization, ec, padding, hash_algorithm = _crypto(algorithm)
    private = serialization.load_pem_private_key(_as_bytes(key), password=None)
    if algorithm.startswith("RS"):
        return bytes(private.sign(signing_input, padding.PKCS1v15(), hash_algorithm))
    signature = private.sign(signing_input, ec.ECDSA(hash_algorithm))
    return _der_to_raw(signature, private.key_size)


def _asymmetric_verify(
    signing_input: bytes, signature: bytes, key: str | bytes, algorithm: str
) -> bool:
    _, serialization, ec, padding, hash_algorithm = _crypto(algorithm)
    try:
        public = serialization.load_pem_public_key(_as_bytes(key))
    except Exception:
        raise JWTError("the verification key is not a valid PEM public key") from None
    try:
        if algorithm.startswith("RS"):
            public.verify(signature, signing_input, padding.PKCS1v15(), hash_algorithm)
        else:
            public.verify(_raw_to_der(signature), signing_input, ec.ECDSA(hash_algorithm))
    except Exception:
        return False
    return True


def _der_to_raw(signature: bytes, key_size: int) -> bytes:
    """JWS wants ECDSA as r‖s of fixed width, not the DER the library produces."""
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    r, s = decode_dss_signature(signature)
    width = (key_size + 7) // 8
    return r.to_bytes(width, "big") + s.to_bytes(width, "big")


def _raw_to_der(signature: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if len(signature) % 2:
        raise InvalidToken("the ECDSA signature has an odd length")
    half = len(signature) // 2
    return encode_dss_signature(
        int.from_bytes(signature[:half], "big"), int.from_bytes(signature[half:], "big")
    )


# -- encoding ---------------------------------------------------------------


def _segment(value: Mapping[str, Any]) -> bytes:
    from .._compat import json_dumps

    return _b64(json_dumps(dict(value)))


def _read_json(segment: bytes, what: str) -> dict[str, Any]:
    from .._compat import json_loads

    try:
        decoded = json_loads(_unb64(segment))
    except ValueError:
        raise InvalidToken(f"the token {what} is not valid JSON") from None
    if not isinstance(decoded, dict):
        raise InvalidToken(f"the token {what} is not an object")
    return dict(decoded)  # pyright: ignore[reportUnknownArgumentType]


def _b64(raw: bytes) -> bytes:
    from base64 import urlsafe_b64encode

    return urlsafe_b64encode(raw).rstrip(b"=")


def _unb64(encoded: bytes) -> bytes:
    from base64 import urlsafe_b64decode

    padding = b"=" * (-len(encoded) % 4)
    try:
        return urlsafe_b64decode(encoded + padding)
    except Exception:
        raise ValueError("invalid base64") from None


def _as_bytes(value: str | bytes) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


class JWTAuth:
    """Authentication by a bearer token.

    Stateless in the way a session cookie is not: nothing is stored anywhere, so
    a token cannot be revoked before it expires. Keep ``expires_in`` short.

    For HS256 one secret both signs and verifies. For RS256/ES256 pass the
    public key as ``key`` and the private one as ``signing_key``.
    """

    __slots__ = (
        "algorithms",
        "audience",
        "header",
        "issuer",
        "key",
        "leeway",
        "roles_key",
        "signing_key",
        "subject_key",
    )

    #: What a 401 advertises in ``WWW-Authenticate``.
    scheme = "Bearer"

    def __init__(
        self,
        key: str | bytes,
        *,
        algorithms: Sequence[str] = ("HS256",),
        signing_key: str | bytes | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        leeway: float = 0.0,
        subject_key: str = "sub",
        roles_key: str = "roles",
        header: str = "authorization",
    ) -> None:
        # Validated now, at startup, rather than on the first request that uses it.
        self.algorithms = tuple(_allow_list(algorithms))
        self.key = key
        self.signing_key = key if signing_key is None else signing_key
        self.issuer = issuer
        self.audience = audience
        self.leeway = leeway
        self.subject_key = subject_key
        self.roles_key = roles_key
        self.header = header

    def issue(
        self,
        subject: str,
        *,
        roles: Sequence[str] = (),
        expires_in: float | None = 3600,
        algorithm: str | None = None,
        **claims: Any,
    ) -> str:
        """Mint a token for ``subject``, valid for ``expires_in`` seconds."""
        import time

        now = int(time.time())
        payload: dict[str, Any] = {self.subject_key: subject, "iat": now, **claims}
        if roles:
            payload[self.roles_key] = list(roles)
        if expires_in is not None:
            payload["exp"] = now + int(expires_in)
        if self.issuer is not None:
            payload.setdefault("iss", self.issuer)
        if self.audience is not None:
            payload.setdefault("aud", self.audience)
        return encode(payload, self.signing_key, algorithm=algorithm or self.algorithms[0])

    def authenticate(self, request: Any) -> Any:
        """What the application calls; a token is all this strategy needs."""
        return self.identify(request)

    def identify(self, request: Any) -> Any:
        """The caller this request's token names.

        No token at all means "not logged in", and is not an error — a route
        without a guard still works. A token that is *present* and does not
        verify is a 401: the caller tried to authenticate and failed, and
        saying so beats treating them as anonymous.
        """
        from .guards import Identity, Unauthenticated

        token = self._token(request)
        if token is None:
            return None
        try:
            claims = decode(
                token,
                self.key,
                algorithms=self.algorithms,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
            )
        except JWTError as exc:
            raise Unauthenticated(str(exc), scheme=self.scheme) from None
        subject = claims.get(self.subject_key)
        if subject is None:
            raise Unauthenticated("the token names no subject", scheme=self.scheme)
        roles = claims.get(self.roles_key) or ()
        if isinstance(roles, str):
            roles = [roles]
        return Identity(
            str(subject),
            roles=[str(role) for role in roles],  # pyright: ignore[reportUnknownVariableType]
            claims=claims,
        )

    def _token(self, request: Any) -> str | None:
        """The bearer token in the header, if there is one."""
        value = request.headers.get(self.header)
        if not value:
            return None
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return token.strip()

    def __repr__(self) -> str:
        return f"JWTAuth({', '.join(self.algorithms)})"
