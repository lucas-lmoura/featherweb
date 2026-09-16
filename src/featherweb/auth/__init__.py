"""Authentication: who the caller is, and what that lets them reach.

A strategy is configured once on the application::

    app = App(controllers=[...], auth=SessionAuth(secret=SECRET))
    app = App(controllers=[...], auth=JWTAuth(SECRET, algorithms=["HS256"]))

and the guards go on the controllers that need them. ``Identity`` and
``Session`` are injectable by type, like ``Request``.

Nothing here is imported by ``import featherweb``: an application that does not
authenticate does not pay for this.
"""

from __future__ import annotations

from .guards import Authenticated, Forbidden, Guard, Identity, Roles, Unauthenticated
from .jwt import ExpiredToken, InvalidToken, JWTAuth, JWTError
from .session import Session, SessionAuth
from .signing import BadSignature, SignatureExpired, Signer

__all__ = [
    "Authenticated",
    "BadSignature",
    "ExpiredToken",
    "Forbidden",
    "Guard",
    "Identity",
    "InvalidToken",
    "JWTAuth",
    "JWTError",
    "Roles",
    "Session",
    "SessionAuth",
    "SignatureExpired",
    "Signer",
    "Unauthenticated",
]
