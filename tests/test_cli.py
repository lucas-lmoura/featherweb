"""Tests for the command line."""

from __future__ import annotations

import subprocess
import sys

import pytest

from featherweb import App, __version__
from featherweb.cli import build_parser, load, main


def test_run_defaults() -> None:
    arguments = build_parser().parse_args(["run", "myapp:app"])
    assert arguments.target == "myapp:app"
    assert arguments.host == "127.0.0.1"
    assert arguments.port == 8000
    assert arguments.log_level == "info"


def test_run_options() -> None:
    arguments = build_parser().parse_args(
        ["run", "myapp:app", "--host", "0.0.0.0", "--port", "9000", "--log-level", "debug"]
    )
    assert arguments.host == "0.0.0.0"
    assert arguments.port == 9000
    assert arguments.log_level == "debug"


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_load_module_and_attribute() -> None:
    assert isinstance(load("tests.sample_app.entrypoint:app"), App)
    assert isinstance(load("tests.sample_app.entrypoint:alias"), App)


def test_load_defaults_to_app() -> None:
    assert isinstance(load("tests.sample_app.entrypoint"), App)


def test_load_reports_a_missing_attribute() -> None:
    with pytest.raises(AttributeError, match="has no attribute 'nope'"):
        load("tests.sample_app.entrypoint:nope")


def test_load_reports_a_missing_module() -> None:
    with pytest.raises(ImportError):
        load("tests.sample_app.nope:app")


def test_load_refuses_an_empty_target() -> None:
    with pytest.raises(ValueError, match="not a valid target"):
        load(":app")


def test_main_reports_a_bad_target(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "tests.sample_app.nope:app"]) == 2
    assert "featherweb:" in capsys.readouterr().err


def test_version_flag() -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "featherweb", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout
