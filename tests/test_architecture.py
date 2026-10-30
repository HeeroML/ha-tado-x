"""Architecture rules enforced on the source tree."""

import ast
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "custom_components" / "tado_x"
CLIENT = PACKAGE / "client"
CLIENT_FILES = sorted(CLIENT.rglob("*.py"))
PURE_FILES = [*CLIENT_FILES, PACKAGE / "budget.py", PACKAGE / "termination.py"]
CLIENT_THIRD_PARTY = {"aiohttp", "yarl"}


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _imports(path: Path) -> list[tuple[str, int]]:
    """Return (module, relative level) of every import of a file."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def test_client_package_exists() -> None:
    """The rules below have files to check."""
    assert CLIENT / "__init__.py" in CLIENT_FILES
    assert (CLIENT / "py.typed").is_file()


@pytest.mark.parametrize("path", PURE_FILES, ids=_relative)
def test_pure_modules_do_not_import_home_assistant(path: Path) -> None:
    """The client, the budget planner and the termination policy stay pure."""
    offending = [
        module
        for module, level in _imports(path)
        if level == 0 and module.split(".")[0] == "homeassistant"
    ]
    assert offending == []


@pytest.mark.parametrize("path", CLIENT_FILES, ids=_relative)
def test_client_does_not_import_the_integration(path: Path) -> None:
    """client/ imports only itself, by relative imports."""
    offending = [
        (module, level)
        for module, level in _imports(path)
        if level > 1 or (level == 0 and module.split(".")[0] == "custom_components")
    ]
    assert offending == []


@pytest.mark.parametrize("path", CLIENT_FILES, ids=_relative)
def test_client_depends_on_stdlib_aiohttp_and_yarl(path: Path) -> None:
    """client/ can be published without other dependencies."""
    allowed = set(sys.stdlib_module_names) | CLIENT_THIRD_PARTY
    offending = [
        module
        for module, level in _imports(path)
        if level == 0 and module.split(".")[0] not in allowed
    ]
    assert offending == []


@pytest.mark.parametrize(
    "path",
    sorted([*PACKAGE.rglob("*.py"), *(ROOT / "tests").rglob("*.py")]),
    ids=_relative,
)
def test_no_future_annotations(path: Path) -> None:
    """Python 3.14 defers annotations; the __future__ import is banned."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            assert "annotations" not in {alias.name for alias in node.names}


def test_client_imports_without_home_assistant(tmp_path: Path) -> None:
    """A copy of client/ imports with Home Assistant made unimportable."""
    shutil.copytree(
        CLIENT, tmp_path / "tadox_client", ignore=shutil.ignore_patterns("__pycache__")
    )
    code = (
        "import sys\n"
        "sys.modules['homeassistant'] = None\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "import tadox_client\n"
        "assert tadox_client.TadoXClient.__module__ == 'tadox_client.api'\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-W", "error::DeprecationWarning", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
