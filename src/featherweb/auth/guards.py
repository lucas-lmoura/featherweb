"""Who the request is from, and what that lets it reach.

``@Authenticated`` and ``@Roles`` go on a controller class or on one of its
methods. On a method they *add to* the class's restrictions rather than
replacing them, so a controller cannot be loosened one method at a time::

    @Authenticated
    @Route("/admin")
    class AdminController:
        @Get("/reports")                    # needs a login
        async def reports(self) -> ...

        @Roles("owner")                     # needs a login *and* the role
        @Delete("/{id:int}")
        async def delete(self, id: int) -> ...

No identity at all is 401 — "say who you are". An identity without the role is
403 — "I know who you are, and no".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

from ..exceptions import HTTPError

__all__ = ["Authenticated", "Forbidden", "Guard", "Identity", "Roles", "Unauthenticated"]

#: Attribute where the guard decorators record what they require.
GUARD_ATTR: Final = "__featherweb_guard__"


class Unauthenticated(HTTPError):
    """No usable credentials came with the request."""

    status = 401
    detail = "authentication required"

    def __init__(self, detail: Any = None, *, scheme: str = "Bearer") -> None:
        # RFC 9110 §11.6.1: a 401 has to say how to authenticate.
        super().__init__(detail=detail, headers={"www-authenticate": scheme})


class Forbidden(HTTPError):
    """The caller is known, and still not allowed."""

    status = 403
    detail = "not allowed"

    def __init__(self, detail: Any = None) -> None:
        # The status is fixed, so the first argument here is the detail rather
        # than the status HTTPError would otherwise read it as.
        super().__init__(detail=detail)


class Identity:
    """The authenticated caller.

    ``claims`` is whatever the strategy found — the session contents, or the
    token's payload — so an application can read its own fields out of it
    without the framework having to know about them.
    """

    __slots__ = ("claims", "id", "roles")

    def __init__(
        self,
        id: str,
        *,
        roles: Iterable[str] = (),
        claims: Mapping[str, Any] | None = None,
    ) -> None:
        self.id = id
        self.roles = frozenset(roles)
        self.claims: Mapping[str, Any] = claims or {}

    def has_role(self, *roles: str) -> bool:
        """Whether this identity holds any of ``roles``."""
        return bool(self.roles.intersection(roles))

    def __repr__(self) -> str:
        roles = ", ".join(sorted(self.roles))
        return f"Identity({self.id!r}{', roles=' + roles if roles else ''})"


class Guard:
    """What one endpoint requires before its handler runs.

    A guard is a list of requirements, and **every** one has to be met. Within a
    single requirement any one role will do, so ``@Roles("admin", "owner")``
    means "admin or owner".

    That distinction is the whole reason this is a list rather than a set of
    roles. A class saying ``@Roles("staff")`` and a method on it saying
    ``@Roles("admin")`` produces two requirements, so the caller has to be staff
    *and* admin. Merging the roles into one set instead would have let a plain
    staff member through the admin method — a guard that quietly grants more
    than either decorator asked for.
    """

    __slots__ = ("requirements",)

    def __init__(self, requirements: Iterable[Iterable[str]] = ()) -> None:
        #: Each entry is an "any one of these" set; an empty set means "any login".
        self.requirements = tuple(frozenset(roles) for roles in requirements)

    @classmethod
    def for_roles(cls, roles: Iterable[str]) -> Guard:
        """One requirement: these roles, or just a login when there are none."""
        return cls([frozenset(roles)])

    @property
    def roles(self) -> frozenset[str]:
        """Every role mentioned, for display; the structure is in ``requirements``."""
        every: set[str] = set()
        for required in self.requirements:
            every |= required
        return frozenset(every)

    def merge(self, other: Guard | None) -> Guard:
        """Both levels apply, so the requirements are concatenated, not merged."""
        if other is None:
            return self
        return Guard(self.requirements + other.requirements)

    def check(self, identity: Identity | None, *, scheme: str = "Bearer") -> None:
        """Raise the right error, or return quietly.

        No identity is 401 and a missing role is 403: the difference matters,
        because retrying with credentials only helps for the first one.
        """
        if identity is None:
            raise Unauthenticated(scheme=scheme)
        for required in self.requirements:
            if required and not identity.roles.intersection(required):
                raise Forbidden(f"this needs one of: {', '.join(sorted(required))}")

    def __repr__(self) -> str:
        if not self.requirements:
            return "Guard()"
        return "Guard(" + " + ".join(str(sorted(each)) for each in self.requirements) + ")"


def Authenticated[T](target: T) -> T:
    """Require a logged-in caller for this class or method."""
    return _require(target, ())


def Roles[T](*roles: str) -> Callable[[T], T]:
    """Require a logged-in caller holding at least one of ``roles``."""
    if not roles:
        raise TypeError("@Roles needs at least one role; use @Authenticated for any login")

    def decorate(target: T) -> T:
        return _require(target, roles)

    return decorate


def _require[T](target: T, roles: Iterable[str]) -> T:
    existing: Guard | None = getattr(target, GUARD_ATTR, None)
    guard = Guard.for_roles(roles).merge(existing)
    try:
        setattr(target, GUARD_ATTR, guard)
    except AttributeError:  # a slotted object, or something else undecoratable
        raise TypeError(
            f"@Authenticated/@Roles cannot be applied to {target!r}; "
            f"use them on a controller class or one of its methods"
        ) from None
    return target


def guard_of(target: Any) -> Guard | None:
    """The guard declared directly on ``target``, if any."""
    guard = getattr(target, GUARD_ATTR, None)
    return guard if isinstance(guard, Guard) else None
