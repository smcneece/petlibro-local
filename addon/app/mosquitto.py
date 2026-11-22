"""Managing the Mosquitto logins for device credentials.

Two backends share the same functions and return values:
  - Supervisor API: manages the Mosquitto add-on's login list (Home Assistant add-on).
  - Password file: when MOSQUITTO_PASSWD_FILE is set (docker-compose.standalone.yml),
    edits Mosquitto's password_file directly and reloads Mosquitto with SIGHUP.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import signal
import stat
import tempfile

import aiohttp

from ha_config import SUPERVISOR_TOKEN, _headers

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor"
MOSQUITTO_SLUG = "core_mosquitto"


def _action_headers() -> dict:
    """Headers for Supervisor action POSTs (no body, so no Content-Type)."""
    return {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}


async def _addon_action(action: str) -> bool:
    if not SUPERVISOR_TOKEN:
        _LOGGER.error("SUPERVISOR_TOKEN is not set — hassio_api may not be enabled")
        return False

    url = f"{SUPERVISOR_URL}/addons/{MOSQUITTO_SLUG}/{action}"
    _LOGGER.info("Supervisor: POST %s", url)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=_action_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                if resp.status == 200:
                    _LOGGER.info("Mosquitto %s: OK (response: %s)", action, text[:120])
                    return True
                _LOGGER.error(
                    "Mosquitto %s failed: HTTP %d — %s",
                    action, resp.status, text[:300],
                )
                return False
    except Exception:
        _LOGGER.exception("Mosquitto %s request failed", action)
        return False


async def get_addon_info() -> dict:
    """Fetch Mosquitto add-on info for diagnostics. Logs the slug + state."""
    if not SUPERVISOR_TOKEN:
        _LOGGER.error("SUPERVISOR_TOKEN not set")
        return {}
    url = f"{SUPERVISOR_URL}/addons/{MOSQUITTO_SLUG}/info"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=_action_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    state = data.get("data", {}).get("state", "?")
                    _LOGGER.info("Mosquitto info: state=%s", state)
                    return data
                text = await resp.text()
                _LOGGER.error("Mosquitto info failed: HTTP %d — %s", resp.status, text[:200])
                return {}
    except Exception:
        _LOGGER.exception("Mosquitto info request failed")
        return {}


async def stop_mosquitto() -> bool:
    return await _addon_action("stop")


async def start_mosquitto() -> bool:
    return await _addon_action("start")


async def _remove_mosquitto_login(username: str) -> str:
    """Remove a login entry from Mosquitto's options.

    Returns:
        "removed"   — entry found and removed
        "not_found" — entry was not present, no change
        "error"     — Supervisor API call failed
    """
    if not SUPERVISOR_TOKEN:
        _LOGGER.error("SUPERVISOR_TOKEN is not set")
        return "error"

    info_url = f"{SUPERVISOR_URL}/addons/{MOSQUITTO_SLUG}/info"
    options_url = f"{SUPERVISOR_URL}/addons/{MOSQUITTO_SLUG}/options"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                info_url,
                headers=_action_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.error("Could not read Mosquitto info: HTTP %d — %s", resp.status, text[:200])
                    return "error"
                info = await resp.json()

        current_opts = info.get("data", {}).get("options", {})
        logins = list(current_opts.get("logins", []))
        new_logins = [e for e in logins if e.get("username") != username]
        if len(new_logins) == len(logins):
            _LOGGER.info("Login for %s... not found in Mosquitto (nothing to remove)", username[:4])
            return "not_found"

        new_options = {**current_opts, "logins": new_logins}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                options_url,
                json={"options": new_options},
                headers={**_action_headers(), "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    _LOGGER.info("Removed Mosquitto login for %s...", username[:4])
                    return "removed"
                text = await resp.text()
                _LOGGER.error("Failed to save Mosquitto options: HTTP %d — %s", resp.status, text[:200])
                return "error"
    except Exception:
        _LOGGER.exception("remove_mosquitto_login failed")
        return "error"


async def _add_mosquitto_login(username: str, password: str, allow_update: bool = False) -> str:
    """Add a login entry to Mosquitto's options and save.

    An existing login with the same username but a different password is only
    changed when allow_update is True (the caller knows this app created it);
    otherwise it is left alone and "conflict" is returned.

    Returns:
        "added"    — credentials were new and saved successfully
        "exists"   — credentials already present, no change made
        "conflict" — username belongs to a login this app did not create, no change made
        "error"    — Supervisor API call failed
    """
    if not SUPERVISOR_TOKEN:
        _LOGGER.error("SUPERVISOR_TOKEN is not set")
        return "error"

    info_url = f"{SUPERVISOR_URL}/addons/{MOSQUITTO_SLUG}/info"
    options_url = f"{SUPERVISOR_URL}/addons/{MOSQUITTO_SLUG}/options"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                info_url,
                headers=_action_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.error("Could not read Mosquitto info: HTTP %d — %s", resp.status, text[:200])
                    return "error"
                info = await resp.json()

        current_opts = info.get("data", {}).get("options", {})
        logins = list(current_opts.get("logins", []))
        for entry in logins:
            if entry.get("username") == username:
                if entry.get("password") == password:
                    _LOGGER.info("Login for %s... already exists in Mosquitto (unchanged)", username[:4])
                    return "exists"
                if not allow_update:
                    _LOGGER.warning("Mosquitto login %s... exists and was not created by Petlibro Local, leaving it alone", username[:4])
                    return "conflict"
                # Same username, different password, and it is ours — update it.
                entry["password"] = password
                _LOGGER.info("Login for %s... exists in Mosquitto — updating password", username[:4])
                new_options = {**current_opts, "logins": logins}
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        options_url,
                        json={"options": new_options},
                        headers={**_action_headers(), "Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            return "added"
                        text = await resp.text()
                        _LOGGER.error("Failed to update Mosquitto password for %s...: HTTP %d — %s", username[:4], resp.status, text[:200])
                        return "error"

        logins.append({"username": username, "password": password})
        new_options = {**current_opts, "logins": logins}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                options_url,
                json={"options": new_options},
                headers={**_action_headers(), "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    _LOGGER.info("Added Mosquitto login for %s...", username[:4])
                    return "added"
                text = await resp.text()
                _LOGGER.error("Failed to save Mosquitto options: HTTP %d — %s", resp.status, text[:200])
                return "error"
    except Exception:
        _LOGGER.exception("add_mosquitto_login failed")
        return "error"


# ── Password-file backend ──────────────────────────────────────────────────
# Standalone stack: Mosquitto runs in its own container with `password_file` on a volume
# shared with this app. Logins are edited in that file and Mosquitto re-reads it on SIGHUP,
# without a restart (a restart reboots the PetLibro devices). The app finds the Mosquitto
# process through /proc, which needs the containers to share a PID namespace
# (`pid: "service:mosquitto"` in the compose file).

# The "mosquitto" user of the eclipse-mosquitto image. Mosquitto drops to it after start and
# can only read a password file it owns (or a world-readable one, with a warning).
_MOSQUITTO_UID = 1883
_MOSQUITTO_GID = 1883

# PBKDF2 parameters accepted by every Mosquitto 2.x. The salt must be 12 bytes: 2.0 refuses
# longer ones (2.1's own mosquitto_passwd makes 64-byte salts). The iteration count is read
# from each entry; 20000 is the 2.1 default (2.0 used 101).
_HASH_ITERATIONS = 20000
_HASH_SALT_BYTES = 12
_HASH_BYTES = 64


def passwd_backend_active() -> bool:
    return bool(os.environ.get("MOSQUITTO_PASSWD_FILE", "").strip())


def _passwd_path() -> str:
    return os.environ.get("MOSQUITTO_PASSWD_FILE", "").strip()


def _hash_password(password: str) -> str:
    """Mosquitto 2.x password hash: $7$<iterations>$<base64 salt>$<base64 PBKDF2-HMAC-SHA512>."""
    salt = secrets.token_bytes(_HASH_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, _HASH_ITERATIONS, _HASH_BYTES)
    return "$7$%d$%s$%s" % (
        _HASH_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def _verify_password(password: str, stored: str) -> bool:
    """True if `stored` (a $7$ entry from any Mosquitto 2.x) matches the password."""
    parts = stored.split("$")
    if len(parts) != 5 or parts[0] != "" or parts[1] != "7":
        return False
    try:
        iterations = int(parts[2])
        salt = base64.b64decode(parts[3], validate=True)
        expected = base64.b64decode(parts[4], validate=True)
    except ValueError:
        return False
    if iterations < 1 or not expected:
        return False
    actual = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations, len(expected))
    return hmac.compare_digest(actual, expected)


def _entry_name(line: str):
    """Username of a "user:hash" line, None for blank, comment or malformed lines."""
    name, sep, _ = line.partition(":")
    return name if sep else None


def _read_passwd_lines(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def _write_passwd_lines(path: str, lines: list) -> None:
    """Replace the file atomically, keeping its owner and mode so Mosquitto can still read it."""
    try:
        st = os.stat(path)
        uid, gid, mode = st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
    except FileNotFoundError:
        uid, gid, mode = _MOSQUITTO_UID, _MOSQUITTO_GID, 0o600
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".passwd.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        try:
            os.chown(tmp, uid, gid)
        except OSError:
            _LOGGER.warning(
                "Could not set owner %d:%d on the Mosquitto password file (not running as root?) "
                "— Mosquitto may not be able to read it", uid, gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _reload_mosquitto() -> bool:
    """SIGHUP every process named mosquitto so it re-reads the password file."""
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", "r") as f:
                if f.read().strip() == "mosquitto":
                    pids.append(int(entry))
        except OSError:
            continue
    if not pids:
        _LOGGER.warning(
            "Mosquitto process not found — the password file was updated but Mosquitto was not "
            "reloaded. Check that the containers share a PID namespace (pid: \"service:mosquitto\"), "
            "or restart Mosquitto")
        return False
    reloaded = False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGHUP)
            _LOGGER.info("Sent SIGHUP to Mosquitto (pid %d) to reload logins", pid)
            reloaded = True
        except OSError:
            _LOGGER.warning("Could not send SIGHUP to Mosquitto (pid %d)", pid, exc_info=True)
    return reloaded


def _passwd_add(username: str, password: str, allow_update: bool) -> str:
    if not username or ":" in username or "\n" in username or "\r" in username:
        _LOGGER.error("Invalid Mosquitto username (empty, or contains ':' or a line break)")
        return "error"
    path = _passwd_path()
    lines = _read_passwd_lines(path)
    for i, line in enumerate(lines):
        if _entry_name(line) != username:
            continue
        if _verify_password(password, line.partition(":")[2]):
            _LOGGER.info("Login for %s... already exists in Mosquitto (unchanged)", username[:4])
            return "exists"
        if not allow_update:
            _LOGGER.warning("Mosquitto login %s... exists and was not created by Petlibro Local, leaving it alone", username[:4])
            return "conflict"
        _LOGGER.info("Login for %s... exists in Mosquitto — updating password", username[:4])
        lines[i] = f"{username}:{_hash_password(password)}"
        break
    else:
        lines.append(f"{username}:{_hash_password(password)}")
    _write_passwd_lines(path, lines)
    _LOGGER.info("Saved Mosquitto login for %s...", username[:4])
    _reload_mosquitto()
    return "added"


def _passwd_remove(username: str) -> str:
    path = _passwd_path()
    lines = _read_passwd_lines(path)
    kept = [line for line in lines if _entry_name(line) != username]
    if len(kept) == len(lines):
        _LOGGER.info("Login for %s... not found in Mosquitto (nothing to remove)", username[:4])
        return "not_found"
    _write_passwd_lines(path, kept)
    _LOGGER.info("Removed Mosquitto login for %s...", username[:4])
    _reload_mosquitto()
    return "removed"


async def _add_passwd_login(username: str, password: str, allow_update: bool) -> str:
    try:
        return await asyncio.to_thread(_passwd_add, username, password, allow_update)
    except Exception:
        _LOGGER.exception("add_mosquitto_login failed (password file %s)", _passwd_path())
        return "error"


async def _remove_passwd_login(username: str) -> str:
    try:
        return await asyncio.to_thread(_passwd_remove, username)
    except Exception:
        _LOGGER.exception("remove_mosquitto_login failed (password file %s)", _passwd_path())
        return "error"


# Both functions read Mosquitto's login list, change it and write it back. Two requests at
# once (add, delete, resave) could overwrite each other's change, so they take turns.
_login_lock = asyncio.Lock()


async def remove_mosquitto_login(username: str) -> str:
    async with _login_lock:
        if passwd_backend_active():
            return await _remove_passwd_login(username)
        return await _remove_mosquitto_login(username)


async def add_mosquitto_login(username: str, password: str, allow_update: bool = False) -> str:
    async with _login_lock:
        if passwd_backend_active():
            return await _add_passwd_login(username, password, allow_update)
        return await _add_mosquitto_login(username, password, allow_update)


async def ensure_app_login(username: str, password: str) -> str:
    """Put the app's own MQTT_USER/MQTT_PASS login in the password file (startup, standalone stack).

    The environment is authoritative, so a different password already stored is replaced.
    """
    if not username or not password:
        _LOGGER.warning("MQTT_USER / MQTT_PASS not set — not adding the app's own login to the Mosquitto password file")
        return "error"
    async with _login_lock:
        return await _add_passwd_login(username, password, allow_update=True)
