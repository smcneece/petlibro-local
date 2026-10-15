"""Polar Wet Food Feeder (PLAF109): command building, safety rules and feed tracking.

EARLY BETA. The maintainer has no Polar, so nothing here has been run on real
hardware by them. This module is pure (no MQTT, no storage) so the rules can be
tested without a broker or a feeder. What each message rests on:

OBSERVED in a relay capture of the real Petlibro cloud talking to a Polar
(rgnv/haos-petlibro-local docs, firmware 2.0.32):
- Feed now = WET_GRAIN_FEEDING_PLAN_SERVICE on service/sub with one plan,
  planId 666666666 (the manual slot) and feedNowState true. The device acks on
  service/post with code 0 (code 2099 "FeedPlanErro" = rejected, for example a
  feed is already running).
- The feed then reports WET_GRAIN_OUTPUT_EVENT on event/post: execStep
  GRAIN_THAW, GRAIN_START, OPEN_DOOR, then a message with finished true.
- The cloud echoes device event posts on event/sub as {cmd, msgId, code 0,
  linkId} (plus execStep for the output event). It does NOT echo service/post
  replies: echoing one re-triggers the command on the device (an endless 2099
  loop was seen).
- executionTime / executionDay in the plan are LOCAL time (the capture's time
  stamp is 03:16 UTC while the plan said 20:16 on the previous day, UTC-7).

REPORTED by the gianlucariccobono fork (which says it tested on hardware, not
independently confirmed): WET_FOOD_FEED_STOP_SERVICE (planId), SWITCH_DOOR_SERVICE
(barnDoorState), SET_PLATE_POS_SERVICE (platePosition), and a GRAIN_END step.

REPORTED by a Polar owner (Leo, own hardware): a plate outside 1..3, including 0,
makes the plate motor run on and on. So the plate is always validated here and
anything else is refused before it reaches the wire.

UNRESOLVED: the unit of feedingDuration. One source says seconds, another says
minutes, and both use 240 as the cloud's own value. Until the unit is set for a
device, the only duration ever sent is TEST_DURATION (1), which is harmless in
either unit, and the app times the real door-open window so the unit can be
read off a test feed.
"""

import uuid

MANUAL_PLAN_ID = 666666666
PLATES = (1, 2, 3)

# unit -> (smallest, largest) raw feedingDuration value this app will send
DURATION_UNITS = {"seconds": (1, 600), "minutes": (1, 240)}
DEFAULT_SERVE = {"seconds": 240, "minutes": 30}
TEST_DURATION = 1
SECONDS_PER_UNIT = {"seconds": 1, "minutes": 60}

PLATE_MOVE_MIN_GAP_SECS = 8        # do not hammer the plate motor
FEED_START_GRACE_SECS = 180        # slack on top of the requested door-open window
FEEDER_STARTED_WINDOW_SECS = 900   # a feed we did not start: assume it may take this long
STOP_SETTLE_SECS = 30              # after Stop, stop treating the feed as running

# Event posts the real cloud echoes (see above). Service replies are never echoed.
ECHO_EVENTS = frozenset({
    "DEVICE_START_EVENT", "ATTR_PUSH_EVENT", "GET_FEEDING_PLAN_EVENT", "WET_GRAIN_OUTPUT_EVENT",
})
START_STEPS = frozenset({"GRAIN_THAW", "GRAIN_START", "OPEN_DOOR"})
END_STEPS = frozenset({"GRAIN_END"})


class PolarError(ValueError):
    """A Polar command refused on purpose. The message is safe to show the user."""


# ── Validation ────────────────────────────────────────────────────────────

def validate_plate(plate, field: str = "Plate") -> int:
    if isinstance(plate, bool) or not isinstance(plate, int) or plate not in PLATES:
        raise PolarError(f"{field} must be 1, 2 or 3")
    return plate


def duration_unit(cfg: dict):
    unit = (cfg or {}).get("polar_duration_unit")
    return unit if unit in DURATION_UNITS else None


def _valid_duration(value, unit):
    lo, hi = DURATION_UNITS[unit] if unit else (1, max(h for _, h in DURATION_UNITS.values()))
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise PolarError(f"Serve duration must be a whole number from {lo} to {hi}" + (f" {unit}" if unit else ""))
    return value


def serve_duration(cfg: dict, override=None):
    """(value, unit, is_test). With no unit chosen for this device the value is
    always TEST_DURATION, whatever was asked for."""
    unit = duration_unit(cfg)
    if unit is None:
        return TEST_DURATION, None, True
    value = override if override is not None else (cfg or {}).get("polar_serve_duration")
    if value is None:
        value = DEFAULT_SERVE[unit]
    return _valid_duration(value, unit), unit, False


def validate_settings(data: dict) -> None:
    """Checks the Polar fields of a device-settings save (raises PolarError)."""
    if "polar_duration_unit" in data and data["polar_duration_unit"] not in (None, "", "seconds", "minutes"):
        raise PolarError("Duration unit must be seconds or minutes")
    if data.get("polar_serve_duration") not in (None, ""):
        unit = data.get("polar_duration_unit")
        _valid_duration(data["polar_serve_duration"], unit if unit in DURATION_UNITS else None)


# ── Messages ──────────────────────────────────────────────────────────────

def _envelope(cmd: str, now_ms: int, msg_id: str | None, **fields) -> dict:
    return {"cmd": cmd, "ts": int(now_ms), "msgId": msg_id or uuid.uuid4().hex, **fields}


def feed_payload(plate: int, duration: int, local_now, now_ms: int, msg_id: str | None = None) -> dict:
    """The feed-now plan write. local_now is a timezone-aware local datetime."""
    validate_plate(plate)
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 1:
        raise PolarError("Serve duration must be a positive whole number")
    return _envelope("WET_GRAIN_FEEDING_PLAN_SERVICE", now_ms, msg_id, plans=[{
        "planId": MANUAL_PLAN_ID,
        "executionTime": local_now.strftime("%H:%M"),
        "executionDay": local_now.strftime("%Y-%m-%d"),
        "plate": plate,
        "feedingDuration": duration,
        "feedNowState": True,
    }])


def stop_payload(now_ms: int, msg_id: str | None = None) -> dict:
    return _envelope("WET_FOOD_FEED_STOP_SERVICE", now_ms, msg_id, planId=MANUAL_PLAN_ID)


def door_payload(open_door: bool, now_ms: int, msg_id: str | None = None) -> dict:
    return _envelope("SWITCH_DOOR_SERVICE", now_ms, msg_id, barnDoorState=bool(open_door))


def plate_position_payload(position: int, now_ms: int, msg_id: str | None = None) -> dict:
    validate_plate(position, "Plate position")
    return _envelope("SET_PLATE_POS_SERVICE", now_ms, msg_id, platePosition=position)


def echo_payload(topic: str, data, now_ms: int, link_id: str | None = None):
    """The reply the real cloud sends to an event post, or None. Fails closed: only
    a known event on an event/post topic, never a service reply, never a message
    that itself carries a linkId."""
    if (not isinstance(topic, str) or not topic.endswith("/device/event/post")
            or not isinstance(data, dict) or "linkId" in data
            or not isinstance(data.get("cmd"), str) or data["cmd"] not in ECHO_EVENTS):
        return None
    echo = {"cmd": data["cmd"], "ts": int(now_ms), "msgId": data.get("msgId", ""),
            "code": 0, "linkId": link_id or uuid.uuid4().hex}
    if data["cmd"] == "WET_GRAIN_OUTPUT_EVENT":
        echo["execStep"] = data.get("execStep", "")
    return echo


# ── Feed tracking (runtime state, never persisted) ────────────────────────

def new_runtime() -> dict:
    return {"active": False}


def is_active(rt: dict, now: float) -> bool:
    return bool(rt.get("active")) and now < rt.get("expires_at", 0)


def _window_secs(requested, unit) -> float:
    # An unknown unit is treated as the longer one, so the guard errs towards waiting.
    return (requested or 0) * SECONDS_PER_UNIT.get(unit, 60)


def check_feed_allowed(rt: dict, now: float) -> None:
    if is_active(rt, now):
        raise PolarError("A feed is already running. Wait for it to finish, or press Stop")


def check_plate_move_allowed(rt: dict, now: float) -> None:
    if is_active(rt, now):
        raise PolarError("Wait for the current feed to finish before moving the plate")
    if now - rt.get("last_plate_move", 0) < PLATE_MOVE_MIN_GAP_SECS:
        raise PolarError("Wait a few seconds between plate moves")


def note_plate_move(rt: dict, now: float) -> None:
    rt["last_plate_move"] = now


def note_feed_requested(rt: dict, plate: int, requested: int, unit, now: float) -> None:
    rt.update(active=True, source="requested", plate=plate, requested=requested, unit=unit,
              step=None, door_open_at=None, stop_requested=False, requested_at=now,
              expires_at=now + _window_secs(requested, unit) + FEED_START_GRACE_SECS)


def note_stop_requested(rt: dict, now: float) -> None:
    if rt.get("active"):
        rt["stop_requested"] = True
        rt["expires_at"] = min(rt.get("expires_at", now + STOP_SETTLE_SECS), now + STOP_SETTLE_SECS)


def note_service_ack(rt: dict, cmd: str, code, now: float) -> None:
    """A rejected feed (any code other than 0) is not running."""
    if cmd == "WET_GRAIN_FEEDING_PLAN_SERVICE" and code not in (0, None):
        rt["active"] = False
        rt["last_error"] = {"code": code, "ts": now}
    elif cmd == "WET_GRAIN_FEEDING_PLAN_SERVICE":
        rt.pop("last_error", None)


def on_output_event(rt: dict, data: dict, now: float):
    """Feeds one WET_GRAIN_OUTPUT_EVENT in. Returns a summary dict when the feed
    finishes, else None."""
    step = data.get("execStep") or data.get("event")
    if not isinstance(step, str):
        step = None
    finished = data.get("finished") is True or step in END_STEPS
    if not rt.get("active"):
        if step not in START_STEPS and not finished:
            return None
        # A feed this app did not request: the feeder's own plan or its button.
        rt.update(active=True, source="feeder", plate=data.get("plate"), requested=None, unit=None,
                  door_open_at=None, stop_requested=False, expires_at=now + FEEDER_STARTED_WINDOW_SECS)
    if step:
        rt["step"] = step
    if isinstance(data.get("plate"), int) and not isinstance(data.get("plate"), bool):
        rt["plate"] = data["plate"]
    if step == "OPEN_DOOR" and rt.get("door_open_at") is None:
        rt["door_open_at"] = now
        window = _window_secs(rt.get("requested"), rt.get("unit")) or FEEDER_STARTED_WINDOW_SECS
        rt["expires_at"] = max(rt.get("expires_at", 0), now + window + FEED_START_GRACE_SECS)
    if not finished:
        return None
    opened = rt.get("door_open_at")
    summary = {
        "finished_at": now,
        "source": rt.get("source"),
        "plate": rt.get("plate"),
        "plan_id": data.get("planId"),
        "requested": rt.get("requested"),
        "unit": rt.get("unit"),
        "stopped": bool(rt.get("stop_requested")),
        "door_open_secs": round(now - opened) if opened else None,
    }
    summary["suggested_unit"] = suggest_unit(summary)
    rt.update(active=False, step=None, last_feed=summary)
    return summary


def suggest_unit(summary: dict):
    """What a measured test feed says about the duration unit. Only a feed we
    asked for with the test value, and that was not stopped early, counts. This
    is a suggestion shown to the user, never applied automatically: a door that
    closed early would otherwise read as a short unit."""
    if (summary.get("unit") is not None or summary.get("requested") != TEST_DURATION
            or summary.get("stopped") or summary.get("source") != "requested"):
        return None
    secs = summary.get("door_open_secs")
    if secs is None:
        return None
    if secs >= 40:
        return "minutes"
    if secs <= 15:
        return "seconds"
    return None


def public_view(rt: dict, now: float) -> dict:
    """The part of the runtime state the web UI and Home Assistant show."""
    last = rt.get("last_feed")
    return {
        "polar_feed_active": is_active(rt, now),
        "polar_feed_step": rt.get("step") if is_active(rt, now) else None,
        "polar_last_feed": dict(last) if last else None,
        "polar_last_error": dict(rt["last_error"]) if rt.get("last_error") else None,
    }
