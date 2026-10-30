#!/usr/bin/env python3
"""Check a release tag before the release workflow publishes it.

The tag must equal the manifest version (a leading "v" aside), hacs.json
must keep the minimum Home Assistant version, and CHANGELOG.md must have a
section for the version. That section becomes the release notes.

    python3 scripts/release_check.py v1.9.0 --notes release_notes.md

Standard library only.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
MIN_HOME_ASSISTANT = "2026.10.0"


def release_notes(changelog: str, version: str) -> str:
    """Return the text of a version's CHANGELOG.md section without its heading.

    The heading is "## <version>", optionally in brackets and followed by
    more text such as a date; the section ends at the next "## " heading.
    """
    notes: list[str] | None = None
    for line in changelog.splitlines():
        if line.startswith("## "):
            if notes is not None:
                break
            words = line[3:].split()
            if words and words[0].strip("[]") == version:
                notes = []
        elif notes is not None:
            notes.append(line)
    return "\n".join(notes or []).strip()


def release_errors(tag: str, root: Path = ROOT) -> list[str]:
    """Return what keeps a tag from being released; empty when nothing does."""
    manifest = root / "custom_components" / "tado_x" / "manifest.json"
    version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    hacs = json.loads((root / "hacs.json").read_text(encoding="utf-8"))
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    errors = []
    if tag.removeprefix("v") != version:
        errors.append(f"tag {tag} does not match manifest version {version}")
    if (minimum := hacs.get("homeassistant")) != MIN_HOME_ASSISTANT:
        errors.append(
            f"hacs.json homeassistant is {minimum}, expected {MIN_HOME_ASSISTANT}"
        )
    if not release_notes(changelog, version):
        errors.append(f"CHANGELOG.md has no section '## {version}' with text")
    return errors


def main(argv: list[str] | None = None) -> int:
    """Check the tag, write the release notes and return the exit code."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("tag", help="the release tag, for example v1.9.0")
    parser.add_argument("--notes", type=Path, help="write the release notes here")
    args = parser.parse_args(argv)
    if errors := release_errors(args.tag):
        for error in errors:
            print(f"::error::{error}")
        return 1
    version = args.tag.removeprefix("v")
    if args.notes is not None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        args.notes.write_text(
            release_notes(changelog, version) + "\n", encoding="utf-8"
        )
    print(f"{args.tag} matches manifest version {version} and has release notes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
