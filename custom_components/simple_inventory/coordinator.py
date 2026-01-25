"""Data coordinator for Simple Inventory integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    DEFAULT_AUTO_ADD_ENABLED,
    DEFAULT_AUTO_ADD_TO_LIST_QUANTITY,
    DEFAULT_CATEGORY,
    DEFAULT_EXPIRY_ALERT_DAYS,
    DEFAULT_EXPIRY_DATE,
    DEFAULT_LOCATION,
    DEFAULT_QUANTITY,
    DEFAULT_TODO_LIST,
    DEFAULT_UNIT,
    DOMAIN,
    FIELD_AUTO_ADD_ENABLED,
    FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED,
    FIELD_AUTO_ADD_TO_LIST_QUANTITY,
    FIELD_CATEGORY,
    FIELD_DESCRIPTION,
    FIELD_EXPIRY_ALERT_DAYS,
    FIELD_EXPIRY_DATE,
    FIELD_LOCATION,
    FIELD_NAME,
    FIELD_QUANTITY,
    FIELD_TODO_LIST,
    FIELD_UNIT,
)
from .storage.repository import InventoryRepository

_LOGGER = logging.getLogger(__name__)


class SimpleInventoryCoordinator:
    """Facade around the SQLite repository with HA signaling."""

    _INTEGER_FIELDS = {
        FIELD_QUANTITY,
        FIELD_AUTO_ADD_TO_LIST_QUANTITY,
        FIELD_EXPIRY_ALERT_DAYS,
    }
    _BOOLEAN_FIELDS = {
        FIELD_AUTO_ADD_ENABLED,
        FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED,
    }
    _STRING_FIELDS = {
        FIELD_UNIT,
        FIELD_CATEGORY,
        FIELD_DESCRIPTION,
        FIELD_EXPIRY_DATE,
        FIELD_TODO_LIST,
        FIELD_LOCATION,
    }

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        repository: InventoryRepository,
    ) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.entry = entry
        self.repository = repository
        self._listeners: list[Callable[[], None]] = []
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def async_initialize(self) -> None:
        """Perform per-entry init (repository already opened)."""
        async with self._init_lock:
            if self._initialized:
                return
            _LOGGER.debug("Coordinator ready for %s", self.entry.entry_id)
            self._initialized = True

    async def async_save_data(self, inventory_id: str | None = None) -> None:
        """Compatibility shim for legacy callers (fire update signals)."""
        await self.async_initialize()
        await self._fire_update_events(inventory_id)

    async def async_upsert_inventory_metadata(
        self,
        inventory_id: str,
        name: str,
        description: str = "",
        icon: str = "",
        entry_type: str = "",
        metadata: str | None = None,
    ) -> None:
        """Ensure an inventory row exists (called from config entry setup)."""
        await self.async_initialize()
        await self.repository.upsert_inventory(
            inventory_id, name, description, icon, entry_type, metadata
        )
        await self._fire_update_events(inventory_id)

    async def async_add_item(self, inventory_id: str, **kwargs: Any) -> str | None:
        """Add a new item to an inventory."""
        await self.async_initialize()

        name = kwargs.get(FIELD_NAME)
        cleaned_name = self._validate_and_clean_name(str(name) if name else "", "add", inventory_id)
        quantity = max(0, int(kwargs.get(FIELD_QUANTITY, DEFAULT_QUANTITY)))

        auto_add_quantity = max(
            0,
            int(kwargs.get(FIELD_AUTO_ADD_TO_LIST_QUANTITY, DEFAULT_AUTO_ADD_TO_LIST_QUANTITY)),
        )
        auto_add_enabled = bool(kwargs.get(FIELD_AUTO_ADD_ENABLED, DEFAULT_AUTO_ADD_ENABLED))
        auto_add_id_enabled = bool(kwargs.get(FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED, False))
        todo_list = kwargs.get(FIELD_TODO_LIST, DEFAULT_TODO_LIST)

        if not self._validate_auto_add_config(
            cleaned_name, inventory_id, auto_add_enabled, auto_add_quantity, todo_list
        ):
            return None

        description = self._process_description_update(
            kwargs.get(FIELD_DESCRIPTION, ""),
            inventory_id,
            auto_add_id_enabled,
        )

        item_payload = {
            FIELD_NAME: cleaned_name,
            FIELD_DESCRIPTION: description,
            FIELD_QUANTITY: quantity,
            FIELD_UNIT: kwargs.get(FIELD_UNIT, DEFAULT_UNIT),
            FIELD_EXPIRY_DATE: kwargs.get(FIELD_EXPIRY_DATE, DEFAULT_EXPIRY_DATE),
            FIELD_EXPIRY_ALERT_DAYS: max(
                0,
                int(kwargs.get(FIELD_EXPIRY_ALERT_DAYS, DEFAULT_EXPIRY_ALERT_DAYS)),
            ),
            FIELD_AUTO_ADD_ENABLED: auto_add_enabled,
            FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED: auto_add_id_enabled,
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: auto_add_quantity,
            FIELD_TODO_LIST: todo_list,
        }

        item_id = await self.repository.create_item(inventory_id, item_payload)

        await self._apply_location_updates(
            inventory_id,
            item_id,
            kwargs.get(FIELD_LOCATION, DEFAULT_LOCATION),
            quantity,
        )
        await self._apply_category_updates(item_id, kwargs.get(FIELD_CATEGORY, DEFAULT_CATEGORY))

        await self._after_change(inventory_id)
        return item_id

    async def async_update_item(
        self,
        inventory_id: str,
        old_name: str,
        new_name: str,
        **kwargs: Any,
    ) -> bool:
        """Update an existing item."""
        await self.async_initialize()

        item = await self.repository.get_item_by_name(inventory_id, old_name)
        if not item:
            _LOGGER.warning(
                "Cannot update non-existent item '%s' in inventory '%s'", old_name, inventory_id
            )
            return False

        payload = self._prepare_update_payload(inventory_id, item, new_name, kwargs)

        auto_add_enabled = payload.get(
            FIELD_AUTO_ADD_ENABLED, item.get(FIELD_AUTO_ADD_ENABLED, False)
        )
        auto_add_quantity = payload.get(
            FIELD_AUTO_ADD_TO_LIST_QUANTITY,
            item.get(FIELD_AUTO_ADD_TO_LIST_QUANTITY, DEFAULT_AUTO_ADD_TO_LIST_QUANTITY),
        )
        todo_list = payload.get(FIELD_TODO_LIST, item.get(FIELD_TODO_LIST, DEFAULT_TODO_LIST))

        if not self._validate_auto_add_config(
            payload[FIELD_NAME],
            inventory_id,
            auto_add_enabled,
            auto_add_quantity,
            todo_list,
        ):
            return False

        updated = await self.repository.update_item(item["id"], payload)
        if not updated:
            return False

        if FIELD_LOCATION in kwargs:
            await self._apply_location_updates(
                inventory_id,
                item["id"],
                kwargs.get(FIELD_LOCATION, DEFAULT_LOCATION),
                payload.get(FIELD_QUANTITY, item.get(FIELD_QUANTITY, 0)),
            )

        if FIELD_CATEGORY in kwargs:
            await self._apply_category_updates(
                item["id"], kwargs.get(FIELD_CATEGORY, DEFAULT_CATEGORY)
            )

        await self._after_change(inventory_id)
        return True

    async def async_remove_item(self, inventory_id: str, name: str) -> bool:
        """Remove an item."""
        await self.async_initialize()

        cleaned_name = self._validate_and_clean_name(name, "remove", inventory_id)
        item = await self.repository.get_item_by_name(inventory_id, cleaned_name)
        if not item:
            _LOGGER.warning(
                "Cannot remove non-existent item '%s' from inventory '%s'",
                cleaned_name,
                inventory_id,
            )
            return False

        removed = await self.repository.delete_item(item["id"])
        if removed:
            await self._after_change(inventory_id)
        return removed

    async def async_increment_item(self, inventory_id: str, name: str, amount: int = 1) -> bool:
        """Increment quantity."""
        if amount < 0:
            _LOGGER.warning(
                "Cannot increment item with negative amount: %d in inventory '%s'",
                amount,
                inventory_id,
            )
            return False

        return await self._adjust_quantity(inventory_id, name, amount)

    async def async_decrement_item(self, inventory_id: str, name: str, amount: int = 1) -> bool:
        """Decrement quantity."""
        if amount < 0:
            _LOGGER.warning(
                "Cannot decrement item with negative amount: %d in inventory '%s'",
                amount,
                inventory_id,
            )
            return False

        return await self._adjust_quantity(inventory_id, name, -amount)

    async def async_get_item(self, inventory_id: str, name: str) -> dict[str, Any] | None:
        """Return item data by name."""
        await self.async_initialize()
        return await self.repository.get_item_by_name(inventory_id, name)

    async def async_list_items(self, inventory_id: str) -> list[dict[str, Any]]:
        """Return detailed items for an inventory."""
        await self.async_initialize()
        return await self.repository.list_items_with_details(inventory_id)

    async def async_get_inventory_statistics(self, inventory_id: str) -> dict[str, Any]:
        """Compute aggregates for an inventory."""
        items = await self.async_list_items(inventory_id)

        total_items = len(items)
        total_quantity = sum(int(item.get(FIELD_QUANTITY, DEFAULT_QUANTITY)) for item in items)

        categories = self._group_items_by_field(items, FIELD_CATEGORY, DEFAULT_CATEGORY)
        locations = self._group_location_counts(items)

        below_threshold = []
        for item in items:
            quantity = int(item.get(FIELD_QUANTITY, 0))
            threshold = int(item.get(FIELD_AUTO_ADD_TO_LIST_QUANTITY, 0))
            if threshold > 0 and quantity <= threshold:
                below_threshold.append(
                    {
                        FIELD_NAME: item.get(FIELD_NAME),
                        FIELD_QUANTITY: quantity,
                        "threshold": threshold,
                        FIELD_UNIT: item.get(FIELD_UNIT, DEFAULT_UNIT),
                        FIELD_CATEGORY: item.get(FIELD_CATEGORY, DEFAULT_CATEGORY),
                    }
                )

        expiring_items = await self.async_get_items_expiring_soon(inventory_id)

        return {
            "total_items": total_items,
            "total_quantity": total_quantity,
            "categories": categories,
            "locations": locations,
            "below_threshold": below_threshold,
            "expiring_items": expiring_items,
        }

    async def async_get_items_expiring_soon(
        self, inventory_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return items expiring within their individual thresholds."""
        await self.async_initialize()

        if inventory_id:
            inventories = {inventory_id: await self.async_list_items(inventory_id)}
        else:
            inventories = {}
            for inventory in await self.repository.list_inventories():
                inv_id = inventory["id"]
                inventories[inv_id] = await self.async_list_items(inv_id)

        now = datetime.now().date()
        expiring: list[dict[str, Any]] = []

        for inv_id, items in inventories.items():
            for item in items:
                expiry_str = item.get(FIELD_EXPIRY_DATE, "")
                threshold = int(item.get(FIELD_EXPIRY_ALERT_DAYS, DEFAULT_EXPIRY_ALERT_DAYS))
                quantity = int(item.get(FIELD_QUANTITY, DEFAULT_QUANTITY))

                if not expiry_str or not threshold or quantity <= 0:
                    continue

                try:
                    expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
                except ValueError:
                    _LOGGER.warning(
                        "Invalid expiry date format for %s: %s", item.get(FIELD_NAME), expiry_str
                    )
                    continue

                if expiry_date <= now + timedelta(days=threshold):
                    expiring.append(
                        {
                            "inventory_id": inv_id,
                            FIELD_NAME: item.get(FIELD_NAME),
                            FIELD_EXPIRY_DATE: expiry_str,
                            "days_until_expiry": (expiry_date - now).days,
                            "threshold": threshold,
                            **item,
                        }
                    )

        expiring.sort(key=lambda entry: entry["days_until_expiry"])
        return expiring

    def get_data(self) -> dict[str, Any]:
        """Legacy compatibility stub (returns empty data structure)."""
        return {"inventories": {}}

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a listener."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def notify_listeners(self) -> None:
        """Invoke registered listeners."""
        for listener in list(self._listeners):
            listener()

    async def async_unload(self) -> None:
        """Unload per-entry resources (listeners, tasks).

        Repository is shared and is closed by __init__.py when the last entry unloads.
        """
        async with self._init_lock:
            self._listeners.clear()
            self._initialized = False

    # Internal helpers -----------------------------------------------------

    async def _adjust_quantity(self, inventory_id: str, name: str, delta: int) -> bool:
        await self.async_initialize()

        cleaned_name = self._validate_and_clean_name(name, "update quantity", inventory_id)
        item = await self.repository.get_item_by_name(inventory_id, cleaned_name)
        if not item:
            _LOGGER.warning(
                "Cannot adjust quantity for non-existent item '%s' in inventory '%s'",
                cleaned_name,
                inventory_id,
            )
            return False

        new_quantity = max(0, int(item.get(FIELD_QUANTITY, 0)) + delta)
        updated = await self.repository.update_item(item["id"], {FIELD_QUANTITY: new_quantity})
        if updated:
            await self._after_change(inventory_id)
        return updated

    async def _apply_location_updates(
        self,
        inventory_id: str,
        item_id: str,
        location_name: str,
        quantity: int,
    ) -> None:
        if not location_name:
            await self.repository.set_item_locations(item_id, [])
            return

        location_id = await self.repository.ensure_location(inventory_id, location_name)
        await self.repository.set_item_locations(item_id, [(location_id, quantity)])

    async def _apply_category_updates(self, item_id: str, category_name: str) -> None:
        if not category_name:
            await self.repository.set_item_categories(item_id, [])
            return

        category_id = await self.repository.ensure_category(category_name)
        await self.repository.set_item_categories(item_id, [category_id])

    def _prepare_update_payload(
        self,
        inventory_id: str,
        current_item: dict[str, Any],
        new_name: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        allowed_fields = self._get_allowed_update_fields()

        for field, value in data.items():
            if field not in allowed_fields and field not in (
                FIELD_NAME,
                FIELD_LOCATION,
                FIELD_CATEGORY,
            ):
                continue
            if field in (FIELD_LOCATION, FIELD_CATEGORY):
                continue
            processed = self._process_field_value(field, value)
            payload[field] = processed

        payload[FIELD_NAME] = self._validate_and_clean_name(
            new_name or current_item.get(FIELD_NAME, ""), "update", inventory_id
        )

        if FIELD_DESCRIPTION in data or FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED in data:
            description_value = payload.get(
                FIELD_DESCRIPTION, current_item.get(FIELD_DESCRIPTION, "")
            )
            auto_add_id_enabled = payload.get(
                FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED,
                current_item.get(FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED, False),
            )
            payload[FIELD_DESCRIPTION] = self._process_description_update(
                description_value,
                inventory_id,
                bool(auto_add_id_enabled),
            )

        return payload

    async def _after_change(self, inventory_id: str) -> None:
        await self._fire_update_events(inventory_id)
        self.notify_listeners()

    async def _fire_update_events(self, inventory_id: str | None) -> None:
        if inventory_id:
            self.hass.bus.async_fire(f"{DOMAIN}_updated_{inventory_id}")
        self.hass.bus.async_fire(f"{DOMAIN}_updated")

    def _process_field_value(self, field: str, value: Any) -> Any:
        if field in self._INTEGER_FIELDS:
            return max(0, int(value)) if value is not None else 0
        if field in self._BOOLEAN_FIELDS:
            return bool(value)
        if field in self._STRING_FIELDS:
            return str(value) if value is not None else ""
        return value

    def _validate_auto_add_config(
        self,
        item_name: str,
        inventory_id: str,
        auto_add_enabled: bool,
        auto_add_quantity: int | None,
        todo_list: str | None,
    ) -> bool:
        if not auto_add_enabled:
            return True

        if auto_add_quantity is None or auto_add_quantity < 0:
            _LOGGER.error(
                "Auto-add enabled but no valid quantity specified for item '%s' in inventory '%s'",
                item_name,
                inventory_id,
            )
            return False

        if not todo_list or not todo_list.strip():
            _LOGGER.error(
                "Auto-add enabled but no todo list specified for item '%s' in inventory '%s'",
                item_name,
                inventory_id,
            )
            return False

        return True

    def _validate_and_clean_name(self, name: str, operation: str, inventory_id: str) -> str:
        if not name or not name.strip():
            raise ValueError(
                f"Cannot {operation} item with empty name in inventory '{inventory_id}'"
            )
        return name.strip()

    def _get_allowed_update_fields(self) -> set[str]:
        return {
            FIELD_AUTO_ADD_ENABLED,
            FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED,
            FIELD_AUTO_ADD_TO_LIST_QUANTITY,
            FIELD_CATEGORY,
            FIELD_DESCRIPTION,
            FIELD_EXPIRY_ALERT_DAYS,
            FIELD_EXPIRY_DATE,
            FIELD_QUANTITY,
            FIELD_TODO_LIST,
            FIELD_UNIT,
            FIELD_LOCATION,
        }

    def _process_description_update(
        self,
        description: str | None,
        inventory_id: str,
        auto_add_id_enabled: bool,
    ) -> str:
        normalized = (description or "").rstrip()
        if not inventory_id:
            return normalized

        suffix = f" ({inventory_id})"
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].rstrip()

        if auto_add_id_enabled:
            return f"{normalized} ({inventory_id})" if normalized else f"({inventory_id})"

        return normalized

    def _group_items_by_field(
        self,
        items: list[dict[str, Any]],
        field: str,
        default: str,
    ) -> dict[str, int]:
        groups: dict[str, int] = {}
        for item in items:
            value = item.get(field, default)
            if isinstance(value, list):
                for entry in value:
                    if entry:
                        key = str(entry)
                        groups[key] = groups.get(key, 0) + 1
            else:
                key = str(value) if value else default
                if key:
                    groups[key] = groups.get(key, 0) + 1
        return groups

    def _group_location_counts(self, items: list[dict[str, Any]]) -> dict[str, int]:
        locations: dict[str, int] = {}
        for item in items:
            loc_list = item.get("locations", [])
            if isinstance(loc_list, list) and loc_list:
                for loc in loc_list:
                    name = loc.get("name", DEFAULT_LOCATION)
                    quantity = int(loc.get("quantity", 0))
                    if not name:
                        continue
                    locations[name] = locations.get(name, 0) + quantity
            else:
                name = item.get(FIELD_LOCATION, DEFAULT_LOCATION)
                if name:
                    quantity = int(item.get(FIELD_QUANTITY, 0))
                    locations[name] = locations.get(name, 0) + quantity
        return locations
