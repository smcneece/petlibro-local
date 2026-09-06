"""One RFID Smart Feeder."""

DEVICE_TYPES = ["one_rfid"]

MQTT_MODELS = {
    "one_rfid": "PLAF301",
}

ALERT_MESSAGES = {
    "food_low":      "Food hopper is empty or running low.",
    "desiccant_due": "Desiccant needs replacing.",
    "bowl_due":      "Food bowl needs cleaning.",
    "housing_due":   "Feeder housing needs cleaning.",
    "power_battery": "Running on battery power (AC lost).",
    "battery_low":   "Backup battery is low.",
    "door_jam":      "Food door is jammed, likely something is blocking it from closing.",
}

DEFAULT_NOTIFICATIONS = {
    "food_low":      True,
    "desiccant_due": True,
    "bowl_due":      True,
    "housing_due":   True,
    "power_battery": True,
    "door_jam":      True,
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
    if notif.get("power_battery", True):
        # Opportunistic: this feeder appears to drop WiFi shortly after
        # losing AC power to save the battery, so this typically only
        # catches the single transient state update sent right at the
        # transition, not a sustained live signal. Real outages mostly show
        # up as the existing "offline" alert instead.
        # powerType: 2 = battery, 3 = AC, confirmed via a direct AC-cut
        # test. 1 has never been observed in any capture. (An io:35 sensor
        # log was tried as a second, more precise timing source but turned
        # out to also fire for reasons unrelated to AC loss -- removed.)
        if state.get("powerType") == 2:
            alerts.add("power_battery")
    # Threshold of 0 disables this alert entirely; any other value both
    # enables it and sets the level, so there's no separate on/off toggle.
    threshold = cfg.get("battery_low_pct", 20)
    if threshold > 0:
        pct = state.get("electricQuantity")
        if pct is not None and pct > 0 and pct <= threshold:
            alerts.add("battery_low")
    if notif.get("door_jam", True):
        # Set by devices.py when an ERROR_EVENT with errorCode 2032 arrives
        # (confirmed via a real capture, 2026-09-04, of a pet blocking the
        # door from closing) and cleared once the door is next confirmed
        # closed. Only errorCode 2032 has ever been observed -- other codes
        # may exist and mean something else, not just other jam variants.
        if state.get("_door_jam_pending"):
            alerts.add("door_jam")
    return alerts


def track_intake(old_state: dict, new_state: dict, min_grams: float = 5) -> float | None:
    return None  # feeders don't track water intake
