"""MQTT client for Petlibro devices.

Maintains a persistent connection to HA Mosquitto, subscribes to all configured
device topics, and keeps an in-memory state dict updated with the latest telemetry.
Device-type-specific logic (alerts, intake tracking) lives in device_types/.
"""

import asyncio
import collections
import json
import logging
import logging.handlers
import os

import aiomqtt
import device_types as _device_types
import ha_mqtt
import polar_control as _polar
import water_cap as _water_cap

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
# by our own pushes keeping its clock in sync regardless.
#
# UPDATE (2026-09-07): the One RFID feeder's natural log settled the
# question for that device type -- 4 consecutive nights all requested a
# sync at 23:54:01 local, each interval within a second of exactly 24h00m
# (86399.6s / 86399.9s / 86400.1s), including a night the owner confirmed
# had no reboot at all. This disproves the "only syncs at boot, drifts
# after" theory this feature was built around: the feeder resyncs on a
# fixed ~24h internal timer on its own, independent of reconnects. Fountains
# (WF03) haven't shown the same clean pattern yet -- their natural requests
# so far all line up with confirmed reboots, no repeating same-time-of-day
# signal, so this isn't settled for that device type. Leaving this OFF for
# now given the feeder finding; revisit if a fountain-specific time-drift
# complaint ever comes in.
#
# TURNED BACK ON (2026-09-20), CONFIRMED WORKING: Zoey's Feeder rebooted at
# ~17:39 during an HA update, came up with a clock 555s slow (stale restored
# time), its boot-time NTP request went unanswered because the broker/this
# app were restarting, and it doesn't retry until its next ~24h timer, so a
# 6:00 PM feed dropped at 6:09 PM. Pushing an unsolicited sync snapped its
# clock from 555s slow to exact within a second, so the firmware DOES apply
# one. Now used two ways: one push whenever a device comes back online, and
# detect-and-correct (see _check_device_clock): every heartbeat carries the
# device's own clock, so a correction is pushed only when it's actually off
# by more than CLOCK_OFFSET_THRESHOLD_SECS. No blanket periodic push.
ENABLE_PROACTIVE_NTP_PUSH = True

CLOCK_OFFSET_THRESHOLD_SECS = 30
CLOCK_CORRECTION_MIN_INTERVAL_SECS = 300   # at most one correction push per device per 5 min
CLOCK_CORRECTION_MAX_TRIES = 3             # give up after this many pushes that don't fix it
CLOCK_CORRECTION_BACKOFF_SECS = 3600       # then leave the device alone for an hour
# A feeder only fires a meal when its own clock reaches that exact second, with
# no catching up (reported by a Reddit user from direct tests, plus one real
# 9-minute-late feed here). So a correction that jumps the clock over a meal
# time is not harmless: forward over a due meal SKIPS it, backward over a meal
# already fed (a fast clock feeds early) feeds it AGAIN. Corrections are held
# while that would happen. A slow clock is held at most this long (the meal then
# fires late instead of being skipped), after which the correction goes ahead
# and the owner is notified which feed(s) were skipped. A fast clock is never
# forced: waiting only costs a wrong clock, jumping back can double-feed.
CLOCK_SLOW_HOLD_MAX_SECS = 3600
# A few seconds of margin on both ends of the window: a feeder applies a new
# time almost instantly (a Reddit user measured ~0.13s) but not literally
# instantly, so a meal right at the edge of the jump is treated as inside it.
CLOCK_HOLD_SLACK_SECS = 5

# Cloud-style time sync (OBSERVED 2026-10-09 on a One RFID, fw 3.0.72): the real
# cloud sends the feeder an unprompted NTP_SYNC on connect, the feeder acks it
# with its new clock, and it answers the feeder's own NTP request with
# calibrationTag true only when that request's clock was off. With this on, a
# One RFID gets the same: calibrationTag false when its clock is fine or a
# correction must be held, true plus an NTP_SYNC when it is off and safe to
# jump. Set False to restore the old behavior (always calibrationTag true).
# Types limited to the one actually captured; the Granary and fountains keep
# the old path until their real-cloud behavior is seen.
USE_CLOUD_STYLE_TIME_SYNC = True
CLOUD_TIME_SYNC_TYPES = ("one_rfid",)
NTP_SYNC_ACK_TIMEOUT_SECS = 10

# Dedicated log file for NTP request/response/push activity, separate from
# the main add-on log, so a multi-day observation window (see issue #5)
# doesn't require scrolling a huge combined log to find these lines.
_NTP_LOG_FILE = "/data/ntp_debug.log"
# Rotates at 1MB, keeping one backup (ntp_debug.log.1) -- plain FileHandler
# had no size limit at all, so a device stuck in a correction retry loop, or
# one that keeps bouncing on and off Wi-Fi, could grow this file unbounded
# on what might be an SD card. 1MB + 1 backup covers well over a month of
# even that misbehaving-device volume (normal use is under 1MB/year).
_NTP_LOG_MAX_BYTES = 1 * 1024 * 1024
_NTP_LOG_BACKUP_COUNT = 1
_ntp_logger = logging.getLogger("petlibro_local.ntp")
_ntp_logger.setLevel(logging.INFO)
_ntp_logger.propagate = False
try:
    _ntp_handler = logging.handlers.RotatingFileHandler(
        _NTP_LOG_FILE, maxBytes=_NTP_LOG_MAX_BYTES, backupCount=_NTP_LOG_BACKUP_COUNT)
    _ntp_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _ntp_logger.addHandler(_ntp_handler)
except Exception:
    _LOGGER.exception("Could not open dedicated NTP log file %s", _NTP_LOG_FILE)

# Rolling capture of raw dl/ traffic for diagnostics (Help/About "Download
# Debug Capture"). Devices only chirp occasionally, so this is passive and
# always-on rather than a fixed listen window that could easily miss a device
# that wakes up once every few minutes. Bounded by a rolling 24h time window
# (pruned on every append and on read), with a count-based safety cap as a
# backstop in case something floods traffic and would otherwise grow this
# buffer unbounded.
_RAW_CAPTURE_MAX_AGE_SECS = 24 * 3600
_RAW_CAPTURE_SAFETY_MAXLEN = 50000
_raw_capture: collections.deque = collections.deque(maxlen=_RAW_CAPTURE_SAFETY_MAXLEN)


def _prune_raw_capture(now: float) -> None:
    while _raw_capture and now - _raw_capture[0]["ts"] > _RAW_CAPTURE_MAX_AGE_SECS:
        _raw_capture.popleft()
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
# In-memory only on purpose (NOT in _state, which gets persisted and reloaded):
# a stale offset resurrected after a restart would be misleading.
_clock_offset: dict[str, int] = {}
_clock_fix_state: dict[str, dict] = {}
# Last audioUrl each device reported. Kept out of _state on purpose (sensitive,
# never shown in the UI) but resent with sound-hours changes so they cannot
# clobber a custom sound.
_audio_urls: dict[str, str] = {}
# msgId -> (serial, device_type) for NTP_SYNC messages awaiting the device's ack
_ntp_sync_pending: dict[str, tuple] = {}
_last_reconnect_push: dict[str, float] = {}
# Missed-feed watchdog (see _check_missed_feeds). In-memory only on purpose:
# it can only judge feeds it was watching, so it never looks back past the
# add-on's own start or a device's last reconnect.
ENABLE_MISSED_FEED_ALERT = True
MISSED_FEED_LEEWAY_SECS = 300       # how long after the scheduled time before it counts as missed
MISSED_FEED_LOOKBACK_SECS = 1800    # ignore misses older than this (don't alert about ancient history)
_ADDON_START_TS = __import__("time").time()
_online_since: dict[str, float] = {}
_missed_feed_alerted: dict[tuple, float] = {}
_pending_manual_feed: dict[str, float] = {}
# Polar Wet Food Feeder feed tracking (see polar_control.py). In-memory only on
# purpose, like _clock_offset: a feed flag resurrected after a restart would
# block feeding for a feed that is long over.
_polar_rt: dict[str, dict] = {}
# Dockstream RFID fountain (newer firmware) drink tracking, see water_cap.py. In memory only.
_wcap: dict[str, "_water_cap.Tracker"] = {}
# Longest observed GRAIN_START -> GRAIN_END gap in real captures was well
# under 10s; this is a generous window, not a measured firmware timeout.
_MANUAL_FEED_WINDOW_SECS = 60
# A broker restart can bounce a device online/offline several times in a
# minute (real, seen 2026-09-20: four pushes to one feeder in ~50s). One
# reconnect push per device per this window is plenty.
RECONNECT_PUSH_COOLDOWN_SECS = 60

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
        saved_model = _storage.get_devices().get(serial, {}).get("wire_model")
        if isinstance(saved_model, str) and _WIRE_MODEL_RE.match(saved_model):
            _wire_model[serial] = saved_model
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
        _online_since[serial] = _last_seen[serial]
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
                # Feeders are left to _check_device_clock on the next heartbeat
                # (about a minute away), which holds the correction if it would
                # skip or repeat a meal. A blind push here could not tell.
                # The Polar is left out too: its own plans run off its own clock and this
                # app cannot see them to tell whether a jump would skip one.
                if device_type not in ("one_rfid", "granary", "polar") and _last_seen[serial] - _last_reconnect_push.get(serial, 0.0) >= RECONNECT_PUSH_COOLDOWN_SECS:
                    _last_reconnect_push[serial] = _last_seen[serial]
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
    _polar_rt.pop(serial, None)
    _wire_model.pop(serial, None)
    _wcap.pop(serial, None)
    if _reconnect_event:
        _reconnect_event.set()


def get_device_state(serial: str) -> dict:
    return _state.get(serial, {})


def get_all_states() -> dict:
    return {
        serial: {**_state.get(serial, {}), "online": _online.get(serial, False),
                 "clock_offset_secs": _clock_offset.get(serial), **_polar_view(serial)}
        for serial in _devices
    }


def is_online(serial: str) -> bool:
    return _online.get(serial, False)


# ── MQTT topic helpers ─────────────────────────────────────────────────────

def _mqtt_model(device_type: str) -> str:
    return _DEVICE_MODELS.get(device_type, device_type)


# The model in a device's topics is whatever the device itself uses, which is not
# always the one its device type assumes: a Granary Smart Camera Feeder (serial
# AF03) talks on PLAF203 while the Granary type assumes PLAF103, so every command
# went to a topic the feeder never listens on. The first guess comes from the
# serial prefix, then from what was learned from the device's own messages
# (saved with the device, so it also holds right after a restart).
_PREFIX_WIRE_MODELS = {"AF03": "PLAF203"}
_WIRE_MODEL_RE = __import__("re").compile(r"^[A-Z0-9]{4,12}$")
_wire_model: dict[str, str] = {}


def _topic_model(device_type: str, serial: str) -> str:
    return _wire_model.get(serial) or _PREFIX_WIRE_MODELS.get(serial[:4]) or _mqtt_model(device_type)


def _learn_wire_model(serial: str, model: str) -> None:
    """Remember the model a registered device really uses in its topics."""
    if serial not in _devices or not isinstance(model, str) or not _WIRE_MODEL_RE.match(model):
        return
    if _wire_model.get(serial) == model:
        return
    previous = _topic_model(_devices[serial][1], serial)
    _wire_model[serial] = model
    if previous != model:
        _LOGGER.info("Device %s... talks on model %s, not %s; commands now go there", serial[:6], model, previous)
    try:
        import storage as _storage
        _storage.save_device(serial, {"wire_model": model})
    except Exception:
        _LOGGER.exception("Could not save the model for %s...", serial[:6])


def _service_sub_topic(device_type: str, serial: str) -> str:
    return f"dl/{_topic_model(device_type, serial)}/{serial}/device/service/sub"


def _ntp_sub_topic(device_type: str, serial: str) -> str:
    return f"dl/{_topic_model(device_type, serial)}/{serial}/device/ntp/sub"


def _event_sub_topic(device_type: str, serial: str) -> str:
    return f"dl/{_topic_model(device_type, serial)}/{serial}/device/event/sub"


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
    if payload.get("cmd") == "MANUAL_FEEDING_SERVICE":
        # Every manual dispense (Feed Now, the calibration wizard) is
        # requested by us, unlike a scheduled feed which the feeder fires on
        # its own with no command from this app. Recording when we asked
        # lets _ack_grain_output tell the two apart in the activity log.
        _pending_manual_feed[serial] = time.time()
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


def _granary_settings_bundle(serial: str, overrides: dict) -> dict:
    """Build a full Granary Smart Feeder settings payload from currently-known
    state, with one or more fields overridden.

    A real mqtt_proxy.py capture (issue #8, 2026-09-11) showed every ATTR_SET_SERVICE
    the vendor app sends this device bundles all six of these fields together,
    volume/audioUrl/enableAudio/lightSwitch/lightAgingType/disableHardwareButton,
    even when the user only changed one of them in the app. Never observed a
    lone single-field update, unlike the One RFID feeder where that's already
    confirmed to work fine. Matching the observed pattern here rather than
    assuming a partial update also works for this device."""
    state = _state.get(serial, {})
    bundle = {
        "volume":                state.get("volume", 50),
        "audioUrl":               state.get("audioUrl", ""),
        "enableAudio":            state.get("enableAudio", False),
        "lightSwitch":            state.get("lightSwitch", True),
        "lightAgingType":         state.get("lightAgingType", 1),
        "disableHardwareButton":  state.get("disableHardwareButton", False),
    }
    bundle.update(overrides)
    return bundle


async def send_display(serial: str, display_text: str, display_icon: int, scene: str = "USER_CUSTOM") -> bool:
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
            while len(frames) > 140 and len(display_text) > 1:
                # More than 140 frames spills into the feeder's pet-name storage.
                display_text = display_text[:-1]
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
                    "displayScene": scene,
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


async def send_display_frames(serial: str, frames: list, interval: int = 4, loop: bool = True) -> bool:
    """Push a multi-frame animation to the feeder's idle LED screen.

    Format from a Reddit reporter's spec (unverified here until tested on a
    real feeder): frames go 20 per message, longer animations as several
    messages with the SAME msgId, subNum = message count, subIndex = 0..n-1,
    screenDisplayNumber = total frames; about a second between messages;
    screenDisplayInterval is the speed. At most 140 frames (more corrupts the
    pet-name buffer). Sending text again overwrites the animation."""
    global _client_ref
    import time
    import animations as anim
    if serial not in _devices:
        return False
    if not anim.validate_frames(frames):
        _LOGGER.warning("send_display_frames: rejected invalid frames for %s...", serial[:6])
        return False
    _, device_type = _devices[serial]
    topic = _service_sub_topic(device_type, serial)
    if _client_ref is None:
        return False
    interval = max(1, min(10, int(interval)))
    n = len(frames)
    chunks = [frames[i:i + 20] for i in range(0, n, 20)]
    ts = int(time.time() * 1000)
    msg_id = f"{serial}_{ts}"
    try:
        for idx, chunk in enumerate(chunks):
            envelope = {
                "cmd": "DISPLAY_MATRIX_SERVICE",
                "ts": ts,
                "msgId": msg_id,
                "screenDisplayMatrix": chunk,
                "screenDisplayNumber": n,
                "screenDisplayId": 0,
                "screenDisplayInterval": interval,
                "displayScene": "USER_CUSTOM",
                "subNum": len(chunks),
                "subIndex": idx,
            }
            if loop and n > 1:
                envelope["loopDisplay"] = True
            await _client_ref.publish(topic, json.dumps(envelope))
            if idx < len(chunks) - 1:
                await asyncio.sleep(1.0)
        _LOGGER.info("Animation sent to %s...: %d frames, %d messages, interval %d, loop=%s",
                     serial[:6], n, len(chunks), interval, loop)
        return True
    except Exception:
        _LOGGER.exception("Failed to send animation to %s...", serial[:6])
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


def _wire_plans(plans: list) -> list:
    """Plans as the feeder should get them: enabled only, our _enabled flag
    removed, a skipEndTime dropped once it is in the past, sorted by time."""
    import time as _t
    now_ms = _t.time() * 1000
    out = []
    for p in plans:
        if not p.get("_enabled", True):
            continue
        q = {k: v for k, v in p.items() if k != "_enabled"}
        if isinstance(q.get("skipEndTime"), (int, float)) and q["skipEndTime"] <= now_ms:
            q.pop("skipEndTime")
        out.append(q)
    out.sort(key=lambda p: p.get("executionTime", "00:00"))
    return out


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
    import storage as _storage
    if (_storage.get_feeding_plans_tz_basis(serial) or {}).get("pending_sync"):
        _recheck_pending(serial, int(time.time() * 1000))   # DST shift not acked yet
        plans = _storage.get_device_feeding_plans(serial)
    if _storage.schedules_paused(serial):
        plans = []
    clean = _wire_plans(plans)
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


# ── Daylight saving: keep plans on local time ──────────────────────────────
# Plan executionTime is UTC on the wire (confirmed on a One RFID, fw 3.0.72: a plan
# sent as "17:54" fired at 19:54 CEST). The schedule editor converts local->UTC once,
# with the offset of the moment the plan is saved, so after a DST change every plan
# would fire an hour early or late. Each device remembers the timezone + offset its
# plans were computed for; when that zone's offset changes, the plans are shifted by
# the difference and re-sent until the feeder acks them.
#
# Only the timezone saved in Settings is used (the editor's fallback is the browser
# zone, which the server cannot know). Changing the timezone in Settings keeps the
# UTC times; plans saved before this feature are taken as correct for the current
# offset.
#
# Fall back moves meals later. If a meal's old time has passed (or is less than 2
# minutes away) but its new time has not (a meal inside the repeated hour, or a shift
# that reached the feeder late), the new time is skipped once (skipEndTime) so the
# cat is not fed twice. Until the feeder acks, the old list may still be active, so
# this is re-checked before every send, also when the schedule is edited meanwhile
# (only for meals the user did not move). Best effort: a lost ack, or a list landing
# just before the old time, can cost a meal instead.
# Spring forward moves meals earlier; a meal whose new time passed before the shift
# landed does not run that day (logged).

import re as _re_dst
_PLAN_TIME_RE = _re_dst.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_PLAN_RESEND_SECS = 300
_RUN_MARGIN_MS = 120000


def current_plan_tz_basis() -> dict | None:
    """{"tz", "offset" (seconds), "at" (ms)} for the timezone saved in Settings, or
    None when it is not set or cannot be resolved."""
    import datetime as _dt
    import time as _time
    import storage as _storage
    tz_name = (_storage.get_settings().get("feeder_timezone") or "").strip()
    if not tz_name:
        return None
    now = _time.time()
    try:
        from zoneinfo import ZoneInfo as _ZI
        offset = _dt.datetime.fromtimestamp(now, _ZI(tz_name)).utcoffset()
    except Exception:
        return None
    if offset is None:
        return None
    return {"tz": tz_name, "offset": int(offset.total_seconds()), "at": int(now * 1000)}


def _plans_valid(plans) -> bool:
    if not isinstance(plans, list):
        return False
    for p in plans:
        if not isinstance(p, dict) or not isinstance(p.get("executionTime"), str):
            return False
        if not _PLAN_TIME_RE.fullmatch(p["executionTime"]):
            return False
        days = p.get("repeatDay", [])
        if not isinstance(days, list) or not all(isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 7 for d in days):
            return False
    return True


def shift_plan_times(plans: list, delta_minutes: int) -> list:
    """Plans with every executionTime moved by -delta_minutes (UTC). delta_minutes is
    new_offset - old_offset, so going from UTC+2 to UTC+1 (-60) moves 05:00 to 06:00.
    A time that crosses midnight also moves its repeatDay weekdays by one. Expects
    plans that passed _plans_valid."""
    out = []
    for p in plans:
        q = dict(p)
        h, m = (int(x) for x in p["executionTime"].split(":"))
        total = h * 60 + m - delta_minutes
        day_shift = total // 1440          # -1, 0 or +1
        total %= 1440
        q["executionTime"] = f"{total // 60:02d}:{total % 60:02d}"
        days = q.get("repeatDay")
        if day_shift and days and len(set(days)) < 7:
            q["repeatDay"] = sorted({((d - 1 + day_shift) % 7) + 1 for d in days})
        out.append(q)
    return out


def _occurrences_ms(plan: dict, now_ms: int) -> list:
    """UTC instants (ms) of a plan's runs yesterday, today and tomorrow (UTC dates):
    enough to pair every meal with its shifted partner across UTC midnight."""
    import datetime as _dt
    h, m = (int(x) for x in plan["executionTime"].split(":"))
    today = _dt.datetime.fromtimestamp(now_ms / 1000, _dt.timezone.utc).date()
    out = []
    for back in (1, 0, -1):
        day = today - _dt.timedelta(days=back)
        if plan.get("repeatDay") and day.weekday() + 1 not in plan["repeatDay"]:
            continue
        out.append(int(_dt.datetime(day.year, day.month, day.day, h, m, tzinfo=_dt.timezone.utc).timestamp() * 1000))
    return out


def _skip_repeats(serial: str, old_plans: list, new_plans: list, delta_min: int, now_ms: int) -> bool:
    """Set skipEndTime on new plans whose old occurrence has passed while the shifted
    one is still ahead (would feed twice). Logs spring-forward meals lost the same way.
    Mutates new_plans; True when any skipEndTime changed."""
    import storage as _storage
    changed = False
    old_by_id = {p.get("planId"): p for p in old_plans}
    for p in new_plans:
        old = old_by_id.get(p.get("planId"))
        if old is None or not old.get("_enabled", True) or not _plans_valid([old]):
            continue
        partner = shift_plan_times([old], delta_min)[0]
        if (p.get("executionTime"), sorted(p.get("repeatDay") or [])) != \
                (partner["executionTime"], sorted(partner.get("repeatDay") or [])):
            continue   # the user moved this meal since: not the same meal any more
        for o in _occurrences_ms(old, now_ms):
            n = o - delta_min * 60000      # the same meal after the shift
            if _storage.occurrence_skipped(old, o / 1000):
                continue
            # "may have run": also within _RUN_MARGIN_MS before its time, since a list
            # sent just before can reach the feeder after the old meal already fired
            if n > o and o - _RUN_MARGIN_MS <= now_ms < n:
                end = n + 60000
                if int(p.get("skipEndTime") or 0) < end:
                    p["skipEndTime"] = end
                    changed = True
                    _LOGGER.info("Plan %s of %s...: its old time %s UTC may have run, skipping today's %s UTC so it is not fed twice",
                                 p.get("planId"), serial[:6], old["executionTime"], p["executionTime"])
            elif n < o and n <= now_ms < o:
                _LOGGER.warning("Plan %s of %s...: its shifted time %s UTC is already past; no catch-up meal is scheduled",
                                p.get("planId"), serial[:6], p["executionTime"])
    return changed


def _reconcile_device_storage(serial: str, basis: dict, now_ms: int) -> bool:
    """Storage half of the reconcile for one device (no awaits): shift the stored
    plans if the timezone's UTC offset changed. True when it shifted them."""
    import storage as _storage
    if _devices.get(serial, (None, None))[1] == "polar":
        return None  # Polar uses dated plans of its own (polar_control)
    plans = _storage.get_device_feeding_plans(serial)
    if not plans:
        return False
    old = _storage.get_feeding_plans_tz_basis(serial)
    if not old or old.get("tz") != basis["tz"]:
        if old is None:
            _LOGGER.info("Plans of %s... have no timezone basis yet; taking them as correct for %s (UTC%+.1f)",
                         serial[:6], basis["tz"], basis["offset"] / 3600)
        else:
            _LOGGER.info("Feeder timezone changed %s -> %s: plans of %s... keep their UTC times",
                         old.get("tz"), basis["tz"], serial[:6])
        # keep any pending delivery; only the conversion basis changes
        _storage.set_feeding_plans_tz_basis(serial, {**(old or {}), "tz": basis["tz"], "offset": basis["offset"], "at": now_ms})
        return False
    if old.get("offset") == basis["offset"]:
        return False
    if not _plans_valid(plans) or not isinstance(old.get("offset"), int):
        _LOGGER.warning("Plans of %s... not shifted for the UTC offset change: stored plans or basis are malformed", serial[:6])
        return False
    delta_min = (basis["offset"] - old["offset"]) // 60
    new_plans = shift_plan_times(plans, delta_min)
    # The feeder still runs the list it last acked. Normally that is `plans`; while a
    # previous shift is unacked it may still be the one before it.
    prev = old.get("prev") if old.get("pending_sync") else None
    if prev:
        prev = {"plans": prev["plans"], "delta": prev["delta"] + delta_min}
    else:
        prev = {"plans": plans, "delta": delta_min}
    _skip_repeats(serial, prev["plans"], new_plans, prev["delta"], now_ms)
    for p in new_plans:
        p["syncTime"] = now_ms
    new_basis = {"tz": basis["tz"], "offset": basis["offset"], "at": now_ms}
    if any(p.get("_enabled", True) for p in new_plans):
        new_basis.update(pending_sync=now_ms, last_sent=0, prev=prev)
    _storage.save_device_feeding_plans(serial, new_plans, tz_basis=new_basis)
    _LOGGER.info("UTC offset of %s changed %+d min: shifted %d plan(s) for %s... %s -> %s",
                 basis["tz"], delta_min, len(new_plans), serial[:6],
                 [p.get("executionTime") for p in plans], [p.get("executionTime") for p in new_plans])
    return True


def _recheck_pending(serial: str, now_ms: int) -> None:
    """Before (re)sending unacked shifted plans: re-run the double-feed check against
    the list the feeder may still be running. A changed payload gets a new syncTime
    so an ack for the earlier payload cannot clear it."""
    import storage as _storage
    b = _storage.get_feeding_plans_tz_basis(serial) or {}
    prev = b.get("prev")
    if not b.get("pending_sync") or not prev:
        return
    plans = _storage.get_device_feeding_plans(serial)
    if _skip_repeats(serial, prev["plans"], plans, prev["delta"], now_ms):
        sync = max(now_ms, int(b["pending_sync"]) + 1)
        for p in plans:
            p["syncTime"] = sync
        b["pending_sync"] = sync
        _storage.save_device_feeding_plans(serial, plans, tz_basis=b)


def prepare_plans_for_feeder(serial: str) -> None:
    """Bring stored plans up to date before they go to a feeder: shift for an offset
    change, then re-check unacked shifted plans."""
    import time as _time
    now_ms = int(_time.time() * 1000)
    basis = current_plan_tz_basis()
    if basis is not None:
        _reconcile_device_storage(serial, basis, now_ms)
    _recheck_pending(serial, now_ms)


def plan_revision(serial: str) -> str:
    """Opaque revision of a device's stored plans plus the Settings timezone they are
    converted with; the schedule editor sends it back so a stale save is refused."""
    import hashlib
    import storage as _storage
    b = current_plan_tz_basis()
    ctx = {"tz": b["tz"], "offset": b["offset"]} if b else None
    raw = json.dumps([_storage.get_device_feeding_plans(serial), ctx], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def accept_plan_edit(serial: str, plans: list, revision: str | None, now_ms: int) -> str | None:
    """Checks and stores a schedule saved in the editor. Returns a reason to refuse
    it (HTTP 409), or None once it is stored and ready to send.

    - revision (from GET's X-Plan-Revision; None = old client, not checked): the
      stored plans or the Settings timezone changed since the editor loaded them,
      e.g. a DST shift or a double-feed skip added since, so its list is stale.
    - While shifted plans are unacked the feeder may still run its old list: keep
      that uncertainty (prev), guard the saved plans and give them a new syncTime.
      Removing a skip the guard still needs is refused.
    """
    import copy
    import storage as _storage
    prepare_plans_for_feeder(serial)   # a due DST shift lands before the comparison
    if revision is not None and revision != plan_revision(serial):
        return "Schedule changed since you opened it; reload and try again."
    basis = current_plan_tz_basis()
    b = _storage.get_feeding_plans_tz_basis(serial) or {}
    prev = b.get("prev") if b.get("pending_sync") else None
    if prev:
        if _skip_repeats(serial, prev["plans"], copy.deepcopy(plans), prev["delta"], now_ms):
            return ("This meal is skipped once so it is not fed twice after the clock change; "
                    "the skip cannot be removed until the feeder confirms the new times.")
        sync = max(now_ms, int(b["pending_sync"]) + 1)
        for p in plans:
            p["syncTime"] = sync
        # kept even when every plan is now disabled: re-enabling must still be guarded
        basis = {**(basis or {"tz": None, "offset": None, "at": now_ms}),
                 "pending_sync": sync, "last_sent": now_ms, "prev": prev}
    _storage.save_device_feeding_plans(serial, plans, tz_basis=basis)
    return None


async def reconcile_plan_timezones() -> None:
    """Shift stored plans when the Settings timezone's UTC offset changed since they
    were saved (DST), and re-send shifted plans to online devices every 5 minutes
    until the feeder acks them. Runs every minute from the watchdog, also while MQTT
    is down (storage only then)."""
    import time as _time
    import storage as _storage
    now_ms = int(_time.time() * 1000)
    basis = current_plan_tz_basis()
    for serial in list(_devices):
        try:
            if basis is not None:
                _reconcile_device_storage(serial, basis, now_ms)
            b = _storage.get_feeding_plans_tz_basis(serial) or {}
            if not b.get("pending_sync") or _storage.schedules_paused(serial):
                continue  # paused: resume re-sends all plans
            if not any(p.get("_enabled", True) for p in _storage.get_device_feeding_plans(serial)):
                continue  # nothing enabled to deliver; keep the uncertainty for re-enabling
            if _client_ref is None or not _online.get(serial):
                continue  # offline: the feeder gets them when it asks for its plans
            if now_ms - int(b.get("last_sent") or 0) < _PLAN_RESEND_SECS * 1000:
                continue
            b["last_sent"] = now_ms
            _storage.set_feeding_plans_tz_basis(serial, b)
            await send_feeding_plans(serial, _storage.get_device_feeding_plans(serial))
            asyncio.ensure_future(publish_ha_state(serial))
        except Exception:
            _LOGGER.exception("Plan timezone reconcile failed for %s...", serial[:6])


def _plan_ack_received(serial: str, acked_plans) -> None:
    """Clear a pending re-send once the feeder acks every enabled plan with the
    pending syncTime."""
    import storage as _storage
    b = _storage.get_feeding_plans_tz_basis(serial)
    if not b or not b.get("pending_sync") or not isinstance(acked_plans, list):
        return
    expected = {(p.get("planId"), b["pending_sync"])
                for p in _storage.get_device_feeding_plans(serial) if p.get("_enabled", True)}
    received = {(p.get("planId"), p.get("syncTime")) for p in acked_plans if isinstance(p, dict)}
    if expected and expected <= received:   # an empty list proves nothing
        for k in ("pending_sync", "last_sent", "prev"):
            b.pop(k, None)
        _storage.set_feeding_plans_tz_basis(serial, b)
        _LOGGER.info("Shifted plans acked by %s...", serial[:6])


# ── Alert logic ────────────────────────────────────────────────────────────

_PROTO_FIELDS     = frozenset({"cmd", "msgId", "code", "ts"})
_SENSITIVE_FIELDS = frozenset({"wifiSsid", "wifiPassword", "audioUrl", "cameraAuthInfo"})

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
# Alerts that were active when the add-on last ran are remembered on disk, so a
# restart or rebuild does not announce the same still-true alert again (door
# jam, running on battery, refill due and so on). The memory only suppresses
# repeats for this long after startup, because right after a restart a device
# has not reported yet and "not reported" must not be read as "cleared" (that
# would send a false "AC power restored"), and because an alert that is really
# gone should be able to fire again later. "offline" is not remembered: the
# watchdog rebuilds it from scratch after a restart.
_ALERT_RESTART_GRACE_SECS = 600
_alert_persisted: dict[str, frozenset] = {}


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
        # Restart memory (see _ALERT_RESTART_GRACE_SECS)
        in_grace   = time.time() - _ADDON_START_TS < _ALERT_RESTART_GRACE_SECS
        restored   = set(_storage.get_alerts_active(serial)) if in_grace else set()
        suppressed = new_alerts & restored
        if suppressed:
            new_alerts = new_alerts - suppressed
            _LOGGER.info("Alerts already sent before the restart, not repeating: %s for %s...",
                         sorted(suppressed), serial[:6])
        to_save = frozenset((current | restored) - {"offline"})
        if _alert_persisted.get(serial) != to_save:
            _storage.save_alerts_active(serial, to_save)
            _alert_persisted[serial] = to_save
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
            title    = f"Petlibro Local: {name} - {msg}"
            notif_id = f"petlibro_local_{serial[:8]}_{alert}"
            await _notifications.fire_notification(title, msg, settings, device_cfg,
                                                   notification_id=notif_id)
            _LOGGER.info("Alert fired: %s for %s...", alert, serial[:6])
        if "offline" in cleared:
            # Auto-dismiss the offline notification (HA bell + mobile clear) before firing back-online
            offline_id = f"petlibro_local_{serial[:8]}_offline"
            await _notifications.dismiss_notification(offline_id, settings, device_cfg)
            msg   = "Device is back online."
            title = f"Petlibro Local: {name} - {msg}"
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
            title = f"Petlibro Local: {name} - {msg}"
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
        plans = _storage.get_active_feeding_plans(serial)
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


async def publish_ha_state(serial: str):
    """Public wrapper so other modules (e.g. main.py's feeding-plans endpoint)
    can force an immediate state refresh -- e.g. next_meal needs to recompute
    right away when plans are saved/emptied, not wait for some unrelated event."""
    await _publish_ha_state(serial)


async def _publish_ha_state(serial: str):
    """Push current device state values to HA MQTT state topics."""
    if _client_ref is None:
        return
    try:
        import storage as _storage
        cfg   = _storage.get_devices().get(serial, {})
        state = {**_state.get(serial, {}), **_polar_view(serial)}
        plans = _storage.get_active_feeding_plans(serial)
        await ha_mqtt.publish_state(_client_ref, serial, cfg, state, plans=plans,
                                    clock_offset=_clock_offset.get(serial))
    except Exception:
        _LOGGER.exception("Failed to publish HA state for %s...", serial[:6])


# ── HA command handling ────────────────────────────────────────────────────

async def _set_display_switch(serial: str, on: bool) -> bool:
    """Turns the One RFID feeder's LED screen on or off with the same message the
    Petlibro app sends. Used by both the web UI and the Home Assistant switch."""
    import storage as _storage
    cfg = _storage.get_devices().get(serial, {})
    # Mirror the vendor app's own on/off message: it also carries audioUrl
    # and soundTimes (OBSERVED), and the mode should be what the feeder
    # itself last reported (the app may have changed it), not our guess.
    aging = _state.get(serial, {}).get("screenDisplayAgingType")
    if aging not in (1, 2):
        aging = 2 if cfg.get("display_hours_mode") == "custom" else 1
    payload = {"screenDisplaySwitch": on, "screenDisplayAgingType": aging, "screenDisplayInterval": 2}
    s_start, s_end = cfg.get("sound_start_local"), cfg.get("sound_end_local")
    if s_start and s_end:
        payload["soundTimes"] = _raw_minutes_between(s_start, s_end)
    elif isinstance(_state.get(serial, {}).get("soundTimes"), int):
        payload["soundTimes"] = _state[serial]["soundTimes"]
    if _audio_urls.get(serial):
        payload["audioUrl"] = _audio_urls[serial]
    _LOGGER.info("Display switch %s... sending %s", serial[:6],
                 {k: v for k, v in payload.items() if k != "audioUrl"})
    ok = await send_command(serial, payload)
    if ok:
        _state.setdefault(serial, {}).update({"screenDisplaySwitch": on, "enableScreenDisplay": on})
        asyncio.ensure_future(_publish_ha_state(serial))
    return ok


async def _play_animation_by_name(serial: str, name: str) -> bool:
    """Plays a built-in or saved animation by its name (the HA select's value).
    "None" (or an unknown name) stops it by sending the saved text again."""
    import animations
    import storage as _storage
    if name and name != "None":
        entry = None
        for a in _storage.get_custom_animations():
            if a["name"] == name:
                entry = {"frames": a["frames"], "interval": a.get("interval", animations.DEFAULT_INTERVAL)}
                break
        if entry is None:
            for p in animations.list_presets():
                if p["name"] == name:
                    entry = p
                    break
        if entry is not None:
            ok = await send_display_frames(serial, entry["frames"], entry["interval"], True)
            if ok:
                _storage.save_device(serial, {"display_animation_name": name, "display_icon_name": None, "display_icon": 0})
            asyncio.ensure_future(_publish_ha_state(serial))
            return ok
    cfg = _storage.get_devices().get(serial, {})
    _storage.save_device(serial, {"display_animation_name": None})
    ok = await send_display(serial, cfg.get("display_text", ""), 0) if cfg.get("display_text") else True
    asyncio.ensure_future(_publish_ha_state(serial))
    return ok


# ── Polar Wet Food Feeder (EARLY BETA, see polar_control.py) ───────────────

_POLAR_COMMAND_KEYS = ("_polar_serve", "_polar_stop", "_polar_door", "_polar_plate_position", "_polar_set_duration")
_POLAR_SERVICE_ACKS = frozenset({
    "WET_GRAIN_FEEDING_PLAN_SERVICE", "WET_FOOD_FEED_STOP_SERVICE",
    "SET_PLATE_POS_SERVICE", "SWITCH_DOOR_SERVICE",
})


def _polar_view(serial: str) -> dict:
    """Feed state for the web UI and Home Assistant; empty for any other device."""
    if _devices.get(serial, (None, None))[1] != "polar":
        return {}
    import time as _t
    return _polar.public_view(_polar_rt.setdefault(serial, _polar.new_runtime()), _t.time())


def _polar_local_now():
    import datetime as _dt
    import storage as _storage
    now = _dt.datetime.now(_dt.timezone.utc)
    tz_name = _storage.get_settings().get("feeder_timezone", "") or os.environ.get("TZ", "")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo as _ZI
            return now.astimezone(_ZI(tz_name))
        except Exception:
            pass
    return now.astimezone()


async def _polar_send(serial: str, payload: dict) -> bool:
    """Publishes one Polar command. Never retained and never above QoS 0, so a
    command sent while the feeder is away is dropped, not replayed on reconnect."""
    if _client_ref is None:
        return False
    try:
        await _client_ref.publish(_service_sub_topic("polar", serial), json.dumps(payload), qos=0, retain=False)
        _LOGGER.info("Polar %s sent to %s...", payload.get("cmd"), serial[:6])
        return True
    except Exception:
        _LOGGER.exception("Polar %s failed for %s...", payload.get("cmd"), serial[:6])
        return False


async def _polar_echo(topic_str: str, data: dict) -> None:
    """Answers a Polar event post the way the real cloud does (see polar_control)."""
    if _client_ref is None:
        return
    import time as _t
    echo = _polar.echo_payload(topic_str, data, int(_t.time() * 1000))
    if echo is None:
        return
    try:
        await _client_ref.publish(topic_str[:-len("/post")] + "/sub", json.dumps(echo, separators=(",", ":")))
    except Exception:
        _LOGGER.debug("Polar echo failed for %s", topic_str)


async def _run_polar_command(serial: str, cmd: dict) -> dict:
    """The one place Polar commands run, for the web UI and Home Assistant alike.
    Raises PolarError (safe to show the user) when a command is refused."""
    import time as _t
    import storage as _storage
    if not any(k in cmd for k in _POLAR_COMMAND_KEYS):
        raise _polar.PolarError("That command is not supported for the Polar feeder")
    now = _t.time()
    rt = _polar_rt.setdefault(serial, _polar.new_runtime())
    cfg = _storage.get_devices().get(serial, {})

    if "_polar_set_duration" in cmd:
        value = cmd["_polar_set_duration"]
        _polar.validate_settings({"polar_duration_unit": cfg.get("polar_duration_unit"), "polar_serve_duration": value})
        _storage.save_device(serial, {"polar_serve_duration": value})
        asyncio.ensure_future(_publish_ha_state(serial))
        return {"saved": True}

    if not _online.get(serial):
        raise _polar.PolarError("The Polar feeder is offline, nothing was sent")
    now_ms = int(now * 1000)
    result: dict = {}

    if "_polar_serve" in cmd:
        arg = cmd["_polar_serve"]
        if not isinstance(arg, dict):
            raise _polar.PolarError("Serve needs a plate")
        plate = _polar.validate_plate(arg.get("plate"))
        value, unit, is_test = _polar.serve_duration(cfg, arg.get("duration"))
        _polar.check_feed_allowed(rt, now)
        ok = await _polar_send(serial, _polar.feed_payload(plate, value, _polar_local_now(), now_ms))
        if ok:
            _polar.note_feed_requested(rt, plate, value, unit, now)
            _LOGGER.info("Polar feed requested on %s...: plate %s, duration %s %s%s", serial[:6], plate, value,
                         unit or "(unit not set)", ", test feed" if is_test else "")
        result = {"duration": value, "unit": unit, "test": is_test}
    elif "_polar_stop" in cmd:
        ok = await _polar_send(serial, _polar.stop_payload(now_ms))
        if ok:
            _polar.note_stop_requested(rt, now)
    elif "_polar_door" in cmd:
        ok = await _polar_send(serial, _polar.door_payload(bool(cmd["_polar_door"]), now_ms))
    else:  # _polar_plate_position
        position = cmd["_polar_plate_position"]
        payload = _polar.plate_position_payload(position, now_ms)
        _polar.check_plate_move_allowed(rt, now)
        ok = await _polar_send(serial, payload)
        if ok:
            _polar.note_plate_move(rt, now)
    if not ok:
        raise _polar.PolarError("The command could not be sent (no connection to the broker)")
    asyncio.ensure_future(_publish_ha_state(serial))
    return result


def _handle_polar_message(serial: str, topic_str: str, data: dict, cmd) -> bool:
    """Polar-only message handling. Returns True when the message is fully dealt
    with, False to let the shared handler store its fields as usual."""
    import time as _t
    rt = _polar_rt.setdefault(serial, _polar.new_runtime())
    now = _t.time()

    if cmd in ("DEVICE_START_EVENT", "ATTR_PUSH_EVENT"):
        asyncio.ensure_future(_polar_echo(topic_str, data))
        return False

    if cmd == "HEARTBEAT":
        temp = data.get("temperature")
        if isinstance(temp, (int, float)) and not isinstance(temp, bool):
            _state.setdefault(serial, {})["temperature"] = temp
        return False

    if cmd == "GET_FEEDING_PLAN_EVENT":
        # Echo only, as the real cloud does. This app does not manage the Polar's plans.
        asyncio.ensure_future(_polar_echo(topic_str, data))
        _mark_online(serial)
        return True

    if cmd == "DEVICE_LOG_REPORT_EVENT":
        _mark_online(serial)
        return True

    if cmd == "WET_GRAIN_OUTPUT_EVENT":
        asyncio.ensure_future(_polar_echo(topic_str, data))
        step = data.get("execStep")
        _LOGGER.info("Polar feed step from %s...: %s plate=%s finished=%s planId=%s", serial[:6], step,
                     data.get("plate"), data.get("finished"), data.get("planId"))
        summary = _polar.on_output_event(rt, data, now)
        try:
            import storage as _storage
            if step == "OPEN_DOOR":
                _storage.save_device(serial, {"last_fed_ts": int(now)})
            if summary:
                _storage.log_feeder_event(serial, "polar_feed", extra={
                    "source": summary["source"], "plate": summary["plate"],
                    "requested": summary["requested"], "unit": summary["unit"],
                    "stopped": summary["stopped"], "door_open_secs": summary["door_open_secs"],
                })
        except Exception:
            _LOGGER.exception("Polar feed log error")
        if summary:
            _LOGGER.info("Polar feed finished on %s...: source=%s plate=%s requested=%s %s, door open about %s s%s",
                         serial[:6], summary["source"], summary["plate"], summary["requested"],
                         summary["unit"] or "(unit not set)", summary["door_open_secs"],
                         " (stopped early)" if summary["stopped"] else "")
            if summary["suggested_unit"]:
                _LOGGER.info("Polar test feed on %s...: a value of 1 kept the door open about %s s, which suggests the "
                             "unit is %s. Set it in the device's Overview tab (not applied automatically).",
                             serial[:6], summary["door_open_secs"], summary["suggested_unit"])
        _mark_online(serial)
        asyncio.ensure_future(_publish_ha_state(serial))
        return True

    if cmd in _POLAR_SERVICE_ACKS:
        code = data.get("code")
        (_LOGGER.info if code in (0, None) else _LOGGER.warning)(
            "Polar %s reply from %s...: code=%s msg=%s", cmd, serial[:6], code, data.get("msg"))
        _polar.note_service_ack(rt, cmd, code, now)
        if cmd == "SET_PLATE_POS_SERVICE" and code in (0, None) and isinstance(data.get("platePosition"), int):
            _state.setdefault(serial, {})["platePosition"] = data["platePosition"]
        _mark_online(serial)
        asyncio.ensure_future(_publish_ha_state(serial))
        return True

    return False


async def _handle_ha_command(serial: str, topic: str, payload: str):
    """Route an HA MQTT command to the device."""
    _, device_type = _devices.get(serial, (None, ""))
    cmd = ha_mqtt.parse_command(topic, payload.strip(), device_type=device_type)
    if cmd is None:
        _LOGGER.warning("Unrecognised HA command topic: %s", topic)
        return
    if device_type == "polar":
        try:
            await _run_polar_command(serial, cmd)
        except _polar.PolarError as exc:
            _LOGGER.warning("HA Polar command refused for %s...: %s", serial[:6], exc)
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
            _storage.save_device(serial, {"display_animation_name": None, "display_text": text, "display_icon": 0, "display_icon_name": None})
            ok = await send_display(serial, text, 0)
            asyncio.ensure_future(_publish_ha_state(serial))
            _LOGGER.info("HA Display Text %s...: %s", serial[:6], "ok" if ok else "failed")
    elif "_display_icon" in cmd:
        import storage as _storage
        icon_name = cmd["_display_icon"]
        icon_id = ha_mqtt._ICON_NAMES.get(icon_name, None)
        if icon_id is not None and icon_id > 0:
            _storage.save_device(serial, {"display_animation_name": None, "display_icon": icon_id, "display_text": "", "display_icon_name": icon_name})
            ok = await send_display(serial, "", icon_id)
        elif icon_name in ha_mqtt._CUSTOM_FRAMES:
            # Built-in custom frame (e.g. Petlibro Salute)
            _storage.save_device(serial, {"display_animation_name": None, "display_icon": icon_id or -1, "display_icon_name": icon_name})
            ok = await send_display_frame(serial, ha_mqtt._CUSTOM_FRAMES[icon_name])
        else:
            # Check user-saved custom icons by name
            saved = {ic["name"]: ic for ic in _storage.get_custom_icons()}
            if icon_name in saved:
                frame = [0] + [r << 7 for r in saved[icon_name]["rows"]] + [0]
                _storage.save_device(serial, {"display_animation_name": None, "display_icon_name": icon_name, "display_icon": 0})
                ok = await send_display_frame(serial, frame)
            else:
                # "None" — revert to stored text
                cfg = _storage.get_devices().get(serial, {})
                _storage.save_device(serial, {"display_animation_name": None, "display_icon": 0, "display_icon_name": None})
                ok = await send_display(serial, cfg.get("display_text", ""), 0)
        asyncio.ensure_future(_publish_ha_state(serial))
        _LOGGER.info("HA Display Icon %s... %s: %s", serial[:6], icon_name, "ok" if ok else "failed")
    elif "_display_switch" in cmd:
        ok = await _set_display_switch(serial, bool(cmd["_display_switch"]))
        _LOGGER.info("HA Display Switch %s... -> %s: %s", serial[:6], bool(cmd["_display_switch"]), "ok" if ok else "failed")
    elif "_display_animation_name" in cmd:
        ok = await _play_animation_by_name(serial, cmd["_display_animation_name"])
        _LOGGER.info("HA Display Animation %s... %s: %s", serial[:6], cmd["_display_animation_name"], "ok" if ok else "failed")
    elif "_display_frame" in cmd:
        frame = cmd["_display_frame"]
        if isinstance(frame, list) and len(frame) in (7, 8):
            ok = await send_display_frame(serial, frame)
            _LOGGER.info("Custom frame sent to %s...: %s", serial[:6], "ok" if ok else "failed")
    elif "_granary_field" in cmd:
        bundle = _granary_settings_bundle(serial, {cmd["_granary_field"]: cmd["_granary_value"]})
        ok = await send_command(serial, bundle)
        _LOGGER.info("HA Granary %s %s...: %s", cmd["_granary_field"], serial[:6], "ok" if ok else "failed")
    else:
        await send_command(serial, cmd)
        _LOGGER.debug("HA command %s... keys=%s", serial[:6], list(cmd.keys()))


async def handle_ha_command(serial: str, cmd: dict):
    """Public entry-point for API-driven commands (already-parsed dict). Returns a
    dict of extra details for the Polar feeder, otherwise nothing."""
    if _devices.get(serial, (None, ""))[1] == "polar":
        return await _run_polar_command(serial, cmd)
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
            _storage.save_device(serial, {"display_animation_name": None, "display_text": text, "display_icon": 0, "display_icon_name": None})
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
                _storage.save_device(serial, {"display_animation_name": None, "display_icon_name": name, "display_icon": 0})
                asyncio.ensure_future(_publish_ha_state(serial))
            _LOGGER.info("API Custom frame %s...: %s", serial[:6], "ok" if ok else "failed")
    elif "_light_schedule" in cmd:
        sched = cmd["_light_schedule"] or {}
        start = sched.get("start")
        end = sched.get("end")
        if start and end:
            import storage as _storage
            _storage.save_device(serial, {"light_start_time": start, "light_end_time": end})
            # lightingTimes is required for the schedule to actually take
            # effect, confirmed via a real proxy capture of the vendor app
            # doing this (2026-09-03): the duration in minutes between start
            # and end, sent alongside the times in every working schedule
            # capture. lightAgingType (1=static, 2=scheduled) is ALSO
            # required, but must preserve whatever the device is already
            # set to, not unconditionally force 2 here -- an earlier version
            # did that, which meant applying new times while the schedule
            # was toggled off silently re-enabled it every time, since this
            # handler always won regardless of the separate enable/disable
            # toggle. Defaults to 2 only if we've never seen this device
            # report a lightAgingType at all (first-ever schedule setup).
            current_aging_type = _state.get(serial, {}).get("lightAgingType", 2)
            ok = await send_command(serial, {
                "lightingStartTime": start,
                "lightingStartTimeUtc": _local_hhmm_to_utc(start),
                "lightingEndTime": end,
                "lightingEndTimeUtc": _local_hhmm_to_utc(end),
                "lightAgingType": current_aging_type,
                "lightingTimes": _minutes_between(start, end),
            })
            asyncio.ensure_future(_publish_ha_state(serial))
            _LOGGER.info("API Light Schedule %s...: %s", serial[:6], "ok" if ok else "failed")
    elif "_light_schedule_enabled" in cmd:
        # lightAgingType is the real schedule on/off flag (1 = static/manual,
        # 2 = scheduled). A first attempt at the disable direction sent just
        # {"lightAgingType": 1} alone and a real live test proved it did
        # nothing, the schedule kept firing regardless. A second real proxy
        # capture (2026-09-03) of the vendor app's own disable action showed
        # why: it never sends lightAgingType alone, always bundled with the
        # stored lightingStartTime/EndTime (both local and UTC) in the same
        # command, so this resends those too, matching that pattern, instead
        # of the bare single-field attempt that didn't work.
        enabled = bool(cmd["_light_schedule_enabled"])
        import storage as _storage
        cfg = _storage.get_devices().get(serial, {})
        start = cfg.get("light_start_time")
        end = cfg.get("light_end_time")
        payload = {"lightAgingType": 2 if enabled else 1}
        if start and end:
            payload["lightingStartTime"] = start
            payload["lightingStartTimeUtc"] = _local_hhmm_to_utc(start)
            payload["lightingEndTime"] = end
            payload["lightingEndTimeUtc"] = _local_hhmm_to_utc(end)
        ok = await send_command(serial, payload)
        asyncio.ensure_future(_publish_ha_state(serial))
        _LOGGER.info("API Light Schedule Enabled %s...: %s -> %s", serial[:6], enabled, "ok" if ok else "failed")
    elif "_send_pet_name" in cmd:
        # The feeder shows a separate stored PET_NAME scene while it reads a pet's
        # tag. The Petlibro cloud used to push it (OBSERVED: one DISPLAY_MATRIX_SERVICE
        # with displayScene PET_NAME, one frame spelling the name). This feeder has
        # one pet, so this sends the name of the pet assigned to it.
        import storage as _storage
        cfg = _storage.get_devices().get(serial, {})
        pets = _storage.get_pets()
        pid = next((p for p in cfg.get("pet_ids", []) if p in pets), None)
        name = (pets.get(pid, {}).get("name") or "").strip()[:20] if pid else ""
        if not name:
            raise RuntimeError("no pet with a name is assigned to this feeder")
        ok = await send_display(serial, name, 0, scene="PET_NAME")
        _LOGGER.info("API Pet Name %s... (%s): %s", serial[:6], name, "ok" if ok else "failed")
        if not ok:
            raise RuntimeError("pet name was not sent")
    elif "_display_animation" in cmd:
        a = cmd["_display_animation"] or {}
        ok = await send_display_frames(serial, a.get("frames"), a.get("interval", 4), bool(a.get("loop", True)))
        if ok:
            import storage as _storage
            import ha_mqtt as _ham
            known = a.get("name") if a.get("name") in _ham.animation_option_names() else None
            _storage.save_device(serial, {"display_icon_name": None, "display_icon": 0, "display_animation_name": known})
            asyncio.ensure_future(_publish_ha_state(serial))
        _LOGGER.info("API Animation %s...: %s", serial[:6], "ok" if ok else "failed")
        if not ok:
            raise RuntimeError("animation was not sent")
    elif "_sound_hours" in cmd:
        ok = await _send_feeder_hours(serial, "sound", cmd["_sound_hours"] or {})
        _LOGGER.info("API Sound Hours %s...: %s", serial[:6], "ok" if ok else "failed")
        if not ok:
            raise RuntimeError("sound hours were not sent")
    elif "_display_hours" in cmd:
        ok = await _send_feeder_hours(serial, "screenDisplay", cmd["_display_hours"] or {})
        _LOGGER.info("API Display Hours %s...: %s", serial[:6], "ok" if ok else "failed")
        if not ok:
            raise RuntimeError("display hours were not sent")
    elif "_display_switch" in cmd:
        on = bool(cmd["_display_switch"])
        ok = await _set_display_switch(serial, on)
        _LOGGER.info("API Display Switch %s... -> %s: %s", serial[:6], on, "ok" if ok else "failed")
        if not ok:
            raise RuntimeError("display switch was not sent")
    elif "_granary_field" in cmd:
        bundle = _granary_settings_bundle(serial, {cmd["_granary_field"]: cmd["_granary_value"]})
        ok = await send_command(serial, bundle)
        _LOGGER.info("API Granary %s %s...: %s", cmd["_granary_field"], serial[:6], "ok" if ok else "failed")
    else:
        ok = await send_command(serial, cmd)
        _LOGGER.debug("API command %s... keys=%s", serial[:6], list(cmd.keys()))
        # Some firmware never reports the filter due date back (issue #12), so what the
        # user sets is kept here, otherwise Filter Due would stay blank after a reload.
        due = cmd.get("filterNextReplacementTimestamp")
        if ok and isinstance(due, (int, float)) and not isinstance(due, bool):
            _state.setdefault(serial, {})["filterNextReplacementTimestamp"] = due
            asyncio.ensure_future(_persist_state_cache(serial))
            asyncio.ensure_future(_publish_ha_state(serial))


# ── Message handling ───────────────────────────────────────────────────────

def _handle_message(serial: str, topic_str: str, raw: str):
    try:
        data = json.loads(raw)
    except Exception:
        _LOGGER.debug("Non-JSON from %s...", serial[:6])
        return
    if not isinstance(data, dict):
        # valid JSON that is not an object (null, 0, [] ...)
        _LOGGER.debug("Ignoring a non-object JSON payload from %s...", serial[:6])
        return

    cmd = data.get("cmd")

    if _devices.get(serial, (None, None))[1] == "polar" and _handle_polar_message(serial, topic_str, data, cmd):
        return

    if cmd == "NTP":
        _LOGGER.info("NTP request from %s...", serial[:6])
        _ntp_logger.info("REQUEST from %s... (%s)", serial[:6], _device_name(serial))
        asyncio.ensure_future(_respond_ntp(topic_str, data.get("ts")))
        _mark_online(serial)
        return

    if cmd == "NTP_SYNC":
        # Device's ack of our NTP_SYNC (matched by msgId), carries its new clock.
        _handle_ntp_sync_ack(serial, data)
        _mark_online(serial)
        return

    if cmd == "GET_FEEDING_PLAN_EVENT":
        _ntp_logger.info("FEEDING_PLAN REQUEST from %s... (%s)", serial[:6], _device_name(serial))
        asyncio.ensure_future(_respond_feeding_plan(serial, topic_str))
        return

    if cmd == "FEEDING_PLAN_SERVICE":
        # Feeder's ack for a plan push -- echoes back what it now has stored,
        # so this is the real evidence of whether the plan was accepted, and
        # whether the times/days it stored actually match what we sent.
        code = data.get("code")
        plans = data.get("plans", [])
        _LOGGER.info("FEEDING_PLAN_SERVICE ack from %s...: code=%s plans=%s", serial[:6], code, json.dumps(plans))
        if code == 0:
            _plan_ack_received(serial, plans)
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
                            was_alerted = bool(pet_id and (_pet_no_eat_alerted.pop(pet_id, False)
                                                           or pets.get(pet_id, {}).get("no_eat_alerted_for")))
                            if was_alerted:
                                _storage.save_pet(pet_id, {"no_eat_alerted_for": None})
                            if was_alerted:
                                try:
                                    import notifications as _notif2
                                    import storage as _st2
                                    notif_id = f"petlibro_local_{pet_id[:8]}_no_eat"
                                    # Clean up only where the alert itself was sent: the pet's own
                                    # channel settings, the same ones the no-eat alert uses. (This
                                    # used to force bell and mobile on, so a pet with mobile off
                                    # still got a mobile "clear" every time it ate.)
                                    _pet_ch = pets.get(pet_id, {})
                                    asyncio.ensure_future(
                                        _notif2.dismiss_notification(notif_id, _st2.get_settings(), {
                                            "notify_bell":   _pet_ch.get("notify_bell", True),
                                            "notify_mobile": _pet_ch.get("notify_mobile", False),
                                        })
                                    )
                                except Exception:
                                    pass
                            # Pet eating notification -- independent on/off switch
                            # (notify_eating_activity) so this frequent, low-stakes
                            # ping can be turned off without also silencing the
                            # separate, rarer no-eat alert, which shares the same
                            # notify_bell/email/mobile channel fields below.
                            try:
                                pet_cfg = pets.get(pet_id, {})
                                if pet_cfg.get("notify_eating_activity", True):
                                    import notifications as _notif
                                    pet_name = pets.get(pet_id, {}).get("name") or "Your pet"
                                    dev_name = cfg.get("name") or serial[:8]
                                    mins = duration_secs // 60
                                    secs_r = duration_secs % 60
                                    dur_str = f"{mins}m{secs_r:02d}s" if mins else f"{secs_r}s"
                                    msg = f"{pet_name} ate for {dur_str} at {dev_name}"
                                    title = f"Petlibro Local: {msg}"
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

    if cmd == "WATER_CAP_EVENT":
        _handle_water_cap(serial, data)
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

    if cmd == "ERROR_EVENT":
        # New cmd, first seen 2026-09-04 (issue #7, a jammed food door). Real
        # capture confirmed errorCode 2032 fires when a pet blocks the door
        # from closing, timed exactly to a reported jam window, door stayed
        # open for several minutes after. Only this one code has ever been
        # observed -- treating it as the door-jam signal for now, but other
        # error codes may exist and could mean something unrelated.
        error_code = data.get("errorCode")
        # 2048 arrives about every 30 minutes from a camera feeder (PLAF203) and carries
        # camera exposure data (the reporter's reading, not confirmed), so it is not logged.
        if error_code != 2048:
            _LOGGER.info("ERROR_EVENT from %s...: errorCode=%s", serial[:6], error_code)
        if error_code == 2032:
            _state.setdefault(serial, {})["_door_jam_pending"] = True
        _mark_online(serial)
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        return

    if cmd == "WAREHOUSE_DOOR_EVENT":
        # Feeder door state change (open/close). Track duration to detect feeding sessions.
        import time as _time
        barn = data.get("barnDoorState")
        if barn is not None:
            prev_barn = _state.get(serial, {}).get("barnDoorState")
            _state.setdefault(serial, {})["barnDoorState"] = barn
            if not barn:
                # Door confirmed closed -- clear any pending jam alert so a
                # future jam can fire again instead of staying stuck "on".
                _state[serial].pop("_door_jam_pending", None)
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
        # Boots go in the NTP log with their restartReason (e.g. "other:0x100000"),
        # so the cause of a reboot can be judged from real data later. What the
        # hex values mean (0x0 power on, 0x80000 firmware restart, 0x100000
        # watchdog) is a Reddit user's reading, not confirmed here.
        _ntp_logger.info("BOOT %s... (%s): restartReason=%s fw=%s",
                         serial[:6], _device_name(serial), data.get("restartReason"), data.get("softwareVersion"))
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
        asyncio.ensure_future(_check_device_clock(serial, data.get("ts")))
        asyncio.ensure_future(_check_and_fire_alerts(serial))
        asyncio.ensure_future(_publish_ha_state(serial))
        return

    if cmd in ("ATTR_GET_SERVICE", "ATTR_SET_SERVICE", "ATTR_PUSH_EVENT"):
        if isinstance(data.get("audioUrl"), str) and data["audioUrl"]:
            _audio_urls[serial] = data["audioUrl"]
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
        shown = {k: v for k, v in partial.items() if k.startswith("screenDisplay") or k == "enableScreenDisplay"}
        if shown and any(_state.get(serial, {}).get(k) != v for k, v in shown.items()):
            # OBSERVED: enableScreenDisplay is whether the LED is lit right now,
            # screenDisplaySwitch is the setting. Logged only when a value changes (the
            # feeder repeats them in every state poll), so display changes can still be
            # checked against what the feeder reports back.
            _LOGGER.info("%s from %s... display fields: %s", cmd, serial[:6], shown)
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
    try:
        prepare_plans_for_feeder(serial)
    except Exception:
        _LOGGER.exception("Plan timezone reconcile before plan response failed for %s...", serial[:6])
    stored_plans = _storage.get_active_feeding_plans(serial)
    clean_plans = _wire_plans(stored_plans)
    payload = json.dumps({
        "cmd":   "GET_FEEDING_PLAN_EVENT",
        "code":  0,
        "ts":    int(_time.time() * 1000),
        "plans": clean_plans,
    })
    try:
        await _client_ref.publish(response_topic, payload)
        _LOGGER.info("Feeding plan response sent to %s... (%d plans)", serial[:6], len(clean_plans))
        _ntp_logger.info("FEEDING_PLAN RESPONSE to %s... (%s): %d plans", serial[:6], _device_name(serial), len(clean_plans))
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
            # A recent send_command(MANUAL_FEEDING_SERVICE) for this device means
            # we asked for this dispense (Feed Now or the calibration wizard);
            # otherwise the feeder fired it on its own from its schedule. Consume
            # the marker either way, a stale one is no longer useful either.
            pending_at = _pending_manual_feed.pop(serial, None)
            source = "manual" if pending_at and (_time.time() - pending_at) <= _MANUAL_FEED_WINDOW_SECS else "scheduled"
            _storage.record_intake(serial, portions)
            _storage.log_feeder_event(serial, "food_dispensed", portions=portions, extra={"source": source})
            _storage.save_device(serial, {"last_fed_ts": int(_time.time())})
            asyncio.ensure_future(_publish_ha_state(serial))
            device_cfg = _storage.get_devices().get(serial, {})
            if device_cfg.get("notifications", {}).get("food_dispensed"):
                import notifications as _notifications
                name  = device_cfg.get("name") or serial[:8]
                source_label = "Manual" if source == "manual" else "Scheduled"
                msg   = f"Food dispensed - {source_label} - {portions} portion{'s' if portions != 1 else ''} at {name}"
                title = f"Petlibro Local: {name} - Food dispensed"
                notif_id = f"petlibro_local_{serial[:8]}_food_dispensed_{int(_time.time())}"
                asyncio.ensure_future(
                    _notifications.fire_notification(title, msg, _storage.get_settings(), device_cfg,
                                                     notification_id=notif_id)
                )
        except Exception:
            pass


def _device_name(serial: str) -> str:
    """User-configured device name, for log lines where a truncated serial
    alone isn't enough to tell devices apart -- multiple devices of the same
    type commonly share the same first 6 characters."""
    try:
        import storage as _storage
        return _storage.get_devices().get(serial, {}).get("name") or "unnamed"
    except Exception:
        return "unknown"


def _minutes_between(start_hhmm: str, end_hhmm: str) -> int:
    """Minutes from start to end, wrapping past midnight if end is earlier
    in the day than start (e.g. a light schedule that runs overnight)."""
    sh, sm = (int(x) for x in start_hhmm.split(":"))
    eh, em = (int(x) for x in end_hhmm.split(":"))
    return ((eh * 60 + em) - (sh * 60 + sm)) % 1440


def _raw_minutes_between(start_hhmm: str, end_hhmm: str) -> int:
    """End minus start in minutes WITHOUT wrapping past midnight. The vendor
    app's soundTimes is exactly this (OBSERVED: 11:51 to 00:51 sent -660,
    08:51 to 00:51 sent -480, 08:51 to 01:51 sent -420)."""
    sh, sm = (int(x) for x in start_hhmm.split(":"))
    eh, em = (int(x) for x in end_hhmm.split(":"))
    return (eh * 60 + em) - (sh * 60 + sm)


async def _send_feeder_hours(serial: str, prefix: str, req: dict) -> bool:
    """Device Sound hours (prefix "sound") and LED Display hours (prefix
    "screenDisplay") on the One RFID Feeder, in the exact shapes captured from
    the vendor app (2026-10-09). req = {"mode": "all"|"custom", "start", "end"}
    with local HH:MM times. AgingType 1 = all day, 2 = custom time; the times
    are kept in every message, as the app does, even for All day."""
    import storage as _storage
    mode_key, start_key, end_key = (("sound_hours_mode", "sound_start_local", "sound_end_local")
                                    if prefix == "sound" else
                                    ("display_hours_mode", "display_start_local", "display_end_local"))
    cfg = _storage.get_devices().get(serial, {})
    mode = "custom" if req.get("mode") == "custom" else "all"
    start = req.get("start") or cfg.get(start_key)
    end = req.get("end") or cfg.get(end_key)
    if mode == "custom" and not (start and end):
        return False
    _storage.save_device(serial, {mode_key: mode, **({start_key: start, end_key: end} if start and end else {})})
    fields = {f"{prefix}AgingType": 2 if mode == "custom" else 1}
    if start and end:
        browser_tz = req.get("tz") or ""
        fields.update({
            f"{prefix}StartTime": start, f"{prefix}StartTimeUtc": _local_hhmm_to_utc(start, browser_tz),
            f"{prefix}EndTime": end,     f"{prefix}EndTimeUtc": _local_hhmm_to_utc(end, browser_tz),
        })
        if prefix == "sound":
            fields["soundTimes"] = _raw_minutes_between(start, end)
    if prefix == "sound":
        fields["soundSwitch"] = True
        if _audio_urls.get(serial):
            fields["audioUrl"] = _audio_urls[serial]
    else:
        fields["screenDisplayInterval"] = 2
        if _audio_urls.get(serial):
            fields["audioUrl"] = _audio_urls[serial]
        s_start, s_end = cfg.get("sound_start_local"), cfg.get("sound_end_local")
        if s_start and s_end:
            fields["soundTimes"] = _raw_minutes_between(s_start, s_end)
        # The app changes a custom window with two messages a second apart,
        # switch off then on, identical schedule fields (the same pattern the
        # fountain light schedule needed). Replicated for custom windows.
        # The OEM app's All day changes were also off then on (OBSERVED 11:53:56
        # and 11:54:00), so the pair is sent for both modes.
        if True:
            if not await send_command(serial, {**fields, "screenDisplaySwitch": False}):
                return False
            await asyncio.sleep(1)
        fields["screenDisplaySwitch"] = True
    _LOGGER.info("%s hours %s... sending %s", prefix, serial[:6], {k: v for k, v in fields.items() if k != "audioUrl"})
    ok = await send_command(serial, fields)
    if ok:
        _state.setdefault(serial, {}).update({k: v for k, v in fields.items() if k != "audioUrl"})
        if prefix == "screenDisplay":
            _state[serial]["enableScreenDisplay"] = True
        asyncio.ensure_future(_publish_ha_state(serial))
    return ok


_TZ_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")


def _local_hhmm_to_utc(hhmm: str, fallback_tz: str = "") -> str:
    """Converts an "HH:MM" local-time string to "HH:MM" UTC, using the same
    feeder_timezone setting the feeding-schedule editor uses. Mirrors
    util.js's _localToUtc so the fountain light schedule is entered and
    displayed in local time but sent to the device in UTC, same as the
    feeder's feeding plan times. The editor falls back to the browser's
    timezone when the setting was never saved, so callers can pass that as
    fallback_tz; without it an unsaved setting meant no conversion at all,
    and a sound or LED window was sent with local times labeled as UTC."""
    import datetime as _dt
    import storage as _storage
    tz_name = _storage.get_settings().get("feeder_timezone", "")
    if not tz_name and isinstance(fallback_tz, str) and len(fallback_tz) <= 64 and _TZ_NAME_RE.match(fallback_tz):
        tz_name = fallback_tz
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


def _handle_water_cap(serial: str, data: dict) -> None:
    """Newer Dockstream RFID Smart Fountain firmware reports its scale this way
    (issue #12) and never answers ATTR_GET_SERVICE: the latest reading becomes the
    water level, and a finished tagged drink goes through the same handler as
    WEIGHT_CHANGE_EVENT."""
    tracker = _wcap.setdefault(serial, _water_cap.Tracker())
    drinks = tracker.feed(data.get("content"))
    st = _state.setdefault(serial, {})
    if tracker.latest:
        st["currentWeight"] = round(tracker.latest["weight"])
        if tracker.latest["percent"] is not None:
            st["weightPercent"] = tracker.latest["percent"]
    for drink in drinks:
        _LOGGER.info("Fountain drink on %s...: %s g over %s s", serial[:6], drink["fluctuatingWeight"], drink["fluctuatingSeconds"])
        asyncio.ensure_future(_handle_fountain_drink(serial, drink))
    _mark_online(serial)
    asyncio.ensure_future(_check_and_fire_alerts(serial))
    asyncio.ensure_future(_publish_ha_state(serial))
    asyncio.ensure_future(_persist_state_cache(serial))


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
    if _devices.get(serial, (None, None))[1] == "polar":
        return  # answered by _handle_polar_message, in the real cloud's shape
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


# Cache of computed DST transitions per IANA tz name: {tz_name: (computed_at_epoch_secs, transitions)}.
# Finding the next two transitions requires a day-by-day scan (see _dst_transitions),
# cheap individually but not worth redoing on every single NTP request/day -- a
# transition, once found, stays valid for a long time, so a 6h refresh is plenty.
_DST_CACHE: dict[str, tuple[float, list]] = {}
_DST_CACHE_TTL_SECS = 6 * 3600


def _tz_utcoffset_at(tz, ts_ms: int):
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ts_ms / 1000, tz=_dt.timezone.utc).astimezone(tz)
    return dt.utcoffset()


def _dst_transitions(tz_name: str) -> list[tuple[int, int]]:
    """Find the next two DST transitions for an IANA tz name.

    Returns up to 2 (new_offset_seconds, transition_ts_ms) tuples, soonest first.
    Empty list for a tz with no DST (or if the name can't be resolved) -- callers
    should treat that as "can't provide these fields", not "no transition ever".
    """
    import time as _time
    cached = _DST_CACHE.get(tz_name)
    now = _time.time()
    if cached and (now - cached[0]) < _DST_CACHE_TTL_SECS:
        return cached[1]

    try:
        from zoneinfo import ZoneInfo as _ZI
        tz = _ZI(tz_name)
    except Exception:
        return []

    transitions: list[tuple[int, int]] = []
    day_ms = 86_400_000
    cur_ms = int(now * 1000)
    cur_offset = _tz_utcoffset_at(tz, cur_ms)
    end_ms = cur_ms + 730 * day_ms  # search up to 2 years ahead
    while len(transitions) < 2 and cur_ms < end_ms:
        next_ms = cur_ms + day_ms
        next_offset = _tz_utcoffset_at(tz, next_ms)
        if next_offset != cur_offset:
            # Binary search within this one day for the exact transition instant.
            lo, hi = cur_ms, next_ms
            lo_offset = cur_offset
            for _ in range(30):  # far more than needed for ms precision over 1 day
                mid = (lo + hi) // 2
                if _tz_utcoffset_at(tz, mid) == lo_offset:
                    lo = mid
                else:
                    hi = mid
            transitions.append((int(next_offset.total_seconds()), hi))
            cur_offset = next_offset
        cur_ms = next_ms

    _DST_CACHE[tz_name] = (now, transitions)
    return transitions


def _build_ntp_payload(calibration: bool = True) -> tuple[str, int]:
    # NOTE: the timezone field here does NOT affect when scheduled plans fire.
    # executionTime in plans is always UTC (the PetLibro app converts local→UTC
    # before sending). This timezone is only used by the feeder for on-device
    # clock display. It is configurable in Settings → General → Feeder Timezone.
    #
    # timezoneOffsetSeconds/nextDST*/secondNextDST* were added 2026-09-12 to
    # match the real PetLibro cloud's own NTP response (confirmed via a real
    # capture, issue #8) -- our response previously only ever sent a bare hour
    # offset and never these fields at all. A lead for issue #5 (delayed
    # scheduled feeding): if a feeder's firmware requires this fuller payload
    # to consider a time sync actually complete, an incomplete response (what
    # we'd always sent before this) could explain both the unusually frequent
    # NTP requests seen in a real user's log and clock drift causing scheduled
    # feeds to fire late. Not proven, but this now matches the real cloud
    # exactly, removing that gap as a possible explanation either way.
    import time as _time
    import storage as _storage
    settings = _storage.get_settings()
    tz_name = settings.get("feeder_timezone", "") or os.environ.get("TZ", "")
    tz_offset_secs = None
    tz_hours = settings.get("feeder_tz_offset", -7)
    if tz_name:
        try:
            import datetime as _dt
            from zoneinfo import ZoneInfo as _ZI
            offset = _dt.datetime.now(_ZI(tz_name)).utcoffset()
            tz_offset_secs = int(offset.total_seconds())
            tz_hours = int(round(tz_offset_secs / 3600))
        except Exception:
            pass

    now_ms = int(_time.time() * 1000)
    payload_dict = {
        "cmd":            "NTP",
        "ts":             now_ms,
        "code":           0,
        "calibrationTag": calibration,
        "timezone":       tz_hours,
    }
    # Only include the fuller DST-aware fields when we have a real IANA zone to
    # compute them from -- a bare configured hour offset carries no DST rules,
    # so fabricating "no transition coming" would be a guess, not a fact.
    if tz_name and tz_offset_secs is not None:
        transitions = _dst_transitions(tz_name)
        if transitions:
            payload_dict["timezoneOffsetSeconds"] = tz_offset_secs
            payload_dict["nextDSTOffsetSeconds"] = transitions[0][0]
            payload_dict["nextDSTTransitionTs"] = transitions[0][1]
            if len(transitions) > 1:
                payload_dict["secondNextDSTOffsetSeconds"] = transitions[1][0]
                payload_dict["secondNextDSTTransitionTs"] = transitions[1][1]

    payload = json.dumps(payload_dict)
    return payload, now_ms


def _build_ntp_sync_payload() -> tuple[str, str, int]:
    """The unprompted NTP_SYNC the real cloud sends on connect: the NTP reply
    fields minus code/calibrationTag, plus msgId and linkId. Returns
    (payload, msgId, ts_ms)."""
    import uuid
    base, now_ms = _build_ntp_payload()
    d = json.loads(base)
    d.pop("code", None)
    d.pop("calibrationTag", None)
    d["cmd"] = "NTP_SYNC"
    msg_id = uuid.uuid4().hex
    d["msgId"] = msg_id
    d["linkId"] = uuid.uuid4().hex
    return json.dumps(d), msg_id, now_ms


def _uses_cloud_time_sync(serial: str) -> bool:
    return USE_CLOUD_STYLE_TIME_SYNC and _devices.get(serial, (None, None))[1] in CLOUD_TIME_SYNC_TYPES


async def _send_ntp_sync(serial: str, device_type: str) -> None:
    """Sends the cloud-style NTP_SYNC and waits for the device's ack. If no ack
    arrives within NTP_SYNC_ACK_TIMEOUT_SECS, falls back to the plain NTP push
    that is known to work."""
    global _client_ref
    if _client_ref is None:
        return
    payload, msg_id, now_ms = _build_ntp_sync_payload()
    _ntp_sync_pending[msg_id] = (serial, device_type)
    try:
        await _client_ref.publish(_ntp_sub_topic(device_type, serial), payload)
        _ntp_logger.info("NTP_SYNC sent to %s... (%s): %s", serial[:6], _device_name(serial), payload)
    except Exception:
        _ntp_sync_pending.pop(msg_id, None)
        _ntp_logger.exception("NTP_SYNC to %s... FAILED", serial[:6])
        await _push_ntp_sync(serial, device_type)
        return
    await asyncio.sleep(NTP_SYNC_ACK_TIMEOUT_SECS)
    if _ntp_sync_pending.pop(msg_id, None) is not None:
        _ntp_logger.info("NTP_SYNC to %s... (%s) got no ack in %ds, falling back to the plain NTP push",
                         serial[:6], _device_name(serial), NTP_SYNC_ACK_TIMEOUT_SECS)
        await _push_ntp_sync(serial, device_type)


async def _correct_clock(serial: str, device_type: str) -> None:
    if _uses_cloud_time_sync(serial):
        await _send_ntp_sync(serial, device_type)
    else:
        await _push_ntp_sync(serial, device_type)


def _handle_ntp_sync_ack(serial: str, data: dict) -> None:
    import time as _t
    pending = _ntp_sync_pending.pop(data.get("msgId"), None)
    ts = data.get("ts")
    if isinstance(ts, (int, float)) and ts > 1e12:
        offset = round(_t.time() - ts / 1000.0)
        _clock_offset[serial] = offset
        _ntp_logger.info("NTP_SYNC ack from %s... (%s) code=%s clock now %+ds%s", serial[:6], _device_name(serial),
                         data.get("code"), offset, "" if pending else " (no matching request)")
    else:
        _ntp_logger.info("NTP_SYNC ack from %s... (%s) code=%s ts=%s", serial[:6], _device_name(serial),
                         data.get("code"), ts)


async def _respond_ntp(request_topic: str, device_ts_ms=None) -> None:
    global _client_ref
    if _client_ref is None:
        return
    response_topic = request_topic.replace("/post", "/sub")
    parts = request_topic.split("/")
    serial = parts[2] if len(parts) > 2 else "?"
    # Cloud-style reply (see USE_CLOUD_STYLE_TIME_SYNC): calibrationTag false
    # unless the request's clock is off AND jumping it is safe.
    calibration, send_sync = True, False
    if _devices.get(serial, (None, None))[1] == "polar":
        # The real cloud answers a Polar with calibrationTag false (OBSERVED in a relay capture).
        calibration = False
    if (_uses_cloud_time_sync(serial) and isinstance(device_ts_ms, (int, float)) and device_ts_ms > 1e12):
        import time as _t
        req_offset = round(_t.time() - device_ts_ms / 1000.0)
        _ntp_logger.info("NTP request from %s... (%s) carried a clock %+ds off",
                         serial[:6], _device_name(serial), req_offset)
        if abs(req_offset) <= CLOCK_OFFSET_THRESHOLD_SECS:
            calibration = False
        else:
            reason, _ = _clock_hold_reason(serial, req_offset, _t.time())
            if reason:
                calibration = False
                _ntp_logger.info("NTP request from %s... (%s) is %+ds off, replying calibrationTag false and holding: %s",
                                 serial[:6], _device_name(serial), req_offset, reason)
            else:
                send_sync = True
    payload, now_ms = _build_ntp_payload(calibration)
    # LOG ONLY for now: the request carries the device's own clock, so we can
    # tell whether this reply's jump would skip or repeat a meal. The reply is
    # still sent as before, since it is untested what a feeder does when its
    # one post-boot time request goes unanswered. This records what a held
    # reply WOULD have been so that can be judged from real data first.
    try:
        if (not _uses_cloud_time_sync(serial) and _devices.get(serial, (None, None))[1] in ("one_rfid", "granary")
                and isinstance(device_ts_ms, (int, float)) and device_ts_ms > 1e12):
            import time as _t
            req_offset = round(_t.time() - device_ts_ms / 1000.0)
            if abs(req_offset) > CLOCK_OFFSET_THRESHOLD_SECS:
                # Always logged (not just when a meal is at stake): comparing this
                # with the first heartbeat after the reply shows whether the
                # feeder actually applied the reply (see CLOCK OFF lines).
                _ntp_logger.info("NTP request from %s... (%s) carried a clock %+ds off",
                                 serial[:6], _device_name(serial), req_offset)
                reason, _ = _clock_hold_reason(serial, req_offset, _t.time())
                if reason:
                    _ntp_logger.info("NTP request from %s... (%s) is %+ds off and replying would jump the clock: %s "
                                     "(replied anyway, log-only)", serial[:6], _device_name(serial), req_offset, reason)
    except Exception:
        _LOGGER.exception("NTP request skip-check failed for %s...", serial[:6])
    try:
        await _client_ref.publish(response_topic, payload)
        _LOGGER.info("NTP response sent: ts=%s", now_ms)
        # Full payload logged here (not just ts=) so the new DST-aware fields
        # (added 2026-09-12) can actually be visually confirmed from the
        # downloadable NTP debug log alone, no MQTT tooling needed.
        _ntp_logger.info("RESPONSE to %s... (%s): %s", serial[:6], _device_name(serial), payload)
    except Exception:
        _LOGGER.exception("Failed to send NTP response")
        _ntp_logger.exception("RESPONSE to %s... FAILED", serial[:6])
        return
    if send_sync:
        await _send_ntp_sync(serial, _devices[serial][1])


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
        _ntp_logger.info("PROACTIVE PUSH to %s... (%s): %s", serial[:6], _device_name(serial), payload)
    except Exception:
        _LOGGER.exception("Failed to push NTP sync to %s...", serial[:6])
        _ntp_logger.exception("PROACTIVE PUSH to %s... FAILED", serial[:6])


def _scheduled_times_between(serial: str, start: float, end: float) -> list:
    """Epoch seconds of every enabled scheduled feed with start < time <= end.
    Plans store executionTime as UTC HH:MM and repeatDay as 1=Mon..7=Sun, the
    same convention the Next Meal sensor and the missed-feed alert use."""
    import datetime as _dt
    import storage as _storage
    if end <= start:
        return []
    out = []
    first = _dt.datetime.fromtimestamp(start, tz=_dt.timezone.utc).date() - _dt.timedelta(days=1)
    last = _dt.datetime.fromtimestamp(end, tz=_dt.timezone.utc).date() + _dt.timedelta(days=1)
    for plan in _storage.get_active_feeding_plans(serial):
        if plan.get("_enabled") is False:
            continue
        try:
            h, m = map(int, plan.get("executionTime", "").split(":"))
        except (ValueError, AttributeError):
            continue
        repeat_day = plan.get("repeatDay", [])
        day = first
        while day <= last:
            if day.weekday() + 1 in repeat_day:
                ts = _dt.datetime(day.year, day.month, day.day, h, m, tzinfo=_dt.timezone.utc).timestamp()
                if start < ts <= end and not _storage.occurrence_skipped(plan, ts):
                    out.append(ts)
            day += _dt.timedelta(days=1)
    return sorted(out)


def _fmt_feed_time(epoch: float) -> str:
    import re as _re
    return _re.sub(r"(:\d\d):\d\d", lambda m: m.group(1), _format_local_time(int(epoch * 1000)))


def _clock_hold_reason(serial: str, offset: int, now: float):
    """Why a time correction should wait, or None if it is safe to jump now.
    offset > 0 means the device clock is slow. Returns (reason, meal_times)."""
    import storage as _storage
    device_now = now - offset
    if offset > 0:
        meals = _scheduled_times_between(serial, device_now - CLOCK_HOLD_SLACK_SECS, now + CLOCK_HOLD_SLACK_SECS)
        if meals:
            return ("a scheduled feed at %s falls inside the %ds jump and would be skipped"
                    % (", ".join(_fmt_feed_time(x) for x in meals), offset)), meals
        return None, []
    meals = _scheduled_times_between(serial, now - CLOCK_HOLD_SLACK_SECS, device_now + CLOCK_HOLD_SLACK_SECS)
    if meals:
        return ("a scheduled feed at %s falls inside the %ds jump back and could feed twice"
                % (", ".join(_fmt_feed_time(x) for x in meals), -offset)), meals
    # Belt and braces: a feed that actually dispensed within the replayed
    # stretch would repeat even if it isn't in the plans we know about (for
    # example plans made in the OEM app).
    recent = [e for e in _storage.get_feeder_log(serial, 20)
              if e.get("type") == "food_dispensed" and e.get("source") == "scheduled"
              and e.get("ts", 0) / 1000 >= now - (-offset + 60)]
    if recent:
        return "a scheduled feed dispensed within the last %ds and could repeat" % (-offset + 60), []
    return None, []


async def _notify_skipped_feeds(serial: str, offset: int, meals: list) -> None:
    import storage as _storage
    import notifications as _notifications
    cfg = _storage.get_devices().get(serial, {})
    if not cfg.get("notifications", {}).get("missed_feed", True):
        return
    name = cfg.get("name") or serial[:8]
    when = ", ".join(_fmt_feed_time(x) for x in meals)
    msg = (f"{name}'s clock was {offset // 60} min slow and has now been corrected. "
           f"The {when} scheduled feed was skipped by the correction. "
           f"No food was added to make up for it, feed manually if you want to.")
    await _notifications.fire_notification(
        f"Petlibro Local: {name} - Scheduled feed skipped", msg, _storage.get_settings(), cfg,
        notification_id=f"petlibro_local_{serial[:8]}_clock_skip_{int(meals[0])}")


async def _check_device_clock(serial: str, device_ts_ms) -> None:
    """Compares a heartbeat's own timestamp to real time. Every heartbeat
    carries the device's clock, so a wrong one is visible within about a
    minute. If it's off by more than CLOCK_OFFSET_THRESHOLD_SECS, pushes a
    time sync (rate limited, with backoff if the device doesn't take it).
    Positive offset means the device clock is slow."""
    import time as _t
    if not isinstance(device_ts_ms, (int, float)) or device_ts_ms < 1e12:
        return  # not an epoch-ms timestamp (e.g. a device that hasn't set its clock yet)
    now = _t.time()
    offset = round(now - device_ts_ms / 1000.0)
    _clock_offset[serial] = offset
    if _devices.get(serial, (None, None))[1] == "polar":
        return  # reported only, never corrected (see device_types/feeders/polar.py)
    fix = _clock_fix_state.setdefault(serial, {"last_push": 0.0, "tries": 0, "backoff_until": 0.0})
    if abs(offset) <= CLOCK_OFFSET_THRESHOLD_SECS:
        fix["tries"] = 0
        fix.pop("held_since", None)
        return
    if not ENABLE_PROACTIVE_NTP_PUSH:
        return
    if now < fix["backoff_until"] or now - fix["last_push"] < CLOCK_CORRECTION_MIN_INTERVAL_SECS:
        return
    device_type = _devices.get(serial, (None, None))[1]
    if not device_type:
        return
    skipped_meals = []
    if device_type in ("one_rfid", "granary"):
        reason, meals = _clock_hold_reason(serial, offset, now)
        if reason:
            held_since = fix.setdefault("held_since", now)
            if offset > 0 and now - held_since > CLOCK_SLOW_HOLD_MAX_SECS:
                skipped_meals = [m for m in meals if now - offset < m <= now]
            else:
                if now - fix.get("last_hold_log", 0.0) >= 600:
                    fix["last_hold_log"] = now
                    _ntp_logger.info("CLOCK OFF %+ds on %s... (%s), holding correction: %s",
                                     offset, serial[:6], _device_name(serial), reason)
                return
        else:
            fix.pop("held_since", None)
    if fix["tries"] >= CLOCK_CORRECTION_MAX_TRIES:
        fix["tries"] = 0
        fix["backoff_until"] = now + CLOCK_CORRECTION_BACKOFF_SECS
        _ntp_logger.info("CLOCK OFF %+ds on %s... (%s), %d correction pushes did not fix it, leaving it alone for %ds",
                         offset, serial[:6], _device_name(serial), CLOCK_CORRECTION_MAX_TRIES,
                         CLOCK_CORRECTION_BACKOFF_SECS)
        return
    fix["tries"] += 1
    fix["last_push"] = now
    _ntp_logger.info("CLOCK OFF %+ds on %s... (%s), pushing correction (attempt %d)",
                     offset, serial[:6], _device_name(serial), fix["tries"])
    await _correct_clock(serial, device_type)
    if skipped_meals:
        await _notify_skipped_feeds(serial, offset, skipped_meals)


async def _check_missed_feeds() -> None:
    """Alerts when an enabled scheduled feed passes its time plus a leeway
    with no dispense recorded. Feeders run their schedule off their own
    clock and report each completed dispense, so a scheduled time with no
    dispense event behind it is a real signal (late, missed, or empty hopper).
    Only judges feeds this add-on was actually watching: skipped if the
    scheduled time is before the add-on started or before the device last
    (re)connected, since the dispense event may simply not have reached us.
    The Granary reports dispenses the same way (confirmed by a user's activity
    log, 2026-09-29), so it is checked too."""
    import datetime as _dt
    import time as _t
    if not ENABLE_MISSED_FEED_ALERT:
        return
    import storage as _storage
    import notifications as _notifications
    now = _t.time()
    for k in [k for k, v in _missed_feed_alerted.items() if now - v > 86400]:
        _missed_feed_alerted.pop(k, None)
    settings = _storage.get_settings()
    devices_data = _storage.get_devices()
    for serial, (_, device_type) in list(_devices.items()):
        if device_type not in ("one_rfid", "granary") or not _online.get(serial):
            continue
        cfg = devices_data.get(serial, {})
        if not cfg.get("notifications", {}).get("missed_feed", True):
            continue
        watching_since = max(_ADDON_START_TS, _online_since.get(serial, 0.0))
        dispenses = [e.get("ts", 0) / 1000 for e in _storage.get_feeder_log(serial, 100)
                     if e.get("type") == "food_dispensed"]
        today = _dt.datetime.now(_dt.timezone.utc).date()
        for plan in _storage.get_active_feeding_plans(serial):
            if plan.get("_enabled") is False:
                continue
            exec_time = plan.get("executionTime", "")
            repeat_day = plan.get("repeatDay", [])
            try:
                h, m = map(int, exec_time.split(":"))
            except (ValueError, AttributeError):
                continue
            for day_offset in (0, -1):
                day = today + _dt.timedelta(days=day_offset)
                if day.weekday() + 1 not in repeat_day:
                    continue
                expected = _dt.datetime(day.year, day.month, day.day, h, m,
                                        tzinfo=_dt.timezone.utc).timestamp()
                if not (now - MISSED_FEED_LOOKBACK_SECS <= expected <= now - MISSED_FEED_LEEWAY_SECS):
                    continue
                if expected < watching_since:
                    continue
                if _storage.occurrence_skipped(plan, expected):
                    continue
                if any(expected - 120 <= d <= now for d in dispenses):
                    continue
                key = (serial, plan.get("planId", exec_time), int(expected))
                if key in _missed_feed_alerted:
                    continue
                _missed_feed_alerted[key] = now
                name = cfg.get("name") or serial[:8]
                import re as _re
                when = _re.sub(r"(:\d\d):\d\d", r"\1", _format_local_time(int(expected * 1000)))
                msg = (f"The {when} scheduled feed on {name} did not dispense "
                       f"(nothing recorded within {MISSED_FEED_LEEWAY_SECS // 60} minutes).")
                title = f"Petlibro Local: {name} - Scheduled feed missed"
                _LOGGER.info("Missed scheduled feed on %s...: expected %s", serial[:6], when)
                asyncio.ensure_future(_notifications.fire_notification(
                    title, msg, settings, cfg,
                    notification_id=f"petlibro_local_{serial[:8]}_missed_feed_{int(expected)}"))


# ── Offline watchdog ───────────────────────────────────────────────────────

async def _offline_watchdog():
    """Periodically mark devices offline if no MQTT message received within WATCHDOG_SECS."""
    import time as _t
    while True:
        await asyncio.sleep(60)
        try:
            await reconcile_plan_timezones()   # storage part also runs while MQTT is down
        except Exception:
            _LOGGER.exception("Plan timezone reconcile error")
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

        try:
            await _check_missed_feeds()
        except Exception:
            _LOGGER.exception("Missed-feed watchdog error")

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
                    if pet.get("no_eat_alerted_for"):
                        _storage.save_pet(pet_id, {"no_eat_alerted_for": None})
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
                # "Already alerted" is remembered on disk as the last meal the alert
                # was sent for, so a restart or rebuild does not send it again for
                # the same stretch of not eating; a new meal changes that value.
                already = pet.get("no_eat_alerted_for") == last_eat_ms or _pet_no_eat_alerted.get(pet_id)
                if secs_since >= threshold and not already:
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
                    _storage.save_pet(pet_id, {"no_eat_alerted_for": last_eat_ms})
                    _LOGGER.info("No-eat alert fired for pet %s...", pet_id[:6])
                elif secs_since < threshold:
                    _pet_no_eat_alerted.pop(pet_id, None)
                    if pet.get("no_eat_alerted_for"):
                        _storage.save_pet(pet_id, {"no_eat_alerted_for": None})
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
        # A deliberate reconnect (a device was added or removed, so the app re-opens
        # its own broker connection to subscribe again) must not make every device
        # look offline. Only a real failure or shutdown marks them offline.
        planned_reconnect = False
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
                    _LOGGER.info("Registered: %s... model=%s", serial[:6], _topic_model(device_type, serial))
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
                    plans = _storage.get_active_feeding_plans(serial)
                    for ct in ha_mqtt.get_command_topics(serial, device_type):
                        await client.subscribe(ct)
                    custom_icon_names = [ic["name"] for ic in _storage.get_custom_icons()]
                    await ha_mqtt.publish_discovery(client, serial, cfg, state, extra_icon_names=custom_icon_names)
                    await ha_mqtt.retract_legacy_entities(client, serial, cfg)
                    await ha_mqtt.publish_state(client, serial, cfg, state, plans=plans)
                    await ha_mqtt.publish_availability(client, serial, True)

                async for message in client.messages:
                    if _reconnect_event and _reconnect_event.is_set():
                        planned_reconnect = True
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
                        if recognized:
                            _learn_wire_model(serial, parts[1])
                        _now = _time.time()
                        _raw_capture.append({
                            "ts":         _now,
                            "topic":      topic_str,
                            "payload":    payload_str,
                            "recognized": recognized,
                        })
                        _prune_raw_capture(_now)
                        if recognized:
                            # A handler error on one odd message must not end the shared
                            # broker connection (which would mark every device offline).
                            try:
                                _handle_message(serial, topic_str, payload_str)
                            except Exception:
                                _LOGGER.exception("Error handling a message from %s... (%s), skipped", serial[:6], topic_str)
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
            if not planned_reconnect:
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
    """Snapshot of the rolling 24h raw dl/ traffic buffer, oldest first. Populated
    continuously by _mqtt_loop rather than on demand, since devices only chirp
    occasionally and a fixed listen window could easily miss one. Used by the
    Help/About "Download Debug Capture" button."""
    import time as _time
    _prune_raw_capture(_time.time())
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
