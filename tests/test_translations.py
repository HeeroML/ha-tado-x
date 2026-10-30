"""Consistency of strings.json, the translation files, services.yaml and the code."""

import ast
from collections.abc import Iterator
import importlib.util
from itertools import pairwise
from pathlib import Path
import re
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "custom_components" / "tado_x"
TRANSLATIONS = PACKAGE / "translations"
LANGUAGES = ("de", "fr", "it")
PLATFORMS = frozenset(
    {
        "binary_sensor",
        "button",
        "climate",
        "device_tracker",
        "number",
        "select",
        "sensor",
        "switch",
    }
)
PLACEHOLDER = re.compile(r"\{(\w+)\}")
STATE_KEY = re.compile(r"^[a-z0-9_-]+$")
MODULES = sorted(PACKAGE.rglob("*.py"))


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_translations = _load_script("gen_translations")


def _flat(path: Path) -> dict[str, str]:
    leaves: dict[str, str] = gen_translations.flatten(gen_translations.load(path))
    return leaves


STRINGS = _flat(PACKAGE / "strings.json")


def _language(code: str) -> dict[str, str]:
    return _flat(TRANSLATIONS / f"{code}.json")


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _defined(path: str, strings: dict[str, str] = STRINGS) -> bool:
    """Return whether a leaf or a subtree exists at a dotted path."""
    return path in strings or any(key.startswith(f"{path}.") for key in strings)


def test_translation_files_are_generated() -> None:
    """en.json is strings.json, and every file has the canonical layout."""
    assert gen_translations.main(["--check"]) == 0, "run scripts/gen_translations.py"


def test_english_equals_strings() -> None:
    """en.json holds exactly the strings of strings.json."""
    assert _language("en") == STRINGS


def test_german_is_complete() -> None:
    """German has every key of strings.json and no other."""
    german = _language("de")
    assert sorted(STRINGS.keys() - german.keys()) == []
    assert sorted(german.keys() - STRINGS.keys()) == []


@pytest.mark.parametrize("code", list(LANGUAGES))
def test_translation_keys_exist_in_english(code: str) -> None:
    """A translation never has a key that strings.json lacks."""
    assert sorted(_language(code).keys() - STRINGS.keys()) == []


@pytest.mark.parametrize("code", ["en", *LANGUAGES])
def test_values_are_non_empty_strings(code: str) -> None:
    """Every leaf is text."""
    bad = [
        key
        for key, value in _language(code).items()
        if not isinstance(value, str) or not value.strip()
    ]
    assert bad == []


@pytest.mark.parametrize("code", ["en", *LANGUAGES])
def test_state_and_option_keys_are_lowercase(code: str) -> None:
    """Hassfest only accepts lowercase state and selector option keys."""
    bad: list[str] = []
    for key in _language(code):
        parts = key.split(".")
        bad.extend(
            key
            for parent, child in pairwise(parts)
            if parent in {"state", "options"} and not STATE_KEY.match(child)
        )
    assert bad == []


@pytest.mark.parametrize("code", list(LANGUAGES))
def test_placeholders_match_english(code: str) -> None:
    """A translation uses exactly the placeholders of the English text."""
    mismatched = {
        key: (
            sorted(PLACEHOLDER.findall(text)),
            sorted(PLACEHOLDER.findall(STRINGS[key])),
        )
        for key, text in _language(code).items()
        if key in STRINGS
        and sorted(PLACEHOLDER.findall(text))
        != sorted(PLACEHOLDER.findall(STRINGS[key]))
    }
    assert mismatched == {}


def _services() -> dict[str, Any]:
    services: dict[str, Any] = yaml.safe_load(
        (PACKAGE / "services.yaml").read_text(encoding="utf-8")
    )
    return services


def _service_fields(service: dict[str, Any] | None) -> Iterator[str]:
    for name, field in ((service or {}).get("fields") or {}).items():
        if isinstance(field, dict) and "fields" in field and "selector" not in field:
            yield from field["fields"]
        else:
            yield name


def _selectors(node: object) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (selector type, config) of every selector below a node."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "selector" and isinstance(value, dict):
                for kind, config in value.items():
                    yield kind, config or {}
            yield from _selectors(value)
    elif isinstance(node, list):
        for item in node:
            yield from _selectors(item)


def test_services_and_fields_have_strings() -> None:
    """Every service and field of services.yaml has a name and a description."""
    missing: list[str] = []
    for name, service in _services().items():
        prefix = f"services.{name}"
        missing.extend(
            key
            for key in (f"{prefix}.name", f"{prefix}.description")
            if key not in STRINGS
        )
        for field in _service_fields(service):
            missing.extend(
                key
                for key in (
                    f"{prefix}.fields.{field}.name",
                    f"{prefix}.fields.{field}.description",
                )
                if key not in STRINGS
            )
    assert missing == []


def test_service_strings_belong_to_services() -> None:
    """strings.json translates no service or field that services.yaml lacks."""
    services = _services()
    known = {f"services.{name}" for name in services} | {
        f"services.{name}.fields.{field}"
        for name, service in services.items()
        for field in _service_fields(service)
    }
    stale = {
        key.rsplit(".", 1)[0]
        for key in STRINGS
        if key.startswith("services.") and key.rsplit(".", 1)[0] not in known
    }
    assert sorted(stale) == []


def test_service_selector_translation_keys() -> None:
    """Select options and object fields of services.yaml selectors are translated."""
    missing: list[str] = []
    for kind, config in _selectors(_services()):
        if (key := config.get("translation_key")) is None:
            continue
        prefix = f"selector.{key}"
        if kind == "select":
            missing.extend(
                f"{prefix}.options.{option}"
                for option in config["options"]
                if f"{prefix}.options.{option}" not in STRINGS
            )
        elif kind == "object":
            missing.extend(
                f"{prefix}.fields.{field}.name"
                for field in config.get("fields", {})
                if f"{prefix}.fields.{field}.name" not in STRINGS
            )
    assert missing == []


def _string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        target: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        value = getattr(node, "value", None)
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            constants[target.id] = value.value
    return constants


CONSTANTS = _string_constants(ast.parse((PACKAGE / "const.py").read_text("utf-8")))


def _literal(node: ast.expr, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _call_keys(
    call: ast.Call, platform: str | None, constants: dict[str, str]
) -> Iterator[str]:
    """Yield the strings.json paths a call refers to."""
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    name = _call_name(call.func)
    if "translation_key" in keywords and (
        key := _literal(keywords["translation_key"], constants)
    ):
        if "translation_domain" in keywords:
            yield f"exceptions.{key}.message"
        elif name == "async_create_issue":
            yield f"issues.{key}"
        elif name in {"DeviceInfo", "async_get_or_create", "async_update_device"}:
            yield f"device.{key}.name"
        elif name.endswith(("Selector", "SelectorConfig")):
            yield f"selector.{key}"
        elif platform is not None:
            yield f"entity.{platform}.{key}"
    if name in {"async_abort", "async_update_reload_and_abort"} and (
        "reason" in keywords and (reason := _literal(keywords["reason"], constants))
    ):
        yield f"config.abort.{reason}"
    if name == "async_show_progress" and (
        "progress_action" in keywords
        and (action := _literal(keywords["progress_action"], constants))
    ):
        yield f"config.progress.{action}"


def code_translation_keys(source: str, platform: str | None) -> list[tuple[int, str]]:
    """Return (line, strings.json path) of every translation key a module uses.

    Keys are found in translation_key= keyword arguments (exceptions when the
    call also passes translation_domain=, repair issues, device names,
    selectors, entity descriptions), _attr_translation_key assignments, abort
    reasons and progress actions. Only literals and module or const.py string
    constants are resolved.
    """
    tree = ast.parse(source)
    constants = {**CONSTANTS, **_string_constants(tree)}
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found.extend(
                (node.lineno, key) for key in _call_keys(node, platform, constants)
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and platform is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {
                target.attr
                if isinstance(target, ast.Attribute)
                else getattr(target, "id", "")
                for target in targets
            }
            if (
                "_attr_translation_key" in names
                and node.value is not None
                and (key := _literal(node.value, constants))
            ):
                found.append((node.lineno, f"entity.{platform}.{key}"))
    return sorted(found)


def _platform(path: Path) -> str | None:
    return path.stem if path.parent == PACKAGE and path.stem in PLATFORMS else None


@pytest.mark.parametrize("path", MODULES, ids=_relative)
def test_code_translation_keys_exist(path: Path) -> None:
    """Every translation key the code uses is defined in strings.json."""
    source = path.read_text(encoding="utf-8")
    missing = [
        f"line {line}: {key}"
        for line, key in code_translation_keys(source, _platform(path))
        if not _defined(key)
        and not _defined(key.replace("config.abort.", "options.abort.", 1))
    ]
    assert missing == []


def test_exception_and_issue_keys_are_used() -> None:
    """Every exception and issue key of strings.json appears in the code."""
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in MODULES]
    literals = {
        node.value
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    defined = {
        ".".join(key.split(".")[:2])
        for key in STRINGS
        if key.startswith(("exceptions.", "issues."))
    }
    unused = sorted(key for key in defined if key.split(".", 1)[1] not in literals)
    assert unused == []


SAMPLE_PLATFORM = """
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

ISSUE = "quota_low"

DESCRIPTIONS = (
    SensorEntityDescription(key="battery", translation_key="battery"),
    SensorEntityDescription(key="ghost", translation_key="no_such_sensor"),
)


class Thing:
    _attr_translation_key = "comfort_level"

    def method(self):
        self._attr_translation_key = "data_updated"
        DeviceInfo(identifiers=set(), translation_key="radiator_valve")
        ir.async_create_issue(hass, "tado_x", "x", translation_key=ISSUE)
        SelectSelectorConfig(options=[], translation_key="manual_termination")
        raise HomeAssistantError(translation_domain="tado_x", translation_key="cannot_connect")
        raise HomeAssistantError(translation_domain="tado_x", translation_key=dynamic())
        self.async_abort(reason="already_configured")
        self.async_show_progress(step_id="user", progress_action="wait_for_device")
"""


def test_code_key_scanner_finds_every_kind() -> None:
    """The scanner sees each way the code refers to strings.json."""
    keys = [key for _, key in code_translation_keys(SAMPLE_PLATFORM, "sensor")]
    assert keys == [
        "entity.sensor.battery",
        "entity.sensor.no_such_sensor",
        "entity.sensor.comfort_level",
        "entity.sensor.data_updated",
        "device.radiator_valve.name",
        "issues.quota_low",
        "selector.manual_termination",
        "exceptions.cannot_connect.message",
        "config.abort.already_configured",
        "config.progress.wait_for_device",
    ]
    assert [key for key in keys if not _defined(key)] == [
        "entity.sensor.no_such_sensor"
    ]
