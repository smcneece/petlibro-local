"""Home Assistant MQTT Discovery integration.

Publishes discovery configs, availability, and state to HA's MQTT integration.
HA auto-creates entities that appear in automations, dashboards, and voice assistants.

Discovery docs: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
Naming standard: https://developers.home-assistant.io/docs/core/entity/#entity-naming
"""

import datetime
import json
import logging
import time

_LOGGER = logging.getLogger(__name__)

_DISCOVERY_PREFIX = "homeassistant"
_STATE_PREFIX     = "petlibro_local"
_MANUFACTURER     = "PetLibro"

_MODEL_NAMES = {
    "dockstream2":          "Dockstream 2 Smart Fountain",
    "dockstream2_cordless": "Dockstream 2 Cordless Fountain",
    "dockstream_rfid":      "Dockstream RFID Smart Fountain",
    "one_rfid":             "One RFID Smart Feeder",
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

# Frames for icons with id -1 (custom frames, not stored in display_matrix)
_CUSTOM_FRAMES: dict[str, list] = {
    "Petlibro Salute": [0, 128 << 7, 128 << 7, 448 << 7, 480 << 7, 224 << 7, 0],
}


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

    # ── Fountain ──────────────────────────────────────────────────────────
    if device_type in _FOUNTAIN_TYPES:
        entities.append(("sensor", "water_level", _e(serial, "water_level", b, {
            "name":                "Water Level",
            "state_topic":         state_topic(serial, "water_level"),
            "unit_of_measurement": "mL",
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
            "options":       list(_ICON_NAMES.keys()) + (extra_icon_names or []),
            "optimistic":    True,
            "icon":          "mdi:emoticon-outline",
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
    return topics


def _days_remaining_ms(epoch_ms) -> int:
    """Days remaining until a millisecond-epoch deadline (clamped to 0)."""
    remaining = (int(epoch_ms) / 1000) - time.time()
    return max(0, int(remaining / 86400))


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
            if best is None or candidate < best:
                best = candidate
            break  # found closest future occurrence for this plan

    return best.isoformat() if best else None


async def publish_state(client, serial: str, cfg: dict, state: dict, plans: list | None = None):
    """Publish current sensor values to all HA state topics for this device."""
    device_type = cfg.get("device_type", "")

    # All devices
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
            await client.publish(state_topic(serial, "next_meal"), next_ts or "", retain=True)

        last_desiccant = cfg.get("last_desiccant_ts")
        if last_desiccant:
            life_days = cfg.get("desiccant_life_days", 14)
            last_desiccant_secs = last_desiccant / 1000  # stored as ms from JS Date.now()
            elapsed = (time.time() - last_desiccant_secs) / 86400
            days = max(0, int(life_days - elapsed))
            await client.publish(state_topic(serial, "desiccant_days"), str(days), retain=True)

        if "barnDoorState" in state:
            val = "ON" if state["barnDoorState"] else "OFF"
            await client.publish(state_topic(serial, "door"), val, retain=True)

        if "volume" in state:
            await client.publish(state_topic(serial, "volume"), str(state["volume"]), retain=True)

        display_text = cfg.get("display_text", "")
        await client.publish(state_topic(serial, "display_text"), display_text[:20], retain=True)

        icon_name = cfg.get("display_icon_name") or _ICON_IDS.get(cfg.get("display_icon", 0), "None")
        await client.publish(state_topic(serial, "display_icon"), icon_name, retain=True)



def parse_command(topic: str, payload: str, device_type: str = "") -> dict | None:
    """Parse an HA command topic into a device payload dict.

    Returns a payload for send_command(), or None if unrecognised.
    {'_feed_now': True} is a special signal to trigger a manual feed.
    """
    parts = topic.split("/")
    if len(parts) < 2:
        return None
    key = parts[-2]  # petlibro_local/{serial}/{key}/set

    if key == "pump":
        return {"waterStopSwitch": 0 if payload == "ON" else 1}
    if key == "light":
        if device_type == "one_rfid":
            return {"screenDisplaySwitch": payload == "ON"}
        return {"lightSwitch": 1 if payload == "ON" else 0}
    if key == "feed_now":
        return {"_feed_now": True}
    if key == "open_door":
        return {"_open_door": True}
    if key == "volume":
        try:
            return {"volume": int(float(payload))}
        except (ValueError, TypeError):
            return None
    if key == "display_text":
        return {"_display_text": payload[:20]}
    if key == "display_icon":
        return {"_display_icon": payload}
    return None
