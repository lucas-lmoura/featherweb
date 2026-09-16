"""Signing values with HMAC, for cookies and anything else that leaves the process.

A signed value is not secret — it is readable by whoever holds it — but it
cannot be changed without the key. That is what a session cookie needs.

Three things here are deliberate rather than incidental:

* **Constant time.** Every signature comparison goes through
  ``hmac.compare_digest``. Comparing with ``==`` leaks, through timing, how much
  of a forged signature was right, which is enough to build one byte by byte.
* **Key rotation.** The first secret signs; every secret verifies. Putting a new
  key at the front and keeping the old one behind it rotates the key without
  invalidating what is already out there.
* **Purpose separation.** The key actually used is derived from the secret and a
  purpose, so a value signed for the session cookie will not verify as anything
  else, even though both use the same secret.
"""

from __future__ import annotations

import hmac
from collections.abc import Sequence
from hashlib import sha256
from typing import Final

__all__ = ["BadSignature", "SignatureExpired", "Signer"]

#: Separates the payload, the timestamp and the signature.
_SEPARATOR: Final = b"."
#: Signatures older than this are refused outright, whatever the caller asks for.
_MAX_REASONABLE_AGE: Final = 10 * 365 * 24 * 3600


class BadSignature(Exception):
    """The value was not signed with a key we know, or was tampered with."""


class SignatureExpired(BadSignature):
    """The signature was genuine but too old."""

    def __init__(self, age: float, max_age: float) -> None:
        self.age = age
        self.max_age = max_age
        super().__init__(f"signature is {age:.0f}s old, limit is {max_age:.0f}s")


class Signer:
    """Signs and verifies values with HMAC-SHA256.

    ``secrets`` is one secret or several, newest first::

        Signer(["the new key", "the old key"])

    Anything signed with any of them still verifies; new values are signed with
    the first.
    """

    __slots__ = ("_keys", "purpose")

    def __init__(self, secrets: str | bytes | Sequence[str | bytes], *, purpose: str = "") -> None:
        keys = [secrets] if isinstance(secrets, str | bytes) else list(secrets)
        if not keys:
            raise ValueError("a Signer needs at least one secret")
        raw = [_as_bytes(secret) for secret in keys]
        if any(not secret for secret in raw):
            # An empty secret signs and verifies happily, which is the worst
            # possible failure mode for SECRET = os.environ.get("SECRET", "").
            raise ValueError("a secret cannot be empty")
        self.purpose = purpose
        #: Derived once: the secret itself never touches a payload.
        self._keys = [_derive(secret, purpose) for secret in raw]

    def sign(self, value: bytes | str, *, timestamp: int | None = None) -> str:
        """``value``, its timestamp and the signature, as one URL-safe string."""
        import time

        payload = _encode(_as_bytes(value))
        stamp = _encode(str(int(time.time()) if timestamp is None else timestamp).encode("ascii"))
        signed = payload + _SEPARATOR + stamp
        return (signed + _SEPARATOR + _encode(self._signature(signed, self._keys[0]))).decode(
            "ascii"
        )

    def unsign(self, signed: bytes | str, *, max_age: float | None = None) -> bytes:
        """The original value, or :class:`BadSignature` if it is not ours.

        ``max_age`` is in seconds; without it, a signature never expires by
        itself, which is rarely what a cookie wants.
        """
        raw = _as_bytes(signed)
        payload, separator, rest = raw.partition(_SEPARATOR)
        stamp, separator2, signature = rest.partition(_SEPARATOR)
        if not separator or not separator2:
            raise BadSignature("the value is not in the signed format")
        body = payload + _SEPARATOR + stamp
        try:
            given = _decode(signature)
        except ValueError:
            raise BadSignature("the signature is not valid base64") from None
        if not self._matches(body, given):
            raise BadSignature("the signature does not match")
        # Only now, with the signature verified, is the timestamp worth reading.
        self._check_age(stamp, max_age)
        try:
            return _decode(payload)
        except ValueError:
            raise BadSignature("the payload is not valid base64") from None

    def _matches(self, body: bytes, given: bytes) -> bool:
        """Whether any key we hold produced this signature, in constant time.

        Every key is tried even after one matches: stopping early would make the
        time taken depend on which key signed the value.
        """
        matched = False
        for key in self._keys:
            if hmac.compare_digest(self._signature(body, key), given):
                matched = True
        return matched

    def _check_age(self, stamp: bytes, max_age: float | None) -> None:
        import time

        try:
            signed_at = int(_decode(stamp))
        except ValueError:
            raise BadSignature("the timestamp is malformed") from None
        age = time.time() - signed_at
        if age < 0:
            # Signed in the future: either a clock is wrong or someone is guessing.
            raise BadSignature("the signature is dated in the future")
        if age > _MAX_REASONABLE_AGE:
            raise SignatureExpired(age, _MAX_REASONABLE_AGE)
        if max_age is not None and age > max_age:
            raise SignatureExpired(age, max_age)

    def _signature(self, body: bytes, key: bytes) -> bytes:
        return hmac.new(key, body, sha256).digest()

    def __repr__(self) -> str:
        return f"Signer({len(self._keys)} keys, purpose={self.purpose!r})"


def _derive(secret: bytes, purpose: str) -> bytes:
    """A key of its own for this purpose, so signatures cannot be moved between them."""
    return hmac.new(secret, b"featherweb-signer:" + purpose.encode("utf-8"), sha256).digest()


def _as_bytes(value: bytes | str) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def _encode(raw: bytes) -> bytes:
    """URL-safe base64 without padding, so it is safe in a cookie."""
    from base64 import urlsafe_b64encode

    return urlsafe_b64encode(raw).rstrip(b"=")


def _decode(encoded: bytes) -> bytes:
    from base64 import urlsafe_b64decode

    padding = b"=" * (-len(encoded) % 4)
    return urlsafe_b64decode(encoded + padding)
