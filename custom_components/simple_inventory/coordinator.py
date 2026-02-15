"""Data coordinator for Simple Inventory integration."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    DEFAULT_AUTO_ADD_ENABLED,
    DEFAULT_AUTO_ADD_TO_LIST_QUANTITY,
    DEFAULT_CATEGORY,
    DEFAULT_DESIRED_QUANTITY,
    DEFAULT_EXPIRY_ALERT_DAYS,
    DEFAULT_EXPIRY_DATE,
    DEFAULT_LOCATION,
    DEFAULT_QUANTITY,
    DEFAULT_TODO_LIST,
    DEFAULT_TODO_QUANTITY_PLACEMENT,
    DEFAULT_UNIT,
    DOMAIN,
    FIELD_AUTO_ADD_ENABLED,
    FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED,
    FIELD_AUTO_ADD_TO_LIST_QUANTITY,
    FIELD_BARCODE,
    FIELD_CATEGORY,
    FIELD_DESCRIPTION,
    FIELD_DESIRED_QUANTITY,
    FIELD_EXPIRY_ALERT_DAYS,
    FIELD_EXPIRY_DATE,
    FIELD_LOCATION,
    FIELD_NAME,
    FIELD_QUANTITY,
    FIELD_TODO_LIST,
    FIELD_TODO_QUANTITY_PLACEMENT,
    FIELD_UNIT,
)
from .storage.repository import InventoryRepository

_LOGGER = logging.getLogger(__name__)


class SimpleInventoryCoordinator:
    """Facade around the SQLite repository with HA signaling."""

    _INTEGER_FIELDS = {
        FIELD_EXPIRY_ALERT_DAYS,
    }
    _NUMERIC_FIELDS = {
        FIELD_QUANTITY,
        FIELD_AUTO_ADD_TO_LIST_QUANTITY,
        FIELD_DESIRED_QUANTITY,
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
        FIELD_TODO_QUANTITY_PLACEMENT,
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
        quantity = max(0, float(kwargs.get(FIELD_QUANTITY, DEFAULT_QUANTITY)))

        auto_add_quantity = max(
            0,
            float(kwargs.get(FIELD_AUTO_ADD_TO_LIST_QUANTITY, DEFAULT_AUTO_ADD_TO_LIST_QUANTITY)),
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
            FIELD_DESIRED_QUANTITY: max(
                0,
                float(kwargs.get(FIELD_DESIRED_QUANTITY, DEFAULT_DESIRED_QUANTITY)),
            ),
            FIELD_TODO_LIST: todo_list,
            FIELD_TODO_QUANTITY_PLACEMENT: kwargs.get(
                FIELD_TODO_QUANTITY_PLACEMENT, DEFAULT_TODO_QUANTITY_PLACEMENT
            ),
        }

        item_id = await self.repository.create_item(inventory_id, item_payload)

        barcode = kwargs.get(FIELD_BARCODE)
        if barcode and barcode.strip():
            await self.repository.add_item_barcode(item_id, inventory_id, barcode.strip())

        await self._apply_location_updates(
            inventory_id,
            item_id,
            kwargs.get(FIELD_LOCATION, DEFAULT_LOCATION),
        )
        await self._apply_category_updates(item_id, kwargs.get(FIELD_CATEGORY, DEFAULT_CATEGORY))

        await self.repository.record_history_event(
            item_id=item_id,
            inventory_id=inventory_id,
            event_type="add",
            amount=quantity,
            quantity_before=0,
            quantity_after=quantity,
        )

        await self._after_change(inventory_id)
        return item_id

    async def async_update_item(
        self,
        inventory_id: str,
        old_name: str,
        new_name: str,
        *,
        barcode: str | None = None,
        **kwargs: Any,
    ) -> bool:
        """Update an existing item."""
        await self.async_initialize()

        resolved_old_name = await self._resolve_item_name(inventory_id, old_name, barcode)
        item = await self.repository.get_item_by_name(inventory_id, resolved_old_name)
        if not item:
            _LOGGER.warning(
                "Cannot update non-existent item '%s' in inventory '%s'",
                resolved_old_name,
                inventory_id,
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
            )

        if FIELD_CATEGORY in kwargs:
            await self._apply_category_updates(
                item["id"], kwargs.get(FIELD_CATEGORY, DEFAULT_CATEGORY)
            )

        if barcode and barcode.strip():
            await self.repository.add_item_barcode(item["id"], inventory_id, barcode.strip())

        await self._after_change(inventory_id)
        return True

    async def async_remove_item(
        self, inventory_id: str, name: str | None = None, *, barcode: str | None = None
    ) -> bool:
        """Remove an item."""
        await self.async_initialize()

        resolved_name = await self._resolve_item_name(inventory_id, name, barcode)
        cleaned_name = self._validate_and_clean_name(resolved_name, "remove", inventory_id)
        item = await self.repository.get_item_by_name(inventory_id, cleaned_name)
        if not item:
            _LOGGER.warning(
                "Cannot remove non-existent item '%s' from inventory '%s'",
                cleaned_name,
                inventory_id,
            )
            return False

        qty_before = float(item.get(FIELD_QUANTITY, 0))
        removed = await self.repository.delete_item(item["id"])
        if removed:
            await self.repository.record_history_event(
                item_id=item["id"],
                inventory_id=inventory_id,
                event_type="remove",
                amount=qty_before,
                quantity_before=qty_before,
                quantity_after=0,
            )
            await self._after_change(inventory_id)
        return removed

    async def async_increment_item(
        self,
        inventory_id: str,
        name: str | None = None,
        amount: float = 1,
        *,
        barcode: str | None = None,
    ) -> bool:
        """Increment quantity."""
        if amount < 0:
            _LOGGER.warning(
                "Cannot increment item with negative amount: %d in inventory '%s'",
                amount,
                inventory_id,
            )
            return False

        resolved_name = await self._resolve_item_name(inventory_id, name, barcode)
        return await self._adjust_quantity(inventory_id, resolved_name, amount)

    async def async_decrement_item(
        self,
        inventory_id: str,
        name: str | None = None,
        amount: float = 1,
        *,
        barcode: str | None = None,
    ) -> bool:
        """Decrement quantity."""
        if amount < 0:
            _LOGGER.warning(
                "Cannot decrement item with negative amount: %d in inventory '%s'",
                amount,
                inventory_id,
            )
            return False

        resolved_name = await self._resolve_item_name(inventory_id, name, barcode)
        return await self._adjust_quantity(inventory_id, resolved_name, -amount)

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
        total_quantity = sum(float(item.get(FIELD_QUANTITY, DEFAULT_QUANTITY)) for item in items)

        categories = self._group_items_by_field(items, FIELD_CATEGORY, DEFAULT_CATEGORY)
        locations = self._group_location_counts(items)

        below_threshold = []
        for item in items:
            quantity = float(item.get(FIELD_QUANTITY, 0))
            threshold = float(item.get(FIELD_AUTO_ADD_TO_LIST_QUANTITY, 0))
            if threshold > 0 and quantity <= threshold:
                desired = float(item.get(FIELD_DESIRED_QUANTITY, DEFAULT_DESIRED_QUANTITY))
                if desired > 0:
                    quantity_needed = desired - quantity
                else:
                    quantity_needed = threshold - quantity + 1
                below_threshold.append(
                    {
                        FIELD_NAME: item.get(FIELD_NAME),
                        FIELD_QUANTITY: quantity,
                        "threshold": threshold,
                        FIELD_DESIRED_QUANTITY: desired,
                        "quantity_needed": quantity_needed,
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
                quantity = float(item.get(FIELD_QUANTITY, DEFAULT_QUANTITY))

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

    async def async_get_item_history(
        self,
        inventory_id: str,
        name: str,
        *,
        event_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return history events for a specific item."""
        await self.async_initialize()
        item = await self.repository.get_item_by_name(inventory_id, name)
        if not item:
            return []
        return await self.repository.get_item_history(
            item["id"],
            event_type=event_type,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

    async def async_get_inventory_history(
        self,
        inventory_id: str,
        *,
        event_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return history events for an inventory."""
        await self.async_initialize()
        return await self.repository.get_inventory_history(
            inventory_id,
            event_type=event_type,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

    async def async_export_inventory(
        self,
        inventory_id: str,
        fmt: str = "json",
    ) -> dict[str, Any] | str:
        """Export inventory data as JSON dict or CSV string."""
        await self.async_initialize()
        inventory = await self.repository.fetch_inventory(inventory_id)
        if not inventory:
            raise ValueError(f"Inventory '{inventory_id}' not found")

        items = await self.async_list_items(inventory_id)
        exported_at = datetime.utcnow().isoformat()

        if fmt == "csv":
            return self._items_to_csv(items)

        return {
            "version": "1.0",
            "exported_at": exported_at,
            "inventory": {
                "id": inventory.get("id"),
                "name": inventory.get("name"),
                "description": inventory.get("description", ""),
            },
            "items": items,
        }

    async def async_import_inventory(
        self,
        inventory_id: str,
        data: Any,
        fmt: str = "json",
        merge_strategy: str = "skip",
    ) -> dict[str, Any]:
        """Import items into an inventory.

        merge_strategy: skip | overwrite | merge_quantities
        Returns summary: {added, updated, skipped, errors}
        """
        await self.async_initialize()

        if fmt == "csv":
            items_to_import = self._csv_to_items(data)
        elif isinstance(data, dict):
            items_to_import = data.get("items", [])
        elif isinstance(data, list):
            items_to_import = data
        else:
            return {"added": 0, "updated": 0, "skipped": 0, "errors": ["Invalid data format"]}

        added = 0
        updated = 0
        skipped = 0
        errors: list[str] = []

        for item_data in items_to_import:
            try:
                name = item_data.get(FIELD_NAME, "")
                if not name or not name.strip():
                    errors.append("Item missing name, skipped")
                    continue

                existing = await self.repository.get_item_by_name(inventory_id, name.strip())

                if existing and merge_strategy == "skip":
                    skipped += 1
                    continue

                if existing and merge_strategy == "merge_quantities":
                    new_qty = float(existing.get(FIELD_QUANTITY, 0)) + float(
                        item_data.get(FIELD_QUANTITY, 0)
                    )
                    await self.repository.update_item(existing["id"], {FIELD_QUANTITY: new_qty})
                    await self.repository.record_history_event(
                        item_id=existing["id"],
                        inventory_id=inventory_id,
                        event_type="import",
                        amount=float(item_data.get(FIELD_QUANTITY, 0)),
                        quantity_before=float(existing.get(FIELD_QUANTITY, 0)),
                        quantity_after=new_qty,
                        source="import",
                    )
                    updated += 1
                    continue

                if existing and merge_strategy == "overwrite":
                    payload = self._build_import_payload(item_data)
                    await self.repository.update_item(existing["id"], payload)
                    qty_before = float(existing.get(FIELD_QUANTITY, 0))
                    qty_after = float(payload.get(FIELD_QUANTITY, qty_before))
                    await self.repository.record_history_event(
                        item_id=existing["id"],
                        inventory_id=inventory_id,
                        event_type="import",
                        amount=qty_after,
                        quantity_before=qty_before,
                        quantity_after=qty_after,
                        source="import",
                    )

                    if FIELD_LOCATION in item_data and item_data[FIELD_LOCATION]:
                        await self._apply_location_updates(
                            inventory_id,
                            existing["id"],
                            item_data[FIELD_LOCATION],
                        )
                    if FIELD_CATEGORY in item_data and item_data[FIELD_CATEGORY]:
                        await self._apply_category_updates(
                            existing["id"], item_data[FIELD_CATEGORY]
                        )

                    updated += 1
                    continue

                # New item
                payload = self._build_import_payload(item_data)
                item_id = await self.repository.create_item(inventory_id, payload)
                qty = float(payload.get(FIELD_QUANTITY, 0))
                await self.repository.record_history_event(
                    item_id=item_id,
                    inventory_id=inventory_id,
                    event_type="import",
                    amount=qty,
                    quantity_before=0,
                    quantity_after=qty,
                    source="import",
                )

                if FIELD_LOCATION in item_data and item_data[FIELD_LOCATION]:
                    await self._apply_location_updates(
                        inventory_id, item_id, item_data[FIELD_LOCATION]
                    )
                if FIELD_CATEGORY in item_data and item_data[FIELD_CATEGORY]:
                    await self._apply_category_updates(item_id, item_data[FIELD_CATEGORY])

                added += 1

            except Exception as exc:
                errors.append(f"Error importing '{item_data.get(FIELD_NAME, '?')}': {exc}")

        if added or updated:
            await self._after_change(inventory_id)

        return {"added": added, "updated": updated, "skipped": skipped, "errors": errors}

    def _build_import_payload(self, item_data: dict[str, Any]) -> dict[str, Any]:
        """Build a clean item payload from imported data."""
        return {
            FIELD_NAME: str(item_data.get(FIELD_NAME, "")).strip(),
            FIELD_DESCRIPTION: str(item_data.get(FIELD_DESCRIPTION, "")),
            FIELD_QUANTITY: float(item_data.get(FIELD_QUANTITY, 0)),
            FIELD_UNIT: str(item_data.get(FIELD_UNIT, DEFAULT_UNIT)),
            FIELD_EXPIRY_DATE: str(item_data.get(FIELD_EXPIRY_DATE, DEFAULT_EXPIRY_DATE)),
            FIELD_EXPIRY_ALERT_DAYS: int(
                item_data.get(FIELD_EXPIRY_ALERT_DAYS, DEFAULT_EXPIRY_ALERT_DAYS)
            ),
            FIELD_AUTO_ADD_ENABLED: bool(
                item_data.get(FIELD_AUTO_ADD_ENABLED, DEFAULT_AUTO_ADD_ENABLED)
            ),
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: float(
                item_data.get(FIELD_AUTO_ADD_TO_LIST_QUANTITY, DEFAULT_AUTO_ADD_TO_LIST_QUANTITY)
            ),
            FIELD_DESIRED_QUANTITY: float(
                item_data.get(FIELD_DESIRED_QUANTITY, DEFAULT_DESIRED_QUANTITY)
            ),
            FIELD_TODO_LIST: str(item_data.get(FIELD_TODO_LIST, DEFAULT_TODO_LIST)),
            FIELD_TODO_QUANTITY_PLACEMENT: str(
                item_data.get(FIELD_TODO_QUANTITY_PLACEMENT, DEFAULT_TODO_QUANTITY_PLACEMENT)
            ),
        }

    def _items_to_csv(self, items: list[dict[str, Any]]) -> str:
        """Convert items list to a CSV string."""
        output = io.StringIO()
        fieldnames = [
            "name",
            "description",
            "quantity",
            "unit",
            "location",
            "category",
            "expiry_date",
            "expiry_alert_days",
            "auto_add_enabled",
            "auto_add_to_list_quantity",
            "desired_quantity",
            "todo_list",
            "barcodes",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for item in items:
            row = {
                "name": item.get(FIELD_NAME, ""),
                "description": item.get(FIELD_DESCRIPTION, ""),
                "quantity": item.get(FIELD_QUANTITY, 0),
                "unit": item.get(FIELD_UNIT, ""),
                "location": ", ".join(item.get("locations", [])) or item.get(FIELD_LOCATION, ""),
                "category": ", ".join(item.get("categories", [])) or item.get(FIELD_CATEGORY, ""),
                "expiry_date": item.get(FIELD_EXPIRY_DATE, ""),
                "expiry_alert_days": item.get(FIELD_EXPIRY_ALERT_DAYS, 0),
                "auto_add_enabled": int(item.get(FIELD_AUTO_ADD_ENABLED, False)),
                "auto_add_to_list_quantity": item.get(FIELD_AUTO_ADD_TO_LIST_QUANTITY, 0),
                "desired_quantity": item.get(FIELD_DESIRED_QUANTITY, 0),
                "todo_list": item.get(FIELD_TODO_LIST, ""),
                "barcodes": ", ".join(item.get("barcodes", [])),
            }
            writer.writerow(row)

        return output.getvalue()

    def _csv_to_items(self, csv_string: str) -> list[dict[str, Any]]:
        """Parse a CSV string into a list of item dicts."""
        reader = csv.DictReader(io.StringIO(csv_string))
        items: list[dict[str, Any]] = []
        for row in reader:
            item: dict[str, Any] = {
                FIELD_NAME: row.get("name", ""),
                FIELD_DESCRIPTION: row.get("description", ""),
                FIELD_QUANTITY: float(row.get("quantity", 0) or 0),
                FIELD_UNIT: row.get("unit", ""),
                FIELD_LOCATION: row.get("location", ""),
                FIELD_CATEGORY: row.get("category", ""),
                FIELD_EXPIRY_DATE: row.get("expiry_date", ""),
                FIELD_EXPIRY_ALERT_DAYS: int(row.get("expiry_alert_days", 0) or 0),
                FIELD_AUTO_ADD_ENABLED: bool(int(row.get("auto_add_enabled", 0) or 0)),
                FIELD_AUTO_ADD_TO_LIST_QUANTITY: float(
                    row.get("auto_add_to_list_quantity", 0) or 0
                ),
                FIELD_DESIRED_QUANTITY: float(row.get("desired_quantity", 0) or 0),
                FIELD_TODO_LIST: row.get("todo_list", ""),
            }
            items.append(item)
        return items

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

    async def _resolve_item_name(
        self,
        inventory_id: str,
        name: str | None,
        barcode: str | None,
    ) -> str:
        """Resolve an item name from name or barcode.

        Returns the item name. Raises ValueError if neither is provided
        or the barcode does not match any item.
        """
        if name and name.strip():
            return name.strip()

        if barcode and barcode.strip():
            item = await self.repository.get_item_by_barcode(inventory_id, barcode.strip())
            if item is None:
                raise ValueError(
                    f"No item found for barcode '{barcode}' in inventory '{inventory_id}'"
                )
            return str(item[FIELD_NAME])

        raise ValueError(
            f"Either 'name' or 'barcode' is required to identify an item "
            f"in inventory '{inventory_id}'"
        )

    async def _adjust_quantity(self, inventory_id: str, name: str, delta: float) -> bool:
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

        qty_before = float(item.get(FIELD_QUANTITY, 0))
        new_quantity = max(0, qty_before + delta)
        updated = await self.repository.update_item(item["id"], {FIELD_QUANTITY: new_quantity})
        if updated:
            event_type = "increment" if delta > 0 else "decrement"
            await self.repository.record_history_event(
                item_id=item["id"],
                inventory_id=inventory_id,
                event_type=event_type,
                amount=abs(delta),
                quantity_before=qty_before,
                quantity_after=new_quantity,
            )
            await self._after_change(inventory_id)
        return updated

    async def _apply_location_updates(
        self,
        inventory_id: str,
        item_id: str,
        location_name: str,
    ) -> None:
        if not location_name:
            await self.repository.set_item_locations(item_id, [])
            return

        names = [n.strip() for n in location_name.split(",") if n.strip()]
        if not names:
            await self.repository.set_item_locations(item_id, [])
            return

        loc_ids = []
        for name in names:
            loc_id = await self.repository.ensure_location(inventory_id, name)
            loc_ids.append(loc_id)
        await self.repository.set_item_locations(item_id, loc_ids)

    async def _apply_category_updates(self, item_id: str, category_name: str) -> None:
        if not category_name:
            await self.repository.set_item_categories(item_id, [])
            return

        names = [n.strip() for n in category_name.split(",") if n.strip()]
        if not names:
            await self.repository.set_item_categories(item_id, [])
            return

        ids = []
        for name in names:
            cat_id = await self.repository.ensure_category(name)
            ids.append(cat_id)
        await self.repository.set_item_categories(item_id, ids)

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
        if field in self._NUMERIC_FIELDS:
            return max(0, float(value)) if value is not None else 0
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
        auto_add_quantity: float | None,
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
            FIELD_DESIRED_QUANTITY,
            FIELD_CATEGORY,
            FIELD_DESCRIPTION,
            FIELD_EXPIRY_ALERT_DAYS,
            FIELD_EXPIRY_DATE,
            FIELD_QUANTITY,
            FIELD_TODO_LIST,
            FIELD_TODO_QUANTITY_PLACEMENT,
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
        elif normalized == f"({inventory_id})":
            normalized = ""

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
                for name in loc_list:
                    if name:
                        locations[name] = locations.get(name, 0) + 1
            else:
                name = item.get(FIELD_LOCATION, DEFAULT_LOCATION)
                if name:
                    locations[name] = locations.get(name, 0) + 1
        return locations
