"""Feeder maintenance reminders (desiccant, bowl, housing), shared by the feeder types.

Issue #17: these used to alert only once the full interval had passed, while the
fountain's filter alert comes 3 days early, so there was no time to get a new
desiccant pack. Each now alerts a few days before it is due. The lead is capped at
half the interval (whole days), so a short custom interval still gets a sensible
warning. The web UI's bell (api.js) uses the same numbers.
"""
import math
import time

LEAD_DAYS = {"desiccant_due": 3, "housing_due": 3, "bowl_due": 1}


def lead_days(alert: str, interval_days) -> int:
    return min(LEAD_DAYS.get(alert, 0), math.floor(float(interval_days) / 2))


def days_left(last_ts_ms, interval_days, now: float | None = None) -> float:
    """Days until due (negative once overdue). last_ts_ms is epoch ms from the UI."""
    now = time.time() if now is None else now
    return float(interval_days) - (now - last_ts_ms / 1000) / 86400


def is_due(alert: str, last_ts_ms, interval_days, now: float | None = None) -> bool:
    """True from lead_days before the due date. False when no start date is set."""
    if last_ts_ms is None:
        return False
    try:
        return days_left(last_ts_ms, interval_days, now) <= lead_days(alert, interval_days)
    except (TypeError, ValueError):
        return False
