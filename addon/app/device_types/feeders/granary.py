"""Granary Smart Feeder (no RFID, no lid) -- issue #8, reported by Danny Darbyshire.

Confirmed via a real mqtt_proxy.py capture (2026-09-11): wire MQTT model is
literally `PLAF103`, same as the box/retail number this time (no discrepancy
like the RFID feeder's PLAF103-box-vs-PLAF301-wire case). Command/field names
mostly reuse the One RFID feeder's protocol (MANUAL_FEEDING_SERVICE,
FEEDING_PLAN_SERVICE, GET_FEEDING_PLAN_EVENT, surplusGrain, grainOutletState,
electricQuantity, volume, audioUrl/enableAudio), but this device also has an
indicator light (lightSwitch/lightAgingType, same fields already confirmed
for the fountain's light schedule) and a "Buttons Lock" feature
(disableHardwareButton) instead of the RFID feeder's childLockSwitch.

Confirmed NOT present in this device's capture, deliberately not built here:
- No barnDoorState / WAREHOUSE_DOOR_EVENT / SWITCH_DOOR_SERVICE -- this is a
  hopper-style feeder with no openable lid, so no "door_jam" alert either
  (nothing that could pinch a paw the way a hinged door could).
- No GRAIN_OUTPUT_EVENT (or equivalent) observed, so "Last Fed" time and any
  eating-session-style logging aren't implemented -- there's no confirmed
  event to hook into yet, unlike the RFID feeder's door-open-duration proxy.
- powerType semantics are NOT assumed to match the RFID feeder's confirmed
  2=battery/3=AC mapping. This capture only ever showed powerType: 1 (no
  battery installed, electricQuantity: 0, matching the cloud integration's
  "Battery/AC %: 0%" reading) -- a value never seen on the RFID feeder. Until
  a real test confirms what this model's powerType values mean, no
  power_battery/on_ac_power alert or sensor is built for it, just the plain
  electricQuantity-based Battery sensor and the existing battery_low_pct
  threshold (safe either way, since that check already requires pct > 0).
"""

DEVICE_TYPES = ["granary"]

MQTT_MODELS = {
    "granary": "PLAF103",
}

ALERT_MESSAGES = {
    "food_low":        "Food hopper is empty or running low.",
    "food_refill_due":  "Food tank hasn't been refilled in a while, may be running low.",
    "desiccant_due":    "Desiccant needs replacing.",
    "bowl_due":         "Food bowl needs cleaning.",
    "housing_due":      "Feeder housing needs cleaning.",
    "battery_low":      "Backup battery is low.",
}

DEFAULT_NOTIFICATIONS = {
    "food_low":        True,
    "food_refill_due": True,
    "desiccant_due":   True,
    "bowl_due":        True,
    "housing_due":     True,
    # battery_low has no separate on/off toggle -- battery_low_pct == 0 disables it
}


def compute_alerts(state: dict, cfg: dict, online: bool) -> set:
    import time as _time
    notif = cfg.get("notifications", {})
    alerts = set()
    if notif.get("food_low", True):
        surplus = state.get("surplusGrain")
        if surplus is not None and not surplus and online:
            alerts.add("food_low")
    if notif.get("food_refill_due", True):
        last_ts  = cfg.get("last_food_refill_ts")
        interval = cfg.get("food_refill_interval_days", 14)
        if last_ts is not None:
            elapsed = (_time.time() - last_ts / 1000) / 86400
            if elapsed >= interval:
                alerts.add("food_refill_due")
    if notif.get("desiccant_due", True):
        last_ts   = cfg.get("last_desiccant_ts")
        life_days = cfg.get("desiccant_life_days", 14)
        if last_ts is not None:
            elapsed = (_time.time() - last_ts / 1000) / 86400
            if elapsed >= life_days:
                alerts.add("desiccant_due")
    if notif.get("bowl_due", True):
        last_ts   = cfg.get("last_bowl_cleaned_ts")
        interval  = cfg.get("bowl_cleaning_interval_days", 7)
        if last_ts is not None:
            elapsed = (_time.time() - last_ts / 1000) / 86400
            if elapsed >= interval:
                alerts.add("bowl_due")
    if notif.get("housing_due", True):
        last_ts   = cfg.get("last_housing_cleaned_ts")
        interval  = cfg.get("housing_cleaning_interval_days", 30)
        if last_ts is not None:
            elapsed = (_time.time() - last_ts / 1000) / 86400
            if elapsed >= interval:
                alerts.add("housing_due")
    threshold = cfg.get("battery_low_pct", 20)
    if threshold > 0:
        pct = state.get("electricQuantity")
        if pct is not None and pct > 0 and pct <= threshold:
            alerts.add("battery_low")
    return alerts


def track_intake(old_state: dict, new_state: dict, min_grams: float = 5) -> float | None:
    return None  # feeders don't track water intake
