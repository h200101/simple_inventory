[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

# Simple Inventory

A Home Assistant custom integration for managing household inventories. Track items across multiple inventories with expiration dates, automatic todo list integration, barcode support, locations, categories, and change history.

## Table of Contents

- [Installation](#installation)
- [Configuration](#configuration)
- [Features](#features)
- [Sensors](#sensors)
- [Service Calls](#service-calls)
- [WebSocket API](#websocket-api)
- [Automation Examples](#automation-examples)

## Installation

### Via HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=blaineventurine&repository=simple_inventory)

or:

1. Add this repository to HACS
2. Install "Simple Inventory"
3. Install the companion card: [Simple Inventory Card](https://github.com/blaineventurine/simple-inventory-card)
4. Restart Home Assistant

### Manual Installation

1. Copy `custom_components/simple_inventory/` to your Home Assistant `custom_components/` directory
2. Restart Home Assistant

## Frontend Card

This integration works best with the companion Lovelace card:
**[Simple Inventory Card](https://github.com/blaineventurine/simple-inventory-card)**

## Configuration

Add via Home Assistant UI: **Settings -> Devices & Services -> Add Integration -> Simple Inventory**

When you create your first inventory, a global device is also created automatically. Each inventory becomes a device with two sensors.

You can edit inventory names, icons, and descriptions in the integration options flow after creation.

## Features

### Items

Each item has a **name** (required) and these optional fields:

| Field | Description |
|---|---|
| `quantity` | Current stock level (supports decimals, e.g. 2.5) |
| `unit` | Unit of measurement (e.g. "boxes", "L", "kg") |
| `description` | Free-text description |
| `barcode` | UPC/EAN barcode for scanning |
| `location` | Where the item is stored (supports multiple, comma-separated) |
| `category` | Item category (supports multiple, comma-separated) |
| `expiry_date` | Expiration date (YYYY-MM-DD) |
| `expiry_alert_days` | Days before expiry to trigger alerts |

### Multi-Value Locations and Categories

Items can belong to multiple locations and categories. Pass comma-separated values:

```yaml
location: "Pantry, Kitchen"
category: "Snacks, Bulk Items"
```

The API returns both scalar fields (`location`, `category` with the first value) and array fields (`locations`, `categories` with all values) for backward compatibility.

### Expiration Tracking

Set an `expiry_date` and `expiry_alert_days` on any item. When the number of remaining days is at or below your threshold, the item appears on the expiry sensors. Items with zero quantity are excluded from expiry alerts.

### Auto-Add to Todo List

Automatically add items to a Home Assistant todo list when stock drops below a threshold. Configure these fields:

| Field | Description |
|---|---|
| `auto_add_enabled` | Enable/disable auto-add for this item |
| `auto_add_to_list_quantity` | Quantity threshold that triggers adding to the list |
| `desired_quantity` | Target stock level (controls how much to buy) |
| `todo_list` | Target todo list entity (e.g. `todo.shopping_list`) |
| `todo_quantity_placement` | Where to show the needed quantity: `"name"` (e.g. "Milk (x4)"), `"description"`, or `"none"` |

**How it works:**

- When `quantity` drops to or below `auto_add_to_list_quantity`, the item is added to your todo list
- When `desired_quantity > 0` (fixed mode): the todo shows the desired quantity, and the item is removed from the list when `quantity >= desired_quantity`
- When `desired_quantity = 0` (threshold mode): the todo shows `threshold - quantity + 1`, updates live on each change, and the item is removed when `quantity > threshold`

**Note:** The built-in `todo.shopping_list` does not support item descriptions, so description-based features only work with other todo list integrations.

### Description with Inventory ID

Enable `auto_add_id_to_description_enabled` to append the inventory ID to item descriptions. This is useful when scanning barcodes from todo lists — the ID lets automations route the item back to the correct inventory. See [this issue](https://github.com/blaineventurine/simple_inventory/issues/19) for the use case.

### Barcodes

Items can have barcodes associated with them. Most service calls accept either `name` or `barcode` to identify an item, so you can build barcode-scanning automations (e.g. scan to increment/decrement).

### Change History

Every add, remove, increment, and decrement is recorded with before/after quantities and timestamps. Query history via the WebSocket API.

### Import and Export

Export your inventory to JSON or CSV, and import data back with configurable merge strategies (`skip`, `overwrite`, or `merge_quantities`). Available via the WebSocket API.

## Sensors

### Per-Inventory Sensors

Each inventory creates two sensors:

**`sensor.<name>_inventory`** — Main inventory sensor
- **State**: Total quantity across all items
- **Attributes**:
  - `inventory_id` — The config entry ID
  - `total_items` — Number of distinct items
  - `total_quantity` — Sum of all quantities
  - `categories` — Category names with item counts
  - `locations` — Location names with item counts
  - `below_threshold` — Items that need restocking (with `quantity_needed`)
  - `expiring_soon` — Count of items expiring soon
  - `items` — Full list of all items with all fields

**`sensor.<name>_items_expiring_soon`** — Expiry alert sensor
- **State**: Count of expiring + expired items
- **Icon**: Changes dynamically (`mdi:calendar-remove` for expired, `mdi:calendar-alert` for expiring, `mdi:calendar-check` when all clear)
- **Attributes**:
  - `expiring_items` — List of items expiring soon
  - `expired_items` — List of already-expired items
  - `total_expiring`, `total_expired` — Counts

### Global Sensor

**`sensor.all_items_expiring_soon`** — Aggregates expiring items across ALL inventories
- **State**: Total count of expiring items across all inventories
- **Icon**: Progressive severity (`mdi:calendar-remove` -> `mdi:calendar-alert` -> `mdi:calendar-clock` -> `mdi:calendar-week` -> `mdi:calendar-check`)
- **Attributes**:
  - `expiring_items`, `expired_items` — Aggregated lists
  - `next_expiring`, `oldest_expired` — Soonest/oldest dates
  - `inventories_count` — Number of inventories

## Service Calls

All services are under the `simple_inventory` domain. You can find your inventory ID by going to **Developer Tools -> States** and filtering for "inventory" — the ID is shown in the sensor attributes.

### `simple_inventory.add_item`

Add a new item to an inventory.

```yaml
service: simple_inventory.add_item
data:
  inventory_id: "01JYFPCDMBRBRK4MB3C26S2FKH"
  name: "Frozen Pizza"
  quantity: 5
  unit: "boxes"
  category: "Frozen Foods"
  location: "Basement Freezer"
  barcode: "012345678901"
  expiry_date: "2026-06-15"
  expiry_alert_days: 7
  auto_add_enabled: true
  auto_add_to_list_quantity: 2
  desired_quantity: 5
  todo_list: "todo.grocery_list"
  todo_quantity_placement: "name"
  description: "Family size pepperoni"
```

Only `inventory_id` and `name` are required. All other fields are optional.

### `simple_inventory.remove_item`

Remove an item. Specify either `name` or `barcode`.

```yaml
service: simple_inventory.remove_item
data:
  inventory_id: "01JYFPCDMBRBRK4MB3C26S2FKH"
  name: "Frozen Pizza"
```

### `simple_inventory.increment_item`

Increase an item's quantity. Supports decimal amounts.

```yaml
service: simple_inventory.increment_item
data:
  inventory_id: "01JYFPCDMBRBRK4MB3C26S2FKH"
  name: "Frozen Pizza"
  amount: 3
```

You can also use `barcode` instead of `name`:

```yaml
service: simple_inventory.increment_item
data:
  inventory_id: "01JYFPCDMBRBRK4MB3C26S2FKH"
  barcode: "012345678901"
  amount: 1
```

### `simple_inventory.decrement_item`

Decrease an item's quantity. Same parameters as `increment_item`.

```yaml
service: simple_inventory.decrement_item
data:
  inventory_id: "01JYFPCDMBRBRK4MB3C26S2FKH"
  name: "Frozen Pizza"
  amount: 1
```

### `simple_inventory.update_item`

Update any fields on an existing item. Use `old_name` to identify the item and `name` for the (possibly new) name.

```yaml
service: simple_inventory.update_item
data:
  inventory_id: "01JYFPCDMBRBRK4MB3C26S2FKH"
  old_name: "Frozen Pizza"
  name: "Frozen Pizza"
  category: "Frozen Foods, Quick Meals"
  location: "Kitchen Freezer"
  expiry_date: "2026-08-01"
```

### `simple_inventory.get_items`

Retrieve all items for a specific inventory. Supports responses — set `return_response: true` in Developer Tools.

Specify the inventory by ID or name (case-insensitive), but not both:

```yaml
service: simple_inventory.get_items
data:
  inventory_id: "01JYFPCDMBRBRK4MB3C26S2FKH"
```

```yaml
service: simple_inventory.get_items
data:
  inventory_name: "Kitchen Freezer"
```

Example response:

```json
{
  "items": [
    {
      "name": "Frozen Pizza",
      "quantity": 5.0,
      "unit": "boxes",
      "category": "Frozen Foods",
      "categories": ["Frozen Foods", "Quick Meals"],
      "location": "Kitchen Freezer",
      "locations": ["Kitchen Freezer"],
      "expiry_date": "2026-06-15",
      "expiry_alert_days": 7,
      "auto_add_enabled": true,
      "auto_add_to_list_quantity": 2.0,
      "desired_quantity": 5.0,
      "todo_list": "todo.grocery_list",
      "todo_quantity_placement": "name",
      "description": "Family size pepperoni",
      "barcode": "012345678901",
      "barcodes": ["012345678901"]
    }
  ]
}
```

Note the dual fields: `category`/`categories` and `location`/`locations`. The singular fields contain only the first value for backward compatibility. Prefer the array fields.

### `simple_inventory.get_items_from_all_inventories`

Retrieve items from every inventory at once.

```yaml
service: simple_inventory.get_items_from_all_inventories
```

Example response:

```json
{
  "inventories": [
    {
      "inventory_id": "01JYFPCDMBRBRK4MB3C26S2FKH",
      "inventory_name": "Kitchen Freezer",
      "description": "Frozen items",
      "items": [
        {
          "name": "Frozen Pizza",
          "quantity": 5.0,
          "unit": "boxes"
        }
      ]
    }
  ]
}
```

## WebSocket API

The integration provides WebSocket commands for real-time communication. These are used by the companion card and can also be used by custom panels or scripts.

### `simple_inventory/list_items`

Fetch all items for an inventory.

```json
{
  "type": "simple_inventory/list_items",
  "inventory_id": "01JYFPCDMBRBRK4MB3C26S2FKH"
}
```

Returns: `{ "items": [...] }`

### `simple_inventory/get_item`

Fetch a single item by name.

```json
{
  "type": "simple_inventory/get_item",
  "inventory_id": "01JYFPCDMBRBRK4MB3C26S2FKH",
  "name": "Frozen Pizza"
}
```

Returns: `{ "item": { ... } }`

### `simple_inventory/subscribe`

Subscribe to real-time inventory updates. When any item changes, you receive the full updated item list.

```json
{
  "type": "simple_inventory/subscribe",
  "inventory_id": "01JYFPCDMBRBRK4MB3C26S2FKH"
}
```

Omit `inventory_id` to subscribe to updates from all inventories:

```json
{
  "type": "simple_inventory/subscribe"
}
```

When subscribed to a specific inventory, each event delivers `{ "items": [...] }` with the full item list. When subscribed globally, events deliver `{ "event": "updated" }`.

### `simple_inventory/get_history`

Query change history for an inventory or a specific item.

```json
{
  "type": "simple_inventory/get_history",
  "inventory_id": "01JYFPCDMBRBRK4MB3C26S2FKH",
  "item_name": "Frozen Pizza",
  "event_type": "decrement",
  "start_date": "2026-01-01",
  "end_date": "2026-02-15",
  "limit": 50,
  "offset": 0
}
```

All fields except `inventory_id` are optional. Supported `event_type` values: `add`, `remove`, `increment`, `decrement`, `update`.

Returns: `{ "events": [...] }` where each event has:
- `event_type` — What happened
- `amount` — How much changed
- `quantity_before`, `quantity_after` — Stock levels
- `timestamp` — When it happened

### `simple_inventory/export`

Export inventory data.

```json
{
  "type": "simple_inventory/export",
  "inventory_id": "01JYFPCDMBRBRK4MB3C26S2FKH",
  "format": "json"
}
```

Supported formats: `json`, `csv`

### `simple_inventory/import`

Import inventory data with a merge strategy.

```json
{
  "type": "simple_inventory/import",
  "inventory_id": "01JYFPCDMBRBRK4MB3C26S2FKH",
  "data": [{"name": "New Item", "quantity": 3}],
  "format": "json",
  "merge_strategy": "skip"
}
```

Merge strategies:
- `skip` — Skip items that already exist (default)
- `overwrite` — Replace existing items with imported data
- `merge_quantities` — Add imported quantities to existing quantities

Returns: `{ "added": 1, "updated": 0, "skipped": 0, "errors": [] }`

## Automation Examples

### Barcode scanner: increment on scan

```yaml
automation:
  - alias: "Barcode scan - add to inventory"
    trigger:
      - platform: event
        event_type: tag_scanned
    action:
      - service: simple_inventory.increment_item
        data:
          inventory_id: "01JYFPCDMBRBRK4MB3C26S2FKH"
          barcode: "{{ trigger.event.data.tag_id }}"
          amount: 1
```

### Notify when items expire

```yaml
automation:
  - alias: "Expiry notification"
    trigger:
      - platform: state
        entity_id: sensor.all_items_expiring_soon
    condition:
      - condition: numeric_state
        entity_id: sensor.all_items_expiring_soon
        above: 0
    action:
      - service: notify.mobile_app
        data:
          title: "Items expiring soon"
          message: >
            {{ state_attr('sensor.all_items_expiring_soon', 'total_expiring') }} items expiring soon,
            {{ state_attr('sensor.all_items_expiring_soon', 'total_expired') }} already expired.
```

### Low stock alert

```yaml
automation:
  - alias: "Low stock alert"
    trigger:
      - platform: state
        entity_id: sensor.kitchen_inventory
    condition:
      - condition: template
        value_template: >
          {{ state_attr('sensor.kitchen_inventory', 'below_threshold') | length > 0 }}
    action:
      - service: notify.mobile_app
        data:
          title: "Low stock items"
          message: >
            {% for item in state_attr('sensor.kitchen_inventory', 'below_threshold') %}
            - {{ item.name }}: {{ item.quantity }} remaining (need {{ item.quantity_needed }})
            {% endfor %}
```

### Get inventory data in a script

```yaml
script:
  get_inventory_report:
    sequence:
      - service: simple_inventory.get_items
        data:
          inventory_name: "Kitchen Fridge"
        response_variable: result
      - service: notify.mobile_app
        data:
          title: "Inventory Report"
          message: "You have {{ result.items | length }} items in the fridge."
```
