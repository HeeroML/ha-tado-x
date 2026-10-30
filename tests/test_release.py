"""Tests of the release workflow and scripts/release_check.py."""

import importlib.util
import json
from pathlib import Path
import re
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_check", ROOT / "scripts" / "release_check.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_check = _load_script()


def _workflow(name: str) -> dict[Any, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _steps_running(workflow: dict[Any, Any], command: str) -> list[tuple[str, Any]]:
    return [
        (job_id, step)
        for job_id, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if command in step.get("run", "")
    ]


def test_release_is_created_only_after_the_checks_pass() -> None:
    """A tag push runs version check, lint and tests before any release exists."""
    workflow = _workflow("release.yml")
    triggers = _triggers(workflow)
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*"]}
    assert workflow["permissions"] == {"contents": "read"}

    jobs = workflow["jobs"]
    assert jobs["tests"]["uses"] == "./.github/workflows/tests.yml"
    assert jobs["tests"]["needs"] == "version"
    assert "workflow_call" in _triggers(_workflow("tests.yml"))
    checks = _steps_running(workflow, "scripts/release_check.py")
    assert "version" in {job_id for job_id, _ in checks}

    [(job_id, publish)] = _steps_running(workflow, "gh release create")
    assert set(jobs[job_id]["needs"]) == {"version", "tests"}
    assert jobs[job_id]["permissions"] == {"contents": "write"}
    assert publish["if"] == "github.event_name == 'push'"
    assert "tado_x.zip" in publish["run"]
    assert "--notes-file" in publish["run"]
    assert "--verify-tag" in publish["run"]
    assert not _steps_running(workflow, "gh release upload")


def test_the_current_version_can_be_released() -> None:
    """Manifest, hacs.json and CHANGELOG.md agree on the version being built."""
    manifest = ROOT / "custom_components" / "tado_x" / "manifest.json"
    version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    assert release_check.release_errors(f"v{version}") == []
    assert release_check.release_errors(version) == []


def _repo(tmp_path: Path, *, version: str, changelog: str, minimum: str) -> Path:
    integration = tmp_path / "custom_components" / "tado_x"
    integration.mkdir(parents=True)
    (integration / "manifest.json").write_text(
        json.dumps({"domain": "tado_x", "version": version}), encoding="utf-8"
    )
    (tmp_path / "hacs.json").write_text(
        json.dumps({"name": "Tado X", "homeassistant": minimum}), encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return tmp_path


CHANGELOG = """# Changelog

## 2.0.0

Something new.

### Fixed issues

- one

## 1.9.0

The rewrite.
"""


@pytest.mark.parametrize(
    ("tag", "version", "changelog", "minimum", "error"),
    [
        ("v2.0.1", "2.0.0", CHANGELOG, "2026.10.0", "does not match manifest"),
        ("v2.1.0", "2.1.0", CHANGELOG, "2026.10.0", "no section '## 2.1.0'"),
        ("v2.0.0", "2.0.0", "## 2.0.0\n\n## 1.9.0\n", "2026.10.0", "no section"),
        ("v2.0.0", "2.0.0", CHANGELOG, "2025.1.0", "hacs.json homeassistant"),
    ],
)
def test_release_errors(
    tmp_path: Path, tag: str, version: str, changelog: str, minimum: str, error: str
) -> None:
    """A wrong tag, a missing changelog section or a changed minimum blocks it."""
    root = _repo(tmp_path, version=version, changelog=changelog, minimum=minimum)
    [message] = release_check.release_errors(tag, root)
    assert error in message


@pytest.mark.parametrize("heading", ["## 2.0.0", "## [2.0.0]", "## 2.0.0 (2026-11-01)"])
def test_release_notes_are_the_version_section(heading: str) -> None:
    """The notes hold the version's section up to the next version heading."""
    changelog = CHANGELOG.replace("## 2.0.0", heading)
    notes = release_check.release_notes(changelog, "2.0.0")
    assert notes == "Something new.\n\n### Fixed issues\n\n- one"
    assert release_check.release_notes(changelog, "2.0") == ""


def test_main_writes_the_notes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workflow passes the notes file on to gh release create."""
    notes = tmp_path / "notes.md"
    assert release_check.main(["v1.9.0", "--notes", str(notes)]) == 0
    text = notes.read_text(encoding="utf-8")
    assert text.startswith("A rewrite of v1.8.11.")
    assert "### Breaking changes" in text
    assert not re.search(r"^## ", text, re.MULTILINE)
    assert "v1.9.0" in capsys.readouterr().out


def test_main_reports_errors_for_github(capsys: pytest.CaptureFixture[str]) -> None:
    """A failed check exits non-zero with GitHub error annotations."""
    assert release_check.main(["v0.0.1"]) == 1
    assert "::error::tag v0.0.1 does not match manifest version" in (
        capsys.readouterr().out
    )
