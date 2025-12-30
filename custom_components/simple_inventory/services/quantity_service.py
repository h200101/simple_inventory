"""Quantity management service handler."""

import logging
from typing import Awaitable, Callable, Literal, cast

from homeassistant.core import HomeAssistant, ServiceCall

from ..const import DOMAIN
from ..coordinator import SimpleInventoryCoordinator
from ..todo_manager import TodoManager
from ..types import InventoryItem
from .base_service import BaseServiceHandler

_LOGGER = logging.getLogger(__name__)


class QuantityService(BaseServiceHandler):
    """Handle quantity operations (increment, decrement)."""

    def __init__(
        self,
        hass: HomeAssistant,
        todo_manager: TodoManager,
    ) -> None:
        """Initialize quantity service with todo manager."""
        super().__init__(hass)
        self.todo_manager = todo_manager

    # ------------------------------------------------------------------
    # Coordinator helpers
    # ------------------------------------------------------------------

    def _get_coordinator_optional(self, inventory_id: str) -> SimpleInventoryCoordinator | None:
        domain_data = self.hass.data.get(DOMAIN)
        if not domain_data:
            return None
        coordinators = domain_data.get("coordinators", {})
        return coordinators.get(inventory_id)

    def _require_coordinator(self, inventory_id: str) -> SimpleInventoryCoordinator | None:
        coordinator = self._get_coordinator_optional(inventory_id)
        if coordinator is None:
            _LOGGER.error(
                "No coordinator loaded for inventory '%s'; cannot process quantity change",
                inventory_id,
            )
        return coordinator

    # ------------------------------------------------------------------
    # Core handlers
    # ------------------------------------------------------------------

    async def _handle_quantity_change(
        self,
        call: ServiceCall,
        operation: Literal["increment", "decrement"],
        coordinator_method: Callable[
            [SimpleInventoryCoordinator, str, str, int],
            Awaitable[bool],
        ],
        todo_method: Callable[[str, InventoryItem], Awaitable[bool]],
    ) -> None:
        inventory_id, name = self._get_inventory_and_name(call)
        amount = call.data.get("amount", 1)

        coordinator = self._require_coordinator(inventory_id)
        if coordinator is None:
            return

        try:
            if await coordinator_method(coordinator, inventory_id, name, amount):
                item_data = await coordinator.async_get_item(inventory_id, name)
                if item_data:
                    await todo_method(name, cast(InventoryItem, item_data))

                await self._save_and_log_success(
                    coordinator,
                    inventory_id,
                    f"{operation.capitalize()}ed {name} by {amount}",
                    name,
                )
            else:
                self._log_item_not_found(
                    f"{operation.capitalize()} item",
                    name,
                    inventory_id,
                )

        except Exception as exc:
            _LOGGER.error(
                "Failed to %s item %s in inventory %s: %s",
                operation,
                name,
                inventory_id,
                exc,
            )

    async def async_increment_item(self, call: ServiceCall) -> None:
        await self._handle_quantity_change(
            call,
            "increment",
            lambda coordinator, inv_id, item_name, amt: coordinator.async_increment_item(
                inv_id, item_name, amt
            ),
            self.todo_manager.check_and_remove_item,
        )

    async def async_decrement_item(self, call: ServiceCall) -> None:
        await self._handle_quantity_change(
            call,
            "decrement",
            lambda coordinator, inv_id, item_name, amt: coordinator.async_decrement_item(
                inv_id, item_name, amt
            ),
            self.todo_manager.check_and_add_item,
        )

    async def async_update_todo_status(self, item_name: str, item_data: InventoryItem) -> None:
        """Update todo list status based on current quantity (manual sync hook)."""
        if not item_data:
            return

        quantity = item_data.get("quantity", 0)
        auto_add_quantity = item_data.get("auto_add_to_list_quantity", 0)

        if quantity <= auto_add_quantity:
            await self.todo_manager.check_and_add_item(item_name, item_data)
        else:
            await self.todo_manager.check_and_remove_item(item_name, item_data)
