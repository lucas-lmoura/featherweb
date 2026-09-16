"""Sessions kept in a signed cookie.

Nothing is stored on the server: the session travels with the client, signed so
it cannot be edited. That buys statelessness and costs two things worth knowing
about — the contents are *readable* by the client, so nothing secret goes in,
and a session cannot be revoked before it expires, only replaced.

The cookie defaults are the safe ones: ``HttpOnly`` so script cannot read it,
``Secure`` so it never crosses plain HTTP, ``SameSite=Lax`` so it does not ride
along with cross-site requests.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping, Sequence
from typing import Any, Literal, cast

from ..request import Request
from ..response import Response
from .guards import Identity
from .signing import BadSignature, Signer

__all__ = ["Session", "SessionAuth"]


class Session(MutableMapping[str, Any]):
    """The session, which reads and writes like a dict.

    It remembers whether anything changed, so an untouched session does not
    cost a ``Set-Cookie`` on every response.
    """

    __slots__ = ("_data", "modified")

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = data or {}
        #: Whether this session needs writing back to the client.
        self.modified = False

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.modified = True

    def __delitem__(self, key: str) -> None:
        del self._data[key]
        self.modified = True

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        """Empty the session, which is what logging out means here."""
        if self._data:
            self._data.clear()
        self.modified = True

    def __repr__(self) -> str:
        return f"Session({sorted(self._data)}{', modified' if self.modified else ''})"


class SessionAuth:
    """Authentication by a signed session cookie.

    ``secret`` may be a list, newest first, to rotate the key without logging
    everyone out.
    """

    __slots__ = (
        "cookie_name",
        "domain",
        "httponly",
        "max_age",
        "path",
        "roles_key",
        "samesite",
        "secure",
        "signer",
        "user_key",
    )

    #: What the guards call this in a ``WWW-Authenticate`` header.
    scheme = "Cookie"

    def __init__(
        self,
        secret: str | bytes | Sequence[str | bytes],
        *,
        cookie_name: str = "session",
        max_age: int = 14 * 24 * 3600,
        secure: bool = True,
        httponly: bool = True,
        samesite: Literal["lax", "strict", "none"] = "lax",
        path: str = "/",
        domain: str | None = None,
        user_key: str = "user",
        roles_key: str = "roles",
    ) -> None:
        self.signer = Signer(secret, purpose=f"session:{cookie_name}")
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.secure = secure
        self.httponly = httponly
        self.samesite: Literal["lax", "strict", "none"] = samesite
        self.path = path
        self.domain = domain
        #: Where in the session the caller's id and roles live.
        self.user_key = user_key
        self.roles_key = roles_key

    # -- reading --------------------------------------------------------

    def load(self, request: Request) -> Session:
        """The session this request carries, or an empty one.

        A cookie that does not verify is treated as absent rather than as an
        error: the usual cause is a rotated key or an expired session, and the
        right answer to both is "you are not logged in".
        """
        raw = request.cookies.get(self.cookie_name)
        if not raw:
            return Session()
        try:
            payload = self.signer.unsign(raw, max_age=self.max_age)
        except BadSignature:
            return Session()
        from .._compat import json_loads

        try:
            data = json_loads(payload)
        except ValueError:
            return Session()
        return Session(dict(cast(dict[str, Any], data))) if isinstance(data, dict) else Session()

    def authenticate(self, request: Request) -> Identity | None:
        """What the application calls: load the session and say who it names."""
        session = self.load(request)
        request.session = session
        return self.identify(session)

    def finish(self, request: Request, response: Response[Any]) -> None:
        """Write the session back on the way out, if the handler changed it."""
        if request.session is not None:
            self.save(response, request.session)

    def identify(self, session: Session) -> Identity | None:
        """Who the session says this is, if anyone."""
        user = session.get(self.user_key)
        if user is None:
            return None
        roles = session.get(self.roles_key) or ()
        if isinstance(roles, str):
            roles = [roles]
        return Identity(str(user), roles=[str(role) for role in roles], claims=dict(session))

    # -- writing --------------------------------------------------------

    def login(self, session: Session, user: str, *, roles: Sequence[str] = ()) -> Identity:
        """Put a caller into the session and return the identity it now holds."""
        session[self.user_key] = user
        session[self.roles_key] = list(roles)
        return Identity(user, roles=roles, claims=dict(session))

    def logout(self, session: Session) -> None:
        """Empty the session; the cookie is replaced on the way out."""
        session.clear()

    def save(self, response: Response[Any], session: Session) -> None:
        """Write the session back, if it changed."""
        if not session.modified:
            return
        if not session:
            response.delete_cookie(self.cookie_name, path=self.path, domain=self.domain)
            return
        from .._compat import json_dumps

        signed = self.signer.sign(json_dumps(dict(session)))
        response.set_cookie(
            self.cookie_name,
            signed,
            max_age=self.max_age,
            path=self.path,
            domain=self.domain,
            secure=self.secure,
            httponly=self.httponly,
            samesite=self.samesite,
        )

    def __repr__(self) -> str:
        return f"SessionAuth(cookie={self.cookie_name!r})"
