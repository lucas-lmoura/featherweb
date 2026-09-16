"""Authentication through the application: guards, sessions and identities.

This covers section 5.5 of the plan — the guards on a class and on a method,
401 against 403, and the two strategies behaving the same way from a handler's
point of view.
"""

from __future__ import annotations

from typing import Any

import pytest

from featherweb import (
    App,
    Authenticated,
    Get,
    Identity,
    Post,
    Roles,
    Route,
    Session,
)
from featherweb.auth import JWTAuth, SessionAuth
from featherweb.auth.guards import Forbidden, Guard, Unauthenticated
from featherweb.testing import TestClient, TestResponse

SECRET = "a secret of a plausible length for a test"
SESSION_AUTH = SessionAuth(SECRET, secure=False)


@Route("/app")
class AppController:
    @Post("/login")
    async def login(self, session: Session) -> dict[str, Any]:
        SESSION_AUTH.login(session, "ada", roles=["editor"])
        return {"ok": True}

    @Post("/login-admin")
    async def login_admin(self, session: Session) -> dict[str, Any]:
        SESSION_AUTH.login(session, "root", roles=["admin", "editor"])
        return {"ok": True}

    @Post("/logout")
    async def logout(self, session: Session) -> dict[str, Any]:
        SESSION_AUTH.logout(session)
        return {"ok": True}

    @Get("/open")
    async def open(self) -> dict[str, Any]:
        return {"open": True}

    @Get("/maybe")
    async def maybe(self, identity: Identity | None = None) -> dict[str, Any]:
        return {"who": identity.id if identity is not None else None}

    @Get("/me")
    @Authenticated
    async def me(self, identity: Identity) -> dict[str, Any]:
        return {"id": identity.id, "roles": sorted(identity.roles)}

    @Get("/editors")
    @Roles("editor")
    async def editors(self) -> dict[str, Any]:
        return {"ok": True}

    @Get("/admins")
    @Roles("admin")
    async def admins(self) -> dict[str, Any]:
        return {"ok": True}

    @Get("/either")
    @Roles("admin", "editor")
    async def either(self) -> dict[str, Any]:
        return {"ok": True}

    @Get("/counter")
    async def counter(self, session: Session) -> dict[str, Any]:
        session["count"] = session.get("count", 0) + 1
        return {"count": session["count"]}

    @Get("/untouched")
    async def untouched(self, session: Session) -> dict[str, Any]:
        return {"size": len(session)}


def application(**kwargs: Any) -> App:
    return App(controllers=[AppController], auth=SESSION_AUTH, **kwargs)


async def login(client: TestClient, path: str = "/app/login") -> dict[str, str]:
    """Log in and return the cookie jar to send back."""
    response = await client.post(path)
    return {"session": response.cookies["session"]}


# -- guards -----------------------------------------------------------------


async def test_an_unguarded_route_needs_nothing() -> None:
    async with TestClient(application()) as client:
        assert (await client.get("/app/open")).json() == {"open": True}


async def test_a_guarded_route_without_a_login_is_401() -> None:
    async with TestClient(application()) as client:
        response = await client.get("/app/me")
    assert response.status == 401
    assert response.headers["www-authenticate"] == "Cookie"


async def test_a_guarded_route_after_a_login_works() -> None:
    async with TestClient(application()) as client:
        jar = await login(client)
        assert (await client.get("/app/me", cookies=jar)).json() == {
            "id": "ada",
            "roles": ["editor"],
        }


async def test_a_role_the_caller_holds_passes() -> None:
    async with TestClient(application()) as client:
        jar = await login(client)
        assert (await client.get("/app/editors", cookies=jar)).status == 200


async def test_a_role_the_caller_lacks_is_403() -> None:
    """403, not 401: retrying with credentials would not help."""
    async with TestClient(application()) as client:
        jar = await login(client)
        response = await client.get("/app/admins", cookies=jar)
    assert response.status == 403
    assert "admin" in response.json()["detail"]


async def test_any_one_of_several_roles_is_enough() -> None:
    async with TestClient(application()) as client:
        jar = await login(client)
        assert (await client.get("/app/either", cookies=jar)).status == 200


async def test_roles_without_a_login_is_401_not_403() -> None:
    async with TestClient(application()) as client:
        assert (await client.get("/app/admins")).status == 401


async def test_an_optional_identity_is_none_when_anonymous() -> None:
    async with TestClient(application()) as client:
        assert (await client.get("/app/maybe")).json() == {"who": None}


async def test_an_optional_identity_is_filled_in_when_logged_in() -> None:
    async with TestClient(application()) as client:
        jar = await login(client)
        assert (await client.get("/app/maybe", cookies=jar)).json() == {"who": "ada"}


async def test_a_required_identity_without_a_guard_is_still_401() -> None:
    """``identity: Identity`` says the handler cannot run without one."""

    @Route("/strict")
    class StrictController:
        @Get
        async def index(self, identity: Identity) -> dict[str, Any]:
            return {"id": identity.id}

    app = App(controllers=[StrictController], auth=SESSION_AUTH)
    async with TestClient(app) as client:
        assert (await client.get("/strict")).status == 401


# -- guards on the class ----------------------------------------------------


async def test_a_guard_on_the_class_covers_every_method() -> None:
    @Authenticated
    @Route("/locked")
    class LockedController:
        @Get("/one")
        async def one(self) -> dict[str, Any]:
            return {"ok": 1}

        @Get("/two")
        async def two(self) -> dict[str, Any]:
            return {"ok": 2}

    app = App(controllers=[LockedController], auth=SESSION_AUTH)
    async with TestClient(app) as client:
        assert (await client.get("/locked/one")).status == 401
        assert (await client.get("/locked/two")).status == 401


async def test_a_method_adds_to_the_class_rather_than_replacing_it() -> None:
    """Both levels apply: the method tightens the class, it cannot loosen it.

    Merging the two role sets into one would have made this weaker than either
    decorator asked for — a staff member walking into the admin method.
    """

    @Roles("staff")
    @Route("/both")
    class BothController:
        @Get("/plain")
        async def plain(self) -> dict[str, Any]:
            return {"ok": True}

        @Get("/extra")
        @Roles("admin")
        async def extra(self) -> dict[str, Any]:
            return {"ok": True}

    auth = JWTAuth(SECRET)
    app = App(controllers=[BothController], auth=auth)

    async def get(path: str, *roles: str) -> int:
        token = auth.issue("someone", roles=list(roles))
        async with TestClient(app) as client:
            response = await client.get(path, headers={"authorization": f"Bearer {token}"})
        return response.status

    assert await get("/both/plain", "staff") == 200
    assert await get("/both/extra", "staff") == 403  # staff, but not admin
    assert await get("/both/extra", "admin") == 403  # admin, but not staff
    assert await get("/both/extra", "staff", "admin") == 200
    assert await get("/both/plain", "guest") == 403


async def test_several_roles_on_one_decorator_are_alternatives() -> None:
    """Within one @Roles, any of them will do."""

    @Route("/either")
    class EitherController:
        @Get
        @Roles("admin", "owner")
        async def index(self) -> dict[str, Any]:
            return {"ok": True}

    auth = JWTAuth(SECRET)
    app = App(controllers=[EitherController], auth=auth)
    async with TestClient(app) as client:
        for role in ("admin", "owner"):
            token = auth.issue("x", roles=[role])
            response = await client.get("/either", headers={"authorization": f"Bearer {token}"})
            assert response.status == 200, role


# -- sessions ---------------------------------------------------------------


async def test_the_session_survives_between_requests() -> None:
    async with TestClient(application()) as client:
        first = await client.get("/app/counter")
        jar = {"session": first.cookies["session"]}
        second = await client.get("/app/counter", cookies=jar)
    assert first.json() == {"count": 1}
    assert second.json() == {"count": 2}


async def test_an_untouched_session_sends_no_cookie() -> None:
    """Otherwise every response would carry a Set-Cookie for no reason."""
    async with TestClient(application()) as client:
        response = await client.get("/app/untouched")
    assert "set-cookie" not in response.headers


async def test_the_session_cookie_is_defended_by_default() -> None:
    app = App(controllers=[AppController], auth=SessionAuth(SECRET))
    async with TestClient(app) as client:
        header = (await client.post("/app/login")).headers["set-cookie"]
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=Lax" in header


async def test_logging_out_clears_the_cookie() -> None:
    async with TestClient(application()) as client:
        jar = await login(client)
        response = await client.post("/app/logout", cookies=jar)
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert (await client.get("/app/me", cookies={"session": ""})).status == 401


async def test_a_forged_cookie_is_not_a_login() -> None:
    async with TestClient(application()) as client:
        response = await client.get("/app/me", cookies={"session": "made.up.value"})
    assert response.status == 401


async def test_a_tampered_cookie_is_not_a_login() -> None:
    async with TestClient(application()) as client:
        jar = await login(client)
        broken = jar["session"][:-4] + ("aaaa" if not jar["session"].endswith("aaaa") else "bbbb")
        response = await client.get("/app/me", cookies={"session": broken})
    assert response.status == 401


async def test_a_session_signed_with_another_secret_is_ignored() -> None:
    other = SessionAuth("a completely different secret", secure=False)
    async with TestClient(App(controllers=[AppController], auth=other)) as client:
        jar = await login(client)
    async with TestClient(application()) as client:
        assert (await client.get("/app/me", cookies=jar)).status == 401


async def test_the_session_contents_reach_the_identity_claims() -> None:
    @Route("/claims")
    class ClaimsController:
        @Post("/set")
        async def set(self, session: Session) -> dict[str, Any]:
            SESSION_AUTH.login(session, "ada")
            session["team"] = "core"
            return {"ok": True}

        @Get
        @Authenticated
        async def show(self, identity: Identity) -> dict[str, Any]:
            return {"team": identity.claims.get("team")}

    app = App(controllers=[ClaimsController], auth=SESSION_AUTH)
    async with TestClient(app) as client:
        jar = {"session": (await client.post("/claims/set")).cookies["session"]}
        assert (await client.get("/claims", cookies=jar)).json() == {"team": "core"}


# -- JWT through the application --------------------------------------------


def jwt_app(**kwargs: Any) -> tuple[App, JWTAuth]:
    auth = JWTAuth(SECRET, **kwargs)

    @Authenticated
    @Route("/api")
    class ApiController:
        @Get("/me")
        async def me(self, identity: Identity) -> dict[str, Any]:
            return {"id": identity.id, "roles": sorted(identity.roles)}

        @Get("/admin")
        @Roles("admin")
        async def admin(self) -> dict[str, Any]:
            return {"ok": True}

    return App(controllers=[ApiController], auth=auth), auth


async def bearer(client: TestClient, auth: JWTAuth, path: str, **kwargs: Any) -> TestResponse:
    token = auth.issue(**kwargs)
    return await client.get(path, headers={"authorization": f"Bearer {token}"})


async def test_a_valid_token_authenticates() -> None:
    app, auth = jwt_app()
    async with TestClient(app) as client:
        response = await bearer(client, auth, "/api/me", subject="grace", roles=["admin"])
    assert response.json() == {"id": "grace", "roles": ["admin"]}


async def test_no_token_is_401_with_a_bearer_challenge() -> None:
    app, _ = jwt_app()
    async with TestClient(app) as client:
        response = await client.get("/api/me")
    assert response.status == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_a_tampered_token_is_401() -> None:
    app, auth = jwt_app()
    async with TestClient(app) as client:
        token = auth.issue("grace")
        response = await client.get("/api/me", headers={"authorization": f"Bearer {token[:-3]}aaa"})
    assert response.status == 401


async def test_an_expired_token_is_401() -> None:
    app, auth = jwt_app()
    async with TestClient(app) as client:
        token = auth.issue("grace", expires_in=-60)
        response = await client.get("/api/me", headers={"authorization": f"Bearer {token}"})
    assert response.status == 401
    assert "expired" in response.json()["detail"]


async def test_a_token_from_another_issuer_is_401() -> None:
    app, _ = jwt_app(issuer="us")
    stranger = JWTAuth(SECRET, issuer="them")
    async with TestClient(app) as client:
        response = await client.get(
            "/api/me", headers={"authorization": f"Bearer {stranger.issue('x')}"}
        )
    assert response.status == 401


async def test_a_token_signed_with_another_key_is_401() -> None:
    app, _ = jwt_app()
    stranger = JWTAuth("a different secret altogether")
    async with TestClient(app) as client:
        response = await client.get(
            "/api/me", headers={"authorization": f"Bearer {stranger.issue('x')}"}
        )
    assert response.status == 401


async def test_a_token_without_the_role_is_403() -> None:
    app, auth = jwt_app()
    async with TestClient(app) as client:
        response = await bearer(client, auth, "/api/admin", subject="bob", roles=["guest"])
    assert response.status == 403


# -- configuration errors ---------------------------------------------------


async def test_a_guard_without_a_strategy_is_a_server_error() -> None:
    """Better a loud 500 than a route that silently lets everyone through."""

    @Authenticated
    @Route("/oops")
    class OopsController:
        @Get
        async def index(self) -> dict[str, Any]:
            return {"ok": True}

    async with TestClient(App(controllers=[OopsController])) as client:
        assert (await client.get("/oops")).status == 500


async def test_asking_for_a_session_without_a_session_strategy_fails() -> None:
    @Route("/nosession")
    class NoSessionController:
        @Get
        async def index(self, session: Session) -> dict[str, Any]:
            return {"size": len(session)}

    app = App(controllers=[NoSessionController], auth=JWTAuth(SECRET))
    async with TestClient(app) as client:
        assert (await client.get("/nosession")).status == 500


def test_roles_needs_at_least_one_role() -> None:
    with pytest.raises(TypeError, match="at least one role"):
        Roles()


# -- the pieces on their own ------------------------------------------------


def test_an_identity_reports_its_roles() -> None:
    identity = Identity("ada", roles=["editor", "admin"])
    assert identity.has_role("admin")
    assert not identity.has_role("owner")
    assert "ada" in repr(identity)


def test_a_guard_keeps_both_levels_as_separate_requirements() -> None:
    merged = Guard.for_roles({"a"}).merge(Guard.for_roles({"b"}))
    assert merged.requirements == (frozenset({"a"}), frozenset({"b"}))


def test_merging_with_nothing_keeps_the_guard() -> None:
    assert Guard.for_roles({"a"}).merge(None).roles == frozenset({"a"})


def test_every_requirement_has_to_be_met() -> None:
    guard = Guard.for_roles({"a"}).merge(Guard.for_roles({"b"}))
    with pytest.raises(Forbidden):
        guard.check(Identity("x", roles=["a"]))
    guard.check(Identity("x", roles=["a", "b"]))


def test_a_guard_refuses_an_absent_identity() -> None:
    with pytest.raises(Unauthenticated):
        Guard().check(None)


def test_a_guard_refuses_a_missing_role() -> None:
    with pytest.raises(Forbidden):
        Guard.for_roles({"admin"}).check(Identity("ada", roles=["editor"]))


def test_a_guard_without_roles_accepts_any_identity() -> None:
    Guard().check(Identity("ada"))


def test_the_401_names_the_scheme_it_wants() -> None:
    assert Unauthenticated(scheme="Cookie").headers["www-authenticate"] == "Cookie"


def test_a_forbidden_carries_its_detail_not_a_status() -> None:
    """HTTPError takes the status first, so this subclass has to say otherwise."""
    error = Forbidden("no entry")
    assert (error.status, error.detail) == (403, "no entry")


def test_a_session_tracks_whether_it_changed() -> None:
    from featherweb.auth.session import Session as SessionType

    session = SessionType({"a": 1})
    assert not session.modified
    session["b"] = 2
    assert session.modified
    assert dict(session) == {"a": 1, "b": 2}


def test_clearing_a_session_marks_it_modified() -> None:
    from featherweb.auth.session import Session as SessionType

    session = SessionType({"a": 1})
    session.clear()
    assert session.modified
    assert len(session) == 0
