"""Fixtures for Tado X tests."""
import pytest

from custom_components.tado_x.const import DOMAIN  # noqa: F401  (imports this repo's package before HA mounts its test config dir)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    yield
