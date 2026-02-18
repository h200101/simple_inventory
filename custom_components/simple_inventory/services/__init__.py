"""Service handlers for Simple Inventory integration."""

import logging
from typing import cast

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util.json import JsonObjectType

from ..const import DOMAIN
from ..todo_manager import TodoManager
from .domain_data import get_coordinators
from .inventory_service import InventoryService
from .quantity_service import QuantityService

_LOGGER = logging.getLogger(__name__)


class ServiceHandler:
    """Main service handler that coordinates specialized service handlers."""

    def __init__(
        self,
        hass: HomeAssistant,
        todo_manager: TodoManager,
    ):
        """Initialize the main service handler."""
        self.hass = hass
        self.todo_manager = todo_manager
        self.inventory_service = InventoryService(hass, todo_manager)
        self.quantity_service = QuantityService(hass, todo_manager)

    async def async_add_item(self, call: ServiceCall) -> None:
        """Add an item to the inventory."""
        await self.inventory_service.async_add_item(call)

    async def async_remove_item(self, call: ServiceCall) -> None:
        """Remove an item from the inventory."""
        await self.inventory_service.async_remove_item(call)

    async def async_update_item(self, call: ServiceCall) -> None:
        """Update an existing item with new values."""
        await self.inventory_service.async_update_item(call)

    async def async_increment_item(self, call: ServiceCall) -> None:
        """Increment item quantity."""
        await self.quantity_service.async_increment_item(call)

    async def async_decrement_item(self, call: ServiceCall) -> None:
        """Decrement item quantity."""
        await self.quantity_service.async_decrement_item(call)

    async def async_get_items(self, call: ServiceCall) -> JsonObjectType:
        """Fetch items, fire an event, and return the result."""
        result = await self.inventory_service.async_get_items(call)
        self.hass.bus.async_fire(
            f"{DOMAIN}_get_items_result",
            {
                "context_id": call.context.id,
                "inventory_id": call.data.get("inventory_id"),
                "inventory_name": call.data.get("inventory_name"),
                "result": result,
            },
        )
        return result

    async def async_get_items_from_all_inventories(self, call: ServiceCall) -> JsonObjectType:
        """Fetch all inventories, fire an event, and return the result."""
        result = await self.inventory_service.async_get_items_from_all_inventories(call)
        self.hass.bus.async_fire(
            f"{DOMAIN}_get_all_items_result",
            {
                "context_id": call.context.id,
                "result": result,
            },
        )
        return result

    async def async_get_item_consumption_rates(self, call: ServiceCall) -> JsonObjectType:
        """Return consumption rates for a single item."""
        data = call.data
        inventory_id: str = data["inventory_id"]
        item_name: str = data["name"]
        window_days: int | None = data.get("window_days")

        coordinators = get_coordinators(self.hass)
        coordinator = coordinators.get(inventory_id)
        if coordinator is None:
            raise ValueError(f"No coordinator available for inventory '{inventory_id}'")

        result = await coordinator.async_get_item_consumption_rates(
            inventory_id, item_name, window_days=window_days
        )
        if result is None:
            raise ValueError(f"Item '{item_name}' not found in inventory '{inventory_id}'")
        return cast(JsonObjectType, result)


__all__ = ["ServiceHandler", "InventoryService", "QuantityService"]
