from importlib.metadata import requires, version

import featherweb


def test_version_matches_metadata() -> None:
    assert featherweb.__version__ == version("featherweb")


def test_no_required_runtime_dependencies() -> None:
    required = [req for req in requires("featherweb") or [] if "extra ==" not in req]
    assert required == []
