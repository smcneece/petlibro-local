"""Home Assistant MQTT Discovery integration.

Publishes discovery configs, availability, and state to HA's MQTT integration.
HA auto-creates entities that appear in automations, dashboards, and voice assistants.

Discovery docs: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
Naming standard: https://developers.home-assistant.io/docs/core/entity/#entity-naming
"""

import datetime
import json
import logging
import math
import time

import polar_control

_LOGGER = logging.getLogger(__name__)

_DISCOVERY_PREFIX = "homeassistant"
_STATE_PREFIX     = "petlibro_local"
_MANUFACTURER     = "PetLibro"

_MODEL_NAMES = {
    "dockstream2":          "Dockstream 2 Smart Fountain",
    "dockstream2_cordless": "Dockstream 2 Cordless Fountain",
    "dockstream_rfid":      "Dockstream RFID Smart Fountain",
    "one_rfid":             "One RFID Smart Feeder",
    "granary":              "Granary Smart Feeder",
    "polar":                "Polar Wet Food Feeder",
}

_FOUNTAIN_TYPES = {"dockstream2", "dockstream2_cordless", "dockstream_rfid"}

# Built-in icon names — keep in sync with display_matrix.ICON_FRAMES keys.
# "None" (id 0) means revert to text mode. -1 = custom frame (salute etc.)
_ICON_NAMES: dict[str, int] = {
    "None": 0,
    "Heart": 5,
    "Dog": 6,
    "Cat": 7,
    "Elk": 8,
    "Petlibro Salute": -1,
}
_ICON_IDS: dict[int, str] = {v: k for k, v in _ICON_NAMES.items()}

# Home Assistant's MQTT select treats a state payload of "None" as "reset to
# unknown", so an option literally named "None" can never be shown as the
# current state (the entity just reads "unknown"). These are the labels used in
# the option lists and state topics instead; the old "None" is still accepted
# as a command (anything that is not a known icon or animation means "stop").
NO_ICON_LABEL = "No icon"
NO_ANIMATION_LABEL = "No animation"

# Frames for icons with id -1 (custom frames, not stored in display_matrix)
_CUSTOM_FRAMES: dict[str, list] = {
    "Petlibro Salute": [0, 128 << 7, 128 << 7, 448 << 7, 480 << 7, 224 << 7, 0],
}

# The Icon Editor's "stock preset" quick-start frames (modal-icon-editor.js's
# ICON_PRESETS, "Heart (stock)"/"Dog (stock)"/"Cat (stock)"/"Elk (stock)")
# are send-only, never persisted as a saved custom icon, but sending one
# still writes its name into display_icon_name so this select's state topic
# reflects what's actually showing. That name has to be a registered valid
# option here too, or HA logs "Invalid option for select..." every time one
# gets sent (a real bug, caught 2026-09-11).
_STOCK_PRESET_NAMES = ["Heart (stock)", "Dog (stock)", "Cat (stock)", "Elk (stock)"]
# These are no longer offered as separate options (they duplicated Heart, Dog,
# Cat and Elk in the dropdown); a stock preset sent from the Icon Editor is
# reported as its plain built-in name instead, see publish_state.


def animation_option_names() -> list[str]:
    """"None" (stop, back to the saved text) plus the built-in animations and
    the user's saved ones, in that order, without duplicates."""
    import animations
    import storage
    names = [NO_ANIMATION_LABEL]
    for n in [p["name"] for p in animations.PRESETS] + [a["name"] for a in storage.get_custom_animations()]:
        if n not in names:
            names.append(n)
    return names


# ── Topic helpers ──────────────────────────────────────────────────────────

def avail_topic(serial: str) -> str:
    return f"{_STATE_PREFIX}/{serial}/availability"

def state_topic(serial: str, key: str) -> str:
    return f"{_STATE_PREFIX}/{serial}/{key}/state"

def cmd_topic(serial: str, key: str) -> str:
    return f"{_STATE_PREFIX}/{serial}/{key}/set"

def _oid(serial: str, key: str) -> str:
    """Globally unique ID — full serial keeps it collision-free."""
    return f"petlibro_{serial}_{key}"

def _obj_id(serial: str, key: str) -> str:
    """Stable object_id — overrides HA's user entity-ID format so IDs never change
    even if the user renames the device or adjusts their naming preference."""
    return f"petlibro_{serial[:8].lower()}_{key}"

def _disc_topic(component: str, serial: str, key: str) -> str:
    return f"{_DISCOVERY_PREFIX}/{component}/{_oid(serial, key)}/config"


# ── Device block ───────────────────────────────────────────────────────────

def _device_block(serial: str, cfg: dict, state: dict) -> dict:
    device_type = cfg.get("device_type", "")
    block = {
        "identifiers":  [f"petlibro_local_{serial}"],
        "name":         cfg.get("name") or serial[:8],
        "manufacturer": _MANUFACTURER,
        "model":        _MODEL_NAMES.get(device_type, device_type),
    }
    if cfg.get("room"):
        block["suggested_area"] = cfg["room"]
    if state.get("softwareVersion"):
        block["sw_version"] = state["softwareVersion"]
    if state.get("hardwareVersion"):
        block["hw_version"] = state["hardwareVersion"]
    return block


# ── Entity config builder ──────────────────────────────────────────────────

def _base(serial: str, cfg: dict, state: dict) -> dict:
    return {
        "has_entity_name":       True,
        "availability_topic":    avail_topic(serial),
        "payload_available":     "online",
        "payload_not_available": "offline",
        "device":                _device_block(serial, cfg, state),
    }


def _e(serial: str, key: str, b: dict, extra: dict) -> dict:
    """Merge base with stable unique_id + object_id and entity-specific fields."""
    return {**b, "unique_id": _oid(serial, key), "object_id": _obj_id(serial, key), **extra}


def _entity_configs(serial: str, cfg: dict, state: dict, extra_icon_names: list[str] | None = None) -> list[tuple[str, str, dict]]:
    """Return [(component, key, discovery_config), ...] for all entities of this device."""
    device_type = cfg.get("device_type", "")
    b = _base(serial, cfg, state)
    entities: list[tuple[str, str, dict]] = []

    # ── Shared (all device types) ─────────────────────────────────────────
    entities.append(("sensor", "rssi", _e(serial, "rssi", b, {
        "name":                "Signal Strength",
        "state_topic":         state_topic(serial, "rssi"),
        "unit_of_measurement": "dBm",
        "device_class":        "signal_strength",
        "state_class":         "measurement",
        "entity_category":     "diagnostic",
    })))

    entities.append(("sensor", "firmware", _e(serial, "firmware", b, {
        "name":            "Firmware Version",
        "state_topic":     state_topic(serial, "firmware"),
        "entity_category": "diagnostic",
    })))

    entities.append(("sensor", "hardware", _e(serial, "hardware", b, {
        "name":            "Hardware Version",
        "state_topic":     state_topic(serial, "hardware"),
        "entity_category": "diagnostic",
    })))

    # Seconds the device's own clock is behind real time (negative = ahead),
    # measured from each heartbeat's timestamp. The add-on pushes a correction
    # on its own when this exceeds ~30s, so it normally sits near zero.
    entities.append(("sensor", "clock_offset", _e(serial, "clock_offset", b, {
        "name":                "Clock Offset",
        "state_topic":         state_topic(serial, "clock_offset"),
        "unit_of_measurement": "s",
        "state_class":         "measurement",
        "entity_category":     "diagnostic",
        "icon":                "mdi:clock-alert-outline",
    })))

    # ── Fountain ──────────────────────────────────────────────────────────
    if device_type in _FOUNTAIN_TYPES:
        entities.append(("sensor", "water_level", _e(serial, "water_level", b, {
            "name":                "Water Level",
            "state_topic":         state_topic(serial, "water_level"),
            "unit_of_measurement": "mL",
            # Stored volume: Home Assistant can show it in fl. oz. (pick the unit in the
            # entity's settings) while the history stays in mL.
            "device_class":        "volume_storage",
            "state_class":         "measurement",
            "icon":                "mdi:cup-water",
        })))

        entities.append(("sensor", "last_drink", _e(serial, "last_drink", b, {
            "name":         "Last Drink",
            "state_topic":  state_topic(serial, "last_drink"),
            "device_class": "timestamp",
            "icon":         "mdi:cup-water",
        })))

        entities.append(("sensor", "filter_days", _e(serial, "filter_days", b, {
            "name":                "Filter Days Remaining",
            "state_topic":         state_topic(serial, "filter_days"),
            "unit_of_measurement": "d",
            "device_class":        "duration",
            "state_class":         "measurement",
            "entity_category":     "diagnostic",
        })))

        # waterStopSwitch=0 → pump running (ON), =1 → pump stopped (OFF)
        entities.append(("switch", "pump", _e(serial, "pump", b, {
            "name":          "Pump",
            "state_topic":   state_topic(serial, "pump"),
            "command_topic": cmd_topic(serial, "pump"),
            "payload_on":    "ON",
            "payload_off":   "OFF",
            "icon":          "mdi:water-pump",
        })))

        entities.append(("switch", "light", _e(serial, "light", b, {
            "name":          "Light",
            "state_topic":   state_topic(serial, "light"),
            "command_topic": cmd_topic(serial, "light"),
            "payload_on":    "ON",
            "payload_off":   "OFF",
            "icon":          "mdi:lightbulb",
        })))

        if device_type == "dockstream2_cordless":
            entities.append(("sensor", "battery", _e(serial, "battery", b, {
                "name":                "Battery",
                "state_topic":         state_topic(serial, "battery"),
                "unit_of_measurement": "%",
                "device_class":        "battery",
                "state_class":         "measurement",
            })))

    # ── Feeder ────────────────────────────────────────────────────────────
    elif device_type == "one_rfid":
        entities.append(("sensor", "battery", _e(serial, "battery", b, {
            "name":                "Battery",
            "state_topic":         state_topic(serial, "battery"),
            "unit_of_measurement": "%",
            "device_class":        "battery",
            "state_class":         "measurement",
        })))

        entities.append(("binary_sensor", "on_ac_power", _e(serial, "on_ac_power", b, {
            "name":         "On AC Power",
            "state_topic":  state_topic(serial, "on_ac_power"),
            "device_class": "plug",
            "payload_on":   "ON",
            "payload_off":  "OFF",
        })))

        entities.append(("sensor", "last_fed", _e(serial, "last_fed", b, {
            "name":         "Last Fed",
            "state_topic":  state_topic(serial, "last_fed"),
            "device_class": "timestamp",
        })))

        entities.append(("sensor", "last_eating_duration", _e(serial, "last_eating_duration", b, {
            "name":                "Last Eating Duration",
            "state_topic":         state_topic(serial, "last_eating_duration"),
            "unit_of_measurement": "s",
            "device_class":        "duration",
            "state_class":         "measurement",
            "icon":                "mdi:timer",
        })))

        entities.append(("sensor", "next_meal", _e(serial, "next_meal", b, {
            "name":         "Next Meal",
            "state_topic":  state_topic(serial, "next_meal"),
            "device_class": "timestamp",
            "icon":         "mdi:clock-outline",
        })))

        entities.append(("sensor", "desiccant_days", _e(serial, "desiccant_days", b, {
            "name":                "Desiccant Days Remaining",
            "state_topic":         state_topic(serial, "desiccant_days"),
            "unit_of_measurement": "d",
            "device_class":        "duration",
            "state_class":         "measurement",
            "entity_category":     "diagnostic",
        })))

        entities.append(("binary_sensor", "door", _e(serial, "door", b, {
            "name":         "Food Door",
            "state_topic":  state_topic(serial, "door"),
            "payload_on":   "ON",
            "payload_off":  "OFF",
            "device_class": "door",
        })))

        entities.append(("button", "feed_now", _e(serial, "feed_now", b, {
            "name":           "Feed Now",
            "command_topic":  cmd_topic(serial, "feed_now"),
            "payload_press":  "PRESS",
            "icon":           "mdi:food-drumstick",
        })))

        entities.append(("button", "open_door", _e(serial, "open_door", b, {
            "name":          "Open Door",
            "command_topic": cmd_topic(serial, "open_door"),
            "payload_press": "PRESS",
            "icon":          "mdi:door-open",
        })))

        entities.append(("number", "volume", _e(serial, "volume", b, {
            "name":          "Volume",
            "state_topic":   state_topic(serial, "volume"),
            "command_topic": cmd_topic(serial, "volume"),
            "min":           0,
            "max":           100,
            "step":          1,
            "mode":          "slider",
            "icon":          "mdi:volume-high",
        })))

        entities.append(("text", "display_text", _e(serial, "display_text", b, {
            "name":          "Display Text",
            "state_topic":   state_topic(serial, "display_text"),
            "command_topic": cmd_topic(serial, "display_text"),
            "min":           0,
            "max":           20,
            "optimistic":    True,
            "icon":          "mdi:text",
        })))

        entities.append(("select", "display_icon", _e(serial, "display_icon", b, {
            "name":          "Display Icon",
            "state_topic":   state_topic(serial, "display_icon"),
            "command_topic": cmd_topic(serial, "display_icon"),
            "options":       [NO_ICON_LABEL] + [n for n in _ICON_NAMES if n != "None"] + (extra_icon_names or []),
            "optimistic":    True,
            "icon":          "mdi:emoticon-outline",
        })))

        entities.append(("switch", "display_switch", _e(serial, "display_switch", b, {
            "name":          "LED Display",
            "state_topic":   state_topic(serial, "display_switch"),
            "command_topic": cmd_topic(serial, "display_switch"),
            "payload_on":    "ON",
            "payload_off":   "OFF",
            "icon":          "mdi:led-strip-variant",
        })))

        entities.append(("select", "display_animation", _e(serial, "display_animation", b, {
            "name":          "Display Animation",
            "state_topic":   state_topic(serial, "display_animation"),
            "command_topic": cmd_topic(serial, "display_animation"),
            "options":       animation_option_names(),
            "optimistic":    True,
            "icon":          "mdi:movie-open-outline",
        })))

    # ── Granary Smart Feeder (no RFID, no lid) ───────────────────────────────
    # ── Polar Wet Food Feeder (early beta, see polar_control.py) ─────────────
    elif device_type == "polar":
        for plate in polar_control.PLATES:
            key = f"serve_plate_{plate}"
            entities.append(("button", key, _e(serial, key, b, {
                "name":          f"Serve Plate {plate}",
                "command_topic": cmd_topic(serial, key),
                "payload_press": "PRESS",
                "icon":          "mdi:food-drumstick",
            })))
        for key, name, icon in (("stop_feed", "Stop Feed", "mdi:stop-circle-outline"),
                                ("open_lid", "Open Lid", "mdi:door-open"),
                                ("close_lid", "Close Lid", "mdi:door-closed")):
            entities.append(("button", key, _e(serial, key, b, {
                "name":          name,
                "command_topic": cmd_topic(serial, key),
                "payload_press": "PRESS",
                "icon":          icon,
            })))

        entities.append(("select", "plate_position", _e(serial, "plate_position", b, {
            "name":          "Plate Position",
            "state_topic":   state_topic(serial, "plate_position"),
            "command_topic": cmd_topic(serial, "plate_position"),
            "options":       [str(p) for p in polar_control.PLATES],
            "icon":          "mdi:rotate-3d-variant",
        })))

        unit = polar_control.duration_unit(cfg)
        duration = {
            "name":            "Serve Duration" if unit else "Serve Duration (set the unit in the app)",
            "state_topic":     state_topic(serial, "serve_duration"),
            "command_topic":   cmd_topic(serial, "serve_duration"),
            "min":             1,
            "max":             polar_control.DURATION_UNITS[unit][1] if unit else 600,
            "step":            1,
            "mode":            "box",
            "entity_category": "config",
            "icon":            "mdi:timer-outline",
        }
        if unit:
            duration["unit_of_measurement"] = "s" if unit == "seconds" else "min"
        entities.append(("number", "serve_duration", _e(serial, "serve_duration", b, duration)))

        entities.append(("sensor", "feed_status", _e(serial, "feed_status", b, {
            "name":        "Feed Status",
            "state_topic": state_topic(serial, "feed_status"),
            "icon":        "mdi:food-variant",
        })))
        entities.append(("sensor", "last_fed", _e(serial, "last_fed", b, {
            "name":         "Last Fed",
            "state_topic":  state_topic(serial, "last_fed"),
            "device_class": "timestamp",
        })))
        entities.append(("sensor", "temperature", _e(serial, "temperature", b, {
            "name":                "Temperature",
            "state_topic":         state_topic(serial, "temperature"),
            "unit_of_measurement": "\u00b0C",
            "device_class":        "temperature",
            "state_class":         "measurement",
            "entity_category":     "diagnostic",
        })))
        entities.append(("sensor", "last_door_open", _e(serial, "last_door_open", b, {
            "name":                "Last Door Open Time",
            "state_topic":         state_topic(serial, "last_door_open"),
            "unit_of_measurement": "s",
            "device_class":        "duration",
            "entity_category":     "diagnostic",
        })))

    elif device_type == "granary":
        entities.append(("sensor", "battery", _e(serial, "battery", b, {
            "name":                "Battery",
            "state_topic":         state_topic(serial, "battery"),
            "unit_of_measurement": "%",
            "device_class":        "battery",
            "state_class":         "measurement",
        })))

        entities.append(("sensor", "last_fed", _e(serial, "last_fed", b, {
            "name":         "Last Fed",
            "state_topic":  state_topic(serial, "last_fed"),
            "device_class": "timestamp",
        })))

        entities.append(("sensor", "next_meal", _e(serial, "next_meal", b, {
            "name":         "Next Meal",
            "state_topic":  state_topic(serial, "next_meal"),
            "device_class": "timestamp",
            "icon":         "mdi:clock-outline",
        })))

        entities.append(("sensor", "desiccant_days", _e(serial, "desiccant_days", b, {
            "name":                "Desiccant Days Remaining",
            "state_topic":         state_topic(serial, "desiccant_days"),
            "unit_of_measurement": "d",
            "device_class":        "duration",
            "state_class":         "measurement",
            "entity_category":     "diagnostic",
        })))

        entities.append(("button", "feed_now", _e(serial, "feed_now", b, {
            "name":           "Feed Now",
            "command_topic":  cmd_topic(serial, "feed_now"),
            "payload_press":  "PRESS",
            "icon":           "mdi:food-drumstick",
        })))

        entities.append(("switch", "light", _e(serial, "light", b, {
            "name":          "Light",
            "state_topic":   state_topic(serial, "light"),
            "command_topic": cmd_topic(serial, "light"),
            "payload_on":    "ON",
            "payload_off":   "OFF",
            "icon":          "mdi:lightbulb",
        })))

        entities.append(("switch", "buttons_lock", _e(serial, "buttons_lock", b, {
            "name":            "Buttons Lock",
            "state_topic":     state_topic(serial, "buttons_lock"),
            "command_topic":   cmd_topic(serial, "buttons_lock"),
            "payload_on":      "ON",
            "payload_off":     "OFF",
            "icon":            "mdi:lock",
            "entity_category": "config",
        })))

        entities.append(("number", "volume", _e(serial, "volume", b, {
            "name":          "Volume",
            "state_topic":   state_topic(serial, "volume"),
            "command_topic": cmd_topic(serial, "volume"),
            "min":           0,
            "max":           100,
            "step":          1,
            "mode":          "slider",
            "icon":          "mdi:volume-high",
        })))

    return entities


# ── Publish helpers ────────────────────────────────────────────────────────

async def publish_discovery(client, serial: str, cfg: dict, state: dict, extra_icon_names: list[str] | None = None):
    """Publish retained discovery configs for all entities of a device."""
    configs = _entity_configs(serial, cfg, state, extra_icon_names=extra_icon_names)
    for component, key, config in configs:
        topic = _disc_topic(component, serial, key)
        await client.publish(topic, json.dumps(config), retain=True)
    _LOGGER.info("Discovery: published %d entities for %s...", len(configs), serial[:6])


async def retract_legacy_entities(client, serial: str, cfg: dict):
    """Retract HA discovery topics for entities removed in previous versions."""
    device_type = cfg.get("device_type", "")
    stale: list[tuple[str, str]] = []
    if device_type == "one_rfid":
        stale.append(("switch", "light"))
    for component, key in stale:
        topic = _disc_topic(component, serial, key)
        await client.publish(topic, "", retain=True)
    if stale:
        _LOGGER.info("Retracted %d legacy entity/entities for %s...", len(stale), serial[:6])


async def retract_discovery(client, serial: str, cfg: dict, state: dict):
    """Retract discovery for a device by publishing empty retained payloads."""
    configs = _entity_configs(serial, cfg, state)
    for component, key, _ in configs:
        topic = _disc_topic(component, serial, key)
        await client.publish(topic, "", retain=True)
    _LOGGER.info("Discovery: retracted %d entities for %s...", len(configs), serial[:6])


async def publish_availability(client, serial: str, online: bool):
    """Publish online/offline to the device availability topic."""
    await client.publish(avail_topic(serial), "online" if online else "offline", retain=True)


# ── Command routing ────────────────────────────────────────────────────────

def get_command_topics(serial: str, device_type: str) -> list[str]:
    """Return all HA command topics this device should subscribe to."""
    topics: list[str] = []
    if device_type in _FOUNTAIN_TYPES:
        topics.append(cmd_topic(serial, "pump"))
        topics.append(cmd_topic(serial, "light"))
    elif device_type == "one_rfid":
        topics.append(cmd_topic(serial, "feed_now"))
        topics.append(cmd_topic(serial, "open_door"))
        topics.append(cmd_topic(serial, "volume"))
        topics.append(cmd_topic(serial, "display_text"))
        topics.append(cmd_topic(serial, "display_icon"))
        topics.append(cmd_topic(serial, "display_animation"))
        topics.append(cmd_topic(serial, "display_switch"))
    elif device_type == "polar":
        for key in ("serve_plate_1", "serve_plate_2", "serve_plate_3", "stop_feed", "open_lid",
                    "close_lid", "plate_position", "serve_duration"):
            topics.append(cmd_topic(serial, key))
    elif device_type == "granary":
        topics.append(cmd_topic(serial, "feed_now"))
        topics.append(cmd_topic(serial, "volume"))
        topics.append(cmd_topic(serial, "light"))
        topics.append(cmd_topic(serial, "buttons_lock"))
    return topics


def _days_remaining_ms(epoch_ms) -> int:
    """Days remaining until a millisecond-epoch deadline (clamped to 0)."""
    remaining = (int(epoch_ms) / 1000) - time.time()
    # Rounded up, like the web UI, so the count matches when early warnings start.
    return max(0, math.ceil(remaining / 86400))


def _next_meal_ts(plans: list) -> str | None:
    """Return ISO 8601 UTC timestamp of the next scheduled feeding, or None.

    Plans use executionTime='HH:MM' (UTC) and repeatDay=[1..7] (1=Mon, 7=Sun).
    Searches up to 8 days ahead to cover all day-of-week combinations.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    best: datetime.datetime | None = None

    for plan in plans:
        if plan.get("_enabled") is False:
            continue
        exec_time = plan.get("executionTime", "")
        repeat_day = plan.get("repeatDay", [])
        if not exec_time or not repeat_day:
            continue
        try:
            h, m = map(int, exec_time.split(":"))
        except (ValueError, AttributeError):
            continue

        for day_offset in range(8):
            candidate_date = now.date() + datetime.timedelta(days=day_offset)
            pl_day = candidate_date.weekday() + 1  # Python 0=Mon → PetLibro 1=Mon
            if pl_day not in repeat_day:
                continue
            candidate = datetime.datetime(
                candidate_date.year, candidate_date.month, candidate_date.day,
                h, m, tzinfo=datetime.timezone.utc,
            )
            if candidate <= now:
                continue
            skip_end = plan.get("skipEndTime")
            if isinstance(skip_end, (int, float)) and candidate.timestamp() * 1000 < skip_end:
                continue  # this occurrence is skipped, look at the plan's next day
            if best is None or candidate < best:
                best = candidate
            break  # found closest future occurrence for this plan

    return best.isoformat() if best else None


async def publish_state(client, serial: str, cfg: dict, state: dict, plans: list | None = None,
                        clock_offset: int | None = None):
    """Publish current sensor values to all HA state topics for this device."""
    device_type = cfg.get("device_type", "")

    # All devices
    if clock_offset is not None:
        await client.publish(state_topic(serial, "clock_offset"), str(clock_offset), retain=True)

    if "rssi" in state:
        await client.publish(state_topic(serial, "rssi"), str(state["rssi"]), retain=True)

    if "softwareVersion" in state:
        await client.publish(state_topic(serial, "firmware"), str(state["softwareVersion"]), retain=True)

    if "hardwareVersion" in state:
        await client.publish(state_topic(serial, "hardware"), str(state["hardwareVersion"]), retain=True)

    # Fountains
    if device_type in _FOUNTAIN_TYPES:
        if "currentWeight" in state:
            await client.publish(state_topic(serial, "water_level"), str(state["currentWeight"]), retain=True)

        if "filterNextReplacementTimestamp" in state:
            days = _days_remaining_ms(state["filterNextReplacementTimestamp"])
            await client.publish(state_topic(serial, "filter_days"), str(days), retain=True)

        if "waterStopSwitch" in state:
            val = "ON" if state["waterStopSwitch"] == 0 else "OFF"
            await client.publish(state_topic(serial, "pump"), val, retain=True)

        if "lightSwitch" in state:
            val = "ON" if state["lightSwitch"] == 1 else "OFF"
            await client.publish(state_topic(serial, "light"), val, retain=True)

        if device_type == "dockstream2_cordless" and "electricQuantity" in state:
            await client.publish(state_topic(serial, "battery"), str(state["electricQuantity"]), retain=True)

        last_drink = cfg.get("last_drink_ts")
        if last_drink:
            dt = datetime.datetime.fromtimestamp(int(last_drink), tz=datetime.timezone.utc)
            await client.publish(state_topic(serial, "last_drink"), dt.isoformat(), retain=True)

    # Feeder
    elif device_type == "one_rfid":
        if "electricQuantity" in state:
            await client.publish(state_topic(serial, "battery"), str(state["electricQuantity"]), retain=True)

        if "powerType" in state:
            # powerType: 2 = battery, 3 = AC, confirmed via a direct AC-cut
            # test. 1 has never been observed. Treat anything other than a
            # confirmed 2 as AC.
            val = "OFF" if state["powerType"] == 2 else "ON"
            await client.publish(state_topic(serial, "on_ac_power"), val, retain=True)

        last_fed = cfg.get("last_fed_ts")
        if last_fed:
            dt = datetime.datetime.fromtimestamp(int(last_fed), tz=datetime.timezone.utc)
            await client.publish(state_topic(serial, "last_fed"), dt.isoformat(), retain=True)

        last_eating = cfg.get("last_eating_secs")
        if last_eating is not None:
            await client.publish(state_topic(serial, "last_eating_duration"), str(last_eating), retain=True)

        if plans is not None:
            next_ts = _next_meal_ts(plans)
            # Empty string is not a valid state for a timestamp-class sensor --
            # HA logs "Invalid state message" for it (real, confirmed
            # 2026-09-12). Only publish when there's an actual upcoming feed,
            # same convention as last_fed/last_drink below.
            # With no upcoming feed (schedules paused, all disabled or deleted)
            # the retained old value would otherwise stay forever. Home
            # Assistant's MQTT sensor resets to unknown on the payload "None".
            await client.publish(state_topic(serial, "next_meal"), next_ts or "None", retain=True)

        last_desiccant = cfg.get("last_desiccant_ts")
        if last_desiccant:
            life_days = cfg.get("desiccant_life_days", 14)
            last_desiccant_secs = last_desiccant / 1000  # stored as ms from JS Date.now()
            elapsed = (time.time() - last_desiccant_secs) / 86400
            days = max(0, math.ceil(life_days - elapsed))
            await client.publish(state_topic(serial, "desiccant_days"), str(days), retain=True)

        if "barnDoorState" in state:
            val = "ON" if state["barnDoorState"] else "OFF"
            await client.publish(state_topic(serial, "door"), val, retain=True)

        if "volume" in state:
            await client.publish(state_topic(serial, "volume"), str(state["volume"]), retain=True)

        display_text = cfg.get("display_text", "")
        await client.publish(state_topic(serial, "display_text"), display_text[:20], retain=True)

        icon_name = cfg.get("display_icon_name") or _ICON_IDS.get(cfg.get("display_icon", 0), "None")
        if icon_name in _STOCK_PRESET_NAMES:
            icon_name = icon_name[:-len(" (stock)")]
        # The select only accepts its own options; the Icon Editor can send an
        # unsaved drawing under whatever name was typed, which Home Assistant
        # would show as "unknown". Report None for anything not in the list.
        import storage as _storage
        if icon_name not in _ICON_NAMES and icon_name not in {ic["name"] for ic in _storage.get_custom_icons()}:
            icon_name = "None"
        if icon_name == "None":
            icon_name = NO_ICON_LABEL
        await client.publish(state_topic(serial, "display_icon"), icon_name, retain=True)
        # The setting (screenDisplaySwitch), not enableScreenDisplay, which is
        # whether the LED is lit right now and goes false outside a custom window.
        disp = state.get("screenDisplaySwitch", state.get("enableScreenDisplay"))
        if disp is not None:
            await client.publish(state_topic(serial, "display_switch"), "ON" if disp else "OFF", retain=True)
        anim_name = cfg.get("display_animation_name") or NO_ANIMATION_LABEL
        if anim_name not in animation_option_names():
            anim_name = NO_ANIMATION_LABEL
        await client.publish(state_topic(serial, "display_animation"), anim_name, retain=True)

    # Polar Wet Food Feeder (early beta, see polar_control.py)
    elif device_type == "polar":
        temp = state.get("temperature")
        if isinstance(temp, (int, float)) and not isinstance(temp, bool):
            await client.publish(state_topic(serial, "temperature"), str(temp), retain=True)

        status = "Idle"
        if state.get("polar_feed_active"):
            step = state.get("polar_feed_step")
            status = f"Feeding ({step})" if step else "Feeding"
        await client.publish(state_topic(serial, "feed_status"), status, retain=True)

        last_fed = cfg.get("last_fed_ts")
        if last_fed:
            dt = datetime.datetime.fromtimestamp(int(last_fed), tz=datetime.timezone.utc)
            await client.publish(state_topic(serial, "last_fed"), dt.isoformat(), retain=True)

        last_feed = state.get("polar_last_feed") or {}
        if last_feed.get("door_open_secs") is not None:
            await client.publish(state_topic(serial, "last_door_open"), str(last_feed["door_open_secs"]), retain=True)

        pos = state.get("platePosition")
        if isinstance(pos, int) and not isinstance(pos, bool) and pos in polar_control.PLATES:
            await client.publish(state_topic(serial, "plate_position"), str(pos), retain=True)

        if cfg.get("polar_serve_duration") is not None:
            await client.publish(state_topic(serial, "serve_duration"), str(cfg["polar_serve_duration"]), retain=True)

    # Granary Smart Feeder (no RFID, no lid, no display -- see device_types
    # module docstring for what's deliberately not implemented and why)
    elif device_type == "granary":
        if "electricQuantity" in state:
            await client.publish(state_topic(serial, "battery"), str(state["electricQuantity"]), retain=True)

        last_fed = cfg.get("last_fed_ts")
        if last_fed:
            dt = datetime.datetime.fromtimestamp(int(last_fed), tz=datetime.timezone.utc)
            await client.publish(state_topic(serial, "last_fed"), dt.isoformat(), retain=True)

        if plans is not None:
            next_ts = _next_meal_ts(plans)
            # Empty string is not a valid state for a timestamp-class sensor --
            # HA logs "Invalid state message" for it (real, confirmed
            # 2026-09-12). Only publish when there's an actual upcoming feed,
            # same convention as last_fed/last_drink below.
            # With no upcoming feed (schedules paused, all disabled or deleted)
            # the retained old value would otherwise stay forever. Home
            # Assistant's MQTT sensor resets to unknown on the payload "None".
            await client.publish(state_topic(serial, "next_meal"), next_ts or "None", retain=True)

        last_desiccant = cfg.get("last_desiccant_ts")
        if last_desiccant:
            life_days = cfg.get("desiccant_life_days", 14)
            last_desiccant_secs = last_desiccant / 1000
            elapsed = (time.time() - last_desiccant_secs) / 86400
            days = max(0, math.ceil(life_days - elapsed))
            await client.publish(state_topic(serial, "desiccant_days"), str(days), retain=True)

        if "volume" in state:
            await client.publish(state_topic(serial, "volume"), str(state["volume"]), retain=True)

        # lightSwitch/disableHardwareButton are real booleans on this device
        # (not the 1/0 ints the fountain uses) -- `state["lightSwitch"] == 1`
        # still works fine either way since Python treats True == 1.
        if "lightSwitch" in state:
            val = "ON" if state["lightSwitch"] == 1 else "OFF"
            await client.publish(state_topic(serial, "light"), val, retain=True)

        if "disableHardwareButton" in state:
            val = "ON" if state["disableHardwareButton"] else "OFF"
            await client.publish(state_topic(serial, "buttons_lock"), val, retain=True)


def parse_command(topic: str, payload: str, device_type: str = "") -> dict | None:
    """Parse an HA command topic into a device payload dict.

    Returns a payload for send_command(), or None if unrecognised.
    {'_feed_now': True} is a special signal to trigger a manual feed.
    """
    parts = topic.split("/")
    if len(parts) < 2:
        return None
    key = parts[-2]  # petlibro_local/{serial}/{key}/set

    if device_type == "polar":
        def _whole(text):
            try:
                number = float(text)
            except (ValueError, TypeError):
                return None
            return int(number) if number == int(number) else None
        if key.startswith("serve_plate_"):
            plate = _whole(key.rsplit("_", 1)[1])
            return None if plate is None else {"_polar_serve": {"plate": plate}}
        if key == "stop_feed":
            return {"_polar_stop": True}
        if key == "open_lid":
            return {"_polar_door": True}
        if key == "close_lid":
            return {"_polar_door": False}
        if key == "plate_position":
            n = _whole(payload)
            return None if n is None else {"_polar_plate_position": n}
        if key == "serve_duration":
            n = _whole(payload)
            return None if n is None else {"_polar_set_duration": n}
        return None

    if key == "pump":
        return {"waterStopSwitch": 0 if payload == "ON" else 1}
    if key == "light":
        if device_type == "one_rfid":
            return {"screenDisplaySwitch": payload == "ON"}
        if device_type == "granary":
            # Granary's own real ATTR_SET_SERVICE captures always resend its
            # full settings snapshot together (volume/audioUrl/enableAudio/
            # lightSwitch/lightAgingType/disableHardwareButton), never a lone
            # changed field alone -- flagged here so devices.py can build
            # that same bundle from currently-known state (see
            # _granary_settings_bundle in devices.py).
            return {"_granary_field": "lightSwitch", "_granary_value": payload == "ON"}
        return {"lightSwitch": 1 if payload == "ON" else 0}
    if key == "buttons_lock":
        return {"_granary_field": "disableHardwareButton", "_granary_value": payload == "ON"}
    if key == "feed_now":
        return {"_feed_now": True}
    if key == "open_door":
        return {"_open_door": True}
    if key == "volume":
        try:
            vol = int(float(payload))
        except (ValueError, TypeError):
            return None
        if device_type == "granary":
            return {"_granary_field": "volume", "_granary_value": vol}
        return {"volume": vol}
    if key == "display_text":
        return {"_display_text": payload[:20]}
    if key == "display_icon":
        return {"_display_icon": payload}
    if key == "display_animation":
        return {"_display_animation_name": payload}
    if key == "display_switch":
        return {"_display_switch": payload == "ON"}
    return None
