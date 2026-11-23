"""Notification dispatch for Petlibro Local alerts.

Sends bell (HA persistent notification), email, and mobile alerts through Home
Assistant, and push alerts to an ntfy topic or a webhook without it, based on
global settings and per-device channel overrides.
"""

import base64
import logging
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import aiohttp

from ha_config import HA_API_URL, _headers, ha_configured

_LOGGER = logging.getLogger(__name__)

_NOTIFY_ID = "petlibro_local"


async def fire_notification(title: str, message: str, settings: dict, device: dict,
                            notification_id: str = _NOTIFY_ID):
    # The bell, email and mobile channels all go through Home Assistant; with no HA
    # to reach they are skipped rather than failing on every alert.
    ha = ha_configured()
    if ha and settings.get("notify_bell_enabled", True) and device.get("notify_bell", True):
        await _fire_persistent(title, message, notification_id)

    service = settings.get("notify_email_service", "").strip()
    if ha and device.get("notify_email", True) and service:
        addr = device.get("notify_email_address", "") or settings.get("notify_email_to", "")
        targets = [a.strip() for a in addr.split(",") if a.strip()] if addr else []
        if targets:
            await _fire_notify_service(service, title, message, targets)

    if ha and device.get("notify_mobile", False):
        mobile = (device.get("notify_mobile_service", "").strip()
                  or settings.get("notify_mobile_default_service", "").strip())
        if mobile:
            await _fire_notify_service(mobile.removeprefix("notify."), title, message, [],
                                       tag=notification_id)

    if device.get("notify_push", True) and push_configured(settings):
        await send_push(title, message, settings, notification_id)


async def dismiss_notification(notification_id: str, settings: dict, device: dict):
    """Dismiss a previously fired HA bell notification and clear the mobile push.

    Called when an alert clears (e.g. device back online) so stale notifications
    disappear without the user having to manually dismiss them. The ntfy and webhook
    channel has nothing to dismiss: the "back online" message it gets says it.
    """
    if not ha_configured():
        return
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
            async with session.post(
                f"{HA_API_URL}/services/notify/{service}",
                headers={**_headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                status = resp.status
        if status != 200:
            # A missing or misspelled notify service is refused by Home Assistant
            # (issue #16): say so instead of logging it as sent.
            _LOGGER.warning("Notify service '%s' failed (HTTP %s): %s", service, status, title if not clear else "(clear)")
            return
        _LOGGER.info("Notify service '%s' fired: %s", service, title if not clear else "(clear)")
    except Exception:
        _LOGGER.exception("Failed to fire notify service '%s'", service)


# ── Push without Home Assistant: ntfy or a generic webhook ───────────────────

_PUSH_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _push_url(settings: dict) -> str:
    url = (settings.get("notify_push_url") or "").strip()
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.hostname else ""


def push_configured(settings: dict) -> bool:
    return bool(_push_url(settings))


async def send_push(title: str, message: str, settings: dict,
                    notification_id: str = _NOTIFY_ID) -> tuple[bool, str]:
    """Send one push to the configured ntfy topic or webhook.

    Returns (sent, reason). The reason is short and safe to show in the UI. The URL is
    never logged or returned: an ntfy topic name is the only secret protecting it.
    """
    url = _push_url(settings)
    if not url:
        return False, "No valid http(s) URL is set"
    token = (settings.get("notify_push_token") or "").strip()
    headers = {}
    if token:
        if ":" in token:  # user:password
            headers["Authorization"] = "Basic " + base64.b64encode(token.encode("utf-8")).decode("ascii")
        else:
            headers["Authorization"] = f"Bearer {token}"

    if (settings.get("notify_push_format") or "ntfy") == "webhook":
        kwargs = {"json": {"title": title, "message": message, "id": notification_id,
                           "source": "petlibro-local"}}
    else:
        # ntfy takes the message as the body. The title goes in the query string, where
        # UTF-8 (device names) survives, unlike an HTTP header.
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True) + [("title", title)]
        url = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
        kwargs = {"data": message.encode("utf-8")}
        headers["Content-Type"] = "text/plain; charset=utf-8"

    host = urlsplit(url).hostname
    try:
        async with aiohttp.ClientSession() as session:
            # A redirect (http to https) would turn the POST into a GET and drop the message.
            async with session.post(url, headers=headers, timeout=_PUSH_TIMEOUT,
                                    allow_redirects=False, **kwargs) as resp:
                status = resp.status
    except Exception as exc:
        reason = (f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__).replace(url, "<url>")
        _LOGGER.warning("Push notification to %s failed: %s", host, reason)
        return False, reason
    if 300 <= status < 400:
        reason = f"HTTP {status} redirect, set the final URL (for example https://) instead"
    elif status >= 400:
        reason = f"HTTP {status}"
    else:
        _LOGGER.info("Push notification sent to %s: %s", host, title)
        return True, ""
    _LOGGER.warning("Push notification to %s failed: %s", host, reason)
    return False, reason
