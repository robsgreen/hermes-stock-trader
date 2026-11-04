"""Architecture rules: broker-specific code stays inside the `brokers` package."""

import ast
from pathlib import Path

import trader_broker
from trader_broker.brokers import Broker
from trader_broker.brokers.fake import FakeBroker

_PACKAGE_DIR = Path(trader_broker.__file__).parent


def test_fake_broker_satisfies_the_broker_protocol() -> None:
    assert isinstance(FakeBroker(), Broker)


def test_only_brokers_package_imports_alpaca() -> None:
    sources = [
        path
        for path in _PACKAGE_DIR.rglob("*.py")
        if "brokers" not in path.relative_to(_PACKAGE_DIR).parts
    ]
    assert _PACKAGE_DIR / "models.py" in sources  # the scan sees the package

    offenders = [
        f"{path.relative_to(_PACKAGE_DIR)}: {module}"
        for path in sources
        for module in _imported_modules(path)
        if module.split(".")[0] == "alpaca"
    ]

    assert offenders == []


def _imported_modules(path: Path) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules
