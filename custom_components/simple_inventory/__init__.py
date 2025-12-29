"""Simple Inventory integration."""

from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, SupportsResponse

from .const import (
    DOMAIN,
    SERVICE_ADD_ITEM,
    SERVICE_DECREMENT_ITEM,
    SERVICE_GET_ALL_ITEMS,
    SERVICE_GET_ITEMS,
    SERVICE_INCREMENT_ITEM,
    SERVICE_REMOVE_ITEM,
    SERVICE_UPDATE_ITEM,
)
from .coordinator import SimpleInventoryCoordinator
from .schemas.service_schemas import (
    ADD_ITEM_SCHEMA,
    GET_ALL_ITEMS_SCHEMA,
    GET_ITEMS_SCHEMA,
    QUANTITY_UPDATE_SCHEMA,
    REMOVE_ITEM_SCHEMA,
    UPDATE_ITEM_SCHEMA,
)
from .services import ServiceHandler
from .todo_manager import TodoManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Simple Inventory from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    coordinator: SimpleInventoryCoordinator | None = domain_data.get("coordinator")
    todo_manager: TodoManager | None = domain_data.get("todo_manager")

    if coordinator is None:
        coordinator = SimpleInventoryCoordinator(hass)
        await coordinator.async_initialize()

        todo_manager = TodoManager(hass)
        service_handler = ServiceHandler(hass, coordinator, todo_manager)

        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_ITEM,
            service_handler.async_update_item,
            schema=UPDATE_ITEM_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_ITEM,
            service_handler.async_add_item,
            schema=ADD_ITEM_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_ITEM,
            service_handler.async_remove_item,
            schema=REMOVE_ITEM_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_INCREMENT_ITEM,
            service_handler.async_increment_item,
            schema=QUANTITY_UPDATE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_DECREMENT_ITEM,
            service_handler.async_decrement_item,
            schema=QUANTITY_UPDATE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_ITEMS,
            service_handler.async_get_items,
            schema=GET_ITEMS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_ALL_ITEMS,
            service_handler.async_get_items_from_all_inventories,
            schema=GET_ALL_ITEMS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

        domain_data["coordinator"] = coordinator
        domain_data["todo_manager"] = todo_manager
        domain_data["service_handler"] = service_handler

    else:
        await coordinator.async_initialize()
        todo_manager = domain_data["todo_manager"]

    # Persist inventory metadata in the repository
    await coordinator.async_upsert_inventory_metadata(
        inventory_id=entry.entry_id,
        name=entry.data.get("name", entry.title or entry.entry_id),
        description=entry.data.get("description", ""),
        icon=entry.data.get("icon", ""),
        entry_type=entry.data.get("entry_type", "inventory"),
        metadata=None,
    )

    domain_data[entry.entry_id] = {
        "coordinator": coordinator,
        "todo_manager": todo_manager,
        "config": entry.data,
    }

    if entry.data.get("create_global", False):
        existing_entries = hass.config_entries.async_entries(DOMAIN)
        global_exists = any(
            config_entry.data.get("entry_type") == "global" for config_entry in existing_entries
        )
        if not global_exists:
            await _create_global_entry(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _create_global_entry(hass: HomeAssistant) -> None:
    """Create the global config entry."""
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "internal"},
        data={
            "name": "All Items Expiring Soon",
            "icon": "mdi:calendar-alert",
            "description": "Tracks expiring items across all inventories",
            "entry_type": "global",
        },
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain_data = hass.data.get(DOMAIN)
    if not domain_data:
        return True

    domain_data.pop(entry.entry_id, None)

    remaining_entries = [
        entry_id
        for entry_id in domain_data
        if entry_id not in {"coordinator", "todo_manager", "service_handler"}
    ]

    if not remaining_entries:
        for service in (
            SERVICE_ADD_ITEM,
            SERVICE_DECREMENT_ITEM,
            SERVICE_INCREMENT_ITEM,
            SERVICE_REMOVE_ITEM,
            SERVICE_UPDATE_ITEM,
            SERVICE_GET_ITEMS,
            SERVICE_GET_ALL_ITEMS,
        ):
            hass.services.async_remove(DOMAIN, service)

        coordinator: SimpleInventoryCoordinator | None = domain_data.get("coordinator")
        if coordinator:
            await coordinator.repository.async_close()

        hass.data.pop(DOMAIN, None)

    return True


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the component via YAML (legacy support)."""
    return True
