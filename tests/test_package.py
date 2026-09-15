from importlib.metadata import requires, version

import leve


def test_version_matches_metadata() -> None:
    assert leve.__version__ == version("leve")


def test_no_required_runtime_dependencies() -> None:
    required = [req for req in requires("leve") or [] if "extra ==" not in req]
    assert required == []
