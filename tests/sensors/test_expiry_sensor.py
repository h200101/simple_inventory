"""Tests for ExpiryNotificationSensor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import EventBus, HomeAssistant

from custom_components.simple_inventory.sensors import ExpiryNotificationSensor


@pytest.fixture
def mock_sensor_coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.async_get_items_expiring_soon = AsyncMock(return_value=[])
    coordinator.async_add_listener = MagicMock(return_value=lambda: None)
    return coordinator


@pytest.fixture
def expiry_sensor(
    hass: HomeAssistant, mock_sensor_coordinator: MagicMock
) -> ExpiryNotificationSensor:
    return ExpiryNotificationSensor(hass, mock_sensor_coordinator, "kitchen_inventory", "Kitchen")


def test_init(expiry_sensor: ExpiryNotificationSensor) -> None:
    assert expiry_sensor._attr_name == "Kitchen Items Expiring Soon"
    assert expiry_sensor._attr_unique_id == "simple_inventory_expiring_items_kitchen_inventory"
    assert expiry_sensor._attr_native_unit_of_measurement == "items"
    assert expiry_sensor.inventory_id == "kitchen_inventory"
    assert expiry_sensor.inventory_name == "Kitchen"


@pytest.mark.asyncio
async def test_async_added_to_hass(expiry_sensor: ExpiryNotificationSensor) -> None:
    with (
        patch.object(expiry_sensor, "_async_update_state", new=AsyncMock()) as mock_update,
        patch.object(EventBus, "async_listen") as mock_listen,
        patch.object(expiry_sensor, "async_on_remove") as mock_on_remove,
    ):
        await expiry_sensor.async_added_to_hass()

        mock_update.assert_awaited_once()
        assert mock_listen.call_count >= 2
        mock_on_remove.assert_called()


@pytest.mark.asyncio
async def test_update_state_with_items(
    expiry_sensor: ExpiryNotificationSensor, mock_sensor_coordinator: MagicMock
) -> None:
    test_items = [
        {
            "inventory_id": "kitchen_inventory",
            "name": "milk",
            "expiry_date": "2024-06-20",
            "days_until_expiry": 5,
            "threshold": 7,
            "quantity": 1,
            "unit": "liter",
            "category": "dairy",
        },
        {
            "inventory_id": "kitchen_inventory",
            "name": "yogurt",
            "expiry_date": "2024-06-14",
            "days_until_expiry": -1,
            "threshold": 7,
            "quantity": 1,
            "unit": "cup",
            "category": "dairy",
        },
    ]

    mock_sensor_coordinator.async_get_items_expiring_soon.return_value = test_items

    with patch.object(expiry_sensor, "async_write_ha_state"):
        await expiry_sensor._async_update_state()

    assert expiry_sensor._attr_native_value == 2

    attributes = expiry_sensor._attr_extra_state_attributes
    assert attributes["inventory_id"] == "kitchen_inventory"
    assert attributes["inventory_name"] == "Kitchen"

    assert len(attributes["expired_items"]) == 1
    assert attributes["expired_items"][0]["name"] == "yogurt"

    assert len(attributes["expiring_items"]) == 1
    assert attributes["expiring_items"][0]["name"] == "milk"

    assert expiry_sensor._attr_icon == "mdi:calendar-remove"


@pytest.mark.asyncio
async def test_update_state_no_items(
    expiry_sensor: ExpiryNotificationSensor, mock_sensor_coordinator: MagicMock
) -> None:
    mock_sensor_coordinator.async_get_items_expiring_soon.return_value = []

    with patch.object(expiry_sensor, "async_write_ha_state"):
        await expiry_sensor._async_update_state()

    assert expiry_sensor._attr_native_value == 0
    assert expiry_sensor._attr_icon == "mdi:calendar-check"

    attributes = expiry_sensor._attr_extra_state_attributes
    assert len(attributes["expiring_items"]) == 0
    assert len(attributes["expired_items"]) == 0


@pytest.mark.asyncio
async def test_update_state_only_expiring(
    expiry_sensor: ExpiryNotificationSensor, mock_sensor_coordinator: MagicMock
) -> None:
    test_items = [
        {
            "inventory_id": "kitchen_inventory",
            "name": "milk",
            "expiry_date": "2024-06-20",
            "days_until_expiry": 5,
            "threshold": 7,
            "quantity": 1,
        }
    ]
    mock_sensor_coordinator.async_get_items_expiring_soon.return_value = test_items

    with patch.object(expiry_sensor, "async_write_ha_state"):
        await expiry_sensor._async_update_state()

    assert expiry_sensor._attr_native_value == 1
    assert expiry_sensor._attr_icon == "mdi:calendar-alert"

    attributes = expiry_sensor._attr_extra_state_attributes
    assert len(attributes["expiring_items"]) == 1
    assert len(attributes["expired_items"]) == 0


@pytest.mark.asyncio
async def test_coordinator_called_with_inventory_id(
    expiry_sensor: ExpiryNotificationSensor, mock_sensor_coordinator: MagicMock
) -> None:
    with patch.object(expiry_sensor, "async_write_ha_state"):
        await expiry_sensor._async_update_state()

    mock_sensor_coordinator.async_get_items_expiring_soon.assert_awaited_once_with(
        "kitchen_inventory"
    )


def test_handle_update_schedules_task(expiry_sensor: ExpiryNotificationSensor) -> None:
    with patch.object(expiry_sensor.hass, "async_create_task") as mock_create_task:
        expiry_sensor._handle_update(None)
        mock_create_task.assert_called_once()
