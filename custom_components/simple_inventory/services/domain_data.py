from __future__ import annotations

from typing import cast

from homeassistant.core import HomeAssistant

from ..const import DOMAIN
from ..coordinator import SimpleInventoryCoordinator
from ..storage.repository import InventoryRepository
from ..types import SimpleInventoryDomainData


def get_domain_data(hass: HomeAssistant) -> SimpleInventoryDomainData | None:
    """Return typed hass.data[DOMAIN] if present."""
    return cast(
        SimpleInventoryDomainData | None,
        cast(object, hass.data.get(DOMAIN)),
    )


def get_coordinators(hass: HomeAssistant) -> dict[str, SimpleInventoryCoordinator]:
    """Return the coordinators mapping (or an empty dict)."""
    domain_data = get_domain_data(hass)
    if domain_data is None:
        return {}
    return domain_data["coordinators"]


def get_repository(hass: HomeAssistant) -> InventoryRepository | None:
    """Return the shared repository if available."""
    domain_data = get_domain_data(hass)
    if domain_data is None:
        return None
    return domain_data["repository"]
