"""Repository integration tests for consumption analytics queries."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.simple_inventory.const import FIELD_NAME, FIELD_QUANTITY
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
    repository = InventoryRepository(hass_with_tmp_config, db_filename="test_analytics.db")
    await repository.async_initialize()
    try:
        yield repository
    finally:
        await repository.async_close()


async def _seed_history(
    repo: InventoryRepository,
    item_id: str,
    inventory_id: str,
    events: list[dict[str, Any]],
) -> None:
    """Insert history events with controlled timestamps.

    Each event dict has: event_type, amount, days_ago (int).
    """
    for ev in events:
        ts = (datetime.utcnow() - timedelta(days=ev["days_ago"])).isoformat()
        conn = repo._connection()
        import uuid

        await conn.execute(
            """
            INSERT INTO consumption_history (
                id, item_id, inventory_id, event_type, amount,
                quantity_before, quantity_after, source, timestamp
            ) VALUES (?, ?, ?, ?, ?, 0, 0, 'test', ?)
            """,
            (str(uuid.uuid4()), item_id, inventory_id, ev["event_type"], ev["amount"], ts),
        )
        await conn.commit()


class TestGetItemConsumptionStats:
    async def test_no_history_returns_zeros(self, repo: InventoryRepository) -> None:
        await repo.upsert_inventory("inv1", "Test")
        item_id = await repo.create_item("inv1", {FIELD_NAME: "milk", FIELD_QUANTITY: 5})

        result = await repo.get_item_consumption_stats(item_id)

        assert result["decrement_count"] == 0
        assert result["total_consumed"] == 0.0
        assert result["first_event_ts"] is None
        assert result["last_event_ts"] is None
        assert result["restock_count"] == 0
        assert result["restock_timestamps"] == []
        assert result["window_days"] is None

    async def test_multiple_decrements(self, repo: InventoryRepository) -> None:
        await repo.upsert_inventory("inv1", "Test")
        item_id = await repo.create_item("inv1", {FIELD_NAME: "milk", FIELD_QUANTITY: 5})

        await _seed_history(
            repo,
            item_id,
            "inv1",
            [
                {"event_type": "decrement", "amount": 1.0, "days_ago": 10},
                {"event_type": "decrement", "amount": 2.5, "days_ago": 5},
                {"event_type": "decrement", "amount": 0.5, "days_ago": 1},
            ],
        )

        result = await repo.get_item_consumption_stats(item_id)

        assert result["decrement_count"] == 3
        assert result["total_consumed"] == 4.0
        assert result["first_event_ts"] is not None
        assert result["last_event_ts"] is not None

    async def test_window_days_filters_old_events(self, repo: InventoryRepository) -> None:
        await repo.upsert_inventory("inv1", "Test")
        item_id = await repo.create_item("inv1", {FIELD_NAME: "milk", FIELD_QUANTITY: 5})

        await _seed_history(
            repo,
            item_id,
            "inv1",
            [
                {"event_type": "decrement", "amount": 1.0, "days_ago": 60},
                {"event_type": "decrement", "amount": 2.0, "days_ago": 10},
            ],
        )

        result = await repo.get_item_consumption_stats(item_id, window_days=30)

        assert result["decrement_count"] == 1
        assert result["total_consumed"] == 2.0
        assert result["window_days"] == 30

    async def test_window_none_includes_all(self, repo: InventoryRepository) -> None:
        await repo.upsert_inventory("inv1", "Test")
        item_id = await repo.create_item("inv1", {FIELD_NAME: "milk", FIELD_QUANTITY: 5})

        await _seed_history(
            repo,
            item_id,
            "inv1",
            [
                {"event_type": "decrement", "amount": 1.0, "days_ago": 200},
                {"event_type": "decrement", "amount": 2.0, "days_ago": 10},
            ],
        )

        result = await repo.get_item_consumption_stats(item_id, window_days=None)

        assert result["decrement_count"] == 2
        assert result["total_consumed"] == 3.0

    async def test_restock_timestamps_includes_increment_and_add(
        self, repo: InventoryRepository
    ) -> None:
        await repo.upsert_inventory("inv1", "Test")
        item_id = await repo.create_item("inv1", {FIELD_NAME: "milk", FIELD_QUANTITY: 5})

        await _seed_history(
            repo,
            item_id,
            "inv1",
            [
                {"event_type": "increment", "amount": 3.0, "days_ago": 20},
                {"event_type": "add", "amount": 5.0, "days_ago": 10},
                {"event_type": "decrement", "amount": 1.0, "days_ago": 5},
            ],
        )

        result = await repo.get_item_consumption_stats(item_id)

        assert result["restock_count"] == 2
        assert len(result["restock_timestamps"]) == 2


class TestGetInventoryConsumptionStats:
    async def test_returns_all_items_including_zero_activity(
        self, repo: InventoryRepository
    ) -> None:
        await repo.upsert_inventory("inv1", "Test")
        await repo.create_item("inv1", {FIELD_NAME: "milk", FIELD_QUANTITY: 5})
        item_id2 = await repo.create_item("inv1", {FIELD_NAME: "bread", FIELD_QUANTITY: 2})

        await _seed_history(
            repo,
            item_id2,
            "inv1",
            [
                {"event_type": "decrement", "amount": 1.0, "days_ago": 5},
            ],
        )

        results = await repo.get_inventory_consumption_stats("inv1")

        assert len(results) == 2
        names = {r["item_name"] for r in results}
        assert names == {"milk", "bread"}

        # milk has no history
        milk = next(r for r in results if r["item_name"] == "milk")
        assert milk["decrement_count"] == 0

        # bread has history
        bread = next(r for r in results if r["item_name"] == "bread")
        assert bread["decrement_count"] == 1
        assert bread["total_consumed"] == 1.0

    async def test_per_item_aggregates_correct(self, repo: InventoryRepository) -> None:
        await repo.upsert_inventory("inv1", "Test")
        item_a = await repo.create_item("inv1", {FIELD_NAME: "apple", FIELD_QUANTITY: 10})
        item_b = await repo.create_item("inv1", {FIELD_NAME: "banana", FIELD_QUANTITY: 3})

        await _seed_history(
            repo,
            item_a,
            "inv1",
            [
                {"event_type": "decrement", "amount": 2.0, "days_ago": 10},
                {"event_type": "decrement", "amount": 3.0, "days_ago": 5},
            ],
        )
        await _seed_history(
            repo,
            item_b,
            "inv1",
            [
                {"event_type": "decrement", "amount": 1.0, "days_ago": 3},
            ],
        )

        results = await repo.get_inventory_consumption_stats("inv1")

        apple = next(r for r in results if r["item_name"] == "apple")
        assert apple["decrement_count"] == 2
        assert apple["total_consumed"] == 5.0

        banana = next(r for r in results if r["item_name"] == "banana")
        assert banana["decrement_count"] == 1
        assert banana["total_consumed"] == 1.0

    async def test_window_filter_applies_across_all_items(self, repo: InventoryRepository) -> None:
        await repo.upsert_inventory("inv1", "Test")
        item_a = await repo.create_item("inv1", {FIELD_NAME: "apple", FIELD_QUANTITY: 10})
        item_b = await repo.create_item("inv1", {FIELD_NAME: "banana", FIELD_QUANTITY: 3})

        await _seed_history(
            repo,
            item_a,
            "inv1",
            [
                {"event_type": "decrement", "amount": 2.0, "days_ago": 60},
                {"event_type": "decrement", "amount": 3.0, "days_ago": 5},
            ],
        )
        await _seed_history(
            repo,
            item_b,
            "inv1",
            [
                {"event_type": "decrement", "amount": 1.0, "days_ago": 60},
            ],
        )

        results = await repo.get_inventory_consumption_stats("inv1", window_days=30)

        apple = next(r for r in results if r["item_name"] == "apple")
        assert apple["decrement_count"] == 1
        assert apple["total_consumed"] == 3.0

        banana = next(r for r in results if r["item_name"] == "banana")
        assert banana["decrement_count"] == 0
        assert banana["total_consumed"] == 0.0
