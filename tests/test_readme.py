"""The README's examples, executed rather than admired.

Documentation rots quietly: a rename lands, every test still passes, and the
first thing a new reader copies does not work. So these read the README itself
rather than a copy of it — change the file and this notices.

Three levels of checking, because the blocks are not all the same kind of thing:

* the opening example is self-contained, so it is run and its routes are called;
* every other block is compiled, which catches syntax rot in a fragment;
* every name the README imports from ``featherweb`` has to exist, which is what
  actually catches a rename.
"""

from __future__ import annotations

import ast
import re
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from featherweb.testing import TestClient

README = Path(__file__).parent.parent / "README.md"
#: Fenced blocks tagged as Python, in the order they appear.
_BLOCK = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)


def blocks() -> list[str]:
    return _BLOCK.findall(README.read_text(encoding="utf-8"))


def test_the_readme_is_where_the_tests_think_it_is() -> None:
    assert README.exists(), f"no README at {README}"
    assert blocks(), "the README has no Python blocks; this file is testing nothing"


# -- the opening example ----------------------------------------------------


@pytest.fixture
def quickstart() -> Iterator[dict[str, Any]]:
    """The first block, executed as a module of its own.

    A real module rather than a bare dict: ``@dataclass`` resolves annotations
    through ``sys.modules[cls.__module__]``, and a namespace that is not there
    fails in a way that has nothing to do with the example.
    """
    module = types.ModuleType("readme_quickstart")
    # Not "__main__", so the app.run() at the bottom of the block stays asleep.
    sys.modules[module.__name__] = module
    try:
        source = blocks()[0]
        exec(compile(source, "README.md", "exec"), module.__dict__)
        yield module.__dict__
    finally:
        sys.modules.pop(module.__name__, None)


def test_the_opening_example_builds_an_application(quickstart: dict[str, Any]) -> None:
    from featherweb import App

    assert isinstance(quickstart.get("app"), App)


async def test_the_opening_example_lists_tasks(quickstart: dict[str, Any]) -> None:
    async with TestClient(quickstart["app"]) as client:
        response = await client.get("/tasks")
    assert response.status == 200
    assert [task["title"] for task in response.json()] == [
        "write a framework",
        "write its README",
    ]


async def test_the_opening_example_filters_by_a_query_parameter(
    quickstart: dict[str, Any],
) -> None:
    """``done: bool | None`` is inferred as a query parameter, as the prose claims."""
    async with TestClient(quickstart["app"]) as client:
        response = await client.get("/tasks", params={"done": "true"})
    assert [task["id"] for task in response.json()] == [2]


async def test_the_opening_example_reads_a_path_parameter(quickstart: dict[str, Any]) -> None:
    async with TestClient(quickstart["app"]) as client:
        response = await client.get("/tasks/1")
    assert response.json()["title"] == "write a framework"


async def test_the_opening_example_creates_with_201(quickstart: dict[str, Any]) -> None:
    async with TestClient(quickstart["app"]) as client:
        response = await client.post("/tasks", json={"id": 3, "title": "ship it"})
    assert response.status == 201
    assert response.json() == {"id": 3, "title": "ship it", "done": False}


async def test_the_opening_example_answers_422_as_documented(
    quickstart: dict[str, Any],
) -> None:
    """The README shows this shape of error body; it has to keep being that shape."""
    async with TestClient(quickstart["app"]) as client:
        response = await client.post("/tasks", json={"id": "nope", "title": "x"})
    assert response.status == 422
    detail = response.json()["detail"]
    assert set(detail[0]) == {"location", "field", "message"}


def test_the_documented_error_shape_matches_a_real_one() -> None:
    """The JSON block under 'Input, inferred' is not decoration."""
    text = README.read_text(encoding="utf-8")
    assert '{"detail": [{"location": "query", "field": "perPage"' in text, (
        "the documented 422 example changed; check it still matches what the framework sends"
    )


# -- the rest of the blocks -------------------------------------------------


def test_every_python_block_parses() -> None:
    """A fragment cannot be run, but it can at least be valid Python."""
    for index, source in enumerate(blocks()):
        try:
            ast.parse(source)
        except SyntaxError as exc:
            raise AssertionError(f"README block {index} does not parse: {exc}") from exc


def test_every_name_imported_from_featherweb_exists() -> None:
    """What catches a rename: the README imports it, so it has to be there."""
    import featherweb

    missing: list[str] = []
    for source in blocks():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ImportFrom) or not (node.module or "").startswith(
                "featherweb"
            ):
                continue
            module = featherweb
            if node.module != "featherweb":
                module = __import__(node.module or "", fromlist=["_"])
            missing.extend(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if not hasattr(module, alias.name)
            )
    assert not missing, f"the README imports names that do not exist: {sorted(set(missing))}"


def test_the_readme_only_promises_extras_that_exist() -> None:
    """``pip install "featherweb[crypto]"`` has to name a real extra."""
    import tomllib

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    extras = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    text = README.read_text(encoding="utf-8")
    for extra in re.findall(r"featherweb\[(\w+)\]", text):
        assert extra in extras, (
            f"the README offers an extra that pyproject does not define: {extra}"
        )
