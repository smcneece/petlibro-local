import os

# Supervisor token, injected by HA only when running as an add-on. It is used for
# the Supervisor API (managing the Mosquitto add-on, see mosquitto.py). It must NOT
# fall back to HA_TOKEN: standalone there is no Supervisor, and a standalone HA token
# here would make mosquitto.py believe it can reach http://supervisor and hang/fail.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Home Assistant REST API. As an add-on the Supervisor token reaches HA at
# http://supervisor/core/api. Standalone containers instead set HA_BASE_URL to the HA
# REST API (e.g. http://homeassistant:8123/api) and HA_TOKEN to a long-lived access
# token, kept separate from the Supervisor token above. HA is optional standalone: a
# blank token just disables HA notifications and the area/notify-service dropdowns.
HA_API_URL = os.environ.get("HA_BASE_URL", "http://supervisor/core/api")
_HA_TOKEN = SUPERVISOR_TOKEN or os.environ.get("HA_TOKEN", "")

def _headers() -> dict:
    return {"Authorization": f"Bearer {_HA_TOKEN}", "Content-Type": "application/json"}

def _token() -> str:
    return _HA_TOKEN
