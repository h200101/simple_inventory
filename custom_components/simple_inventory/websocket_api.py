"""WebSocket API for Simple Inventory."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .services.domain_data import get_coordinators

_LOGGER = logging.getLogger(__name__)


def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register WebSocket commands."""
    websocket_api.async_register_command(hass, ws_list_items)
    websocket_api.async_register_command(hass, ws_get_item)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_get_history)
    websocket_api.async_register_command(hass, ws_export)
    websocket_api.async_register_command(hass, ws_import)


async def _handle_list_items(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return all items for an inventory."""
    inventory_id = msg["inventory_id"]
    coordinator = get_coordinators(hass).get(inventory_id)
    if coordinator is None:
        connection.send_error(
            msg["id"],
            "inventory_not_found",
            f"Inventory '{inventory_id}' not found",
        )
        return

    items = await coordinator.async_list_items(inventory_id)
    connection.send_result(msg["id"], {"items": items})


async def _handle_get_item(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return a single item by name."""
    inventory_id = msg["inventory_id"]
    name = msg["name"]
    coordinator = get_coordinators(hass).get(inventory_id)
    if coordinator is None:
        connection.send_error(
            msg["id"],
            "inventory_not_found",
            f"Inventory '{inventory_id}' not found",
        )
        return

    item = await coordinator.async_get_item(inventory_id, name)
    if item is None:
        connection.send_error(
            msg["id"],
            "item_not_found",
            f"Item '{name}' not found in inventory '{inventory_id}'",
        )
        return

    connection.send_result(msg["id"], {"item": item})


async def _handle_get_history(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return history events."""
    inventory_id = msg["inventory_id"]
    coordinator = get_coordinators(hass).get(inventory_id)
    if coordinator is None:
        connection.send_error(
            msg["id"],
            "inventory_not_found",
            f"Inventory '{inventory_id}' not found",
        )
        return

    item_name = msg.get("item_name")
    event_type = msg.get("event_type")
    start_date = msg.get("start_date")
    end_date = msg.get("end_date")
    limit = msg.get("limit", 100)
    offset = msg.get("offset", 0)

    if item_name:
        events = await coordinator.async_get_item_history(
            inventory_id,
            item_name,
            event_type=event_type,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )
    else:
        events = await coordinator.async_get_inventory_history(
            inventory_id,
            event_type=event_type,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

    connection.send_result(msg["id"], {"events": events})


async def _handle_export(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Export inventory data."""
    inventory_id = msg["inventory_id"]
    fmt = msg.get("format", "json")
    coordinator = get_coordinators(hass).get(inventory_id)
    if coordinator is None:
        connection.send_error(
            msg["id"],
            "inventory_not_found",
            f"Inventory '{inventory_id}' not found",
        )
        return

    try:
        result = await coordinator.async_export_inventory(inventory_id, fmt)
        connection.send_result(msg["id"], {"data": result, "format": fmt})
    except ValueError as exc:
        connection.send_error(msg["id"], "export_failed", str(exc))


async def _handle_import(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Import inventory data."""
    inventory_id = msg["inventory_id"]
    fmt = msg.get("format", "json")
    data = msg["data"]
    merge_strategy = msg.get("merge_strategy", "skip")

    coordinator = get_coordinators(hass).get(inventory_id)
    if coordinator is None:
        connection.send_error(
            msg["id"],
            "inventory_not_found",
            f"Inventory '{inventory_id}' not found",
        )
        return

    summary = await coordinator.async_import_inventory(inventory_id, data, fmt, merge_strategy)
    connection.send_result(msg["id"], summary)


def _handle_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe to inventory update events."""
    inventory_id = msg.get("inventory_id")

    if inventory_id:
        event_type = f"{DOMAIN}_updated_{inventory_id}"
    else:
        event_type = f"{DOMAIN}_updated"

    async def _forward_event(event: Any) -> None:
        """Forward HA event to WS subscriber."""
        coordinator = get_coordinators(hass).get(inventory_id or "")
        if inventory_id and coordinator:
            items = await coordinator.async_list_items(inventory_id)
            connection.send_event(msg["id"], {"items": items})
        else:
            connection.send_event(msg["id"], {"event": "updated"})

    unsub = hass.bus.async_listen(event_type, _forward_event)
    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/list_items",
        vol.Required("inventory_id"): str,
    }
)
@websocket_api.async_response
async def ws_list_items(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """WS command: list items."""
    await _handle_list_items(hass, connection, msg)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/get_item",
        vol.Required("inventory_id"): str,
        vol.Required("name"): str,
    }
)
@websocket_api.async_response
async def ws_get_item(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """WS command: get item."""
    await _handle_get_item(hass, connection, msg)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/export",
        vol.Required("inventory_id"): str,
        vol.Optional("format", default="json"): vol.In(["json", "csv"]),
    }
)
@websocket_api.async_response
async def ws_export(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """WS command: export inventory."""
    await _handle_export(hass, connection, msg)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/import",
        vol.Required("inventory_id"): str,
        vol.Required("data"): vol.Any(str, dict, list),
        vol.Optional("format", default="json"): vol.In(["json", "csv"]),
        vol.Optional("merge_strategy", default="skip"): vol.In(
            ["skip", "overwrite", "merge_quantities"]
        ),
    }
)
@websocket_api.async_response
async def ws_import(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """WS command: import inventory."""
    await _handle_import(hass, connection, msg)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/get_history",
        vol.Required("inventory_id"): str,
        vol.Optional("item_name"): str,
        vol.Optional("event_type"): str,
        vol.Optional("start_date"): str,
        vol.Optional("end_date"): str,
        vol.Optional("limit", default=100): int,
        vol.Optional("offset", default=0): int,
    }
)
@websocket_api.async_response
async def ws_get_history(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """WS command: get history."""
    await _handle_get_history(hass, connection, msg)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/subscribe",
        vol.Optional("inventory_id"): str,
    }
)
@callback
def ws_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """WS command: subscribe."""
    _handle_subscribe(hass, connection, msg)
