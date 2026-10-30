"""Every interface stub of the foundation has been replaced by its implementation."""

from pathlib import Path

PACKAGE = Path(__file__).parent.parent / "custom_components" / "tado_x"


def test_no_module_raises_not_implemented() -> None:
    """No module of the integration still contains a stub body."""
    stubs = sorted(
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if "raise NotImplementedError" in path.read_text(encoding="utf-8")
    )
    assert stubs == []
