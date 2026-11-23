import os

# Supervisor token, injected by HA only when running as an add-on. Used for the
# Supervisor API (managing the Mosquitto add-on, see mosquitto.py). It must NOT
# fall back to HA_TOKEN: standalone there is no Supervisor, and a standalone HA
# token here would make mosquitto.py try to reach http://supervisor.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Home Assistant REST API. As an add-on the Supervisor token reaches HA at
# http://supervisor/core/api. A standalone container (PR #21) instead sets
# HA_BASE_URL to HA's REST API (e.g. http://homeassistant:8123/api) and HA_TOKEN
# to a long-lived access token. HA is optional standalone: a blank token just
# turns off HA notifications and the area/notify-service dropdowns.
HA_API_URL = os.environ.get("HA_BASE_URL", "http://supervisor/core/api")
_HA_TOKEN = SUPERVISOR_TOKEN or os.environ.get("HA_TOKEN", "")

def _headers() -> dict:
    return {"Authorization": f"Bearer {_HA_TOKEN}", "Content-Type": "application/json"}

def _token() -> str:
    return _HA_TOKEN

def ha_configured() -> bool:
    """True when there is a token to reach Home Assistant (add-on, or HA_TOKEN set)."""
    return bool(_HA_TOKEN)
