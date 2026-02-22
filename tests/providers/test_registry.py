"""Tests for the barcode provider registry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.simple_inventory.providers.openfoodfacts import OpenFoodFactsProvider
from custom_components.simple_inventory.providers.registry import (
    DEFAULT_PROVIDER,
    create_provider,
)


@pytest.fixture
def hass_mock() -> MagicMock:
    return MagicMock()


class TestCreateProvider:
    def test_creates_openfoodfacts_provider(self, hass_mock: MagicMock) -> None:
        provider = create_provider(hass_mock, "openfoodfacts")
        assert isinstance(provider, OpenFoodFactsProvider)

    def test_default_provider(self, hass_mock: MagicMock) -> None:
        provider = create_provider(hass_mock)
        assert isinstance(provider, OpenFoodFactsProvider)

    def test_default_provider_constant(self) -> None:
        assert DEFAULT_PROVIDER == "openfoodfacts"

    def test_unknown_provider_raises(self, hass_mock: MagicMock) -> None:
        with pytest.raises(ValueError, match="Unknown barcode provider"):
            create_provider(hass_mock, "nonexistent")

    def test_none_uses_default(self, hass_mock: MagicMock) -> None:
        provider = create_provider(hass_mock, None)
        assert isinstance(provider, OpenFoodFactsProvider)
