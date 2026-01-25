from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence, cast

import aiosqlite
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import (
    DEFAULT_AUTO_ADD_TO_LIST_QUANTITY,
    DEFAULT_EXPIRY_ALERT_DAYS,
    DEFAULT_QUANTITY,
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
    INVENTORY_ITEMS,
    INVENTORY_NAME,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
LEGACY_MIGRATION_FLAG = "legacy_migrated"


class InventoryRepository:
    """SQLite-backed storage for Simple Inventory."""

    _migration_lock: asyncio.Lock = asyncio.Lock()

    def __init__(self, hass: HomeAssistant, db_filename: str = "simple_inventory.db") -> None:
        self._hass = hass
        self._db_path = Path(hass.config.path(db_filename))
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def async_initialize(self) -> None:
        """Open DB, ensure schema, migrate if needed."""
        async with self._lock:
            if self._conn is None:
                self._conn = await aiosqlite.connect(self._db_path)
                self._conn.row_factory = aiosqlite.Row
                _LOGGER.debug("Opened Simple Inventory database at %s", self._db_path)
                await self._conn.execute("PRAGMA foreign_keys = ON")
                await self._conn.execute("PRAGMA journal_mode = WAL")
                await self._conn.execute("PRAGMA synchronous = NORMAL")
                await self._conn.execute("PRAGMA busy_timeout = 5000")

        await self._ensure_schema()
        await self._maybe_migrate_legacy_store()

    async def _maybe_migrate_legacy_store(self) -> None:
        async with InventoryRepository._migration_lock:
            await self._maybe_migrate_legacy_store_locked()

    async def _maybe_migrate_legacy_store_locked(self) -> None:
        """Load legacy JSON storage and persist into SQLite once."""
        assert self._conn is not None

        cursor = await self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (LEGACY_MIGRATION_FLAG,)
        )
        row = await cursor.fetchone()
        await cursor.close()

        if row is not None and row[0] == "1":
            return

        await self._conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (LEGACY_MIGRATION_FLAG, "running"),
        )
        await self._conn.commit()

        store = Store[dict[str, Any]](self._hass, STORAGE_VERSION, STORAGE_KEY)
        legacy_data = await store.async_load()

        if not legacy_data:
            await self._conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                (LEGACY_MIGRATION_FLAG, "1"),
            )
            await self._conn.commit()
            return

        inventories = legacy_data.get("inventories", {})
        for inventory_id, inventory_payload in inventories.items():
            await self._migrate_inventory(inventory_id, inventory_payload)

        await self._conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (LEGACY_MIGRATION_FLAG, "1"),
        )
        await self._conn.commit()
        _LOGGER.info("Legacy Simple Inventory data migrated to SQLite backend")

    async def _migrate_inventory(self, inventory_id: str, payload: dict[str, Any]) -> None:
        """Migrate a single inventory record into SQLite."""
        name = payload.get(INVENTORY_NAME, inventory_id)
        description = payload.get("description", "")
        icon = payload.get("icon", "")

        await self.upsert_inventory(inventory_id, name, description, icon)

        items = payload.get(INVENTORY_ITEMS, {})
        for item_name, item_data in items.items():
            await self._migrate_item(inventory_id, item_name, item_data, payload)

    async def _migrate_item(
        self,
        inventory_id: str,
        legacy_name: str,
        legacy_item: dict[str, Any],
        inventory_payload: dict[str, Any],
    ) -> None:
        """Migrate a single item entry, merging with existing rows if needed."""
        item_payload: dict[str, Any] = {
            FIELD_NAME: legacy_item.get(FIELD_NAME, legacy_name),
            FIELD_DESCRIPTION: legacy_item.get(FIELD_DESCRIPTION, ""),
            FIELD_QUANTITY: int(legacy_item.get(FIELD_QUANTITY, 0)),
            FIELD_UNIT: legacy_item.get(FIELD_UNIT, ""),
            FIELD_EXPIRY_DATE: legacy_item.get(FIELD_EXPIRY_DATE, ""),
            FIELD_EXPIRY_ALERT_DAYS: int(legacy_item.get(FIELD_EXPIRY_ALERT_DAYS, 0)),
            FIELD_AUTO_ADD_ENABLED: legacy_item.get(FIELD_AUTO_ADD_ENABLED, False),
            FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED: legacy_item.get(
                FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED, False
            ),
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: int(
                legacy_item.get(FIELD_AUTO_ADD_TO_LIST_QUANTITY, 0)
            ),
            FIELD_TODO_LIST: legacy_item.get(FIELD_TODO_LIST, ""),
        }

        item_id = await self.create_item(inventory_id, item_payload)

        location_name = legacy_item.get(FIELD_LOCATION, "")
        if location_name:
            location_id = await self.ensure_location(inventory_id, location_name)
            await self.set_item_locations(item_id, [(location_id, item_payload[FIELD_QUANTITY])])

        category_name = legacy_item.get(FIELD_CATEGORY, "")
        if category_name:
            category_id = await self.ensure_category(category_name)
            await self.set_item_categories(item_id, [category_id])

    async def async_close(self) -> None:
        """Close the database connection."""
        async with self._lock:
            if self._conn:
                await self._conn.close()
                self._conn = None

    async def _ensure_schema(self) -> None:
        """Create tables and metadata if needed."""
        async with self._lock:
            assert self._conn is not None

            await self._conn.executescript(
                """
                DROP INDEX IF EXISTS idx_items_name_inventory;
                DROP INDEX IF EXISTS idx_locations_unique_name;

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inventories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    icon TEXT DEFAULT '',
                    entry_type TEXT DEFAULT '',
                    metadata TEXT DEFAULT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_inventories_name
                    ON inventories (LOWER(name));

                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    inventory_id TEXT NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    description TEXT DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 0,
                    unit TEXT DEFAULT '',
                    expiry_date TEXT DEFAULT '',
                    expiry_alert_days INTEGER DEFAULT 0,
                    auto_add_enabled INTEGER NOT NULL DEFAULT 0,
                    auto_add_id_to_description_enabled INTEGER NOT NULL DEFAULT 0,
                    auto_add_to_list_quantity INTEGER NOT NULL DEFAULT 0,
                    todo_list TEXT DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (inventory_id) REFERENCES inventories(id) ON DELETE CASCADE,
                    UNIQUE (inventory_id, name)
                );

                CREATE INDEX IF NOT EXISTS idx_items_inventory_id
                    ON items (inventory_id);

                CREATE TABLE IF NOT EXISTS locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inventory_id TEXT NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    description TEXT DEFAULT '',
                    color TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (inventory_id) REFERENCES inventories(id) ON DELETE CASCADE,
                    UNIQUE (inventory_id, name)
                );

                CREATE TABLE IF NOT EXISTS item_locations (
                    item_id TEXT NOT NULL,
                    location_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    notes TEXT DEFAULT '',
                    PRIMARY KEY (item_id, location_id),
                    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE,
                    FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL COLLATE NOCASE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name
                    ON categories (name);

                CREATE TABLE IF NOT EXISTS item_categories (
                    item_id TEXT NOT NULL,
                    category_id INTEGER NOT NULL,
                    PRIMARY KEY (item_id, category_id),
                    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE,
                    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
                );
                """
            )

            await self._ensure_schema_version()
            await self._conn.commit()

    async def _ensure_schema_version(self) -> None:
        """Store or validate the schema version entry."""
        assert self._conn is not None
        cursor = await self._conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'")
        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO metadata (key, value)
                VALUES (?, ?)
                """,
                ("schema_version", str(SCHEMA_VERSION)),
            )
            cursor = await self._conn.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            )
            row = await cursor.fetchone()
            await cursor.close()

        if row is None:
            raise RuntimeError("Unable to initialize schema_version metadata")

        if int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {row[0]} does not match expected {SCHEMA_VERSION}"
            )

    def _connection(self) -> aiosqlite.Connection:
        """Accessor for the open connection."""
        if self._conn is None:
            raise RuntimeError("InventoryRepository not initialized")
        return self._conn

    async def fetch_inventory(self, inventory_id: str) -> dict[str, Any] | None:
        conn = self._connection()
        cursor = await conn.execute(
            """
            SELECT id, name, description, icon, entry_type, metadata,
                   created_at, updated_at
            FROM inventories
            WHERE id = ?
            """,
            (inventory_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None

        return {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "icon": row[3],
            "entry_type": row[4],
            "metadata": row[5],
            "created_at": row[6],
            "updated_at": row[7],
        }

    async def upsert_inventory(
        self,
        inventory_id: str,
        name: str,
        description: str = "",
        icon: str = "",
        entry_type: str = "",
        metadata: str | None = None,
    ) -> None:
        """Create or update an inventory record."""
        conn = self._connection()
        async with self._lock:
            await conn.execute(
                """
                INSERT INTO inventories (id, name, description, icon, entry_type, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    icon = excluded.icon,
                    entry_type = excluded.entry_type,
                    metadata = excluded.metadata,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (inventory_id, name, description, icon, entry_type, metadata),
            )
            await conn.commit()

    async def list_inventories(self) -> list[dict[str, Any]]:
        """Return all inventories."""
        conn = self._connection()
        cursor = await conn.execute(
            """
            SELECT id, name, description, icon, entry_type, metadata,
                   created_at, updated_at
            FROM inventories
            ORDER BY name COLLATE NOCASE
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "icon": row[3],
                "entry_type": row[4],
                "metadata": row[5],
                "created_at": row[6],
                "updated_at": row[7],
            }
            for row in rows
        ]

    async def create_item(self, inventory_id: str, data: dict[str, Any]) -> str:
        """Insert or merge an item; returns item_id."""
        item_id = data.get("id") or str(uuid.uuid4())
        payload = {
            FIELD_NAME: data[FIELD_NAME],
            FIELD_DESCRIPTION: data.get(FIELD_DESCRIPTION, ""),
            FIELD_QUANTITY: data.get(FIELD_QUANTITY, 0),
            FIELD_UNIT: data.get(FIELD_UNIT, ""),
            FIELD_EXPIRY_DATE: data.get(FIELD_EXPIRY_DATE, ""),
            FIELD_EXPIRY_ALERT_DAYS: data.get(FIELD_EXPIRY_ALERT_DAYS, 0),
            FIELD_AUTO_ADD_ENABLED: int(data.get(FIELD_AUTO_ADD_ENABLED, False)),
            FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED: int(
                data.get(FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED, False)
            ),
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: data.get(FIELD_AUTO_ADD_TO_LIST_QUANTITY, 0),
            FIELD_TODO_LIST: data.get(FIELD_TODO_LIST, ""),
        }

        conn = self._connection()
        async with self._lock:
            cursor = await conn.execute(
                """
                INSERT INTO items (
                    id, inventory_id, name, description, quantity, unit,
                    expiry_date, expiry_alert_days,
                    auto_add_enabled, auto_add_id_to_description_enabled,
                    auto_add_to_list_quantity, todo_list
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(inventory_id, name) DO UPDATE SET
                    quantity = items.quantity + excluded.quantity,
                    description = CASE
                        WHEN excluded.description != '' THEN excluded.description
                        ELSE items.description
                    END,
                    unit = CASE
                        WHEN excluded.unit != '' THEN excluded.unit
                        ELSE items.unit
                    END,
                    expiry_date = CASE
                        WHEN excluded.expiry_date != '' THEN excluded.expiry_date
                        ELSE items.expiry_date
                    END,
                    expiry_alert_days = excluded.expiry_alert_days,
                    auto_add_enabled = excluded.auto_add_enabled,
                    auto_add_id_to_description_enabled = excluded.auto_add_id_to_description_enabled,
                    auto_add_to_list_quantity = excluded.auto_add_to_list_quantity,
                    todo_list = CASE
                        WHEN excluded.todo_list != '' THEN excluded.todo_list
                        ELSE items.todo_list
                    END,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id
                """,
                (
                    item_id,
                    inventory_id,
                    payload[FIELD_NAME],
                    payload[FIELD_DESCRIPTION],
                    payload[FIELD_QUANTITY],
                    payload[FIELD_UNIT],
                    payload[FIELD_EXPIRY_DATE],
                    payload[FIELD_EXPIRY_ALERT_DAYS],
                    payload[FIELD_AUTO_ADD_ENABLED],
                    payload[FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED],
                    payload[FIELD_AUTO_ADD_TO_LIST_QUANTITY],
                    payload[FIELD_TODO_LIST],
                ),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await conn.commit()

        return cast(str, row[0]) if row else item_id

    async def update_item(self, item_id: str, data: dict[str, Any]) -> bool:
        """Update an item record; returns True if a row was updated."""
        column_map = {
            FIELD_NAME: "name",
            FIELD_DESCRIPTION: "description",
            FIELD_QUANTITY: "quantity",
            FIELD_UNIT: "unit",
            FIELD_EXPIRY_DATE: "expiry_date",
            FIELD_EXPIRY_ALERT_DAYS: "expiry_alert_days",
            FIELD_AUTO_ADD_ENABLED: "auto_add_enabled",
            FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED: "auto_add_id_to_description_enabled",
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: "auto_add_to_list_quantity",
            FIELD_TODO_LIST: "todo_list",
        }

        fields: list[str] = []
        params: list[Any] = []

        for field, column in column_map.items():
            if field in data:
                value = data[field]
                if field in (FIELD_AUTO_ADD_ENABLED, FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED):
                    value = int(value)
                fields.append(f"{column} = ?")
                params.append(value)

        if not fields:
            return False

        params.append(item_id)
        conn = self._connection()
        async with self._lock:
            cursor = await conn.execute(
                f"""
                UPDATE items
                SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                tuple(params),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_item(self, item_id: str) -> bool:
        """Delete an item and any related rows."""
        conn = self._connection()
        async with self._lock:
            cursor = await conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def list_items_with_details(self, inventory_id: str) -> list[dict[str, Any]]:
        """Return items plus associated locations and categories."""
        conn = self._connection()

        cursor = await conn.execute(
            """
            SELECT
                id,
                name,
                description,
                quantity,
                unit,
                expiry_date,
                expiry_alert_days,
                auto_add_enabled,
                auto_add_id_to_description_enabled,
                auto_add_to_list_quantity,
                todo_list,
                created_at,
                updated_at
            FROM items
            WHERE inventory_id = ?
            ORDER BY LOWER(name)
            """,
            (inventory_id,),
        )
        base_rows = await cursor.fetchall()
        await cursor.close()

        items: dict[str, dict[str, Any]] = {}
        for row in base_rows:
            item_id = row[0]
            items[item_id] = {
                "id": item_id,
                "inventory_id": inventory_id,
                FIELD_NAME: row[1],
                FIELD_DESCRIPTION: row[2],
                FIELD_QUANTITY: row[3],
                FIELD_UNIT: row[4],
                FIELD_EXPIRY_DATE: row[5],
                FIELD_EXPIRY_ALERT_DAYS: row[6],
                FIELD_AUTO_ADD_ENABLED: bool(row[7]),
                FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED: bool(row[8]),
                FIELD_AUTO_ADD_TO_LIST_QUANTITY: row[9],
                FIELD_TODO_LIST: row[10],
                "created_at": row[11],
                "updated_at": row[12],
                FIELD_CATEGORY: "",
                FIELD_LOCATION: "",
                "locations": [],
                "categories": [],
            }

        if not items:
            return []

        cursor = await conn.execute(
            """
            SELECT il.item_id, l.name, il.quantity
            FROM item_locations il
            JOIN locations l ON l.id = il.location_id
            JOIN items i ON i.id = il.item_id
            WHERE i.inventory_id = ?
            """,
            (inventory_id,),
        )
        location_rows = await cursor.fetchall()
        await cursor.close()
        for item_id, location_name, quantity in location_rows:
            if item_id not in items:
                continue
            location_entry = {"name": location_name, "quantity": quantity}
            items[item_id]["locations"].append(location_entry)
            if not items[item_id][FIELD_LOCATION]:
                items[item_id][FIELD_LOCATION] = location_name

        cursor = await conn.execute(
            """
            SELECT ic.item_id, c.name
            FROM item_categories ic
            JOIN categories c ON c.id = ic.category_id
            JOIN items i ON i.id = ic.item_id
            WHERE i.inventory_id = ?
            """,
            (inventory_id,),
        )
        category_rows = await cursor.fetchall()
        await cursor.close()
        for item_id, category_name in category_rows:
            if item_id not in items:
                continue
            items[item_id]["categories"].append(category_name)
            if not items[item_id][FIELD_CATEGORY]:
                items[item_id][FIELD_CATEGORY] = category_name

        return list(items.values())

    async def get_item_by_name(self, inventory_id: str, name: str) -> dict[str, Any] | None:
        """Retrieve item by inventory and case-insensitive name."""
        conn = self._connection()
        cursor = await conn.execute(
            """
            SELECT id, inventory_id, name, description, quantity, unit,
                   expiry_date, expiry_alert_days, auto_add_enabled,
                   auto_add_id_to_description_enabled, auto_add_to_list_quantity,
                   todo_list, created_at, updated_at
            FROM items
            WHERE inventory_id = ?
              AND name = ?
            """,
            (inventory_id, name),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None

        return {
            "id": row[0],
            "inventory_id": row[1],
            FIELD_NAME: row[2],
            FIELD_DESCRIPTION: row[3],
            FIELD_QUANTITY: row[4],
            FIELD_UNIT: row[5],
            FIELD_EXPIRY_DATE: row[6],
            FIELD_EXPIRY_ALERT_DAYS: row[7],
            FIELD_AUTO_ADD_ENABLED: bool(row[8]),
            FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED: bool(row[9]),
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: row[10],
            FIELD_TODO_LIST: row[11],
            "created_at": row[12],
            "updated_at": row[13],
        }

    async def ensure_location(self, inventory_id: str, name: str) -> int:
        """Fetch or create a location for an inventory."""
        conn = self._connection()
        async with self._lock:
            cursor = await conn.execute(
                """
                INSERT INTO locations (inventory_id, name)
                VALUES (?, ?)
                ON CONFLICT(inventory_id, name) DO UPDATE SET
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id
                """,
                (inventory_id, name),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await conn.commit()
        if row is None:
            raise RuntimeError("Failed to ensure location; no row returned")

        return cast(int, row["id"])

    async def set_item_locations(
        self,
        item_id: str,
        locations: Sequence[tuple[int, int]],
    ) -> None:
        """Replace all location entries for an item."""
        conn = self._connection()
        async with self._lock:
            await conn.execute("DELETE FROM item_locations WHERE item_id = ?", (item_id,))
            if locations:
                await conn.executemany(
                    """
                    INSERT INTO item_locations (item_id, location_id, quantity)
                    VALUES (?, ?, ?)
                    """,
                    [(item_id, loc_id, qty) for loc_id, qty in locations],
                )
            await conn.commit()

    async def ensure_category(self, name: str) -> int:
        """Fetch or create a category."""
        conn = self._connection()
        async with self._lock:
            cursor = await conn.execute(
                """
                INSERT INTO categories (name)
                VALUES (?)
                ON CONFLICT(name) DO UPDATE SET
                    name = excluded.name
                RETURNING id
                """,
                (name,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await conn.commit()
        if row is None:
            raise RuntimeError("Failed to ensure location; no row returned")

        return cast(int, row["id"])

    async def set_item_categories(self, item_id: str, category_ids: Sequence[int]) -> None:
        """Replace category associations for an item."""
        conn = self._connection()
        async with self._lock:
            await conn.execute("DELETE FROM item_categories WHERE item_id = ?", (item_id,))
            if category_ids:
                await conn.executemany(
                    "INSERT INTO item_categories (item_id, category_id) VALUES (?, ?)",
                    [(item_id, category_id) for category_id in category_ids],
                )
            await conn.commit()

    async def compute_inventory_stats(self, inventory_id: str) -> dict[str, Any]:
        """Return aggregate statistics for an inventory."""
        items = await self.list_items_with_details(inventory_id)

        total_items = len(items)
        total_quantity = sum(int(item.get(FIELD_QUANTITY, DEFAULT_QUANTITY)) for item in items)

        categories = await self.get_category_counts(inventory_id)
        locations = await self.get_location_quantities(inventory_id)

        below_threshold: list[dict[str, Any]] = []
        for item in items:
            quantity = int(item.get(FIELD_QUANTITY, 0))
            threshold = int(
                item.get(FIELD_AUTO_ADD_TO_LIST_QUANTITY, DEFAULT_AUTO_ADD_TO_LIST_QUANTITY)
            )
            if threshold > 0 and quantity <= threshold:
                below_threshold.append(
                    {
                        FIELD_NAME: item.get(FIELD_NAME),
                        FIELD_QUANTITY: quantity,
                        "threshold": threshold,
                        FIELD_UNIT: item.get(FIELD_UNIT, ""),
                        FIELD_CATEGORY: item.get(FIELD_CATEGORY, ""),
                    }
                )

        expiring = await self.list_items_expiring_before(
            date.today()
            + timedelta(
                days=max(
                    DEFAULT_EXPIRY_ALERT_DAYS,
                    *(int(it.get(FIELD_EXPIRY_ALERT_DAYS, 0)) for it in items),
                )
            ),
            inventory_id=inventory_id,
        )

        return {
            "total_items": total_items,
            "total_quantity": total_quantity,
            "categories": categories,
            "locations": locations,
            "below_threshold": below_threshold,
            "expiring_items": expiring,
        }

    async def list_items_expiring_before(
        self,
        limit_date: date | datetime,
        inventory_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return items whose expiry date is before/eq limit_date."""
        conn = self._connection()
        params: list[Any] = []
        where_clause = ""

        if isinstance(limit_date, datetime):
            limit_date = limit_date.date()

        if inventory_id:
            where_clause = "AND items.inventory_id = ?"
            params.append(inventory_id)

        cursor = await conn.execute(
            f"""
            SELECT
                items.inventory_id,
                items.id,
                items.name,
                items.expiry_date,
                items.expiry_alert_days,
                items.quantity
            FROM items
            WHERE items.expiry_date != ''
              AND DATE(items.expiry_date) <= DATE(?)
              AND items.quantity > 0
              {where_clause}
            ORDER BY DATE(items.expiry_date)
            """,
            [limit_date.isoformat(), *params],
        )
        rows = await cursor.fetchall()
        await cursor.close()

        results: list[dict[str, Any]] = []
        today = date.today()

        for inv_id, item_id, name, expiry_str, alert_days, quantity in rows:
            try:
                expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            results.append(
                {
                    "inventory_id": inv_id,
                    "item_id": item_id,
                    FIELD_NAME: name,
                    FIELD_EXPIRY_DATE: expiry_str,
                    FIELD_EXPIRY_ALERT_DAYS: alert_days,
                    FIELD_QUANTITY: quantity,
                    "days_until_expiry": (expiry_date - today).days,
                }
            )

        return results

    async def list_items_with_auto_add_condition(
        self,
        inventory_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return items eligible for auto-add processing (quantity <= threshold)."""
        conn = self._connection()
        params: list[Any] = []
        where_clause = ""

        if inventory_id:
            where_clause = "AND items.inventory_id = ?"
            params.append(inventory_id)

        cursor = await conn.execute(
            f"""
            SELECT
                items.inventory_id,
                items.id,
                items.name,
                items.quantity,
                items.auto_add_to_list_quantity,
                items.todo_list,
                items.auto_add_enabled
            FROM items
            WHERE items.auto_add_enabled = 1
              AND items.todo_list != ''
              AND items.auto_add_to_list_quantity >= 0
              AND items.quantity <= items.auto_add_to_list_quantity
              {where_clause}
            """,
            params,
        )
        rows = await cursor.fetchall()
        await cursor.close()

        return [
            {
                "inventory_id": row[0],
                "item_id": row[1],
                FIELD_NAME: row[2],
                FIELD_QUANTITY: row[3],
                FIELD_AUTO_ADD_TO_LIST_QUANTITY: row[4],
                FIELD_TODO_LIST: row[5],
                FIELD_AUTO_ADD_ENABLED: bool(row[6]),
            }
            for row in rows
        ]

    async def get_location_quantities(self, inventory_id: str) -> dict[str, int]:
        """Return total quantity per location."""
        conn = self._connection()
        cursor = await conn.execute(
            """
            SELECT locations.name, SUM(item_locations.quantity)
            FROM item_locations
            JOIN locations ON locations.id = item_locations.location_id
            WHERE locations.inventory_id = ?
            GROUP BY locations.name
            """,
            (inventory_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        return {row[0]: int(row[1]) if row[1] is not None else 0 for row in rows}

    async def get_category_counts(self, inventory_id: str) -> dict[str, int]:
        """Return count of items per category."""
        conn = self._connection()
        cursor = await conn.execute(
            """
            SELECT categories.name, COUNT(*)
            FROM item_categories
            JOIN categories ON categories.id = item_categories.category_id
            JOIN items ON items.id = item_categories.item_id
            WHERE items.inventory_id = ?
            GROUP BY categories.name
            """,
            (inventory_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        return {row[0]: int(row[1]) for row in rows}
