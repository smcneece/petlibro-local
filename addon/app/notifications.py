"""HA notification dispatch for Petlibro Local alerts.

Sends bell (HA persistent notification), email, and mobile alerts based
on global settings and per-device channel overrides.
"""

import logging

import aiohttp

from ha_config import HA_API_URL, _headers

_LOGGER = logging.getLogger(__name__)

_NOTIFY_ID = "petlibro_local"


async def fire_notification(title: str, message: str, settings: dict, device: dict,
                            notification_id: str = _NOTIFY_ID):
    if settings.get("notify_bell_enabled", True) and device.get("notify_bell", True):
        await _fire_persistent(title, message, notification_id)

    service = settings.get("notify_email_service", "").strip()
    if device.get("notify_email", True) and service:
        addr = device.get("notify_email_address", "") or settings.get("notify_email_to", "")
        targets = [a.strip() for a in addr.split(",") if a.strip()] if addr else []
        if targets:
            await _fire_notify_service(service, title, message, targets)

    if device.get("notify_mobile", False):
        mobile = (device.get("notify_mobile_service", "").strip()
                  or settings.get("notify_mobile_default_service", "").strip())
        if mobile:
            await _fire_notify_service(mobile.removeprefix("notify."), title, message, [],
                                       tag=notification_id)


async def dismiss_notification(notification_id: str, settings: dict, device: dict):
    """Dismiss a previously fired HA bell notification and clear the mobile push.

    Called when an alert clears (e.g. device back online) so stale notifications
    disappear without the user having to manually dismiss them.
    """
    if settings.get("notify_bell_enabled", True) and device.get("notify_bell", True):
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{HA_API_URL}/services/persistent_notification/dismiss",
                    headers={**_headers(), "Content-Type": "application/json"},
                    json={"notification_id": notification_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            _LOGGER.info("Dismissed notification: %s", notification_id)
        except Exception:
            _LOGGER.exception("Failed to dismiss notification %s", notification_id)

    if device.get("notify_mobile", False):
        mobile = (device.get("notify_mobile_service", "").strip()
                  or settings.get("notify_mobile_default_service", "").strip())
        if mobile:
            await _fire_notify_service(
                mobile.removeprefix("notify."), "", "", [],
                tag=notification_id, clear=True,
            )


async def _fire_persistent(title: str, message: str, notification_id: str = _NOTIFY_ID):
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{HA_API_URL}/services/persistent_notification/create",
                headers={**_headers(), "Content-Type": "application/json"},
                json={"title": title, "message": message, "notification_id": notification_id},
                timeout=aiohttp.ClientTimeout(total=10),
            )
        _LOGGER.info("Persistent notification: %s", title)
    except Exception:
        _LOGGER.exception("Failed to fire persistent notification")


_SERVICE_NAME_RE = __import__("re").compile(r"^[a-z0-9_]{1,100}$")


async def _fire_notify_service(service: str, title: str, message: str, targets: list,
                                tag: str = "", clear: bool = False):
    # The name goes into a URL path on the Home Assistant API, so anything that is
    # not a plain service name (for example one containing "/" or "..") is refused.
    if not _SERVICE_NAME_RE.match(service or ""):
        _LOGGER.warning("Refusing to call a notify service with an invalid name: %r", (service or "")[:40])
        return
    if clear:
        payload: dict = {"message": "clear_notification", "data": {"tag": tag}}
    elif service.startswith("mobile_app_"):
        payload = {"title": title, "message": message}
        if tag:
            payload["data"] = {"tag": tag}
    else:
        html_msg = "<br>".join(message.split("\n"))
        payload = {
            "title": title,
            "message": html_msg,
            "data": {"html": f"<html><body>{html_msg}</body></html>"},
        }
    if targets:
        payload["target"] = targets
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{HA_API_URL}/services/notify/{service}",
                headers={**_headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            )
        _LOGGER.info("Notify service '%s' fired: %s", service, title if not clear else "(clear)")
    except Exception:
        _LOGGER.exception("Failed to fire notify service '%s'", service)
