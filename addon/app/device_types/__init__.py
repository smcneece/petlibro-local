"""Device type registry.

To add a new device family:
  1. Create a module under the appropriate subfolder (fountains/, feeders/, litterboxes/)
  2. Define DEVICE_TYPES, MQTT_MODELS, ALERT_MESSAGES, DEFAULT_NOTIFICATIONS,
     compute_alerts(state, cfg, online) -> set, and track_intake(old, new) -> float | None
  3. Import the module below and add it to _MODULES
"""

from .fountains import dockstream2
from .fountains import dockstream_rfid
from .feeders import one_rfid
from .feeders import granary
# from .litterboxes import luma  # (future)

_MODULES = [dockstream2, dockstream_rfid, one_rfid, granary]

_REGISTRY: dict = {}
for _mod in _MODULES:
    for _dt in _mod.DEVICE_TYPES:
        _REGISTRY[_dt] = _mod


def get(device_type: str):
    """Return the device module for this device_type, or None."""
    return _REGISTRY.get(device_type)


def all_mqtt_models() -> dict[str, str]:
    """Return merged dict of device_type → MQTT model prefix for all registered types."""
    result = {}
    for mod in set(_REGISTRY.values()):
        result.update(mod.MQTT_MODELS)
    return result


def all_alert_messages() -> dict[str, str]:
    """Return merged alert messages across all device types, plus the shared offline message."""
    result = {"offline": "Device is offline."}
    for mod in set(_REGISTRY.values()):
        result.update(mod.ALERT_MESSAGES)
    return result
