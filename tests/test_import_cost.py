"""What ``import featherweb`` is allowed to drag in.

Timing is too noisy to assert on, but the *mechanism* behind it is not: either a
module is on the import path or it is not. These guard that, so the work that
took the import from 29 ms to 20 ms cannot be undone by a stray top-level
import that nobody notices.

Section 2 of PLAN.md records the timings themselves, and
``benchmarks/measure.py`` reproduces them.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: Loaded only when an application actually asks for them.
ON_DEMAND = ("featherweb.multipart", "featherweb.staticfiles", "featherweb.websocket")
#: The whole auth package, which most applications never touch.
ON_DEMAND_PACKAGES = ("featherweb.auth",)
#: Expensive standard library modules featherweb must not force on its importer.
EXPENSIVE_STDLIB = ("logging", "dataclasses", "inspect", "ssl", "json")


def loaded_modules(statement: str = "import featherweb") -> set[str]:
    """Every module in ``sys.modules`` of a fresh interpreter after ``statement``."""
    code = f"{statement}; import sys; print('\\n'.join(sorted(sys.modules)))"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return set(result.stdout.split())


@pytest.fixture(scope="module")
def after_import() -> set[str]:
    return loaded_modules()


def test_the_package_imports_at_all(after_import: set[str]) -> None:
    assert "featherweb" in after_import


@pytest.mark.parametrize("module", ON_DEMAND)
def test_an_on_demand_module_stays_off_the_import_path(module: str, after_import: set[str]) -> None:
    assert module not in after_import, (
        f"{module} is now imported by `import featherweb`; it should be loaded on first use"
    )


@pytest.mark.parametrize("package", ON_DEMAND_PACKAGES)
def test_an_on_demand_package_stays_off_the_import_path(
    package: str, after_import: set[str]
) -> None:
    inside = sorted(name for name in after_import if name.startswith(package))
    assert not inside, f"{package} reached the import path through {inside}"


@pytest.mark.parametrize("module", EXPENSIVE_STDLIB)
def test_an_expensive_stdlib_module_is_not_forced_on_the_importer(
    module: str, after_import: set[str]
) -> None:
    """``logging`` is the one that cost the most; the others are cheap to keep out."""
    assert module not in after_import, (
        f"importing featherweb now pulls in {module}; something gained a top-level import"
    )


def test_the_lazy_names_still_resolve() -> None:
    """Keeping a module off the path is only correct if the name still works."""
    import featherweb

    for name in ("StaticFiles", "UploadFile", "WebSocket", "SessionAuth", "JWTAuth", "Identity"):
        assert getattr(featherweb, name) is not None, name


def test_an_unknown_name_still_raises_attribute_error() -> None:
    import featherweb

    with pytest.raises(AttributeError, match="no attribute"):
        featherweb.NotAThing  # noqa: B018 - the point is the lookup


def test_logging_arrives_when_something_is_actually_logged() -> None:
    """The lazy logger has to work, not merely defer."""
    loaded = loaded_modules(
        "import featherweb; from featherweb.app import logger; logger.debug('x')"
    )
    assert "logging" in loaded


def test_typing_is_loaded_because_a_generic_class_needs_it(after_import: set[str]) -> None:
    """Recorded, not lamented: PLAN.md section 2 explains the decision.

    ``class Response[BodyT]`` inherits ``Generic`` implicitly, and that alone
    imports ``typing``. The cost cannot be deferred without giving up
    ``Response[T]``, so this asserts the reason rather than the absence.
    """
    assert "typing" in after_import
    probe = loaded_modules("class B[T]: pass")
    assert "typing" in probe, "a generic class no longer imports typing; section 2 can be revisited"
