"""Custom intent handlers for Simple Inventory."""

from __future__ import annotations

from typing import Any, cast

import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

from .const import DOMAIN
from .coordinator import SimpleInventoryCoordinator

INTENT_GET_QUANTITY = "SimpleInventoryGetQuantity"
INTENT_ADD_ITEM = "SimpleInventoryAddItem"
INTENT_REMOVE_ITEM = "SimpleInventoryRemoveItem"
INTENT_INCREMENT_ITEM = "SimpleInventoryIncrementItem"
INTENT_EXPIRING_SOON = "SimpleInventoryExpiringSoon"
INTENT_HANDLERS: list[intent.IntentHandler] = []

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_inventory_id(hass: HomeAssistant, inventory_name: str | None) -> str | None:
    """Find an inventory entry_id by friendly name (case-insensitive)."""
    if not inventory_name:
        return None

    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get("name", "").lower() == inventory_name.lower():
            return entry.entry_id
    return None


async def _get_default_inventory_id(hass: HomeAssistant) -> str | None:
    """Fallback to the first non-global inventory."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get("entry_type") != "global":
            return entry.entry_id
    return None


def _get_string_slot(slots: dict[str, Any], slot_name: str) -> str | None:
    """Safely extract a string slot value."""
    slot = slots.get(slot_name)
    if not slot:
        return None

    value = slot.get("value") if isinstance(slot, dict) else getattr(slot, "value", None)
    if isinstance(value, dict):
        value = value.get("value")
    return str(value).strip() if value else None


def _get_number_slot(slots: dict[str, Any], slot_name: str, default: int = 1) -> int:
    """Safely extract a numeric slot value."""
    slot = slots.get(slot_name)
    if not slot:
        return default

    value = slot.get("value") if isinstance(slot, dict) else getattr(slot, "value", None)
    if isinstance(value, dict):
        value = value.get("number", value.get("value"))

    if value is None:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Base handler
# ---------------------------------------------------------------------------


class _BaseInventoryIntentHandler(intent.IntentHandler):
    """Base helper providing coordinator access."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__()
        self.hass = hass

    @property
    def coordinator(self) -> SimpleInventoryCoordinator:
        return cast(SimpleInventoryCoordinator, self.hass.data[DOMAIN]["coordinator"])

    async def _resolve_inventory(self, inventory_name: str | None) -> str | None:
        inventory_id = _resolve_inventory_id(self.hass, inventory_name)
        if inventory_id:
            return inventory_id
        return await _get_default_inventory_id(self.hass)


# ---------------------------------------------------------------------------
# Intent implementations
# ---------------------------------------------------------------------------


class SimpleInventoryGetQuantityHandler(_BaseInventoryIntentHandler):
    intent_type = INTENT_GET_QUANTITY
    description = "Returns the quantity of an item in an inventory"
    slot_schema = {
        "item_name": cv.string,
        str("inventory_name"): cv.string,
    }
    platforms = {DOMAIN}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)

        item_name = _get_string_slot(slots, "item_name")
        inv_name = _get_string_slot(slots, "inventory_name")
        inventory_id = await self._resolve_inventory(inv_name)

        if not item_name:
            raise intent.IntentHandleError("An item name is required.")
        if not inventory_id:
            raise intent.IntentHandleError("No inventory available to query.")

        item = await self.coordinator.async_get_item(inventory_id, item_name)
        response = intent_obj.create_response()

        if not item:
            response.async_set_speech(f"I couldn’t find {item_name} in that inventory.")
        else:
            quantity = item.get("quantity", 0)
            unit = item.get("unit", "")
            speech = f"You have {quantity} {unit} of {item_name}".strip()
            response.async_set_speech(speech)

        return response


class SimpleInventoryAddItemHandler(_BaseInventoryIntentHandler):
    intent_type = INTENT_ADD_ITEM
    description = "Adds an item to an inventory"
    slot_schema = {
        "item_name": cv.string,
        str("inventory_name"): cv.string,
        str("unit"): cv.string,
        str("quantity"): cv.positive_int,
    }
    platforms = {DOMAIN}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)

        item_name = _get_string_slot(slots, "item_name")
        inv_name = _get_string_slot(slots, "inventory_name")
        unit = _get_string_slot(slots, "unit") or ""
        quantity = _get_number_slot(slots, "quantity", default=1)

        inventory_id = await self._resolve_inventory(inv_name)

        if not item_name:
            raise intent.IntentHandleError("An item name is required.")
        if not inventory_id:
            raise intent.IntentHandleError("No inventory available to add items.")

        await self.coordinator.async_add_item(
            inventory_id,
            name=item_name,
            quantity=quantity,
            unit=unit,
        )

        response = intent_obj.create_response()
        response.async_set_speech(f"Added {quantity} {unit} of {item_name}.")
        return response


class SimpleInventoryRemoveItemHandler(_BaseInventoryIntentHandler):
    intent_type = INTENT_REMOVE_ITEM
    description = "Removes an item from an inventory"
    slot_schema = {
        "item_name": cv.string,
        str("inventory_name"): cv.string,
    }
    platforms = {DOMAIN}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)

        item_name = _get_string_slot(slots, "item_name")
        inv_name = _get_string_slot(slots, "inventory_name")
        inventory_id = await self._resolve_inventory(inv_name)

        if not item_name:
            raise intent.IntentHandleError("An item name is required.")
        if not inventory_id:
            raise intent.IntentHandleError("No inventory available to remove items.")

        removed = await self.coordinator.async_remove_item(inventory_id, item_name)

        response = intent_obj.create_response()
        if removed:
            response.async_set_speech(f"Removed {item_name}.")
        else:
            response.async_set_speech(f"I couldn’t find {item_name} to remove.")

        return response


class SimpleInventoryIncrementItemHandler(_BaseInventoryIntentHandler):
    intent_type = INTENT_INCREMENT_ITEM
    description = "Increases the quantity of an item in an inventory"
    slot_schema = {
        "item_name": cv.string,
        str("inventory_name"): cv.string,
        str("amount"): cv.positive_int,
    }
    platforms = {DOMAIN}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)

        item_name = _get_string_slot(slots, "item_name")
        inv_name = _get_string_slot(slots, "inventory_name")
        amount = _get_number_slot(slots, "amount", default=1)

        inventory_id = await self._resolve_inventory(inv_name)

        if not item_name:
            raise intent.IntentHandleError("An item name is required.")
        if not inventory_id:
            raise intent.IntentHandleError("No inventory available to update items.")

        await self.coordinator.async_increment_item(inventory_id, item_name, amount)

        updated_item = await self.coordinator.async_get_item(inventory_id, item_name)
        quantity = updated_item.get("quantity", "unknown") if updated_item else "unknown"

        response = intent_obj.create_response()
        response.async_set_speech(f"{item_name} is now at {quantity}.")
        return response


class SimpleInventoryExpiringSoonHandler(_BaseInventoryIntentHandler):
    intent_type = INTENT_EXPIRING_SOON
    description = "Lists items expiring soon"
    slot_schema = {
        str("inventory_name"): cv.string,
    }
    platforms = {DOMAIN}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)

        inv_name = _get_string_slot(slots, "inventory_name")
        inventory_id = await self._resolve_inventory(inv_name)

        items = await self.coordinator.async_get_items_expiring_soon(inventory_id)
        response = intent_obj.create_response()

        if not items:
            response.async_set_speech("No items are expiring soon.")
            return response

        top = items[:3]
        summary = ", ".join(f"{item['name']} in {item['days_until_expiry']} days" for item in top)
        response.async_set_speech(f"{len(items)} items expiring soon. Next up: {summary}.")

        return response


async def async_setup_intents(hass: HomeAssistant) -> None:
    """Register Simple Inventory intents with Home Assistant."""
    if INTENT_HANDLERS:
        return  # already registered in this runtime

    handlers: list[intent.IntentHandler] = [
        SimpleInventoryGetQuantityHandler(hass),
        SimpleInventoryAddItemHandler(hass),
        SimpleInventoryRemoveItemHandler(hass),
        SimpleInventoryIncrementItemHandler(hass),
        SimpleInventoryExpiringSoonHandler(hass),
    ]

    for handler in handlers:
        intent.async_register(hass, handler)

    INTENT_HANDLERS.extend(handlers)


async def async_unload_intents(hass: HomeAssistant) -> None:
    """Remove registered intents (call when the integration fully unloads)."""
    global INTENT_HANDLERS
    for handler in INTENT_HANDLERS:
        intent.async_remove(hass, handler.intent_type)
    INTENT_HANDLERS = []
