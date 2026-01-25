"""Tests for the SimpleInventoryCoordinator class (SQLite-backed)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import EventBus, HomeAssistant

from custom_components.simple_inventory.const import (
    DOMAIN,
    FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED,
    FIELD_DESCRIPTION,
    FIELD_NAME,
    FIELD_QUANTITY,
)
from custom_components.simple_inventory.coordinator import SimpleInventoryCoordinator


@pytest.fixture
def mock_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "kitchen_123"
    entry.data = {"name": "Kitchen", "entry_type": "inventory"}
    entry.title = "Kitchen"
    return entry


@pytest.fixture
def mock_repository(sample_inventory_data: dict) -> MagicMock:
    repo = MagicMock()

    repo.async_initialize = AsyncMock()

    # Basic inventory metadata
    repo.list_inventories = AsyncMock(
        return_value=[
            {
                "id": "kitchen_123",
                "name": "Kitchen",
                "description": "",
                "icon": "",
                "entry_type": "inventory",
            },
            {
                "id": "pantry_123",
                "name": "Pantry",
                "description": "",
                "icon": "",
                "entry_type": "inventory",
            },
        ]
    )

    # Use fixture items as "DB rows"
    repo.list_items_with_details = AsyncMock(
        side_effect=lambda inv_id: {
            "kitchen_123": sample_inventory_data["kitchen"]["items"],
            "pantry_123": sample_inventory_data["pantry"]["items"],
        }.get(inv_id, [])
    )

    async def _get_item_by_name(inv_id: str, name: str) -> dict[str, Any] | None:
        items = await repo.list_items_with_details(inv_id)
        for it in items:
            if str(it.get("name", "")).lower() == name.lower():
                return {"id": it.get("id", f"{inv_id}:{it['name']}"), **it}
        return None

    repo.get_item_by_name = AsyncMock(side_effect=_get_item_by_name)

    repo.create_item = AsyncMock(return_value="new-item-id")
    repo.update_item = AsyncMock(return_value=True)
    repo.delete_item = AsyncMock(return_value=True)

    repo.ensure_location = AsyncMock(return_value=1)
    repo.set_item_locations = AsyncMock()
    repo.ensure_category = AsyncMock(return_value=1)
    repo.set_item_categories = AsyncMock()

    repo.upsert_inventory = AsyncMock()

    return repo


@pytest.fixture
def coordinator(
    hass: HomeAssistant, mock_entry: MagicMock, mock_repository: MagicMock
) -> SimpleInventoryCoordinator:
    return SimpleInventoryCoordinator(hass, mock_entry, mock_repository)


@pytest.mark.asyncio
async def test_async_unload_removes_listeners(
    coordinator: SimpleInventoryCoordinator,
) -> None:
    listener = MagicMock()
    remove = coordinator.async_add_listener(listener)
    assert listener in coordinator._listeners

    await coordinator.async_unload()
    assert coordinator._listeners == []

    # remove callback should be safe even after unload
    remove()
    assert coordinator._listeners == []


@pytest.mark.asyncio
async def test_async_initialize_is_idempotent(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    await coordinator.async_initialize()
    await coordinator.async_initialize()
    # Coordinator init should not call repo.async_initialize (repo is initialized in __init__.py),
    # but if you ever add it back, this test will catch multiple calls.
    mock_repository.async_initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_save_data_fires_events(
    coordinator: SimpleInventoryCoordinator,
) -> None:
    with patch.object(EventBus, "async_fire") as mock_fire:
        await coordinator.async_save_data("kitchen_123")

        mock_fire.assert_any_call(f"{DOMAIN}_updated_kitchen_123")
        mock_fire.assert_any_call(f"{DOMAIN}_updated")


@pytest.mark.asyncio
async def test_async_add_item_applies_description_suffix(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    mock_repository.create_item.reset_mock()

    with patch.object(EventBus, "async_fire"):
        item_id = await coordinator.async_add_item(
            "kitchen_123",
            name="coffee",
            quantity=1,
            description="Pantry staple",
            auto_add_id_to_description_enabled=True,
        )

    assert item_id == "new-item-id"
    assert mock_repository.create_item.await_count == 1

    # Verify payload passed to repository includes suffix
    _, args, _ = mock_repository.create_item.mock_calls[0]
    assert args[0] == "kitchen_123"
    payload = args[1]
    assert payload[FIELD_NAME] == "coffee"
    assert payload[FIELD_DESCRIPTION] == "Pantry staple (kitchen_123)"
    assert payload[FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED] is True


@pytest.mark.asyncio
async def test_async_add_item_empty_name_raises(coordinator: SimpleInventoryCoordinator) -> None:
    with pytest.raises(ValueError):
        await coordinator.async_add_item("kitchen_123", name="", quantity=1)

    with pytest.raises(ValueError):
        await coordinator.async_add_item("kitchen_123", name="   ", quantity=1)


@pytest.mark.asyncio
async def test_async_increment_item_item_missing_returns_false(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    mock_repository.get_item_by_name = AsyncMock(return_value=None)

    ok = await coordinator.async_increment_item("kitchen_123", "nope", 1)
    assert ok is False
    mock_repository.update_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_increment_item_update_fails_returns_false(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    mock_repository.get_item_by_name = AsyncMock(return_value={"id": "x", "quantity": 1})
    mock_repository.update_item = AsyncMock(return_value=False)

    with patch.object(EventBus, "async_fire"):
        ok = await coordinator.async_increment_item("kitchen_123", "milk", 1)

    assert ok is False


@pytest.mark.asyncio
async def test_async_update_item_updates_location_and_category(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    mock_repository.get_item_by_name = AsyncMock(
        return_value={"id": "item1", "name": "milk", "quantity": 2}
    )
    mock_repository.update_item = AsyncMock(return_value=True)
    mock_repository.ensure_location = AsyncMock(return_value=7)
    mock_repository.ensure_category = AsyncMock(return_value=9)

    with patch.object(EventBus, "async_fire"):
        ok = await coordinator.async_update_item(
            "kitchen_123",
            old_name="milk",
            new_name="milk",
            location="Fridge",
            category="Dairy",
            quantity=2,
        )

    assert ok is True
    mock_repository.ensure_location.assert_awaited_once_with("kitchen_123", "Fridge")
    mock_repository.set_item_locations.assert_awaited_once_with("item1", [(7, 2)])
    mock_repository.ensure_category.assert_awaited_once_with("Dairy")
    mock_repository.set_item_categories.assert_awaited_once_with("item1", [9])


@pytest.mark.asyncio
async def test_async_get_items_expiring_soon_skips_invalid_date(
    coordinator: SimpleInventoryCoordinator,
    mock_repository: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_repository.list_items_with_details = AsyncMock(
        return_value=[
            {"name": "bad", "expiry_date": "not-a-date", "expiry_alert_days": 7, "quantity": 1},
        ]
    )

    with caplog.at_level("WARNING"):
        items = await coordinator.async_get_items_expiring_soon("kitchen_123")

    assert items == []
    assert "Invalid expiry date format" in caplog.text


@pytest.mark.asyncio
async def test_async_get_items_expiring_soon_global_uses_list_inventories(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    today = datetime.now().date()
    soon = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    mock_repository.list_inventories = AsyncMock(
        return_value=[{"id": "kitchen_123"}, {"id": "pantry_123"}]
    )
    mock_repository.list_items_with_details = AsyncMock(
        side_effect=lambda inv_id: [
            {"name": f"{inv_id}_item", "expiry_date": soon, "expiry_alert_days": 7, "quantity": 1}
        ]
    )

    items = await coordinator.async_get_items_expiring_soon()

    assert len(items) == 2
    assert {i["inventory_id"] for i in items} == {"kitchen_123", "pantry_123"}


@pytest.mark.asyncio
async def test_async_add_item_invalid_auto_add_config_returns_none(
    coordinator: SimpleInventoryCoordinator,
) -> None:
    # auto_add_enabled=True but todo_list empty => should fail validation
    item_id = await coordinator.async_add_item(
        "kitchen_123",
        name="butter",
        quantity=1,
        auto_add_enabled=True,
        auto_add_to_list_quantity=0,
        todo_list="",
    )
    assert item_id is None


@pytest.mark.asyncio
async def test_async_update_item_recalculates_description_on_flag_toggle(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    # Existing item in repository without suffix
    mock_repository.get_item_by_name = AsyncMock(
        return_value={
            "id": "milk-id",
            "name": "milk",
            "description": "Fresh milk (kitchen_123)",
            "quantity": 2,
            "auto_add_id_to_description_enabled": True,
        }
    )

    with patch.object(EventBus, "async_fire"):
        ok = await coordinator.async_update_item(
            "kitchen_123",
            old_name="milk",
            new_name="milk",
            auto_add_id_to_description_enabled=False,
            description="Fresh milk (kitchen_123)",
        )

    assert ok is True
    assert mock_repository.update_item.await_count == 1

    # update_item(item_id, payload)
    _, args, _ = mock_repository.update_item.mock_calls[0]
    assert args[0] == "milk-id"
    payload = args[1]
    assert payload[FIELD_AUTO_ADD_ID_TO_DESCRIPTION_ENABLED] is False
    assert payload[FIELD_DESCRIPTION] == "Fresh milk"


@pytest.mark.asyncio
async def test_async_remove_item_deletes_when_present(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    mock_repository.get_item_by_name = AsyncMock(return_value={"id": "x", "name": "milk"})

    with patch.object(EventBus, "async_fire"):
        ok = await coordinator.async_remove_item("kitchen_123", "milk")

    assert ok is True
    mock_repository.delete_item.assert_awaited_once_with("x")


@pytest.mark.asyncio
async def test_async_remove_item_returns_false_when_missing(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    mock_repository.get_item_by_name = AsyncMock(return_value=None)
    ok = await coordinator.async_remove_item("kitchen_123", "nope")
    assert ok is False
    mock_repository.delete_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_increment_item_adjusts_quantity(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    mock_repository.get_item_by_name = AsyncMock(return_value={"id": "milk-id", "quantity": 2})
    mock_repository.update_item = AsyncMock(return_value=True)

    with patch.object(EventBus, "async_fire"):
        ok = await coordinator.async_increment_item("kitchen_123", "milk", 3)

    assert ok is True
    mock_repository.update_item.assert_awaited_once_with("milk-id", {FIELD_QUANTITY: 5})


@pytest.mark.asyncio
async def test_async_increment_item_negative_amount_fails(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    ok = await coordinator.async_increment_item("kitchen_123", "milk", -1)
    assert ok is False
    mock_repository.get_item_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_decrement_item_does_not_go_below_zero(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    mock_repository.get_item_by_name = AsyncMock(return_value={"id": "milk-id", "quantity": 2})
    mock_repository.update_item = AsyncMock(return_value=True)

    with patch.object(EventBus, "async_fire"):
        ok = await coordinator.async_decrement_item("kitchen_123", "milk", 99)

    assert ok is True
    mock_repository.update_item.assert_awaited_once_with("milk-id", {FIELD_QUANTITY: 0})


@pytest.mark.asyncio
async def test_async_get_item_passthrough(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    mock_repository.get_item_by_name = AsyncMock(return_value={"id": "x", "name": "milk"})
    item = await coordinator.async_get_item("kitchen_123", "milk")
    assert item is not None
    assert item["name"] == "milk"


@pytest.mark.asyncio
async def test_async_list_items_passthrough(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    mock_repository.list_items_with_details = AsyncMock(return_value=[{"name": "milk"}])
    items = await coordinator.async_list_items("kitchen_123")
    assert items == [{"name": "milk"}]


@pytest.mark.asyncio
async def test_async_get_inventory_statistics_counts(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    # minimal two items
    mock_repository.list_items_with_details = AsyncMock(
        return_value=[
            {
                "name": "milk",
                "quantity": 2,
                "category": "dairy",
                "location": "fridge",
                "locations": [],
            },
            {
                "name": "bread",
                "quantity": 1,
                "category": "bakery",
                "location": "pantry",
                "locations": [],
            },
        ]
    )

    with patch.object(coordinator, "async_get_items_expiring_soon", new=AsyncMock(return_value=[])):
        stats = await coordinator.async_get_inventory_statistics("kitchen_123")

    assert stats["total_items"] == 2
    assert stats["total_quantity"] == 3
    assert stats["categories"]["dairy"] == 1
    assert stats["categories"]["bakery"] == 1


@pytest.mark.asyncio
async def test_async_get_items_expiring_soon_filters_and_sorts(
    coordinator: SimpleInventoryCoordinator, mock_repository: MagicMock
) -> None:
    today = datetime.now().date()
    soon = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    later = (today + timedelta(days=30)).strftime("%Y-%m-%d")
    expired = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    mock_repository.list_items_with_details = AsyncMock(
        return_value=[
            {"name": "soon", "expiry_date": soon, "expiry_alert_days": 7, "quantity": 1},
            {"name": "later", "expiry_date": later, "expiry_alert_days": 7, "quantity": 1},
            {"name": "expired", "expiry_date": expired, "expiry_alert_days": 7, "quantity": 1},
            {"name": "no_date", "expiry_date": "", "expiry_alert_days": 7, "quantity": 1},
            {"name": "zero_qty", "expiry_date": soon, "expiry_alert_days": 7, "quantity": 0},
        ]
    )

    items = await coordinator.async_get_items_expiring_soon("kitchen_123")

    # later/no_date/zero_qty excluded, remaining expired + soon
    names = [it["name"] for it in items]
    assert names == ["expired", "soon"]
    assert items[0]["days_until_expiry"] < items[1]["days_until_expiry"]


def test_async_add_listener_and_notify(coordinator: SimpleInventoryCoordinator) -> None:
    listener1 = MagicMock()
    listener2 = MagicMock()

    remove1 = coordinator.async_add_listener(listener1)
    coordinator.async_add_listener(listener2)

    coordinator.notify_listeners()

    listener1.assert_called_once()
    listener2.assert_called_once()

    remove1()
    assert listener1 not in coordinator._listeners
