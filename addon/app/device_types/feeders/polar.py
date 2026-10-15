"""Polar Wet Food Feeder (PLAF109) -- EARLY BETA, not run on real hardware by the maintainer.

The wire model is `PLAF109` (confirmed in a capture of the real cloud, firmware
2.0.32, and by two Polar owners). What the commands rest on, and what is still
unknown, is written down in polar_control.py. Left out on purpose:

- Feeding plans. The feeder keeps and runs its own plans; this app neither
  reads nor writes them, so plans set up in the Petlibro app are left alone.
  Whether a Polar that no longer talks to the Petlibro cloud keeps running them
  is untested.
- Battery and power alerts. The only values ever seen (powerType 1,
  electricQuantity 0) do not match what the One RFID Feeder's confirmed
  powerType meanings (2 battery, 3 AC) would say, so nothing is assumed.
- Cleaning reminders and other maintenance, until someone is using it for real.
- Clock correction. The Polar's clock has been seen about 20 minutes slow, but a
  forward jump can skip a plan the feeder runs on its own and this app cannot see
  those plans, so it only reports the offset.
"""

DEVICE_TYPES = ["polar"]

MQTT_MODELS = {
    "polar": "PLAF109",
}

ALERT_MESSAGES: dict[str, str] = {}

DEFAULT_NOTIFICATIONS: dict[str, bool] = {}


def compute_alerts(state: dict, cfg: dict, online: bool) -> set:
    return set()


def track_intake(old_state: dict, new_state: dict, min_grams: float = 5) -> float | None:
    return None
