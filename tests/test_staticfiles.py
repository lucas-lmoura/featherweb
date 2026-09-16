"""Serving a directory, and refusing to serve anything outside it.

The traversal cases are the point of this file: the path comes from the client,
so every way of writing "go up one level" has to end in a 404 rather than in a
file the application never meant to publish.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from featherweb import App, Get, Route
from featherweb.routing import RouteConflictError
from featherweb.staticfiles import StaticFiles
from featherweb.testing import TestClient, TestResponse


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A published directory, with a secret next to it that must stay unreachable."""
    assets = tmp_path / "assets"
    (assets / "sub").mkdir(parents=True)
    (assets / "app.css").write_text("body{color:red}")
    (assets / "index.html").write_text("<h1>home</h1>")
    (assets / "sub" / "deep.txt").write_text("deep")
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    return tmp_path


def application(tree: Path, **kwargs: Any) -> App:
    @Route("/api")
    class ApiController:
        @Get("/ping")
        async def ping(self) -> str:
            return "pong"

    app = App(controllers=[ApiController])
    app.mount("/static", StaticFiles(tree / "assets", **kwargs))
    return app


async def get(app: App, path: str, **kwargs: Any) -> TestResponse:
    async with TestClient(app) as client:
        return await client.get(path, **kwargs)


# -- serving ----------------------------------------------------------------


async def test_a_file_is_served_with_its_media_type(tree: Path) -> None:
    response = await get(application(tree), "/static/app.css")
    assert response.status == 200
    assert response.text == "body{color:red}"
    assert response.headers["content-type"] == "text/css"


async def test_a_nested_file_is_served(tree: Path) -> None:
    assert (await get(application(tree), "/static/sub/deep.txt")).text == "deep"


async def test_a_missing_file_is_404(tree: Path) -> None:
    assert (await get(application(tree), "/static/nope.css")).status == 404


@pytest.mark.parametrize("path", ["/static", "/static/"])
async def test_the_mount_point_serves_the_index(tree: Path, path: str) -> None:
    response = await get(application(tree, index="index.html"), path)
    assert response.text == "<h1>home</h1>"


async def test_without_an_index_a_directory_is_404(tree: Path) -> None:
    assert (await get(application(tree), "/static/sub")).status == 404


async def test_max_age_becomes_cache_control(tree: Path) -> None:
    response = await get(application(tree, max_age=3600), "/static/app.css")
    assert response.headers["cache-control"] == "public, max-age=3600"


async def test_routes_outside_the_mount_still_work(tree: Path) -> None:
    assert (await get(application(tree), "/api/ping")).text == "pong"


async def test_a_path_under_no_mount_is_404(tree: Path) -> None:
    assert (await get(application(tree), "/elsewhere")).status == 404


async def test_only_get_and_head_are_allowed(tree: Path) -> None:
    async with TestClient(application(tree)) as client:
        response = await client.post("/static/app.css")
    assert response.status == 405
    assert response.headers["allow"] == "GET, HEAD"


# -- caching, through FileResponse ------------------------------------------


async def test_a_static_file_answers_a_conditional_request(tree: Path) -> None:
    app = application(tree)
    etag = (await get(app, "/static/app.css")).headers["etag"]
    assert (await get(app, "/static/app.css", headers={"if-none-match": etag})).status == 304


async def test_a_static_file_answers_a_range(tree: Path) -> None:
    response = await get(application(tree), "/static/app.css", headers={"range": "bytes=0-3"})
    assert response.status == 206
    assert response.text == "body"


# -- traversal --------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/static/../secret.txt",
        "/static/sub/../../secret.txt",
        "/static/%2e%2e/secret.txt",
        "/static/..%2fsecret.txt",
        "/static/sub/%2e%2e/%2e%2e/secret.txt",
        "/static/....//secret.txt",
        "/static/..\\secret.txt",
        "/static/./../secret.txt",
    ],
)
async def test_a_traversal_is_refused(tree: Path, path: str) -> None:
    response = await get(application(tree), path)
    assert response.status == 404
    assert "SECRET" not in response.text


async def test_a_symlink_out_of_the_directory_is_refused(tree: Path) -> None:
    link = tree / "assets" / "escape.txt"
    try:
        link.symlink_to(tree / "secret.txt")
    except (OSError, NotImplementedError):  # Windows without developer mode
        pytest.skip("this account cannot create symlinks")
    response = await get(application(tree), "/static/escape.txt")
    assert response.status == 404
    assert "SECRET" not in response.text


async def test_a_null_byte_is_refused(tree: Path) -> None:
    assert (await get(application(tree), "/static/app%00.css")).status == 404


# -- mounting ---------------------------------------------------------------


def test_the_root_cannot_be_mounted(tree: Path) -> None:
    app = App()
    with pytest.raises(ValueError, match="prefix of its own"):
        app.mount("/", StaticFiles(tree / "assets"))


def test_the_same_prefix_cannot_be_mounted_twice(tree: Path) -> None:
    app = App()
    app.mount("/static", StaticFiles(tree / "assets"))
    with pytest.raises(RouteConflictError):
        app.mount("/static", StaticFiles(tree / "assets"))


async def test_the_longest_matching_mount_wins(tree: Path) -> None:
    inner = tree / "assets" / "sub"
    app = App()
    app.mount("/static", StaticFiles(tree / "assets"))
    app.mount("/static/sub", StaticFiles(inner))
    assert (await get(app, "/static/sub/deep.txt")).text == "deep"


def test_static_files_repr_names_the_directory(tree: Path) -> None:
    assert "assets" in repr(StaticFiles(tree / "assets"))
