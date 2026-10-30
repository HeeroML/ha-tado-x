"""The documentation states what the release and Home Assistant really do."""

from pathlib import Path

import yaml

from homeassistant import config_entries

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"


def _section(path: Path, heading: str) -> str:
    """Return the text under a heading up to the next heading of its level."""
    level = heading.split(" ", 1)[0]
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index(heading) + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].startswith("#")
            and len(lines[index].split(" ", 1)[0]) <= len(level)
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_manual_install_matches_the_zip_layout() -> None:
    """tado_x.zip holds the integration files at its top level, not a folder."""
    release = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    )
    [build] = [
        step
        for job in release["jobs"].values()
        for step in job.get("steps", [])
        if "tado_x.zip" in step.get("run", "") and "zip -r" in step["run"]
    ]
    assert build["working-directory"] == "custom_components/tado_x"
    assert 'zip -r -X "$GITHUB_WORKSPACE/tado_x.zip" . ' in build["run"]

    manual = _section(README, "### Manual")
    assert "extract the archive into it" in manual
    assert "`config/custom_components/tado_x/manifest.json`" in manual
    assert "Copy `custom_components/tado_x`" not in manual


def test_token_saving_is_described_as_home_assistant_does_it() -> None:
    """A rotated token is in the config entry at once and on disk about 1 s later.

    No document may claim the token reaches the disk before it is used.
    """
    assert config_entries.SAVE_DELAY == 1
    for path in (README, ROOT / "docs" / "ARCHITECTURE.md", ROOT / "CHANGELOG.md"):
        text = path.read_text(encoding="utf-8")
        assert "saved before it is used" not in text, path.name
        assert "saved to the store before" not in text, path.name
        assert "flushes it to disk about 1 s later" in text, path.name
