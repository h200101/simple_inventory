"""Quantity management service handler."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Literal, cast

from homeassistant.core import HomeAssistant, ServiceCall

from ..coordinator import SimpleInventoryCoordinator
from ..todo_manager import TodoManager
from ..types import InventoryItem
from .base_service import BaseServiceHandler
from .domain_data import get_coordinators

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
        return get_coordinators(self.hass).get(inventory_id)

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
            [SimpleInventoryCoordinator, str, str | None, float, str | None],
            Awaitable[bool],
        ],
        todo_method: Callable[[str, InventoryItem], Awaitable[bool]],
    ) -> None:
        inventory_id, name, barcode = self._get_inventory_name_barcode(call)
        amount = float(call.data.get("amount", 1))
        display_name = name or barcode or "unknown"

        coordinator = self._require_coordinator(inventory_id)
        if coordinator is None:
            return

        try:
            if await coordinator_method(coordinator, inventory_id, name, amount, barcode):
                resolved_name = name
                if not resolved_name and barcode:
                    item_by_bc = await coordinator.repository.get_item_by_barcode(
                        inventory_id, barcode
                    )
                    if item_by_bc:
                        resolved_name = item_by_bc.get("name")

                if resolved_name:
                    item_data = await coordinator.async_get_item(inventory_id, resolved_name)
                    if item_data:
                        await todo_method(resolved_name, cast(InventoryItem, item_data))

                await self._save_and_log_success(
                    coordinator,
                    inventory_id,
                    f"{operation.capitalize()}ed {display_name} by {amount}",
                    display_name,
                )
            else:
                self._log_item_not_found(
                    f"{operation.capitalize()} item",
                    display_name,
                    inventory_id,
                )

        except Exception as exc:
            _LOGGER.error(
                "Failed to %s item %s in inventory %s: %s",
                operation,
                display_name,
                inventory_id,
                exc,
            )

    async def async_increment_item(self, call: ServiceCall) -> None:
        await self._handle_quantity_change(
            call,
            "increment",
            lambda coordinator, inv_id, item_name, amt, bc: (
                coordinator.async_increment_item(inv_id, item_name, amt, barcode=bc)
            ),
            self.todo_manager.check_and_remove_item,
        )

    async def async_decrement_item(self, call: ServiceCall) -> None:
        await self._handle_quantity_change(
            call,
            "decrement",
            lambda coordinator, inv_id, item_name, amt, bc: (
                coordinator.async_decrement_item(inv_id, item_name, amt, barcode=bc)
            ),
            self.todo_manager.check_and_add_item,
        )

    async def async_update_todo_status(self, item_name: str, item_data: InventoryItem) -> None:
        """Update todo list status based on current quantity (manual sync hook)."""
        quantity = item_data.get("quantity", 0)
        auto_add_quantity = item_data.get("auto_add_to_list_quantity", 0)

        if quantity <= auto_add_quantity:
            await self.todo_manager.check_and_add_item(item_name, item_data)
        else:
            await self.todo_manager.check_and_remove_item(item_name, item_data)
