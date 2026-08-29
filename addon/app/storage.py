"""JSON persistence for Petlibro Local.

All state lives in /data/petlibro_local.json with top-level keys:
  devices  -- device config keyed by serial number
  pets     -- pet profiles keyed by pet id
  settings -- user-configured global settings
  intake   -- daily water intake per serial: {serial: {date_iso: grams}}
"""

import datetime
import json
import logging
import os

_LOGGER = logging.getLogger(__name__)

DATA_FILE = "/data/petlibro_local.json"

DEFAULT_SETTINGS = {
    "language": "en",
    "mqtt_host": "localhost",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "units": "auto",
    "feeder_timezone": "",
    "feeder_tz_offset": -7,
    "notify_bell_enabled": True,
    "notify_email_service": "",
    "notify_email_to": "",
    "notify_mobile_default_service": "",
    "local_audio_base_url": "",
}

ALLOWED_SETTINGS = set(DEFAULT_SETTINGS.keys()) | {"feeder_timezone"}

ALLOWED_DEVICE_FIELDS = {
    "name", "room", "device_type", "model", "image_url", "variant",
    "mqtt_user", "mqtt_pass", "pet_ids",
    "filter_life_days", "last_cleaned_ts", "cleaning_interval_days",
    "low_water_grams", "min_drink_grams", "notifications",
    "notify_bell", "notify_email", "notify_email_address",
    "notify_mobile", "notify_mobile_service",
    "desiccant_life_days", "last_desiccant_ts",
    "last_bowl_cleaned_ts", "bowl_cleaning_interval_days",
    "last_housing_cleaned_ts", "housing_cleaning_interval_days",
    "display_text",
    "display_icon",
    "display_icon_name",
    "min_eating_secs",
    "last_fed_ts",
    "last_eating_secs",
    "battery_low_pct",
    "light_start_time",
    "light_end_time",
    "last_drink_ts",
}

ALLOWED_PET_FIELDS = {
    "name", "breed", "weight_kg", "image_url", "device_serials", "rfid_tag",
    "notify_bell", "notify_email", "notify_mobile",
    "no_eat_alert_enabled", "no_eat_alert_hours",
}

_DEVICE_DEFAULTS = {
    "name": "",
    "room": "",
    "device_type": "",
    "model": "",
    "image_url": "",
    "variant": "b",
    "mqtt_user": "",
    "mqtt_pass": "",
    "pet_ids": [],
    "filter_life_days": 14,
    "last_cleaned_ts": None,
    "cleaning_interval_days": 14,
    "low_water_grams": 500,
    "min_drink_grams": 5,
    "desiccant_life_days": 14,
    "last_desiccant_ts": None,
    "last_bowl_cleaned_ts": None,
    "bowl_cleaning_interval_days": 7,
    "last_housing_cleaned_ts": None,
    "housing_cleaning_interval_days": 30,
    "display_text": "",
    "display_icon": 0,
    "battery_low_pct": 20,
    "notifications": {
        "water_low": True,
        "filter_due": True,
        "cleaning_due": True,
        "food_low": True,
        "desiccant_due": True,
        "bowl_due": True,
        "housing_due": True,
        "power_battery": True,
        "offline": True,
    },
    "notify_bell": True,
    "notify_email": True,
    "notify_email_address": "",
    "notify_mobile": False,
    "notify_mobile_service": "",
}


def _load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"devices": {}, "pets": {}, "settings": {}, "intake": {}}
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
        data.setdefault("devices", {})
        data.setdefault("pets", {})
        data.setdefault("settings", {})
        data.setdefault("intake", {})
        return data
    except Exception:
        _LOGGER.exception("Failed to load data file, starting fresh")
        return {"devices": {}, "pets": {}, "settings": {}, "intake": {}}


def _save(data: dict):
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DATA_FILE)
    except Exception:
        _LOGGER.exception("Failed to save data file")
        try:
            os.remove(tmp)
        except OSError:
            pass


def get_settings() -> dict:
    stored = _load().get("settings", {})
    return {**DEFAULT_SETTINGS, **stored}


def save_settings(updates: dict) -> dict:
    data = _load()
    current = {**DEFAULT_SETTINGS, **data.get("settings", {})}
    for key, val in updates.items():
        if key in ALLOWED_SETTINGS:
            current[key] = val
    data["settings"] = current
    _save(data)
    return current


def get_devices() -> dict:
    return _load().get("devices", {})


def save_device(serial: str, fields: dict) -> dict:
    data = _load()
    devices = data.setdefault("devices", {})
    if serial not in devices:
        devices[serial] = {**_DEVICE_DEFAULTS, "serial": serial}
    # Merge new notification sub-keys without clobbering the rest
    if "notifications" in fields and isinstance(fields["notifications"], dict):
        existing = devices[serial].get("notifications") or {}
        devices[serial]["notifications"] = {**existing, **fields["notifications"]}
        fields = {k: v for k, v in fields.items() if k != "notifications"}
    for key, val in fields.items():
        if key in ALLOWED_DEVICE_FIELDS:
            devices[serial][key] = val
    devices[serial]["serial"] = serial
    _save(data)
    return devices[serial]


def delete_device(serial: str):
    data = _load()
    data.get("devices", {}).pop(serial, None)
    data.get("intake", {}).pop(serial, None)
    data.get("_feeder_log", {}).pop(serial, None)
    data.get("_fountain_log", {}).pop(serial, None)
    _save(data)


def get_pets() -> dict:
    return _load().get("pets", {})


def save_pet(pet_id: str, fields: dict) -> dict:
    data = _load()
    pets = data.setdefault("pets", {})
    if pet_id not in pets:
        pets[pet_id] = {
            "id": pet_id,
            "name": "",
            "breed": "",
            "weight_kg": None,
            "image_url": "",
            "device_serials": [],
        }
    for key, val in fields.items():
        if key in ALLOWED_PET_FIELDS:
            pets[pet_id][key] = val
    pets[pet_id]["id"] = pet_id
    _save(data)
    return pets[pet_id]


def delete_pet(pet_id: str):
    data = _load()
    data.get("pets", {}).pop(pet_id, None)
    _save(data)


# ── MQTT state cache (survives restarts) ────────────────────────────────────

def save_device_mqtt_cache(serial: str, state: dict):
    data = _load()
    data.setdefault("_mqtt_cache", {})[serial] = state
    _save(data)


def get_device_mqtt_cache(serial: str) -> dict:
    return _load().get("_mqtt_cache", {}).get(serial, {})


def get_alert_last_fired(serial: str) -> dict:
    return _load().get("_alert_last_fired", {}).get(serial, {})


def save_alert_last_fired(serial: str, alert: str, ts: float):
    data = _load()
    fired = data.setdefault("_alert_last_fired", {}).setdefault(serial, {})
    fired[alert] = ts
    _save(data)


def get_device_feeding_plans(serial: str) -> list:
    return _load().get("_feeding_plans", {}).get(serial, [])


def save_device_feeding_plans(serial: str, plans: list):
    data = _load()
    data.setdefault("_feeding_plans", {})[serial] = plans
    _save(data)


# ── Feeder activity log ───────────────────────────────────────────────────────

def log_feeder_event(serial: str, event_type: str, portions: int = 0, extra: dict | None = None):
    """Append a timestamped feeder event. event_type: 'food_dispensed' | 'pet_eating'."""
    import time as _time
    now_ms = int(_time.time() * 1000)
    entry = {"ts": now_ms, "type": event_type, "portions": portions}
    if extra:
        entry.update(extra)
    data = _load()
    log = data.setdefault("_feeder_log", {}).setdefault(serial, [])
    # Deduplicate RFID-attributed eating sessions: skip if a pet_eating entry with the
    # same rfid_tag already exists within the last 120 seconds (door + RFID both fire).
    if event_type == "pet_eating" and entry.get("rfid_tag"):
        cutoff = now_ms - 120_000
        for prev in reversed(log):
            if prev.get("ts", 0) < cutoff:
                break
            if prev.get("type") == "pet_eating" and prev.get("rfid_tag") == entry["rfid_tag"]:
                return
    log.append(entry)
    if len(log) > 200:
        data["_feeder_log"][serial] = log[-200:]
    _save(data)


def get_feeder_log(serial: str, limit: int = 60) -> list:
    """Return feeder events newest-first."""
    entries = _load().get("_feeder_log", {}).get(serial, [])
    return list(reversed(entries[-limit:]))


def log_fountain_event(serial: str, event_type: str, grams: float = 0, extra: dict | None = None):
    """Append a timestamped fountain event. event_type: 'drink'."""
    import time as _time
    now_ms = int(_time.time() * 1000)
    entry = {"ts": now_ms, "type": event_type, "grams": round(grams, 1)}
    if extra:
        entry.update(extra)
    data = _load()
    log = data.setdefault("_fountain_log", {}).setdefault(serial, [])
    log.append(entry)
    if len(log) > 500:
        data["_fountain_log"][serial] = log[-500:]
    _save(data)


def get_fountain_log(serial: str, limit: int = 60) -> list:
    """Return fountain events newest-first."""
    entries = _load().get("_fountain_log", {}).get(serial, [])
    return list(reversed(entries[-limit:]))


# ── Intake tracking ──────────────────────────────────────────────────────────

def record_intake(serial: str, grams: float):
    """Accumulate water consumption grams for today."""
    today = datetime.date.today().isoformat()
    data = _load()
    intake = data.setdefault("intake", {})
    serial_intake = intake.setdefault(serial, {})
    serial_intake[today] = round(serial_intake.get(today, 0.0) + grams, 1)
    # Prune entries older than 30 days
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    intake[serial] = {d: v for d, v in serial_intake.items() if d >= cutoff}
    _save(data)


def get_intake_today(serial: str) -> float:
    today = datetime.date.today().isoformat()
    return _load().get("intake", {}).get(serial, {}).get(today, 0.0)


def get_intake_history(serial: str, days: int = 7) -> list:
    """Return list of {date, grams} for the last N days, today first."""
    data = _load()
    serial_intake = data.get("intake", {}).get(serial, {})
    result = []
    for i in range(days):
        d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
        result.append({"date": d, "grams": round(serial_intake.get(d, 0.0), 1)})
    return result


def get_fountain_pet_intake_today(serial: str) -> dict:
    """Return {pet_id: {"grams": float, "visits": int, "duration_secs": int}}
    for today's RFID-confirmed drinks on this fountain (Dockstream RFID Smart
    Fountain). Uses the same local-day boundary as record_intake/get_intake_today."""
    today = datetime.date.today().isoformat()
    entries = _load().get("_fountain_log", {}).get(serial, [])
    result: dict = {}
    for e in entries:
        if e.get("type") != "drink" or not e.get("pet_id"):
            continue
        entry_date = datetime.datetime.fromtimestamp(e.get("ts", 0) / 1000).date().isoformat()
        if entry_date != today:
            continue
        agg = result.setdefault(e["pet_id"], {"grams": 0.0, "visits": 0, "duration_secs": 0})
        agg["grams"] += e.get("grams", 0)
        agg["visits"] += 1
        agg["duration_secs"] += e.get("duration_secs") or 0
    for agg in result.values():
        agg["grams"] = round(agg["grams"], 1)
    return result


# ── Custom display icons ──────────────────────────────────────────────────────

def get_custom_icons() -> list:
    """Return list of saved custom icons [{id, name, rows}]."""
    return list(_load().get("_custom_icons", []))


def save_custom_icon(name: str, rows: list) -> int:
    """Create a new custom icon. Returns its new id. Max 12 icons (oldest pruned)."""
    data = _load()
    icons = data.setdefault("_custom_icons", [])
    max_id = max((ic.get("id", 99) for ic in icons), default=99)
    new_id = max_id + 1
    icons.append({"id": new_id, "name": name[:32], "rows": [int(r) for r in rows[:5]]})
    if len(icons) > 12:
        icons[:] = icons[-12:]
    _save(data)
    return new_id


def update_custom_icon(icon_id: int, name: str, rows: list) -> bool:
    """Update name and rows of an existing icon. Returns False if not found."""
    data = _load()
    for ic in data.get("_custom_icons", []):
        if ic["id"] == icon_id:
            ic["name"] = name[:32]
            ic["rows"] = [int(r) for r in rows[:5]]
            _save(data)
            return True
    return False


def delete_custom_icon(icon_id: int) -> bool:
    """Delete an icon by id. Returns False if not found."""
    data = _load()
    icons = data.get("_custom_icons", [])
    new_icons = [ic for ic in icons if ic["id"] != icon_id]
    if len(new_icons) == len(icons):
        return False
    data["_custom_icons"] = new_icons
    _save(data)
    return True
