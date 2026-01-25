from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.simple_inventory.const import (
    FIELD_AUTO_ADD_ENABLED,
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
)
from custom_components.simple_inventory.storage.repository import InventoryRepository


@pytest.fixture
def hass_with_tmp_config(hass: HomeAssistant, tmp_path: Path) -> HomeAssistant:
    hass.config.config_dir = str(tmp_path)

    def _path(*parts: str) -> str:
        return str(tmp_path.joinpath(*parts))

    hass.config.path = _path  # type: ignore[method-assign]
    return hass


@pytest.fixture
async def repo(hass_with_tmp_config: HomeAssistant) -> AsyncGenerator[InventoryRepository, None]:
    repository = InventoryRepository(hass_with_tmp_config, db_filename="test_simple_inventory.db")
    await repository.async_initialize()
    try:
        yield repository
    finally:
        await repository.async_close()


@pytest.mark.asyncio
async def test_upsert_and_fetch_inventory(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "Desc", "mdi:fridge", "inventory", None)

    inv = await repo.fetch_inventory("inv1")
    assert inv is not None
    assert inv["id"] == "inv1"
    assert inv["name"] == "Kitchen"
    assert inv["description"] == "Desc"
    assert inv["icon"] == "mdi:fridge"
    assert inv["entry_type"] == "inventory"


@pytest.mark.asyncio
async def test_list_inventories_sorted_case_insensitive(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("a", "zeta", "", "", "", None)
    await repo.upsert_inventory("b", "Alpha", "", "", "", None)
    await repo.upsert_inventory("c", "beta", "", "", "", None)

    invs = await repo.list_inventories()
    assert [i["name"] for i in invs] == ["Alpha", "beta", "zeta"]


@pytest.mark.asyncio
async def test_create_item_and_get_by_name_case_insensitive(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)

    item_id = await repo.create_item(
        "inv1",
        {
            FIELD_NAME: "Milk",
            FIELD_QUANTITY: 2,
            FIELD_UNIT: "L",
            FIELD_DESCRIPTION: "Whole milk",
            FIELD_EXPIRY_DATE: "",
            FIELD_EXPIRY_ALERT_DAYS: 0,
            FIELD_AUTO_ADD_ENABLED: False,
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: 0,
            FIELD_TODO_LIST: "",
            "auto_add_id_to_description_enabled": False,
        },
    )

    item = await repo.get_item_by_name("inv1", "milk")
    assert item is not None
    assert item["id"] == item_id
    assert item[FIELD_NAME] == "Milk"
    assert item[FIELD_QUANTITY] == 2


@pytest.mark.asyncio
async def test_create_item_merges_on_name_conflict(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)

    first_id = await repo.create_item(
        "inv1",
        {FIELD_NAME: "Milk", FIELD_QUANTITY: 2, FIELD_DESCRIPTION: "", FIELD_UNIT: ""},
    )
    second_id = await repo.create_item(
        "inv1",
        {FIELD_NAME: "milk", FIELD_QUANTITY: 3, FIELD_DESCRIPTION: "desc", FIELD_UNIT: "L"},
    )

    # Should merge into one row; returned id should be the existing row id
    assert second_id == first_id

    item = await repo.get_item_by_name("inv1", "MILK")
    assert item is not None
    assert item[FIELD_QUANTITY] == 5
    assert item[FIELD_UNIT] == "L"
    assert item[FIELD_DESCRIPTION] == "desc"


@pytest.mark.asyncio
async def test_update_item_updates_fields(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)

    item_id = await repo.create_item("inv1", {FIELD_NAME: "Bread", FIELD_QUANTITY: 1})
    ok = await repo.update_item(item_id, {FIELD_QUANTITY: 5, FIELD_UNIT: "loaf"})
    assert ok is True

    item = await repo.get_item_by_name("inv1", "bread")
    assert item is not None
    assert item[FIELD_QUANTITY] == 5
    assert item[FIELD_UNIT] == "loaf"


@pytest.mark.asyncio
async def test_delete_item_cascades_links(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)

    item_id = await repo.create_item("inv1", {FIELD_NAME: "Milk", FIELD_QUANTITY: 2})

    loc_id = await repo.ensure_location("inv1", "Fridge")
    await repo.set_item_locations(item_id, [(loc_id, 2)])

    cat_id = await repo.ensure_category("Dairy")
    await repo.set_item_categories(item_id, [cat_id])

    ok = await repo.delete_item(item_id)
    assert ok is True

    # Verify it is gone
    item = await repo.get_item_by_name("inv1", "milk")
    assert item is None

    # And links are gone (indirectly: list should be empty)
    items = await repo.list_items_with_details("inv1")
    assert items == []


@pytest.mark.asyncio
async def test_locations_and_categories_show_in_list_items_with_details(
    repo: InventoryRepository,
) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)

    item_id = await repo.create_item("inv1", {FIELD_NAME: "Milk", FIELD_QUANTITY: 2})
    loc_id = await repo.ensure_location("inv1", "Fridge")
    await repo.set_item_locations(item_id, [(loc_id, 2)])
    cat_id = await repo.ensure_category("Dairy")
    await repo.set_item_categories(item_id, [cat_id])

    items = await repo.list_items_with_details("inv1")
    assert len(items) == 1
    item = items[0]
    assert item[FIELD_NAME] == "Milk"
    assert item[FIELD_LOCATION] == "Fridge"
    assert item[FIELD_CATEGORY] == "Dairy"
    assert item["locations"] == [{"name": "Fridge", "quantity": 2}]
    assert item["categories"] == ["Dairy"]


@pytest.mark.asyncio
async def test_list_items_expiring_before(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)
    today = date.today()

    await repo.create_item(
        "inv1",
        {
            FIELD_NAME: "Soon",
            FIELD_QUANTITY: 1,
            FIELD_EXPIRY_DATE: (today + timedelta(days=1)).isoformat(),
            FIELD_EXPIRY_ALERT_DAYS: 7,
        },
    )
    await repo.create_item(
        "inv1",
        {
            FIELD_NAME: "Later",
            FIELD_QUANTITY: 1,
            FIELD_EXPIRY_DATE: (today + timedelta(days=60)).isoformat(),
            FIELD_EXPIRY_ALERT_DAYS: 7,
        },
    )
    await repo.create_item(
        "inv1",
        {
            FIELD_NAME: "ZeroQty",
            FIELD_QUANTITY: 0,
            FIELD_EXPIRY_DATE: (today + timedelta(days=1)).isoformat(),
            FIELD_EXPIRY_ALERT_DAYS: 7,
        },
    )

    results = await repo.list_items_expiring_before(today + timedelta(days=7), inventory_id="inv1")
    names = [r[FIELD_NAME] for r in results]
    assert "Soon" in names
    assert "Later" not in names
    assert "ZeroQty" not in names


@pytest.mark.asyncio
async def test_list_items_with_auto_add_condition(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)

    await repo.create_item(
        "inv1",
        {
            FIELD_NAME: "Eligible",
            FIELD_QUANTITY: 1,
            FIELD_AUTO_ADD_ENABLED: True,
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: 2,
            FIELD_TODO_LIST: "todo.shopping",
        },
    )
    await repo.create_item(
        "inv1",
        {
            FIELD_NAME: "NotEnabled",
            FIELD_QUANTITY: 1,
            FIELD_AUTO_ADD_ENABLED: False,
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: 2,
            FIELD_TODO_LIST: "todo.shopping",
        },
    )
    await repo.create_item(
        "inv1",
        {
            FIELD_NAME: "NoTodoList",
            FIELD_QUANTITY: 1,
            FIELD_AUTO_ADD_ENABLED: True,
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: 2,
            FIELD_TODO_LIST: "",
        },
    )
    await repo.create_item(
        "inv1",
        {
            FIELD_NAME: "TooMuchQty",
            FIELD_QUANTITY: 10,
            FIELD_AUTO_ADD_ENABLED: True,
            FIELD_AUTO_ADD_TO_LIST_QUANTITY: 2,
            FIELD_TODO_LIST: "todo.shopping",
        },
    )

    rows = await repo.list_items_with_auto_add_condition(inventory_id="inv1")
    assert [r[FIELD_NAME] for r in rows] == ["Eligible"]


@pytest.mark.asyncio
async def test_legacy_migration_marks_flag_without_data(repo: InventoryRepository) -> None:
    # Create a fresh repo using a fresh DB file to ensure migration runs
    # (Use a new file name so the above repo doesn't interfere.)
    hass = repo._hass
    fresh = InventoryRepository(hass, db_filename="fresh_migration.db")

    with patch(
        "custom_components.simple_inventory.storage.repository.Store.async_load",
        new=AsyncMock(return_value=None),
    ):
        await fresh.async_initialize()

    # Verify migration flag
    conn = fresh._connection()
    cursor = await conn.execute("SELECT value FROM metadata WHERE key = ?", ("legacy_migrated",))
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row[0] == "1"

    await fresh.async_close()


@pytest.mark.asyncio
async def test_legacy_migration_imports_items(repo: InventoryRepository) -> None:
    hass = repo._hass
    fresh = InventoryRepository(hass, db_filename="fresh_migration_with_data.db")

    legacy_payload: dict[str, Any] = {
        "inventories": {
            "inv_legacy": {
                INVENTORY_NAME: "Legacy Kitchen",
                INVENTORY_ITEMS: {
                    "Milk": {
                        FIELD_NAME: "Milk",
                        FIELD_QUANTITY: 2,
                        FIELD_UNIT: "L",
                        FIELD_LOCATION: "Fridge",
                        FIELD_CATEGORY: "Dairy",
                        FIELD_EXPIRY_DATE: "",
                        FIELD_EXPIRY_ALERT_DAYS: 0,
                        FIELD_DESCRIPTION: "",
                        FIELD_AUTO_ADD_ENABLED: False,
                        "auto_add_id_to_description_enabled": False,
                        FIELD_AUTO_ADD_TO_LIST_QUANTITY: 0,
                        FIELD_TODO_LIST: "",
                    }
                },
            }
        }
    }

    with patch(
        "custom_components.simple_inventory.storage.repository.Store.async_load",
        new=AsyncMock(return_value=legacy_payload),
    ):
        await fresh.async_initialize()

    invs = await fresh.list_inventories()
    assert any(i["id"] == "inv_legacy" for i in invs)

    items = await fresh.list_items_with_details("inv_legacy")
    assert len(items) == 1
    item = items[0]
    assert item[FIELD_NAME] == "Milk"
    assert item[FIELD_LOCATION] == "Fridge"
    assert item[FIELD_CATEGORY] == "Dairy"

    await fresh.async_close()


@pytest.mark.asyncio
async def test_legacy_migration_skips_when_flag_already_set(repo: InventoryRepository) -> None:
    # Force flag in metadata
    conn = repo._connection()
    await conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        ("legacy_migrated", "1"),
    )
    await conn.commit()

    with patch(
        "custom_components.simple_inventory.storage.repository.Store.async_load",
        new=AsyncMock(side_effect=AssertionError("Should not load legacy store")),
    ):
        # Call migration method again; it should return immediately
        await repo._maybe_migrate_legacy_store()


@pytest.mark.asyncio
async def test_schema_version_mismatch_raises(hass_with_tmp_config: HomeAssistant) -> None:
    # Create a fresh DB file and manually set schema_version to something else
    repository = InventoryRepository(hass_with_tmp_config, db_filename="bad_schema.db")
    await repository.async_initialize()

    conn = repository._connection()
    await conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        ("schema_version", "999"),
    )
    await conn.commit()

    await repository.async_close()

    # Re-open; ensure_schema_version should raise
    repository2 = InventoryRepository(hass_with_tmp_config, db_filename="bad_schema.db")
    with pytest.raises(RuntimeError, match="Database schema version"):
        await repository2.async_initialize()
    await repository2.async_close()


@pytest.mark.asyncio
async def test_update_item_no_fields_returns_false(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)
    item_id = await repo.create_item("inv1", {FIELD_NAME: "Milk", FIELD_QUANTITY: 1})

    ok = await repo.update_item(item_id, {})
    assert ok is False


@pytest.mark.asyncio
async def test_delete_item_missing_returns_false(repo: InventoryRepository) -> None:
    ok = await repo.delete_item("does-not-exist")
    assert ok is False


@pytest.mark.asyncio
async def test_list_items_with_details_empty_inventory(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv_empty", "Empty", "", "", "", None)
    items = await repo.list_items_with_details("inv_empty")
    assert items == []


@pytest.mark.asyncio
async def test_get_item_by_name_not_found(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)
    item = await repo.get_item_by_name("inv1", "nope")
    assert item is None


@pytest.mark.asyncio
async def test_set_item_locations_empty_clears_rows(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)
    item_id = await repo.create_item("inv1", {FIELD_NAME: "Milk", FIELD_QUANTITY: 1})
    loc_id = await repo.ensure_location("inv1", "Fridge")

    await repo.set_item_locations(item_id, [(loc_id, 1)])
    await repo.set_item_locations(item_id, [])  # should clear

    items = await repo.list_items_with_details("inv1")
    assert items[0]["locations"] == []


@pytest.mark.asyncio
async def test_set_item_categories_empty_clears_rows(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)
    item_id = await repo.create_item("inv1", {FIELD_NAME: "Milk", FIELD_QUANTITY: 1})
    cat_id = await repo.ensure_category("Dairy")

    await repo.set_item_categories(item_id, [cat_id])
    await repo.set_item_categories(item_id, [])  # should clear

    items = await repo.list_items_with_details("inv1")
    assert items[0]["categories"] == []


@pytest.mark.asyncio
async def test_get_location_quantities_aggregates(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)
    item_id = await repo.create_item("inv1", {FIELD_NAME: "Milk", FIELD_QUANTITY: 5})

    fridge = await repo.ensure_location("inv1", "Fridge")
    pantry = await repo.ensure_location("inv1", "Pantry")

    # Put 2 in fridge, 3 in pantry
    await repo.set_item_locations(item_id, [(fridge, 2), (pantry, 3)])

    totals = await repo.get_location_quantities("inv1")
    assert totals == {"Fridge": 2, "Pantry": 3}


@pytest.mark.asyncio
async def test_get_category_counts_aggregates(repo: InventoryRepository) -> None:
    await repo.upsert_inventory("inv1", "Kitchen", "", "", "", None)
    a = await repo.create_item("inv1", {FIELD_NAME: "Milk", FIELD_QUANTITY: 1})
    b = await repo.create_item("inv1", {FIELD_NAME: "Bread", FIELD_QUANTITY: 1})

    dairy = await repo.ensure_category("Dairy")
    bakery = await repo.ensure_category("Bakery")

    await repo.set_item_categories(a, [dairy])
    await repo.set_item_categories(b, [bakery])

    counts = await repo.get_category_counts("inv1")
    assert counts == {"Dairy": 1, "Bakery": 1}
