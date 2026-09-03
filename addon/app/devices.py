"""MQTT client for Petlibro devices.

Maintains a persistent connection to HA Mosquitto, subscribes to all configured
device topics, and keeps an in-memory state dict updated with the latest telemetry.
Device-type-specific logic (alerts, intake tracking) lives in device_types/.
"""

import asyncio
import collections
import json
import logging
import os

import aiomqtt
import device_types as _device_types
import ha_mqtt

_LOGGER = logging.getLogger(__name__)

# In-memory device states keyed by serial number
_state:        dict[str, dict]  = {}
_online:       dict[str, bool]  = {}
_alert_sent:   dict[str, set]   = {}
_offline_since: dict[str, float] = {}   # epoch when each device first went offline
_last_seen:    dict[str, float]  = {}   # epoch of most recent MQTT message from device
_last_attr_poll: dict[str, float] = {}  # epoch of last proactive ATTR_GET_SERVICE query

OFFLINE_ALERT_DELAY_SECS = 90
WATCHDOG_SECS = 300  # mark offline after 5 min of silence
ATTR_POLL_INTERVAL_SECS = 300  # re-query full attribute state periodically, not just at connect
FOUNTAIN_DRINK_DEDUP_SECS = 15  # ignore a second qualifying weight-drop this soon after the last logged one

# EXPERIMENTAL, temporarily OFF (2026-09-01): proactively pushing an
# unsolicited time sync to every device (see _push_ntp_sync). Turned off
# while investigating issue #5, so a device's own natural NTP-request
# behavior (or lack of it) can actually be observed instead of being masked
# by our own pushes keeping its clock in sync regardless. Flip back to True
# once that observation window is done.
ENABLE_PROACTIVE_NTP_PUSH = False

# Dedicated log file for NTP request/response/push activity, separate from
# the main add-on log, so a multi-day observation window (see issue #5)
# doesn't require scrolling a huge combined log to find these lines.
_NTP_LOG_FILE = "/data/ntp_debug.log"
_ntp_logger = logging.getLogger("petlibro_local.ntp")
_ntp_logger.setLevel(logging.INFO)
_ntp_logger.propagate = False
try:
    _ntp_handler = logging.FileHandler(_NTP_LOG_FILE)
    _ntp_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _ntp_logger.addHandler(_ntp_handler)
except Exception:
    _LOGGER.exception("Could not open dedicated NTP log file %s", _NTP_LOG_FILE)

# Rolling capture of raw dl/ traffic for diagnostics (Help/About "Download
# Debug Capture"). Devices only chirp occasionally, so this is passive and
# always-on rather than a fixed listen window that could easily miss a device
# that wakes up once every few minutes. Bounded by count, not time.
_RAW_CAPTURE_MAXLEN = 2000
_raw_capture: collections.deque = collections.deque(maxlen=_RAW_CAPTURE_MAXLEN)
_unrecognized_serials_seen: set = set()  # dedup so the addon log doesn't spam per-message

_host = "localhost"
_port = 1883
_user = ""
_pass = ""

# Built from the device_types registry at import time
_DEVICE_MODELS:   dict[str, str] = _device_types.all_mqtt_models()
_ALERT_MESSAGES:  dict[str, str] = _device_types.all_alert_messages()

# Serials currently subscribed: serial -> (model, device_type)
_devices: dict[str, tuple[str, str]] = {}

_pet_no_eat_alerted: dict[str, bool] = {}

_client_task:     asyncio.Task | None  = None
_client_ref:      aiomqtt.Client | None = None
_reconnect_event: asyncio.Event | None = None
_suppress_alerts: bool = False


def suppress_alerts(suppress: bool):
    global _suppress_alerts
    _suppress_alerts = suppress


# ── Configuration ──────────────────────────────────────────────────────────

def configure(host: str, port: int, user: str, password: str):
    global _host, _port, _user, _pass
    _host = host
    _port = port
    _user = user
    _pass = password


# ── Device registration ────────────────────────────────────────────────────

def register_device(serial: str, model: str, device_type: str):
    _devices[serial] = (model, device_type)
    try:
        import storage as _storage
        cached = _storage.get_device_mqtt_cache(serial)
        if cached:
            # Disk cache may hold a stale powerType/powerMode from before
            # this app understood the mapping correctly (2 = battery,
            # 3 = AC -- see the incoming message handler). Start these two
            # fresh each run rather than trusting old persisted data; a
            # genuine push will set the real current value soon enough.
            cached = {k: v for k, v in cached.items() if k not in ("powerType", "powerMode")}
            _state.setdefault(serial, {}).update(cached)
    except Exception:
        pass
    _LOGGER.info("Registered device %s... (%s)", serial[:6], device_type)
    if _reconnect_event:
        _reconnect_event.set()


async def _poll_attr_state(serial: str, device_type: str):
    """Sends an ATTR_GET_SERVICE query so we don't have to wait for the
    device's own next spontaneous report. Some fields (e.g. a fountain's
    currentWeight) don't appear to ride along on a device's connect-time
    push at all -- only on an actual weight-change event or a query like
    this one -- so a freshly (re)connected device could otherwise sit with
    no water-level reading until something happens to trigger one."""
    global _client_ref
    if _client_ref is None:
        return
    import time as _t
    now = _t.time()
    _last_attr_poll[serial] = now
    try:
        svc_topic = _service_sub_topic(device_type, serial)
        payload = json.dumps({
            "cmd":   "ATTR_GET_SERVICE",
            "ts":    int(now * 1000),
            "msgId": f"{serial}_poll{int(now)}",
        })
        await _client_ref.publish(svc_topic, payload)
    except Exception:
        _LOGGER.exception("ATTR_GET_SERVICE poll failed for %s...", serial[:6])


def _mark_online(serial: str):
    """Mark device online and clear any pending offline timer."""
    import time as _t
    _last_seen[serial] = _t.time()
    was_online = _online.get(serial, False)
    _online[serial] = True
    if not was_online:
        _offline_since.pop(serial, None)
        if _client_ref is not None:
            asyncio.ensure_future(ha_mqtt.publish_availability(_client_ref, serial, True))
            device_type = _devices.get(serial, (None, None))[1]
            if device_type:
                # Poll full attribute state right away instead of waiting up
                # to ATTR_POLL_INTERVAL_SECS for the next watchdog sweep --
                # shaves the "just added/reconnected, no readings yet" gap
                # a new device would otherwise sit in.
                asyncio.ensure_future(_poll_attr_state(serial, device_type))
                # Also push a fresh time sync right on reconnect -- see
                # _push_ntp_sync's docstring for why. Not feeder-specific:
                # fountains have their own schedule-dependent behavior too
                # (the light schedule, and cordless models' run schedule),
                # so any device type benefits from an accurate clock.
                asyncio.ensure_future(_push_ntp_sync(serial, device_type))


def _mark_offline(serial: str):
    """Mark device offline, recording the time it first went offline."""
    import time as _t
    was_online = _online.get(serial, True)
    if was_online:
        _offline_since.setdefault(serial, _t.time())
    _online[serial] = False
    if was_online and _client_ref is not None:
        asyncio.ensure_future(ha_mqtt.publish_availability(_client_ref, serial, False))


def unregister_device(serial: str):
    _devices.pop(serial, None)
    _state.pop(serial, None)
    _online.pop(serial, None)
    _offline_since.pop(serial, None)
    _last_seen.pop(serial, None)
    _alert_sent.pop(serial, None)
    if _reconnect_event:
        _reconnect_event.set()


def get_device_state(serial: str) -> dict:
    return _state.get(serial, {})


def get_all_states() -> dict:
    return {
        serial: {**_state.get(serial, {}), "online": _online.get(serial, False)}
        for serial in _devices
    }


def is_online(serial: str) -> bool:
    return _online.get(serial, False)


# ── MQTT topic helpers ─────────────────────────────────────────────────────

def _mqtt_model(device_type: str) -> str:
    return _DEVICE_MODELS.get(device_type, device_type)


def _service_sub_topic(device_type: str, serial: str) -> str:
    return f"dl/{_mqtt_model(device_type)}/{serial}/device/service/sub"


def _event_sub_topic(device_type: str, serial: str) -> str:
    return f"dl/{_mqtt_model(device_type)}/{serial}/device/event/sub"


# ── Commands ───────────────────────────────────────────────────────────────

async def send_command(serial: str, payload: dict) -> bool:
    global _client_ref
    import time
    if serial not in _devices:
        _LOGGER.warning("send_command: unknown serial %s...", serial[:6])
        return False
    _, device_type = _devices[serial]
    topic = _service_sub_topic(device_type, serial)
    if _client_ref is None:
        _LOGGER.warning("send_command: no MQTT client")
        return False
    envelope = {
        "cmd":   "ATTR_SET_SERVICE",
        "ts":    int(time.time() * 1000),
        "msgId": f"{serial}_{int(time.time())}",
        **payload,
    }
    try:
        await _client_ref.publish(topic, json.dumps(envelope))
        _LOGGER.debug("Command sent to %s...", serial[:6])
        return True
    except Exception:
        _LOGGER.exception("Failed to send command to %s...", serial[:6])
        return False


async def send_display(serial: str, display_text: str, display_icon: int) -> bool:
    """Push display content to the feeder via MQTT.

    For text: generates a scrolling bitmap and sends DISPLAY_MATRIX_SERVICE.
    For icons: sends ATTR_SET_SERVICE with screenDisplayId (feeder handles built-in bitmaps).
    """
    global _client_ref
    import time
    import display_matrix as dm

    if serial not in _devices:
        _LOGGER.warning("send_display: unknown serial %s...", serial[:6])
        return False
    _, device_type = _devices[serial]
    topic = _service_sub_topic(device_type, serial)
    if _client_ref is None:
        _LOGGER.warning("send_display: no MQTT client")
        return False

    ts = int(time.time() * 1000)
    msg_id = f"{serial}_{ts}"

    _CHUNK = 20  # frames per sub-message, matching cloud protocol
    try:
        if display_text:
            frames = dm.text_to_frames(display_text)
            n = len(frames)
            is_static = n == 1
            chunks = [frames[i:i + _CHUNK] for i in range(0, n, _CHUNK)]
            sub_num = len(chunks)
            for sub_idx, chunk in enumerate(chunks):
                envelope: dict = {
                    "cmd": "DISPLAY_MATRIX_SERVICE",
                    "ts": ts,
                    "msgId": msg_id,
                    "screenDisplayMatrix": chunk,
                    "screenDisplayNumber": n,
                    "screenDisplayId": 0,
                    "screenDisplayInterval": 2,
                    "displayScene": "USER_CUSTOM",
                    "subNum": sub_num,
                    "subIndex": sub_idx,
                }
                if not is_static:
                    envelope["loopDisplay"] = True
                await _client_ref.publish(topic, json.dumps(envelope))
                if sub_idx < sub_num - 1:
                    await asyncio.sleep(0.15)
            _LOGGER.info(
                "DISPLAY_MATRIX_SERVICE sent to %s... (%d frame%s, %d chunk%s)",
                serial[:6], n, "" if n == 1 else "s", sub_num, "" if sub_num == 1 else "s",
            )
        elif display_icon:
            frame = dm.ICON_FRAMES.get(display_icon)
            if frame:
                envelope = {
                    "cmd": "DISPLAY_MATRIX_SERVICE",
                    "ts": ts,
                    "msgId": msg_id,
                    "screenDisplayMatrix": [frame],
                    "screenDisplayNumber": 1,
                    "screenDisplayId": display_icon,
                    "screenDisplayInterval": 2,
                    "displayScene": "USER_CUSTOM",
                    "loopDisplay": True,
                    "subNum": 1,
                    "subIndex": 0,
                }
                await _client_ref.publish(topic, json.dumps(envelope))
                _LOGGER.info("Display icon %d sent to %s...", display_icon, serial[:6])
            else:
                _LOGGER.warning("Unknown icon id %d, not sending", display_icon)
        return True
    except Exception:
        _LOGGER.exception("Failed to send display to %s...", serial[:6])
        return False


async def send_display_frame(serial: str, frame: list) -> bool:
    """Push a raw DISPLAY_MATRIX_SERVICE frame to the feeder (custom icon)."""
    global _client_ref
    import time
    if serial not in _devices:
        return False
    _, device_type = _devices[serial]
    topic = _service_sub_topic(device_type, serial)
    if _client_ref is None:
        return False
    try:
        ts = int(time.time() * 1000)
        envelope = {
            "cmd": "DISPLAY_MATRIX_SERVICE",
            "ts": ts,
            "msgId": f"{serial}_{ts}",
            "screenDisplayMatrix": [frame],
            "screenDisplayNumber": 1,
            "screenDisplayId": 0,
            "screenDisplayInterval": 2,
            "displayScene": "USER_CUSTOM",
            "loopDisplay": False,
            "subNum": 1,
            "subIndex": 0,
        }
        await _client_ref.publish(topic, json.dumps(envelope))
        _LOGGER.info("Custom display frame sent to %s...", serial[:6])
        return True
    except Exception:
        _LOGGER.exception("Failed to send display frame to %s...", serial[:6])
        return False


async def send_feeding_plans(serial: str, plans: list) -> bool:
    """Push FEEDING_PLAN_SERVICE to feeder with the given plans (enabled only, _enabled stripped)."""
    global _client_ref
    import time
    if serial not in _devices:
        _LOGGER.warning("send_feeding_plans: unknown serial %s...", serial[:6])
        return False
    _, device_type = _devices[serial]
    topic = _service_sub_topic(device_type, serial)
    if _client_ref is None:
        _LOGGER.warning("send_feeding_plans: no MQTT client")
        return False
    clean = [{k: v for k, v in p.items() if k != "_enabled"} for p in plans if p.get("_enabled", True)]
    clean.sort(key=lambda p: p.get("executionTime", "00:00"))
    payload = json.dumps({
        "cmd":    "FEEDING_PLAN_SERVICE",
        "ts":     int(time.time() * 1000),
        "msgId":  f"{serial}_{int(time.time())}",
        "plans":  clean,
    })
    try:
        await _client_ref.publish(topic, payload)
        _LOGGER.info("FEEDING_PLAN_SERVICE sent to %s... topic=%s payload=%s", serial[:6], topic, payload)
        return True
    except Exception:
        _LOGGER.exception("Failed to send feeding plans to %s...", serial[:6])
        return False


# ── Alert logic ────────────────────────────────────────────────────────────

_PROTO_FIELDS     = frozenset({"cmd", "msgId", "code", "ts"})
_SENSITIVE_FIELDS = frozenset({"wifiSsid", "wifiPassword", "audioUrl"})

# "Due"/level-style alerts stay true for days until the user acts on them, so
# a fresh "new" edge can reappear after any add-on restart (_alert_sent is
# in-memory only) and re-fire the same notification. Cap these to once per
# 24h regardless of how many times the edge re-triggers. Pure event
# transitions (offline/back-online, power_battery) are rare and already only
# fire on a genuine state change, so they're left unthrottled.
_THROTTLED_ALERTS = frozenset({
    "food_low", "desiccant_due", "bowl_due", "housing_due", "battery_low",
    "water_low", "filter_due", "cleaning_due",
})
_ALERT_THROTTLE_SECS = 86400


def _compute_device_alerts(serial: str) -> set:
    import storage as _storage
    cfg         = _storage.get_devices().get(serial, {})
    state       = _state.get(serial, {})
    device_type = cfg.get("device_type", "")
    notif       = cfg.get("notifications", {})
    alerts: set = set()

    mod = _device_types.get(device_type)
    if mod:
        alerts.update(mod.compute_alerts(state, cfg, _online.get(serial, False)))

    if notif.get("offline", True) and not _online.get(serial, False):
        import time as _t
        offline_secs = _t.time() - _offline_since.get(serial, _t.time())
        if offline_secs >= OFFLINE_ALERT_DELAY_SECS:
            alerts.add("offline")

    return alerts


async def _check_and_fire_alerts(serial: str):
    if _suppress_alerts:
        return
    import time
    import storage as _storage
    import notifications as _notifications
    try:
        current    = _compute_device_alerts(serial)
        prev       = _alert_sent.get(serial, set())
        new_alerts = current - prev
        cleared    = prev - current
        _alert_sent[serial] = current
        settings   = _storage.get_settings()
        device_cfg = _storage.get_devices().get(serial, {})
        name       = device_cfg.get("name") or serial[:6]
        for alert in new_alerts:
            if alert == "power_battery":
                # Our own record of when AC was lost, driven by powerType
                # (2 = battery, confirmed via direct testing) rather than the
                # feeder's io:35 device log, which turned out to also fire
                # for reasons unrelated to AC loss (see project notes).
                _state.setdefault(serial, {})["_power_lost_ts"] = time.time() * 1000
            if alert in _THROTTLED_ALERTS:
                last_fired = _storage.get_alert_last_fired(serial).get(alert, 0)
                if time.time() - last_fired < _ALERT_THROTTLE_SECS:
                    _LOGGER.info("Alert throttled (fired <24h ago): %s for %s...", alert, serial[:6])
                    continue
                _storage.save_alert_last_fired(serial, alert, time.time())
            msg      = _ALERT_MESSAGES.get(alert, alert)
            title    = f"Petlibro Local: {name} — {msg}"
            notif_id = f"petlibro_local_{serial[:8]}_{alert}"
            await _notifications.fire_notification(title, msg, settings, device_cfg,
                                                   notification_id=notif_id)
            _LOGGER.info("Alert fired: %s for %s...", alert, serial[:6])
        if "offline" in cleared:
            # Auto-dismiss the offline notification (HA bell + mobile clear) before firing back-online
            offline_id = f"petlibro_local_{serial[:8]}_offline"
            await _notifications.dismiss_notification(offline_id, settings, device_cfg)
            msg   = "Device is back online."
            title = f"Petlibro Local: {name} — {msg}"
            await _notifications.fire_notification(title, msg, settings, device_cfg)
            _LOGGER.info("Back-online notification fired for %s...", serial[:6])
        if "power_battery" in cleared:
            # This feeder drops WiFi shortly after losing AC, so we rarely see
            # a live "still on battery" signal -- but on reconnect it reports
            # its current power state fresh, so this fires once AC is back.
            power_id = f"petlibro_local_{serial[:8]}_power_battery"
            await _notifications.dismiss_notification(power_id, settings, device_cfg)
            lost_ts = _state.get(serial, {}).pop("_power_lost_ts", None)
            if lost_ts:
                now_ms = time.time() * 1000
                duration_secs = max(0, int((now_ms - lost_ts) / 1000))
                mins, secs = divmod(duration_secs, 60)
                dur_str = f"{mins}m{secs}s" if mins else f"{secs}s"
                msg = (f"Power lost at {_format_local_time(int(lost_ts))}, "
                       f"restored at {_format_local_time(int(now_ms))} ({dur_str}).")
            else:
                msg = "AC power restored."
            title = f"Petlibro Local: {name} — {msg}"
            await _notifications.fire_notification(title, msg, settings, device_cfg)
            _LOGGER.info("Power-restored notification fired for %s...: %s", serial[:6], msg)
    except Exception:
        _LOGGER.exception("Alert check failed for %s...", serial[:6])


async def _persist_state_cache(serial: str):
    try:
        import storage as _storage
        _storage.save_device_mqtt_cache(serial, dict(_state.get(serial, {})))
    except Exception:
        pass


async def republish_ha_discovery(serial: str):
    """Republish HA discovery + state after a device settings change (name, room, etc.)."""
    if _client_ref is None or serial not in _devices:
        return
    try:
        import storage as _storage
        cfg   = _storage.get_devices().get(serial, {})
        state = _state.get(serial, {})
        plans = _storage.get_device_feeding_plans(serial)
        custom_icon_names = [ic["name"] for ic in _storage.get_custom_icons()]
        await ha_mqtt.publish_discovery(_client_ref, serial, cfg, state, extra_icon_names=custom_icon_names)
        await ha_mqtt.publish_state(_client_ref, serial, cfg, state, plans=plans)
    except Exception:
        _LOGGER.exception("Failed to republish HA discovery for %s...", serial[:6])


async def retract_ha_discovery(serial: str, cfg: dict, state: dict):
    """Retract HA MQTT discovery for a device before it is removed."""
    if _client_ref is None:
        return
    try:
        await ha_mqtt.retract_discovery(_client_ref, serial, cfg, state)
    except Exception:
        _LOGGER.exception("Failed to retract HA discovery for %s...", serial[:6])


async def _publish_ha_state(serial: str):
    """Push current device state values to HA MQTT state topics."""
    if _client_ref is None:
        return
    try:
        import storage as _storage
        cfg   = _storage.get_devices().get(serial, {})
        state = _state.get(serial, {})
        plans = _storage.get_device_feeding_plans(serial)
        await ha_mqtt.publish_state(_client_ref, serial, cfg, state, plans=plans)
    except Exception:
        _LOGGER.exception("Failed to publish HA state for %s...", serial[:6])


# ── HA command handling ────────────────────────────────────────────────────

async def _handle_ha_command(serial: str, topic: str, payload: str):
    """Route an HA MQTT command to the device."""
    _, device_type = _devices.get(serial, (None, ""))
    cmd = ha_mqtt.parse_command(topic, payload.strip(), device_type=device_type)
    if cmd is None:
        _LOGGER.warning("Unrecognised HA command topic: %s", topic)
        return
    if cmd.get("_feed_now"):
        ok = await send_command(serial, {"cmd": "MANUAL_FEEDING_SERVICE", "grainNum": 1})
        _LOGGER.info("HA Feed Now %s...: %s", serial[:6], "ok" if ok else "failed")
    elif cmd.get("_open_door"):
        ok = await send_command(serial, {"cmd": "SWITCH_DOOR_SERVICE", "barnDoorState": True})
        _LOGGER.info("HA Open Door %s...: %s", serial[:6], "ok" if ok else "failed")
    elif "_display_text" in cmd:
        text = cmd["_display_text"][:20]
        if text:
            import storage as _storage
            _storage.save_device(serial, {"display_text": text, "display_icon": 0, "display_icon_name": None})
            ok = await send_display(serial, text, 0)
            asyncio.ensure_future(_publish_ha_state(serial))
            _LOGGER.info("HA Display Text %s...: %s", serial[:6], "ok" if ok else "failed")
    elif "_display_icon" in cmd:
        import storage as _storage
        icon_name = cmd["_display_icon"]
        icon_id = ha_mqtt._ICON_NAMES.get(icon_name, None)
        if icon_id is not None and icon_id > 0:
            _storage.save_device(serial, {"display_icon": icon_id, "display_text": "", "display_icon_name": icon_name})
            ok = await send_display(serial, "", icon_id)
        elif icon_name in ha_mqtt._CUSTOM_FRAMES:
            # Built-in custom frame (e.g. Petlibro Salute)
            _storage.save_device(serial, {"display_icon": icon_id or -1, "display_icon_name": icon_name})
            ok = await send_display_frame(serial, ha_mqtt._CUSTOM_FRAMES[icon_name])
        else:
            # Check user-saved custom icons by name
            saved = {ic["name"]: ic for ic in _storage.get_custom_icons()}
            if icon_name in saved:
                frame = [0] + [r << 7 for r in saved[icon_name]["rows"]] + [0]
                _storage.save_device(serial, {"display_icon_name": icon_name, "display_icon": 0})
                ok = await send_display_frame(serial, frame)
            else:
                # "None" — revert to stored text
                cfg = _storage.get_devices().get(serial, {})
                _storage.save_device(serial, {"display_icon": 0, "display_icon_name": None})
                ok = await send_display(serial, cfg.get("display_text", ""), 0)
        asyncio.ensure_future(_publish_ha_state(serial))
        _LOGGER.info("HA Display Icon %s... %s: %s", serial[:6], icon_name, "ok" if ok else "failed")
    elif "_display_frame" in cmd:
        frame = cmd["_display_frame"]
        if isinstance(frame, list) and len(frame) in (7, 8):
            ok = await send_display_frame(serial, frame)
            _LOGGER.info("Custom frame sent to %s...: %s", serial[:6], "ok" if ok else "failed")
    else:
        await send_command(serial, cmd)
        _LOGGER.debug("HA command %s... keys=%s", serial[:6], list(cmd.keys()))


async def handle_ha_command(serial: str, cmd: dict) -> None:
    """Public entry-point for API-driven commands (already-parsed dict)."""
    if cmd.get("_feed_now"):
        ok = await send_command(serial, {"cmd": "MANUAL_FEEDING_SERVICE", "grainNum": 1})
        _LOGGER.info("API Feed Now %s...: %s", serial[:6], "ok" if ok else "failed")
    elif cmd.get("_open_door"):
        ok = await send_command(serial, {"cmd": "SWITCH_DOOR_SERVICE", "barnDoorState": True})
        _LOGGER.info("API Open Door %s...: %s", serial[:6], "ok" if ok else "failed")
    elif "_display_text" in cmd:
        text = cmd["_display_text"][:20]
        if text:
            import storage as _storage
            _storage.save_device(serial, {"display_text": text, "display_icon": 0, "display_icon_name": None})
            ok = await send_display(serial, text, 0)
            asyncio.ensure_future(_publish_ha_state(serial))
            _LOGGER.info("API Display Text %s...: %s", serial[:6], "ok" if ok else "failed")
    elif "_display_frame" in cmd:
        frame = cmd["_display_frame"]
        if isinstance(frame, list) and len(frame) in (7, 8):
            ok = await send_display_frame(serial, frame)
            if ok:
                import storage as _storage
                name = cmd.get("_display_frame_name") or None
                _storage.save_device(serial, {"display_icon_name": name, "display_icon": 0})
                asyncio.ensure_future(_publish_ha_state(serial))
            _LOGGER.info("API Custom frame %s...: %s", serial[:6], "ok" if ok else "failed")
    elif "_light_schedule" in cmd:
        sched = cmd["_light_schedule"] or {}
        start = sched.get("start")
        end = sched.get("end")
        if start and end:
            import storage as _storage
            _storage.save_device(serial, {"light_start_time": start, "light_end_time": end})
            ok = await send_command(serial, {
                "lightingStartTime": start,
                "lightingStartTimeUtc": _local_hhmm_to_utc(start),
                "lightingEndTime": end,
                "lightingEndTimeUtc": _local_hhmm_to_utc(end),
            })
            asyncio.ensure_future(_publish_ha_state(serial))
            _LOGGER.info("API Light Schedule %s...: %s", serial[:6], "ok" if ok else "failed")
    else:
        await send_command(serial, cmd)
        _LOGGER.debug("API command %s... keys=%s", serial[:6], list(cmd.keys()))


# ── Message handling ───────────────────────────────────────────────────────

def _handle_message(serial: str, topic_str: str, raw: str):
    try:
        data = json.loads(raw)
    except Exception:
        _LOGGER.debug("Non-JSON from %s...", serial[:6])
        return

    cmd = data.get("cmd")

    if cmd == "NTP":
        _LOGGER.info("NTP request from %s...", serial[:6])
        _ntp_logger.info("REQUEST from %s...", serial[:6])
        asyncio.ensure_future(_respond_ntp(topic_str))
        _mark_online(serial)
        return

    if cmd == "GET_FEEDING_PLAN_EVENT":
        asyncio.ensure_future(_respond_feeding_plan(serial, topic_str))
        return

    if cmd == "FEEDING_PLAN_SERVICE":
        # Feeder's ack for a plan push -- echoes back what it now has stored,
        # so this is the real evidence of whether the plan was accepted, and
        # whether the times/days it stored actually match what we sent.
        code = data.get("code")
        plans = data.get("plans", [])
        _LOGGER.info("FEEDING_PLAN_SERVICE ack from %s...: code=%s plans=%s", serial[:6], code, json.dumps(plans))
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        return

    if cmd == "SWITCH_DOOR_SERVICE":
        code = data.get("code")
        _LOGGER.debug("SWITCH_DOOR_SERVICE ack from %s... code=%s", serial[:6], code)
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        return

    if cmd == "MANUAL_FEEDING_SERVICE":
        # Feeder ack for manual feed — code 0 = accepted.
        code = data.get("code")
        _LOGGER.debug("MANUAL_FEEDING_SERVICE ack from %s... code=%s", serial[:6], code)
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        return

    if cmd == "GRAIN_OUTPUT_EVENT":
        # Feeder reports grain dispensing progress. Ack it, and track intake on completion.
        asyncio.ensure_future(_ack_grain_output(serial, topic_str, data))
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        return

    if cmd == "PET_IDENTIFY_EVENT":
        import time as _time
        tag_id = (data.get("rfid") or data.get("tagId") or data.get("rfidNo")
                  or data.get("rfId") or data.get("cardNo") or data.get("id"))
        action = (data.get("type") or data.get("action") or data.get("status") or data.get("eventType") or "").upper()
        _LOGGER.debug("PET_IDENTIFY_EVENT serial=%s... tag=%s action=%s raw=%s",
                      serial[:6], tag_id, action, data)
        if tag_id:
            tag_str = str(tag_id)
            _state.setdefault(serial, {})["last_rfid_tag"] = tag_str
            if action == "NEAR":
                _state[serial]["_rfid_near_ts"] = int(_time.time() * 1000)
                _state[serial]["_rfid_tag_active"] = tag_str
            elif action == "LEAVE":
                near_ts  = _state.get(serial, {}).pop("_rfid_near_ts", None)
                act_tag  = _state.get(serial, {}).pop("_rfid_tag_active", tag_str)
                if near_ts:
                    duration_secs = max(0, int((_time.time() * 1000 - near_ts) / 1000))
                    try:
                        import storage as _storage
                        cfg   = _storage.get_devices().get(serial, {})
                        min_s = int(cfg.get("min_eating_secs", 30))
                        if duration_secs >= min_s:
                            # Find pet linked to this feeder that has this tag (or first linked pet)
                            pets   = _storage.get_pets()
                            pet_id = next(
                                (p["id"] for p in pets.values()
                                 if str(p.get("rfid_tag", "")) == act_tag),
                                None,
                            )
                            if pet_id is None:
                                pet_ids = cfg.get("pet_ids", [])
                                pet_id  = pet_ids[0] if pet_ids else None
                            # Auto-save tag to pet profile if not already stored
                            if pet_id and pets.get(pet_id, {}).get("rfid_tag") != act_tag:
                                _storage.save_pet(pet_id, {"rfid_tag": act_tag})
                                _LOGGER.info("Auto-saved RFID tag %s to pet %s", act_tag, pet_id)
                            _storage.log_feeder_event(
                                serial, "pet_eating",
                                extra={"duration_secs": duration_secs,
                                       "rfid_tag": act_tag, "pet_id": pet_id},
                            )
                            # Auto-dismiss no-eat alert if one was firing
                            if pet_id and _pet_no_eat_alerted.pop(pet_id, False):
                                try:
                                    import notifications as _notif2
                                    import storage as _st2
                                    notif_id = f"petlibro_local_{pet_id[:8]}_no_eat"
                                    asyncio.ensure_future(
                                        _notif2.dismiss_notification(notif_id, _st2.get_settings(),
                                                                     {"notify_bell": True, "notify_mobile": True})
                                    )
                                except Exception:
                                    pass
                            # Pet eating notification
                            try:
                                import notifications as _notif
                                pet_name = pets.get(pet_id, {}).get("name") or "Your pet"
                                dev_name = cfg.get("name") or serial[:8]
                                mins = duration_secs // 60
                                secs_r = duration_secs % 60
                                dur_str = f"{mins}m{secs_r:02d}s" if mins else f"{secs_r}s"
                                msg = f"{pet_name} ate for {dur_str} at {dev_name}"
                                title = f"Petlibro Local: {msg}"
                                pet_cfg = pets.get(pet_id, {})
                                notify_cfg = {
                                    "notify_bell":   pet_cfg.get("notify_bell", True),
                                    "notify_email":  pet_cfg.get("notify_email", True),
                                    "notify_mobile": pet_cfg.get("notify_mobile", False),
                                }
                                notif_id = f"petlibro_local_{(pet_id or serial)[:8]}_eating_{int(_time.time())}"
                                asyncio.ensure_future(
                                    _notif.fire_notification(title, msg, _storage.get_settings(), notify_cfg,
                                                             notification_id=notif_id)
                                )
                            except Exception:
                                _LOGGER.exception("Pet eating notification error")
                    except Exception:
                        _LOGGER.exception("PET_IDENTIFY_EVENT eating session error")
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        return

    if cmd == "WEIGHT_CLR_SERVICE":
        # Scale zero-calibration ack. One captured example: a lone `code: 3007`
        # response when the water container was still on the scale, versus a
        # `code: 0` -> `code: 3001` -> `code: 3003` sequence (the last one
        # carrying a "zero_standar_bcd:..." msg reporting the new baseline)
        # once the container was actually removed first. Not documented
        # anywhere, just one aligned before/after example, so this is our
        # best current read of these codes, not a confirmed spec.
        import time as _time
        code = data.get("code")
        msg = data.get("msg", "")
        _state.setdefault(serial, {})["_scale_calibration"] = {
            "code": code, "msg": msg, "ts": int(_time.time() * 1000),
        }
        _LOGGER.debug("WEIGHT_CLR_SERVICE ack from %s...: code=%s msg=%s", serial[:6], code, msg)
        _mark_online(serial)
        return

    if cmd == "WEIGHT_CHANGE_EVENT":
        # RFID-capable fountain (Dockstream RFID Smart Fountain / PLWF305):
        # reports which pet's tag(s) were near the bowl during a detected
        # drink, with the exact drop amount and duration already computed
        # device-side -- no NEAR/LEAVE state tracking needed like the feeder.
        asyncio.ensure_future(_handle_fountain_drink(serial, data))
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        asyncio.ensure_future(_publish_ha_state(serial))
        return

    if cmd == "WAREHOUSE_DOOR_EVENT":
        # Feeder door state change (open/close). Track duration to detect feeding sessions.
        import time as _time
        barn = data.get("barnDoorState")
        if barn is not None:
            prev_barn = _state.get(serial, {}).get("barnDoorState")
            _state.setdefault(serial, {})["barnDoorState"] = barn
            if barn and not prev_barn:
                _state[serial]["_door_open_ts"] = int(_time.time() * 1000)
            elif not barn and prev_barn:
                open_ts = _state[serial].pop("_door_open_ts", None)
                if open_ts:
                    duration_secs = int((_time.time() * 1000 - open_ts) / 1000)
                    try:
                        import storage as _storage
                        cfg = _storage.get_devices().get(serial, {})
                        min_secs = int(cfg.get("min_eating_secs", 30))
                    except Exception:
                        min_secs = 30
                    if duration_secs >= min_secs:
                        try:
                            now_ts = int(_time.time())
                            trigger = (data.get("triggerType") or "").upper()
                            _storage.save_device(serial, {"last_eating_secs": duration_secs})
                            if trigger == "PET_IDENTIFY":
                                # RFID-triggered: attribute to pet and log as eating
                                act_tag = _state.get(serial, {}).get("last_rfid_tag")
                                extra = {"duration_secs": duration_secs}
                                if act_tag:
                                    extra["rfid_tag"] = act_tag
                                    pets = _storage.get_pets()
                                    pet_id = next(
                                        (p["id"] for p in pets.values()
                                         if str(p.get("rfid_tag", "")) == act_tag),
                                        None,
                                    )
                                    if pet_id is None:
                                        pet_ids = cfg.get("pet_ids", [])
                                        pet_id = pet_ids[0] if pet_ids else None
                                    if pet_id:
                                        extra["pet_id"] = pet_id
                                _storage.log_feeder_event(serial, "pet_eating", extra=extra)
                                _storage.save_device(serial, {"last_fed_ts": now_ts})
                            else:
                                # Door-only: log for device history but not as a feeding event
                                _storage.log_feeder_event(
                                    serial, "door_open",
                                    extra={"duration_secs": duration_secs},
                                )
                        except Exception:
                            _LOGGER.exception("WAREHOUSE_DOOR_EVENT eating log error")
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        asyncio.ensure_future(_publish_ha_state(serial))
        return

    if cmd == "DEVICE_START_EVENT":
        # Device just booted. This is the only event we've ever seen carry
        # softwareVersion/hardwareVersion, and until now nothing actually
        # stored them anywhere, so whatever version a modal showed was
        # either blank or a stale value resurrected from the on-disk cache
        # from whenever it first got lucky and captured one, never refreshed.
        version_changed = False
        for key in ("softwareVersion", "hardwareVersion"):
            if key in data and _state.get(serial, {}).get(key) != data[key]:
                _state.setdefault(serial, {})[key] = data[key]
                version_changed = True
        if version_changed:
            # Also refresh HA's own Device Info panel (sw_version/hw_version
            # live in the discovery device block, not just a sensor state),
            # otherwise it'd stay stuck on whatever discovery last published
            # at add-on startup even though the sensors themselves update fine.
            asyncio.ensure_future(republish_ha_discovery(serial))
        asyncio.ensure_future(_ack_device_start(serial, topic_str, data))
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        asyncio.ensure_future(_publish_ha_state(serial))
        return

    if cmd == "HEARTBEAT":
        rssi = data.get("rssi")
        if rssi is not None:
            _state.setdefault(serial, {})["rssi"] = rssi
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        asyncio.ensure_future(_publish_ha_state(serial))
        return

    if cmd in ("ATTR_GET_SERVICE", "ATTR_SET_SERVICE", "ATTR_PUSH_EVENT"):
        skip    = _PROTO_FIELDS | _SENSITIVE_FIELDS
        if cmd == "ATTR_GET_SERVICE":
            # Every ATTR_GET_SERVICE response captured so far has shown
            # powerType=3 (AC) and powerMode=1, always while genuinely on
            # AC -- so 3 may well be a real, live reading here, not a fake
            # placeholder. But it's never been captured mid-battery-outage
            # to confirm it updates correctly in that case, so play it safe
            # and only trust these two fields from a genuine, device-
            # initiated ATTR_PUSH_EVENT.
            skip = skip | {"powerType", "powerMode"}
        partial = {k: v for k, v in data.items() if k not in skip}
        if partial:
            _, device_type = _devices.get(serial, (None, None))
            mod       = _device_types.get(device_type) if device_type else None
            old_state = dict(_state.get(serial, {}))
            _state.setdefault(serial, {}).update(partial)
            if mod:
                try:
                    import storage as _storage
                    min_drink = _storage.get_devices().get(serial, {}).get("min_drink_grams", 5)
                except Exception:
                    min_drink = 5
                grams = mod.track_intake(old_state, _state[serial], min_grams=min_drink)
                if grams:
                    try:
                        import time as _time
                        import storage as _storage
                        now_ms = int(_time.time() * 1000)
                        last_ms = _state[serial].get("_last_drink_log_ms")
                        if last_ms and (now_ms - last_ms) < FOUNTAIN_DRINK_DEDUP_SECS * 1000:
                            # A second qualifying weight-drop within a few seconds
                            # of one we just logged -- almost certainly the same
                            # physical event reported in more than one step (a
                            # continuous drink, or something disturbing the
                            # fountain like lifting the water container off),
                            # not a second separate drink. Skip logging it again.
                            _LOGGER.debug("Skipped duplicate drink log (%.0fg, %dms after last) for %s...",
                                          grams, now_ms - last_ms, serial[:6])
                        else:
                            _state[serial]["_last_drink_log_ms"] = now_ms
                            _storage.record_intake(serial, grams)
                            _storage.log_fountain_event(serial, "drink", grams=grams)
                            _storage.save_device(serial, {"last_drink_ts": int(_time.time())})
                            _LOGGER.debug("Recorded %.0fg drink for %s...", grams, serial[:6])
                    except Exception:
                        _LOGGER.exception("Failed to record intake for %s...", serial[:6])
            asyncio.ensure_future(_persist_state_cache(serial))
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        asyncio.ensure_future(_publish_ha_state(serial))
        return

    # Unknown cmd — store non-protocol fields
    skip    = _PROTO_FIELDS | _SENSITIVE_FIELDS
    partial = {k: v for k, v in data.items() if k not in skip}
    if partial:
        _state.setdefault(serial, {}).update(partial)
    _mark_online(serial)


async def _respond_feeding_plan(serial: str, request_topic: str) -> None:
    import time as _time
    global _client_ref
    if _client_ref is None:
        return
    response_topic = request_topic.replace("/post", "/sub")
    # Respond with GET_FEEDING_PLAN_EVENT + stored plans. Empty list is fine — feeder
    # will report code 2099 but runs its stored schedule autonomously.
    import storage as _storage
    stored_plans = _storage.get_device_feeding_plans(serial)
    clean_plans = [{k: v for k, v in p.items() if k != "_enabled"}
                   for p in stored_plans if p.get("_enabled", True)]
    clean_plans.sort(key=lambda p: p.get("executionTime", "00:00"))
    payload = json.dumps({
        "cmd":   "GET_FEEDING_PLAN_EVENT",
        "code":  0,
        "ts":    int(_time.time() * 1000),
        "plans": clean_plans,
    })
    try:
        await _client_ref.publish(response_topic, payload)
        _LOGGER.info("Feeding plan response sent to %s... (%d plans)", serial[:6], len(clean_plans))
    except Exception:
        pass


async def _ack_grain_output(serial: str, event_topic: str, data: dict) -> None:
    import time as _time
    global _client_ref
    if _client_ref is None:
        return
    response_topic = event_topic.replace("/post", "/sub")
    ack = json.dumps({
        "cmd":      "GRAIN_OUTPUT_EVENT",
        "ts":       int(_time.time() * 1000),
        "msgId":    data.get("msgId", ""),
        "code":     0,
        "execStep": data.get("execStep", ""),
    })
    try:
        await _client_ref.publish(response_topic, ack)
    except Exception:
        pass
    # Track food dispensed on completion
    if data.get("finished") and data.get("actualGrainNum", 0) > 0:
        try:
            import storage as _storage
            portions = data["actualGrainNum"]
            _storage.record_intake(serial, portions)
            _storage.log_feeder_event(serial, "food_dispensed", portions=portions)
        except Exception:
            pass


def _local_hhmm_to_utc(hhmm: str) -> str:
    """Converts an "HH:MM" local-time string to "HH:MM" UTC, using the same
    feeder_timezone setting the feeding-schedule editor uses. Mirrors
    util.js's _localToUtc so the fountain light schedule is entered and
    displayed in local time but sent to the device in UTC, same as the
    feeder's feeding plan times."""
    import datetime as _dt
    import storage as _storage
    tz_name = _storage.get_settings().get("feeder_timezone", "")
    offset_hours = 0.0
    if tz_name:
        try:
            from zoneinfo import ZoneInfo as _ZI
            now = _dt.datetime.now(_dt.timezone.utc)
            offset = now.astimezone(_ZI(tz_name)).utcoffset()
            offset_hours = (offset or _dt.timedelta()).total_seconds() / 3600
        except Exception:
            offset_hours = 0.0
    h, m = (int(x) for x in hhmm.split(":"))
    mins = (h * 60 + m - round(offset_hours * 60)) % 1440
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _format_local_time(ts_ms: int) -> str:
    import datetime as _dt
    import storage as _storage
    tz_name = _storage.get_settings().get("feeder_timezone", "")
    dt = _dt.datetime.fromtimestamp(ts_ms / 1000, tz=_dt.timezone.utc)
    if tz_name:
        try:
            from zoneinfo import ZoneInfo as _ZI
            dt = dt.astimezone(_ZI(tz_name))
        except Exception:
            dt = dt.astimezone()
    else:
        dt = dt.astimezone()
    return dt.strftime("%I:%M:%S %p").lstrip("0")


async def _handle_fountain_drink(serial: str, data: dict) -> None:
    """Attribute a Dockstream RFID Smart Fountain drink to a pet, mirroring
    the feeder's RFID eating-session pattern (devices.py's PET_IDENTIFY_EVENT
    handling), but simpler -- the fountain reports the finished drink
    (amount + duration + tag(s)) in one event instead of separate NEAR/LEAVE
    messages."""
    try:
        import time as _time
        import storage as _storage
        min_grams = _storage.get_devices().get(serial, {}).get("min_drink_grams", 5)
        grams = data.get("fluctuatingWeight")
        if grams is None or not (min_grams <= grams <= 800):
            return

        now_ms = int(_time.time() * 1000)
        last_ms = _state.get(serial, {}).get("_last_drink_log_ms")
        if last_ms and (now_ms - last_ms) < FOUNTAIN_DRINK_DEDUP_SECS * 1000:
            # Same reasoning as the plain fountain's weight-drop path: a
            # second qualifying report this soon after one we just logged is
            # almost certainly the same event, not a second drink.
            _LOGGER.debug("Skipped duplicate RFID drink log (%.0fg, %dms after last) for %s...",
                          grams, now_ms - last_ms, serial[:6])
            return
        _state.setdefault(serial, {})["_last_drink_log_ms"] = now_ms

        rfids = data.get("rfids") or []
        tag = None
        if rfids:
            # Multiple tags can appear if pets drank close together; use
            # whichever was scanned most during the session as the more
            # likely drinker.
            best = max(rfids, key=lambda r: r.get("count", 0))
            if best.get("rfid"):
                tag = str(best["rfid"])

        cfg = _storage.get_devices().get(serial, {})
        pet_ids = cfg.get("pet_ids", [])
        pets = _storage.get_pets()
        pet_id = None
        if tag:
            _state.setdefault(serial, {})["last_rfid_tag"] = tag
            pet_id = next((p["id"] for p in pets.values() if str(p.get("rfid_tag", "")) == tag), None)
            if pet_id is None and len(pet_ids) == 1:
                pet_id = pet_ids[0]
            if pet_id and pets.get(pet_id, {}).get("rfid_tag") != tag:
                _storage.save_pet(pet_id, {"rfid_tag": tag})
                _LOGGER.info("Auto-saved RFID tag %s to pet %s", tag, pet_id)

        extra = {"duration_secs": data.get("fluctuatingSeconds")}
        if tag:
            extra["rfid_tag"] = tag
        if pet_id:
            extra["pet_id"] = pet_id
        _storage.record_intake(serial, grams)
        _storage.log_fountain_event(serial, "drink", grams=grams, extra=extra)
        _storage.save_device(serial, {"last_drink_ts": int(_time.time())})

        if pet_id and tag:
            try:
                import notifications as _notif
                pet_name = pets.get(pet_id, {}).get("name") or "Your pet"
                dev_name = cfg.get("name") or serial[:8]
                msg = f"{pet_name} drank {round(grams)}mL at {dev_name}"
                title = f"Petlibro Local: {msg}"
                pet_cfg = pets.get(pet_id, {})
                notify_cfg = {
                    "notify_bell":   pet_cfg.get("notify_bell", True),
                    "notify_email":  pet_cfg.get("notify_email", True),
                    "notify_mobile": pet_cfg.get("notify_mobile", False),
                }
                notif_id = f"petlibro_local_{(pet_id or serial)[:8]}_drink_{int(_time.time())}"
                await _notif.fire_notification(title, msg, _storage.get_settings(), notify_cfg,
                                               notification_id=notif_id)
            except Exception:
                _LOGGER.exception("Pet drink notification error")
    except Exception:
        _LOGGER.exception("WEIGHT_CHANGE_EVENT handling error for %s...", serial[:6])


async def _ack_device_start(serial: str, event_topic: str, data: dict) -> None:
    import time as _time
    global _client_ref
    if _client_ref is None:
        return
    response_topic = event_topic.replace("/post", "/sub")
    ack = json.dumps({
        "cmd":   "DEVICE_START_EVENT",
        "ts":    int(_time.time() * 1000),
        "msgId": data.get("msgId", ""),
        "code":  0,
    })
    try:
        await _client_ref.publish(response_topic, ack)
        _LOGGER.debug("DEVICE_START_EVENT acked for %s...", serial[:6])
    except Exception:
        pass


def _build_ntp_payload() -> tuple[str, int]:
    # NOTE: the timezone field here does NOT affect when scheduled plans fire.
    # executionTime in plans is always UTC (the PetLibro app converts local→UTC
    # before sending). This timezone is only used by the feeder for on-device
    # clock display. It is configurable in Settings → General → Feeder Timezone.
    import time as _time
    import storage as _storage
    settings = _storage.get_settings()
    tz_name = settings.get("feeder_timezone", "") or os.environ.get("TZ", "")
    tz = settings.get("feeder_tz_offset", -7)
    if tz_name:
        try:
            import datetime as _dt
            from zoneinfo import ZoneInfo as _ZI
            tz = int(_dt.datetime.now(_ZI(tz_name)).utcoffset().total_seconds() / 3600)
        except Exception:
            pass
    now_ms = int(_time.time() * 1000)
    payload = json.dumps({
        "cmd":            "NTP",
        "ts":             now_ms,
        "code":           0,
        "calibrationTag": True,
        "timezone":       tz,
    })
    return payload, now_ms


async def _respond_ntp(request_topic: str) -> None:
    global _client_ref
    if _client_ref is None:
        return
    response_topic = request_topic.replace("/post", "/sub")
    payload, now_ms = _build_ntp_payload()
    parts = request_topic.split("/")
    serial = parts[2] if len(parts) > 2 else "?"
    try:
        await _client_ref.publish(response_topic, payload)
        _LOGGER.info("NTP response sent: ts=%s", now_ms)
        _ntp_logger.info("RESPONSE to %s...: ts=%s", serial[:6], now_ms)
    except Exception:
        _LOGGER.exception("Failed to send NTP response")
        _ntp_logger.exception("RESPONSE to %s... FAILED", serial[:6])


async def _push_ntp_sync(serial: str, device_type: str) -> None:
    """Proactively pushes a time sync instead of waiting for the device to
    ask for one. Added because a real debug capture (issue #5, a delayed
    scheduled feeding) showed the feeder going 40+ minutes of continuous
    activity -- heartbeats, RFID events, door events -- without ever
    requesting an NTP sync on its own, suggesting it may only sync at boot
    and just drift afterward. Applied to every device type, not just the
    feeder: fountains have their own schedule-dependent behavior too (the
    light schedule, and cordless models' run schedule), so an inaccurate
    clock could cause the same kind of drift there. EXPERIMENTAL: this
    sends the same NTP payload a device would normally get in response to
    its own request, but unprompted, on the same service/sub topic it
    already accepts other unsolicited commands (manual feed, door open,
    etc) on. Not confirmed that any of these devices' firmware actually
    applies a sync it didn't ask for."""
    if not ENABLE_PROACTIVE_NTP_PUSH:
        return
    global _client_ref
    if _client_ref is None:
        return
    topic = _service_sub_topic(device_type, serial)
    payload, now_ms = _build_ntp_payload()
    try:
        await _client_ref.publish(topic, payload)
        _LOGGER.info("Pushed unsolicited NTP sync to %s...: ts=%s", serial[:6], now_ms)
        _ntp_logger.info("PROACTIVE PUSH to %s...: ts=%s", serial[:6], now_ms)
    except Exception:
        _LOGGER.exception("Failed to push NTP sync to %s...", serial[:6])
        _ntp_logger.exception("PROACTIVE PUSH to %s... FAILED", serial[:6])


# ── Offline watchdog ───────────────────────────────────────────────────────

async def _offline_watchdog():
    """Periodically mark devices offline if no MQTT message received within WATCHDOG_SECS."""
    import time as _t
    while True:
        await asyncio.sleep(60)
        if _client_ref is None:
            continue  # already fully offline; _mqtt_loop handles marking
        now = _t.time()
        for serial in list(_devices):
            if not _online.get(serial):
                continue
            last = _last_seen.get(serial)
            if last is not None and (now - last) > WATCHDOG_SECS:
                _LOGGER.warning("Watchdog: no message from %s... in %.0fs — marking offline", serial[:6], now - last)
                _offline_since.setdefault(serial, last)
                _mark_offline(serial)
                asyncio.ensure_future(_check_and_fire_alerts(serial))

        # Periodically re-query full attribute state instead of only at
        # connect time. Some fields (e.g. the feeder's powerType) are only
        # pushed by the device on its own boot/transition, not on every
        # regular heartbeat -- without this, a value cached from an old
        # test or transient reboot state (e.g. stuck showing "on battery")
        # would never refresh again as long as the device stays up.
        if _client_ref is not None:
            for serial, (_, device_type) in list(_devices.items()):
                if not _online.get(serial):
                    continue
                if now - _last_attr_poll.get(serial, 0) < ATTR_POLL_INTERVAL_SECS:
                    continue
                await _poll_attr_state(serial, device_type)
                await _push_ntp_sync(serial, device_type)

        # Per-pet hasn't-eaten check
        try:
            import storage as _storage
            import notifications as _notifications
            pets     = _storage.get_pets()
            settings = _storage.get_settings()
            devices_data = _storage.get_devices()
            for pet_id, pet in pets.items():
                if not pet.get("no_eat_alert_enabled"):
                    _pet_no_eat_alerted.pop(pet_id, None)
                    continue
                threshold = int(pet.get("no_eat_alert_hours") or 12) * 3600
                pet_rfid  = str(pet.get("rfid_tag")) if pet.get("rfid_tag") else None
                last_eat_ms = None
                for serial, cfg in devices_data.items():
                    if pet_id not in cfg.get("pet_ids", []):
                        continue
                    sole = len(cfg.get("pet_ids", [])) == 1
                    for entry in _storage.get_feeder_log(serial, 100):
                        if entry.get("type") != "pet_eating":
                            continue
                        if (entry.get("pet_id") == pet_id
                                or (pet_rfid and str(entry.get("rfid_tag", "")) == pet_rfid)
                                or sole):
                            ts = entry.get("ts", 0)
                            if last_eat_ms is None or ts > last_eat_ms:
                                last_eat_ms = ts
                if last_eat_ms is None:
                    continue  # no sessions recorded yet, don't alert
                secs_since = now - last_eat_ms / 1000
                if secs_since >= threshold and not _pet_no_eat_alerted.get(pet_id):
                    pet_name = pet.get("name") or "Your pet"
                    hrs = secs_since / 3600
                    msg   = f"{pet_name} has not eaten in {hrs:.1f} hours."
                    title = f"Petlibro Local: {msg}"
                    notify_cfg = {
                        "notify_bell":   pet.get("notify_bell",   True),
                        "notify_email":  pet.get("notify_email",  True),
                        "notify_mobile": pet.get("notify_mobile", False),
                    }
                    notif_id = f"petlibro_local_{pet_id[:8]}_no_eat"
                    asyncio.ensure_future(
                        _notifications.fire_notification(title, msg, settings, notify_cfg,
                                                         notification_id=notif_id)
                    )
                    _pet_no_eat_alerted[pet_id] = True
                    _LOGGER.info("No-eat alert fired for pet %s...", pet_id[:6])
                elif secs_since < threshold:
                    _pet_no_eat_alerted.pop(pet_id, None)
        except Exception:
            _LOGGER.exception("Pet no-eat watchdog error")


# ── MQTT loop ──────────────────────────────────────────────────────────────

async def _mqtt_loop():
    global _client_ref
    while True:
        if _reconnect_event:
            _reconnect_event.clear()
        if not _devices:
            if _reconnect_event:
                try:
                    await asyncio.wait_for(_reconnect_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(5)
            continue
        try:
            async with aiomqtt.Client(
                hostname=_host,
                port=_port,
                username=_user if _user else None,
                password=_pass if _pass else None,
                keepalive=60,
            ) as client:
                import time as _time
                _client_ref = client
                import storage as _storage

                # Subscribed once, up front: covers every device's own
                # dl/{model}/{serial}/device/+/post topic on its own, so no
                # per-device dl/ subscribe is needed below (that would create
                # an overlapping subscription and cause the broker to deliver
                # a duplicate copy of every message).
                await client.subscribe("dl/#")

                for serial, (_, device_type) in list(_devices.items()):
                    _LOGGER.info("Registered: %s... model=%s", serial[:6], _mqtt_model(device_type))
                    svc_topic    = _service_sub_topic(device_type, serial)
                    init_payload = json.dumps({
                        "cmd":   "ATTR_GET_SERVICE",
                        "ts":    int(_time.time() * 1000),
                        "msgId": f"{serial}_init",
                    })
                    await client.publish(svc_topic, init_payload)
                    _last_attr_poll[serial] = _time.time()
                    # HA MQTT Discovery
                    cfg   = _storage.get_devices().get(serial, {})
                    state = _state.get(serial, {})
                    plans = _storage.get_device_feeding_plans(serial)
                    for ct in ha_mqtt.get_command_topics(serial, device_type):
                        await client.subscribe(ct)
                    custom_icon_names = [ic["name"] for ic in _storage.get_custom_icons()]
                    await ha_mqtt.publish_discovery(client, serial, cfg, state, extra_icon_names=custom_icon_names)
                    await ha_mqtt.retract_legacy_entities(client, serial, cfg)
                    await ha_mqtt.publish_state(client, serial, cfg, state, plans=plans)
                    await ha_mqtt.publish_availability(client, serial, True)

                async for message in client.messages:
                    if _reconnect_event and _reconnect_event.is_set():
                        break
                    topic_str   = str(message.topic)
                    payload_str = message.payload.decode("utf-8", errors="replace")
                    parts       = topic_str.split("/")

                    # HA command: petlibro_local/{serial}/{key}/set
                    if topic_str.startswith("petlibro_local/") and topic_str.endswith("/set") and len(parts) == 4:
                        ha_serial = parts[1]
                        if ha_serial in _devices:
                            asyncio.ensure_future(_handle_ha_command(ha_serial, topic_str, payload_str))
                        continue

                    # Device telemetry: dl/{model}/{serial}/device/{channel}/post
                    # Must check /post specifically -- dl/# also matches our own
                    # outbound .../service/sub and .../event/sub command/ack
                    # topics, and without this filter we'd receive our own
                    # published messages back and reprocess them as if the
                    # device sent them (e.g. re-acking our own DEVICE_START_EVENT
                    # ack forever).
                    if parts[0] == "dl" and len(parts) >= 4 and topic_str.endswith("/post"):
                        serial = parts[2]
                        recognized = serial in _devices
                        _raw_capture.append({
                            "ts":         _time.time(),
                            "topic":      topic_str,
                            "payload":    payload_str,
                            "recognized": recognized,
                        })
                        if recognized:
                            _handle_message(serial, topic_str, payload_str)
                        elif serial not in _unrecognized_serials_seen:
                            _unrecognized_serials_seen.add(serial)
                            _LOGGER.info(
                                "Unrecognized dl/ device seen: serial=%s... model=%s (not configured in this app) — captured for diagnostics",
                                serial[:8], parts[1],
                            )

        except aiomqtt.MqttError as e:
            _LOGGER.warning("MQTT connection lost: %s — retrying in 10s", e)
            await asyncio.sleep(10)
        except Exception:
            _LOGGER.exception("MQTT loop error — retrying in 10s")
            await asyncio.sleep(10)
        finally:
            _client_ref = None
            for serial in _devices:
                _mark_offline(serial)
            for serial in list(_devices):
                asyncio.ensure_future(_check_and_fire_alerts(serial))


# ── Connection test ────────────────────────────────────────────────────────

async def test_connection(host: str, port: int, user: str, password: str) -> bool:
    """Try a quick connect/disconnect to verify credentials. Returns True on success."""
    async def _try():
        async with aiomqtt.Client(
            hostname=host,
            port=port,
            username=user if user else None,
            password=password if password else None,
        ):
            pass
    try:
        await asyncio.wait_for(_try(), timeout=6)
        return True
    except Exception:
        return False


# ── Debug capture ──────────────────────────────────────────────────────────

def get_raw_capture() -> list[dict]:
    """Snapshot of the rolling raw dl/ traffic buffer, oldest first. Populated
    continuously by _mqtt_loop rather than on demand, since devices only chirp
    occasionally and a fixed listen window could easily miss one. Used by the
    Help/About "Download Debug Capture" button."""
    return list(_raw_capture)


def redact_payload(payload_str: str) -> str:
    """Redact known-sensitive fields (WiFi SSID/password, local audio server
    URL) from a raw MQTT payload string so a debug capture is safe to attach
    to a public GitHub issue. Serial numbers and MQTT credentials never
    appear in these payloads in the first place, so nothing else needs it.
    Falls back to the original string if it isn't valid JSON."""
    try:
        data = json.loads(payload_str)
    except Exception:
        return payload_str
    if not isinstance(data, dict):
        return payload_str
    changed = False
    for key in _SENSITIVE_FIELDS:
        if key in data:
            data[key] = "[redacted]"
            changed = True
    return json.dumps(data) if changed else payload_str


_watchdog_task: asyncio.Task | None = None

async def start():
    global _client_task, _watchdog_task, _reconnect_event
    _reconnect_event = asyncio.Event()
    if _client_task is None or _client_task.done():
        _client_task = asyncio.ensure_future(_mqtt_loop())
        _LOGGER.info("MQTT client task started")
    if _watchdog_task is None or _watchdog_task.done():
        _watchdog_task = asyncio.ensure_future(_offline_watchdog())
        _LOGGER.info("Offline watchdog started (threshold=%ds)", WATCHDOG_SECS)
