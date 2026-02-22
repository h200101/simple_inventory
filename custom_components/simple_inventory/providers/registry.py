"""Provider registry for barcode lookup."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .base import BarcodeProvider
from .openfoodfacts import OpenFoodFactsProvider

PROVIDER_REGISTRY: dict[str, type[BarcodeProvider]] = {
    "openfoodfacts": OpenFoodFactsProvider,
}

DEFAULT_PROVIDER = "openfoodfacts"


def create_provider(hass: HomeAssistant, provider_name: str | None = None) -> BarcodeProvider:
    """Create a barcode provider instance by name."""
    name = provider_name or DEFAULT_PROVIDER
    provider_cls = PROVIDER_REGISTRY.get(name)
    if provider_cls is None:
        raise ValueError(f"Unknown barcode provider: {name!r}")
    return provider_cls(hass)
