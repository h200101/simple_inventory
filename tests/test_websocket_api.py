"""Tests for the WebSocket API."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.simple_inventory.const import DOMAIN
from custom_components.simple_inventory.websocket_api import (
    _handle_export,
    _handle_get_history,
    _handle_get_item,
    _handle_import,
    _handle_list_items,
    _handle_subscribe,
)


@pytest.fixture
def mock_connection() -> MagicMock:
    """Create a mock WebSocket connection."""
    conn = MagicMock()
    conn.send_result = MagicMock()
    conn.send_error = MagicMock()
    conn.send_event = MagicMock()
    conn.subscriptions: dict[int, Any] = {}
    return conn


@pytest.fixture
def mock_coordinator_ws() -> MagicMock:
    """Create a mock coordinator for WS tests."""
    coordinator = MagicMock()
    coordinator.async_list_items = AsyncMock(
        return_value=[
            {"name": "milk", "quantity": 2},
            {"name": "bread", "quantity": 1},
        ]
    )
    coordinator.async_get_item = AsyncMock(return_value={"name": "milk", "quantity": 2})
    coordinator.async_get_item_history = AsyncMock(return_value=[])
    coordinator.async_get_inventory_history = AsyncMock(return_value=[{"event_type": "add"}])
    coordinator.async_export_inventory = AsyncMock(return_value={"version": "1.0", "items": []})
    coordinator.async_import_inventory = AsyncMock(
        return_value={"added": 1, "updated": 0, "skipped": 0, "errors": []}
    )
    return coordinator


class TestHandleListItems:
    async def test_list_items_success(
        self, hass_mock: MagicMock, mock_connection: MagicMock, mock_coordinator_ws: MagicMock
    ) -> None:
        hass_mock.data[DOMAIN]["coordinators"]["inv1"] = mock_coordinator_ws
        msg = {"id": 1, "type": f"{DOMAIN}/list_items", "inventory_id": "inv1"}

        await _handle_list_items(hass_mock, mock_connection, msg)

        mock_coordinator_ws.async_list_items.assert_awaited_once_with("inv1")
        mock_connection.send_result.assert_called_once_with(
            1,
            {
                "items": [
                    {"name": "milk", "quantity": 2},
                    {"name": "bread", "quantity": 1},
                ]
            },
        )

    async def test_list_items_inventory_not_found(
        self, hass_mock: MagicMock, mock_connection: MagicMock
    ) -> None:
        msg = {"id": 1, "type": f"{DOMAIN}/list_items", "inventory_id": "missing"}

        await _handle_list_items(hass_mock, mock_connection, msg)

        mock_connection.send_error.assert_called_once_with(
            1, "inventory_not_found", "Inventory 'missing' not found"
        )


class TestHandleGetItem:
    async def test_get_item_success(
        self, hass_mock: MagicMock, mock_connection: MagicMock, mock_coordinator_ws: MagicMock
    ) -> None:
        hass_mock.data[DOMAIN]["coordinators"]["inv1"] = mock_coordinator_ws
        msg = {
            "id": 2,
            "type": f"{DOMAIN}/get_item",
            "inventory_id": "inv1",
            "name": "milk",
        }

        await _handle_get_item(hass_mock, mock_connection, msg)

        mock_coordinator_ws.async_get_item.assert_awaited_once_with("inv1", "milk")
        mock_connection.send_result.assert_called_once_with(
            2, {"item": {"name": "milk", "quantity": 2}}
        )

    async def test_get_item_inventory_not_found(
        self, hass_mock: MagicMock, mock_connection: MagicMock
    ) -> None:
        msg = {
            "id": 2,
            "type": f"{DOMAIN}/get_item",
            "inventory_id": "missing",
            "name": "milk",
        }

        await _handle_get_item(hass_mock, mock_connection, msg)

        mock_connection.send_error.assert_called_once_with(
            2, "inventory_not_found", "Inventory 'missing' not found"
        )

    async def test_get_item_not_found(
        self, hass_mock: MagicMock, mock_connection: MagicMock, mock_coordinator_ws: MagicMock
    ) -> None:
        hass_mock.data[DOMAIN]["coordinators"]["inv1"] = mock_coordinator_ws
        mock_coordinator_ws.async_get_item.return_value = None
        msg = {
            "id": 3,
            "type": f"{DOMAIN}/get_item",
            "inventory_id": "inv1",
            "name": "nonexistent",
        }

        await _handle_get_item(hass_mock, mock_connection, msg)

        mock_connection.send_error.assert_called_once_with(
            3,
            "item_not_found",
            "Item 'nonexistent' not found in inventory 'inv1'",
        )


class TestHandleSubscribe:
    def test_subscribe_with_inventory_id(
        self, hass_mock: MagicMock, mock_connection: MagicMock
    ) -> None:
        msg = {
            "id": 4,
            "type": f"{DOMAIN}/subscribe",
            "inventory_id": "inv1",
        }

        _handle_subscribe(hass_mock, mock_connection, msg)

        hass_mock.bus.async_listen.assert_called_once()
        event_type = hass_mock.bus.async_listen.call_args[0][0]
        assert event_type == f"{DOMAIN}_updated_inv1"
        assert 4 in mock_connection.subscriptions
        mock_connection.send_result.assert_called_once_with(4)

    def test_subscribe_global(self, hass_mock: MagicMock, mock_connection: MagicMock) -> None:
        msg = {
            "id": 5,
            "type": f"{DOMAIN}/subscribe",
        }

        _handle_subscribe(hass_mock, mock_connection, msg)

        hass_mock.bus.async_listen.assert_called_once()
        event_type = hass_mock.bus.async_listen.call_args[0][0]
        assert event_type == f"{DOMAIN}_updated"
        assert 5 in mock_connection.subscriptions
        mock_connection.send_result.assert_called_once_with(5)

    async def test_subscribe_forwards_events(
        self, hass_mock: MagicMock, mock_connection: MagicMock, mock_coordinator_ws: MagicMock
    ) -> None:
        hass_mock.data[DOMAIN]["coordinators"]["inv1"] = mock_coordinator_ws
        msg = {
            "id": 6,
            "type": f"{DOMAIN}/subscribe",
            "inventory_id": "inv1",
        }

        _handle_subscribe(hass_mock, mock_connection, msg)

        # Get the callback that was registered
        listener_callback = hass_mock.bus.async_listen.call_args[0][1]

        # Simulate an event firing
        mock_event = MagicMock()
        await listener_callback(mock_event)

        mock_coordinator_ws.async_list_items.assert_awaited_once_with("inv1")
        mock_connection.send_event.assert_called_once()
        event_data = mock_connection.send_event.call_args[0]
        assert event_data[0] == 6
        assert "items" in event_data[1]


class TestHandleGetHistory:
    async def test_get_inventory_history(
        self, hass_mock: MagicMock, mock_connection: MagicMock, mock_coordinator_ws: MagicMock
    ) -> None:
        hass_mock.data[DOMAIN]["coordinators"]["inv1"] = mock_coordinator_ws
        msg = {
            "id": 20,
            "type": f"{DOMAIN}/get_history",
            "inventory_id": "inv1",
        }

        await _handle_get_history(hass_mock, mock_connection, msg)

        mock_coordinator_ws.async_get_inventory_history.assert_awaited_once()
        mock_connection.send_result.assert_called_once()
        result = mock_connection.send_result.call_args[0][1]
        assert "events" in result

    async def test_get_item_history(
        self, hass_mock: MagicMock, mock_connection: MagicMock, mock_coordinator_ws: MagicMock
    ) -> None:
        hass_mock.data[DOMAIN]["coordinators"]["inv1"] = mock_coordinator_ws
        msg = {
            "id": 21,
            "type": f"{DOMAIN}/get_history",
            "inventory_id": "inv1",
            "item_name": "milk",
        }

        await _handle_get_history(hass_mock, mock_connection, msg)

        mock_coordinator_ws.async_get_item_history.assert_awaited_once()

    async def test_get_history_inventory_not_found(
        self, hass_mock: MagicMock, mock_connection: MagicMock
    ) -> None:
        msg = {
            "id": 22,
            "type": f"{DOMAIN}/get_history",
            "inventory_id": "missing",
        }

        await _handle_get_history(hass_mock, mock_connection, msg)

        mock_connection.send_error.assert_called_once()


class TestHandleExport:
    async def test_export_success(
        self, hass_mock: MagicMock, mock_connection: MagicMock, mock_coordinator_ws: MagicMock
    ) -> None:
        hass_mock.data[DOMAIN]["coordinators"]["inv1"] = mock_coordinator_ws
        msg = {
            "id": 30,
            "type": f"{DOMAIN}/export",
            "inventory_id": "inv1",
            "format": "json",
        }

        await _handle_export(hass_mock, mock_connection, msg)

        mock_coordinator_ws.async_export_inventory.assert_awaited_once_with("inv1", "json")
        mock_connection.send_result.assert_called_once()

    async def test_export_inventory_not_found(
        self, hass_mock: MagicMock, mock_connection: MagicMock
    ) -> None:
        msg = {
            "id": 31,
            "type": f"{DOMAIN}/export",
            "inventory_id": "missing",
            "format": "json",
        }

        await _handle_export(hass_mock, mock_connection, msg)

        mock_connection.send_error.assert_called_once()


class TestHandleImport:
    async def test_import_success(
        self, hass_mock: MagicMock, mock_connection: MagicMock, mock_coordinator_ws: MagicMock
    ) -> None:
        hass_mock.data[DOMAIN]["coordinators"]["inv1"] = mock_coordinator_ws
        msg = {
            "id": 40,
            "type": f"{DOMAIN}/import",
            "inventory_id": "inv1",
            "data": {"items": [{"name": "Apple", "quantity": 5}]},
            "format": "json",
            "merge_strategy": "skip",
        }

        await _handle_import(hass_mock, mock_connection, msg)

        mock_coordinator_ws.async_import_inventory.assert_awaited_once()
        mock_connection.send_result.assert_called_once()

    async def test_import_inventory_not_found(
        self, hass_mock: MagicMock, mock_connection: MagicMock
    ) -> None:
        msg = {
            "id": 41,
            "type": f"{DOMAIN}/import",
            "inventory_id": "missing",
            "data": {"items": []},
        }

        await _handle_import(hass_mock, mock_connection, msg)

        mock_connection.send_error.assert_called_once()
