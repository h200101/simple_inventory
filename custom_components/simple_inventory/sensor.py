"""Sensor platform for Simple Inventory."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SimpleInventoryCoordinator
from .sensors import (
    ExpiryNotificationSensor,
    GlobalExpiryNotificationSensor,
    InventorySensor,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors based on config entry type."""
    domain_data = hass.data[DOMAIN]
    coordinator: SimpleInventoryCoordinator = domain_data["coordinator"]

    entry_type = config_entry.data.get("entry_type", "inventory")

    if entry_type == "global":
        async_add_entities([GlobalExpiryNotificationSensor(hass, coordinator)])
        return

    inventory_name = config_entry.data.get("name", "Inventory")
    icon = config_entry.data.get("icon", "mdi:package-variant")
    entry_id = config_entry.entry_id

    sensors = [
        InventorySensor(hass, coordinator, inventory_name, icon, entry_id),
        ExpiryNotificationSensor(hass, coordinator, entry_id, inventory_name),
    ]
    async_add_entities(sensors)
