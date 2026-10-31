import os

# As an add-on the Supervisor injects SUPERVISOR_TOKEN and the API lives at
# http://supervisor/core/api. Standalone containers instead set HA_BASE_URL to the
# HA REST API (e.g. http://homeassistant:8123/api) and HA_TOKEN to a long-lived
# access token. HA is optional standalone: a blank token just disables HA
# notifications and the area/notify-service dropdowns.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "") or os.environ.get("HA_TOKEN", "")
HA_API_URL = os.environ.get("HA_BASE_URL", "http://supervisor/core/api")

def _headers() -> dict:
    return {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}

def _token() -> str:
    return SUPERVISOR_TOKEN
