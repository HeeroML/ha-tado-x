#!/usr/bin/env python3
"""Generate translations/en.json of the tado_x integration from strings.json.

Custom integrations do not get the translation build of Home Assistant core,
so the English file is generated here: the content of strings.json with sorted
keys, two-space indentation and a trailing newline. The other languages are
rewritten in the same layout; their text is never changed.

    python3 scripts/gen_translations.py           # write the translation files
    python3 scripts/gen_translations.py --check   # exit 1 if a file is out of date
    python3 scripts/gen_translations.py --report  # list untranslated keys per language
"""

import argparse
from collections.abc import Iterator, Mapping
import json
from pathlib import Path
import sys
from typing import Any

INTEGRATION = Path(__file__).resolve().parent.parent / "custom_components" / "tado_x"
STRINGS = INTEGRATION / "strings.json"
TRANSLATIONS = INTEGRATION / "translations"
EN = TRANSLATIONS / "en.json"


def render(tree: object) -> str:
    """Return the canonical JSON text of a translation tree."""
    return json.dumps(tree, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load(path: Path) -> dict[str, Any]:
    """Return the translation tree stored in a JSON file."""
    tree: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return tree


def flatten(tree: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    """Return the leaves of a translation tree keyed by their dotted path."""
    leaves: dict[str, str] = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            leaves.update(flatten(value, f"{path}."))
        else:
            leaves[path] = value
    return leaves


def expected_files() -> dict[Path, str]:
    """Return the canonical text of every translation file."""
    expected = {EN: render(load(STRINGS))}
    for path in sorted(TRANSLATIONS.glob("*.json")):
        if path != EN:
            expected[path] = render(load(path))
    return expected


def _outdated() -> Iterator[tuple[Path, str]]:
    for path, text in expected_files().items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            yield path, text


def report() -> str:
    """Return the translation coverage of every language other than English."""
    english = flatten(load(STRINGS))
    lines = []
    for path in sorted(TRANSLATIONS.glob("*.json")):
        if path == EN:
            continue
        missing = sorted(english.keys() - flatten(load(path)).keys())
        done = len(english) - len(missing)
        lines.append(f"{path.stem}: {done}/{len(english)} keys translated")
        lines.extend(f"  missing {key}" for key in missing)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Write, check or report the translation files."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 when a file differs from its canonical form",
    )
    mode.add_argument(
        "--report",
        action="store_true",
        help="list the keys each language falls back to English for",
    )
    args = parser.parse_args(argv)
    if args.report:
        print(report())
        return 0
    outdated = list(_outdated())
    if args.check:
        for path, _ in outdated:
            print(f"{path} is out of date; run scripts/gen_translations.py")
        return 1 if outdated else 0
    for path, text in outdated:
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
