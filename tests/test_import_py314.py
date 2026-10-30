"""The config flow imports cleanly on Python 3.14 (#73).

The import runs in a fresh interpreter, so modules already imported by the
test session cannot hide a warning or an import-time side effect.
"""

import ast
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).parent.parent
CONFIG_FLOW = ROOT / "custom_components" / "tado_x" / "config_flow.py"

# Home Assistant 2026.10.4 itself warns once while importing its HTTP server
# (aiohttp discourages subclassing web.Application); only that warning is
# tolerated. The script then records every file opened and every socket used
# while the config flow module is imported.
IMPORT_SCRIPT = """
import sys
import warnings
sys.path.insert(0, sys.argv[1])

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="Inheritance class HomeAssistantApplication",
        category=DeprecationWarning,
    )
    import homeassistant.components.http

events = []

def audit(event, args):
    if event == "open" or event.startswith("socket."):
        events.append((event, str(args[0]) if args else ""))

sys.addaudithook(audit)

import custom_components.tado_x.config_flow as config_flow

flow = config_flow.TadoXConfigFlow()
options_flow = config_flow.TadoXConfigFlow.async_get_options_flow(None)
assert (flow.VERSION, flow.MINOR_VERSION) == (1, 2)
assert isinstance(options_flow, config_flow.TadoXOptionsFlow)

for event, target in events:
    if event != "open" or not target.endswith((".py", ".pyc")):
        print(f"{event} {target}")
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-W", "error::DeprecationWarning", *args],
        capture_output=True,
        check=False,
        cwd=ROOT,
        text=True,
        timeout=120,
    )


def test_config_flow_imports_without_deprecation_warning() -> None:
    """Importing the config flow and creating its handlers raises no warning."""
    result = _run("-c", IMPORT_SCRIPT, str(ROOT))

    assert result.returncode == 0, result.stderr
    assert result.stdout == "", "import-time I/O:\n" + result.stdout


def test_config_flow_has_no_future_import_and_no_own_ssl() -> None:
    """Annotations are lazy by default; the flow uses Home Assistant's session."""
    tree = ast.parse(CONFIG_FLOW.read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute | ast.Name)
    }

    assert "__future__" not in imported
    assert "ssl" not in imported
    assert "ClientSession" not in called
    assert "create_default_context" not in called
    assert "async_get_clientsession" in called
