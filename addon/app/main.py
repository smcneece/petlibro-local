"""Petlibro Local -- aiohttp web server and main orchestrator."""

import asyncio
import json
import logging
import os
import re
import uuid

from aiohttp import web

import devices
import ha_api
import mosquitto
import mqtt_broker
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
_LOGGER = logging.getLogger(__name__)

VERSION = "2026.09.1"

# Credential capture state
_capture_state: dict = {"status": "idle", "result": {}}


# ── Helpers ────────────────────────────────────────────────────────────────

def _build_html(base: str) -> str:
    with open("/app/index.html", "r") as f:
        template = f.read()
    return template.replace("{{BASE}}", base).replace("{{VERSION}}", VERSION)


def _devices_with_state() -> list:
    stored = storage.get_devices()
    pets = storage.get_pets()
    all_states = devices.get_all_states()

    result = []
    for serial, cfg in stored.items():
        state = all_states.get(serial, {})
        device_pets = [pets[pid] for pid in cfg.get("pet_ids", []) if pid in pets]
        if cfg.get("device_type") == "dockstream_rfid":
            pet_intake = storage.get_fountain_pet_intake_today(serial)
            device_pets = [
                {**p, **pet_intake.get(p["id"], {"grams": 0, "visits": 0, "duration_secs": 0})}
                for p in device_pets
            ]
        intake_today = storage.get_intake_today(serial)
        feeding_plans = storage.get_device_feeding_plans(serial)
        result.append({**cfg, **state, "pets": device_pets, "intake_today_grams": intake_today, "feeding_plans": feeding_plans})
    return result


# ── Route handlers ─────────────────────────────────────────────────────────

async def handle_index(request):
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    return web.Response(
        text=_build_html(ingress_path),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def handle_icon(request):
    return web.FileResponse("/app/icon.png")


async def handle_locale(request):
    lang = request.match_info["lang"]
    if not lang.isalpha() or len(lang) > 8:
        return web.Response(status=404)
    path = f"/app/locales/{lang}.json"
    if not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def handle_static(request):
    name = request.match_info["name"]
    path = f"/app/{name}"
    if not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def handle_device_image(request):
    name = request.match_info["name"]
    if "/" in name or "\\" in name or name.startswith("."):
        return web.Response(status=400)
    for base_dir in ("/data/images", "/app/images"):
        for ext in ("jpg", "jpeg", "png", "webp"):
            path = f"{base_dir}/{name}.{ext}"
            if os.path.exists(path):
                return web.FileResponse(path)
    return web.Response(status=404)


async def handle_audio_serve(request):
    """Serve a custom feed sound. Checked in /data/audio first (user uploaded
    or recorded) then /app/audio (bundled with the add-on), same fallback
    pattern as handle_device_image. Unauthenticated by design, same as
    /images -- this needs to be fetchable by the feeder itself over plain
    HTTP, not just by the logged-in ingress session."""
    name = request.match_info["name"]
    if "/" in name or "\\" in name or name.startswith("."):
        return web.Response(status=400)
    # Accept the name with or without ".aac" -- but always build the pushed
    # audioUrl (see handle_api_device_push_audio) with the extension included,
    # since every real PetLibro audioUrl we've captured ends in .aac and the
    # feeder's firmware may key off that when deciding how to fetch/decode it.
    stem = name[:-4] if name.lower().endswith(".aac") else name
    for base_dir in ("/data/audio", "/app/audio"):
        path = f"{base_dir}/{stem}.aac"
        if os.path.exists(path):
            # aiohttp's normal access log is silenced (see logging setup at
            # top of file) to cut noise from routine polling, so log fetches
            # of this route explicitly -- it's the only direct evidence we
            # get that a feeder actually requested a custom sound.
            _LOGGER.info("Audio fetch: %s from %s", name, request.remote)
            return web.FileResponse(path, headers={"Content-Type": "audio/aac"})
    _LOGGER.info("Audio fetch (not found): %s from %s", name, request.remote)
    return web.Response(status=404)


async def handle_api_audio_list(request):
    """List available custom feed sound names (without extension), user
    uploaded/recorded ones first, then bundled ones not already overridden."""
    import glob as _glob
    names = []
    for base_dir in ("/data/audio", "/app/audio"):
        for f in sorted(_glob.glob(f"{base_dir}/*.aac")):
            name = os.path.splitext(os.path.basename(f))[0]
            if name not in names:
                names.append(name)
    return web.json_response(names)


_AUDIO_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


async def handle_api_audio_upload(request):
    """Accept an uploaded or recorded sound clip in any ffmpeg-readable format
    and transcode it to AAC/ADTS, the format the feeder expects. Stored under
    a user-chosen name so several clips (breakfast, lunch, dinner, etc.) can
    coexist. multipart fields: name (text), file (binary)."""
    reader = await request.multipart()
    name = None
    tmp_in = f"/tmp/audio_upload_{uuid.uuid4().hex}"
    try:
        async for field in reader:
            if field.name == "name":
                name = (await field.text()).strip().lower()
            elif field.name == "file":
                with open(tmp_in, "wb") as f:
                    while True:
                        chunk = await field.read_chunk()
                        if not chunk:
                            break
                        f.write(chunk)
        if not name or not _AUDIO_NAME_RE.match(name):
            return web.Response(status=400, text="name must be 1-40 lowercase letters, digits, _ or -")
        if not os.path.exists(tmp_in):
            return web.Response(status=400, text="file required")

        os.makedirs("/data/audio", exist_ok=True)
        out_path = f"/data/audio/{name}.aac"
        # -ar/-ac pinned to match PetLibro's own come_to_eat.aac exactly
        # (44100 Hz, stereo, confirmed by parsing its real ADTS header).
        # Without forcing these, the output inherits whatever rate the input
        # happened to use (e.g. 48000 Hz from Windows' Sound Recorder), and
        # if the feeder's decoder doesn't actually read the rate back out of
        # the ADTS header, playback speed drifts with it -- a 48000-sourced
        # file played through a fixed-44100 pipeline runs about 9% fast,
        # matching the "sounds sped up" symptom seen with an M4A upload.
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", tmp_in, "-vn", "-acodec", "aac", "-b:a", "96k",
            "-ar", "44100", "-ac", "2", "-f", "adts", out_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            _LOGGER.warning("Audio transcode failed for %s: %s", name, stderr.decode(errors="replace")[-500:])
            return web.Response(status=400, text="Could not read that audio file")
        return web.json_response({"name": name})
    finally:
        if os.path.exists(tmp_in):
            os.remove(tmp_in)


async def handle_api_audio_delete(request):
    """Delete a stored custom sound. Only removes user uploaded/recorded
    files under /data/audio -- a bundled /app/audio default can't be deleted
    this way (there isn't one bundled yet, but keeping the boundary clear)."""
    name = request.match_info["name"]
    if not _AUDIO_NAME_RE.match(name):
        return web.Response(status=400, text="Bad request")
    path = f"/data/audio/{name}.aac"
    if os.path.exists(path):
        os.remove(path)
        return web.json_response({"status": "ok"})
    return web.Response(status=404)


async def handle_api_device_push_audio(request):
    """Push a stored custom sound to a feeder as its audioUrl, using the
    configured Local Audio Base URL (a plain LAN address, not the ingress
    path, since the feeder itself has to fetch this, not a logged-in browser)."""
    serial = request.match_info["serial"]
    try:
        data = await request.json()
        name = (data.get("name") or "").strip().lower()
    except Exception:
        return web.Response(status=400, text="Bad request")
    if not name or not _AUDIO_NAME_RE.match(name):
        return web.Response(status=400, text="name required")

    base_url = storage.get_settings().get("local_audio_base_url", "").strip()
    if not base_url:
        return web.Response(status=400, text="Set Local Audio Base URL in Settings first")
    if not any(os.path.exists(f"{d}/{name}.aac") for d in ("/data/audio", "/app/audio")):
        return web.Response(status=404, text="Sound not found")

    audio_url = f"{base_url.rstrip('/')}/audio/{name}.aac"
    ok = await devices.send_command(serial, {"audioUrl": audio_url, "enableAudio": True})
    if not ok:
        return web.Response(status=400, text="Failed to send command")
    return web.json_response({"status": "ok", "audio_url": audio_url})


async def handle_locales_available(request):
    import glob as _glob
    files = _glob.glob("/app/locales/*.json")
    codes = sorted(
        os.path.basename(f).replace(".json", "")
        for f in files
        if os.path.basename(f) != "en.json"
    )
    return web.json_response(["en"] + codes)


async def handle_api_devices_get(request):
    return web.json_response(_devices_with_state())


async def handle_api_device_post(request):
    serial = request.match_info["serial"]
    try:
        data = await request.json()
        device = storage.save_device(serial, data)
        model = device.get("model", "")
        device_type = device.get("device_type", "")
        if model:
            devices.register_device(serial, model, device_type)
        if device_type == "one_rfid" and ("display_text" in data or "display_icon" in data):
            asyncio.ensure_future(devices.send_display(
                serial,
                device.get("display_text", ""),
                device.get("display_icon", 0),
            ))
        asyncio.ensure_future(devices.republish_ha_discovery(serial))
        return web.json_response(device)
    except Exception:
        _LOGGER.exception("Failed to save device %s...", serial[:6])
        return web.Response(status=400, text="Bad request")


async def handle_api_device_add(request):
    try:
        data = await request.json()
        serial = data.get("serial", "").strip().upper()
        if not serial:
            return web.Response(status=400, text="serial required")
        device = storage.save_device(serial, data)
        model = device.get("model", "")
        device_type = device.get("device_type", "")
        if model:
            devices.register_device(serial, model, device_type)
        # Ensure device credentials are in Mosquitto.
        # add_mosquitto_login is idempotent — returns True immediately if already present.
        # If credentials were newly added we restart Mosquitto so it picks them up.
        mqtt_user = device.get("mqtt_user", "")
        mqtt_pass = device.get("mqtt_pass", "")
        if mqtt_user and mqtt_pass:
            login_result = await mosquitto.add_mosquitto_login(mqtt_user, mqtt_pass)
            if login_result == "added":
                # Newly added — restart Mosquitto so it picks up the new credentials.
                # Suppress offline alerts briefly while devices reconnect.
                _LOGGER.info("New Mosquitto login for %s... — restarting broker", mqtt_user[:4])
                devices.suppress_alerts(True)
                await mosquitto.stop_mosquitto()
                await asyncio.sleep(2)
                await mosquitto.start_mosquitto()
                async def _lift_suppress():
                    await asyncio.sleep(30)
                    devices.suppress_alerts(False)
                asyncio.ensure_future(_lift_suppress())
            elif login_result == "error":
                _LOGGER.error(
                    "Device %s... added to storage but Mosquitto login save failed — "
                    "device may not authenticate until credentials are re-added manually",
                    serial[:6],
                )
        return web.json_response(device)
    except Exception:
        _LOGGER.exception("Failed to add device")
        return web.Response(status=400, text="Bad request")


async def handle_api_device_delete(request):
    serial = request.match_info["serial"]
    remove_creds = request.query.get("remove_creds") == "1"

    deleted_cfg   = storage.get_devices().get(serial, {})
    deleted_state = devices.get_device_state(serial)
    mqtt_user = deleted_cfg.get("mqtt_user", "") if remove_creds else ""
    mqtt_pass = deleted_cfg.get("mqtt_pass", "") if remove_creds else ""

    await devices.retract_ha_discovery(serial, deleted_cfg, deleted_state)
    storage.delete_device(serial)
    devices.unregister_device(serial)

    if remove_creds and mqtt_user:
        # Check if any remaining device shares this username.
        sharing_device = next(
            (cfg for s, cfg in storage.get_devices().items() if cfg.get("mqtt_user") == mqtt_user),
            None,
        )
        needs_restart = False
        if sharing_device is None:
            # No other device uses this username — safe to remove entirely.
            result = await mosquitto.remove_mosquitto_login(mqtt_user)
            if result == "removed":
                _LOGGER.info("Removed Mosquitto login for %s... after device delete", mqtt_user[:4])
                needs_restart = True
            elif result == "error":
                _LOGGER.error("Failed to remove Mosquitto login for %s... after device delete", mqtt_user[:4])
        elif sharing_device.get("mqtt_pass") != mqtt_pass:
            # Same username, different password — restore the remaining device's password.
            remaining_pass = sharing_device.get("mqtt_pass", "")
            result = await mosquitto.add_mosquitto_login(mqtt_user, remaining_pass)
            if result in ("added", "exists"):
                _LOGGER.info("Restored Mosquitto password for shared user %s... after device delete", mqtt_user[:4])
                needs_restart = True
            else:
                _LOGGER.error("Failed to restore Mosquitto password for %s... after device delete", mqtt_user[:4])
        else:
            _LOGGER.info("Mosquitto login %s... retained — still used by another device with same credentials", mqtt_user[:4])

        if needs_restart:
            devices.suppress_alerts(True)
            await mosquitto.stop_mosquitto()
            await asyncio.sleep(2)
            await mosquitto.start_mosquitto()
            async def _lift():
                await asyncio.sleep(30)
                devices.suppress_alerts(False)
            asyncio.ensure_future(_lift())

    return web.json_response({"status": "ok"})


async def handle_api_device_resave_creds(request):
    serial = request.match_info["serial"]
    cfg = storage.get_devices().get(serial, {})
    mqtt_user = cfg.get("mqtt_user", "")
    mqtt_pass = cfg.get("mqtt_pass", "")
    if not mqtt_user or not mqtt_pass:
        return web.json_response({"status": "error", "detail": "No credentials stored for this device"}, status=400)
    result = await mosquitto.add_mosquitto_login(mqtt_user, mqtt_pass)
    if result == "error":
        return web.json_response({"status": "error", "detail": "Supervisor API call failed — check add-on logs"}, status=500)
    if result == "added":
        devices.suppress_alerts(True)
        await mosquitto.stop_mosquitto()
        await asyncio.sleep(2)
        await mosquitto.start_mosquitto()
        async def _lift():
            await asyncio.sleep(30)
            devices.suppress_alerts(False)
        asyncio.ensure_future(_lift())
    _LOGGER.info("Credentials re-saved for %s... (%s)", serial[:6], result)
    return web.json_response({"status": "ok", "result": result})


async def handle_api_device_command(request):
    serial = request.match_info["serial"]
    try:
        payload = await request.json()
        # Route through handle_ha_command so underscore-prefixed keys
        # (_display_frame, _display_text, _feed_now, etc.) are handled correctly.
        # Normal command dicts fall through to send_command inside that function.
        await devices.handle_ha_command(serial, payload)
        return web.json_response({"status": "ok"})
    except Exception:
        _LOGGER.exception("Command failed for %s...", serial[:6])
        return web.Response(status=400, text="Bad request")


async def handle_api_icons_get(request):
    return web.json_response(storage.get_custom_icons())


def _republish_feeder_icons():
    """Re-announce HA Discovery for all feeder devices so the icon select options stay in sync."""
    for serial, cfg in storage.get_devices().items():
        if cfg.get("device_type") == "one_rfid":
            asyncio.ensure_future(devices.republish_ha_discovery(serial))


async def handle_api_icons_post(request):
    try:
        body = await request.json()
        name = str(body.get("name", "Icon"))[:32]
        rows = body.get("rows", [0, 0, 0, 0, 0])
        if not isinstance(rows, list) or len(rows) != 5:
            return web.Response(status=400, text="rows must be a list of 5 integers")
        icon_id = storage.save_custom_icon(name, rows)
        _republish_feeder_icons()
        return web.json_response({"id": icon_id})
    except Exception:
        _LOGGER.exception("Failed to save icon")
        return web.Response(status=400, text="Bad request")


async def handle_api_icon_put(request):
    try:
        icon_id = int(request.match_info["id"])
        body = await request.json()
        name = str(body.get("name", "Icon"))[:32]
        rows = body.get("rows", [0, 0, 0, 0, 0])
        ok = storage.update_custom_icon(icon_id, name, rows)
        if ok:
            _republish_feeder_icons()
        return web.json_response({"ok": ok})
    except Exception:
        _LOGGER.exception("Failed to update icon")
        return web.Response(status=400, text="Bad request")


async def handle_api_icon_delete(request):
    icon_id = int(request.match_info["id"])
    ok = storage.delete_custom_icon(icon_id)
    if ok:
        _republish_feeder_icons()
    return web.json_response({"ok": ok})


async def handle_api_pets_get(request):
    return web.json_response(list(storage.get_pets().values()))


async def handle_api_pet_add(request):
    try:
        data = await request.json()
        pet_id = str(uuid.uuid4())
        pet = storage.save_pet(pet_id, data)
        return web.json_response(pet)
    except Exception:
        _LOGGER.exception("Failed to add pet")
        return web.Response(status=400, text="Bad request")


async def handle_api_pet_post(request):
    pet_id = request.match_info["pet_id"]
    try:
        data = await request.json()
        pet = storage.save_pet(pet_id, data)
        return web.json_response(pet)
    except Exception:
        _LOGGER.exception("Failed to update pet %s", pet_id)
        return web.Response(status=400, text="Bad request")


async def handle_api_pet_delete(request):
    pet_id = request.match_info["pet_id"]
    storage.delete_pet(pet_id)
    return web.json_response({"status": "ok"})


async def handle_api_settings_get(request):
    return web.json_response(storage.get_settings())


async def handle_api_settings_post(request):
    try:
        data = await request.json()
        result = storage.save_settings(data)
        settings = storage.get_settings()
        devices.configure(
            settings["mqtt_host"],
            settings["mqtt_port"],
            settings["mqtt_user"],
            settings["mqtt_pass"],
        )
        _LOGGER.info("Settings saved — testing MQTT connection")
        mqtt_ok = await devices.test_connection(
            settings["mqtt_host"],
            settings["mqtt_port"],
            settings["mqtt_user"],
            settings["mqtt_pass"],
        )
        if mqtt_ok:
            _LOGGER.info("MQTT connection test: OK")
        else:
            _LOGGER.warning("MQTT connection test: FAILED — check host, port, username, password")
        return web.json_response({**result, "mqtt_ok": mqtt_ok})
    except Exception:
        _LOGGER.exception("Failed to save settings")
        return web.Response(status=400, text="Bad request")


async def handle_api_capture_start(request):
    global _capture_state
    if _capture_state["status"] == "capturing":
        return web.json_response({"status": "capturing"})

    _capture_state = {"status": "capturing", "result": {}}

    async def _run_capture():
        global _capture_state
        mosquitto_was_stopped = False
        try:
            devices.suppress_alerts(True)
            _LOGGER.info("Stopping Mosquitto for credential capture")
            stopped = await mosquitto.stop_mosquitto()
            if not stopped:
                _LOGGER.error("Failed to stop Mosquitto — capture aborted")
                _capture_state = {"status": "error", "result": {}, "detail": "Could not stop Mosquitto. Check hassio_role: manager is set in config.yaml and rebuild the add-on."}
                return
            mosquitto_was_stopped = True
            # Give Mosquitto a moment to release port 1883 before the mini broker tries to bind
            await asyncio.sleep(3)

            creds = await mqtt_broker.capture_credentials(timeout_seconds=60)

            if creds:
                _LOGGER.info("Credentials captured for client %s...", creds.get("client_id", "")[:8])
                # Add device credentials to Mosquitto before restarting so it can authenticate
                username = creds.get("username", "")
                password = creds.get("password", "")
                login_result = "skipped"
                if username and password:
                    login_result = await mosquitto.add_mosquitto_login(username, password)
                    if login_result == "error":
                        _LOGGER.error(
                            "Failed to save credentials for %s... to Mosquitto — "
                            "device may not authenticate until credentials are re-added manually",
                            username[:4],
                        )
                _capture_state = {"status": "captured", "result": creds, "login_saved": login_result in ("added", "exists")}
            else:
                _capture_state = {"status": "timeout", "result": {}}
                _LOGGER.warning("Credential capture timed out — no Petlibro device connected")
        except Exception:
            _LOGGER.exception("Capture flow error")
            _capture_state = {"status": "error", "result": {}}
        finally:
            if mosquitto_was_stopped:
                _LOGGER.info("Restarting Mosquitto")
                await mosquitto.start_mosquitto()
            devices.suppress_alerts(False)

    asyncio.ensure_future(_run_capture())
    return web.json_response({"status": "capturing"})


async def handle_api_capture_status(request):
    return web.json_response(_capture_state)


async def handle_api_capture_reset(request):
    global _capture_state
    _capture_state = {"status": "idle", "result": {}}
    return web.json_response({"status": "ok"})


async def handle_api_about(request):
    ha_version = await ha_api.get_ha_version()
    return web.json_response({
        "version": VERSION,
        "ha_version": ha_version,
    })


async def handle_api_ha_areas(request):
    areas = await ha_api.get_ha_areas()
    return web.json_response(areas)


async def handle_api_ha_notify_services(request):
    services = await ha_api.get_notify_services()
    return web.json_response(services)


async def handle_api_ha_mobile_targets(request):
    services = await ha_api.get_notify_services()
    targets = await ha_api.get_mobile_notify_targets(services)
    return web.json_response(targets)


async def handle_api_diag_mosquitto(request):
    """Diagnostic: test Supervisor API access and Mosquitto info. Safe read-only call."""
    from ha_config import SUPERVISOR_TOKEN
    info = await mosquitto.get_addon_info()
    return web.json_response({
        "token_set": bool(SUPERVISOR_TOKEN),
        "mosquitto_info": info,
    })


async def handle_api_diag_debug_capture(request):
    """Diagnostic: return the rolling buffer of raw dl/ MQTT traffic as a
    downloadable log file, including messages from devices this app doesn't
    recognize. Populated continuously in the background (see devices._mqtt_loop),
    not captured on demand, since devices only chirp occasionally. Used by the
    Help/About "Download Debug Capture" button."""
    import time
    messages = devices.get_raw_capture()
    unrecognized_count = sum(1 for m in messages if not m["recognized"])

    lines = [
        f"Petlibro Local debug capture -- {len(messages)} message(s) buffered, {unrecognized_count} from unrecognized device(s)",
    ]
    if messages:
        span_start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(messages[0]["ts"]))
        span_end   = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(messages[-1]["ts"]))
        lines.append(f"Covering {span_start} through {span_end}")
    lines.append("")
    for m in messages:
        ts_str  = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["ts"]))
        tag     = "" if m["recognized"] else "[UNRECOGNIZED] "
        payload = devices.redact_payload(m["payload"])
        lines.append(f"[{ts_str}] {tag}{m['topic']}: {payload}")
    body = "\n".join(lines) + "\n"

    filename = f"petlibro-debug-capture-{time.strftime('%Y%m%dT%H%M%S')}.log"
    return web.Response(
        text=body,
        content_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def handle_api_diag_ntp_log(request):
    """Diagnostic: return the dedicated NTP request/response/push log
    (see devices._ntp_logger). Lives at /data/ntp_debug.log inside this
    add-on's own container, which isn't visible from the Terminal add-on
    or any other add-on's shell, each add-on gets its own private /data
    volume, so this is served over HTTP instead. Temporary, for the issue #5
    investigation, not linked from any permanent UI yet."""
    import time
    path = "/data/ntp_debug.log"
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
    except FileNotFoundError:
        body = "(no ntp_debug.log yet -- no NTP activity logged since this add-on last started)\n"
    filename = f"ntp-debug-{time.strftime('%Y%m%dT%H%M%S')}.log"
    return web.Response(
        text=body,
        content_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def handle_api_device_intake(request):
    serial = request.match_info["serial"]
    days = min(int(request.query.get("days", 7)), 30)
    history = storage.get_intake_history(serial, days)
    return web.json_response(history)


async def handle_api_feeder_log(request):
    serial = request.match_info["serial"]
    limit = min(int(request.query.get("limit", 60)), 200)
    return web.json_response(storage.get_feeder_log(serial, limit))


async def handle_api_fountain_log(request):
    serial = request.match_info["serial"]
    limit = min(int(request.query.get("limit", 60)), 200)
    return web.json_response(storage.get_fountain_log(serial, limit))


async def handle_api_pet_log(request):
    """Return feeder activity events for a specific pet, newest first."""
    pet_id = request.match_info["pet_id"]
    limit  = min(int(request.query.get("limit", 60)), 200)

    pets = storage.get_pets()
    pet = pets.get(pet_id, {})
    pet_rfid = str(pet.get("rfid_tag")) if pet.get("rfid_tag") else None

    events = []
    devices = storage.get_devices()
    for serial, cfg in devices.items():
        pet_ids = cfg.get("pet_ids", [])
        if pet_id not in pet_ids:
            continue
        sole_pet = (len(pet_ids) == 1 and pet_id in pet_ids)
        log = storage.get_feeder_log(serial, limit)
        for entry in log:
            entry_rfid = str(entry.get("rfid_tag")) if entry.get("rfid_tag") else None
            if (
                entry.get("pet_id") == pet_id
                or (pet_rfid and entry_rfid == pet_rfid)
                or (sole_pet and entry.get("type") == "pet_eating")
            ):
                events.append({**entry, "serial": serial,
                                "device_name": cfg.get("name") or serial[:8]})

        if cfg.get("device_type", "").startswith("dockstream"):
            flog = storage.get_fountain_log(serial, limit)
            for entry in flog:
                entry_rfid = str(entry.get("rfid_tag")) if entry.get("rfid_tag") else None
                # Only RFID-confirmed drinks count as this pet's activity --
                # same rule as the feeder's pet_eating entries, which only
                # ever exist when an RFID scan triggered them in the first
                # place. A plain weight-drop "drink" with no rfid_tag is
                # never attributed, even in a single-pet household.
                if not entry_rfid:
                    continue
                if (
                    entry.get("pet_id") == pet_id
                    or (pet_rfid and entry_rfid == pet_rfid)
                    or sole_pet
                ):
                    events.append({**entry, "serial": serial,
                                    "device_name": cfg.get("name") or serial[:8]})
    events.sort(key=lambda e: e.get("ts", 0), reverse=True)
    return web.json_response(events[:limit])


async def handle_api_pet_image_upload(request):
    pet_id = request.match_info["pet_id"]
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "image":
            return web.Response(status=400, text="image field required")
        content_type = field.headers.get("Content-Type", "image/png")
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(content_type, "png")
        os.makedirs("/data/images", exist_ok=True)
        img_name = f"pet_{pet_id}"
        for old_ext in ("jpg", "jpeg", "png", "webp"):
            old_path = f"/data/images/{img_name}.{old_ext}"
            if os.path.exists(old_path):
                os.remove(old_path)
        path = f"/data/images/{img_name}.{ext}"
        with open(path, "wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
        image_url = f"{ingress_path}/images/{img_name}"
        storage.save_pet(pet_id, {"image_url": image_url})
        _LOGGER.info("Pet image saved for %s", pet_id[:8])
        return web.json_response({"image_url": image_url})
    except Exception:
        _LOGGER.exception("Pet image upload failed for %s", pet_id[:8])
        return web.Response(status=400, text="Upload failed")


async def handle_api_feeding_plans_get(request):
    serial = request.match_info["serial"]
    return web.json_response(storage.get_device_feeding_plans(serial))


async def handle_api_feeding_plans_post(request):
    serial = request.match_info["serial"]
    try:
        plans = await request.json()
        if not isinstance(plans, list):
            return web.Response(status=400, text="Expected a JSON array")
        storage.save_device_feeding_plans(serial, plans)
        ok = await devices.send_feeding_plans(serial, plans)
        asyncio.ensure_future(devices.republish_ha_discovery(serial))
        return web.json_response({"status": "ok", "mqtt": ok})
    except Exception:
        _LOGGER.exception("Feeding plans update failed for %s...", serial[:6])
        return web.Response(status=400, text="Bad request")


async def handle_api_device_image_upload(request):
    serial = request.match_info["serial"]
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "image":
            return web.Response(status=400, text="image field required")
        content_type = field.headers.get("Content-Type", "image/jpeg")
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")
        os.makedirs("/data/images", exist_ok=True)
        img_name = f"custom_{serial}"
        # Remove old custom image in any format
        for old_ext in ("jpg", "jpeg", "png", "webp"):
            old_path = f"/data/images/{img_name}.{old_ext}"
            if os.path.exists(old_path):
                os.remove(old_path)
        path = f"/data/images/{img_name}.{ext}"
        with open(path, "wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
        image_url = f"{ingress_path}/images/{img_name}"
        storage.save_device(serial, {"image_url": image_url})
        _LOGGER.info("Custom image saved for %s...", serial[:6])
        return web.json_response({"image_url": image_url})
    except Exception:
        _LOGGER.exception("Image upload failed for %s...", serial[:6])
        return web.Response(status=400, text="Upload failed")


# ── Startup ────────────────────────────────────────────────────────────────

async def on_startup(app):
    settings = storage.get_settings()
    devices.configure(
        settings["mqtt_host"],
        settings["mqtt_port"],
        settings["mqtt_user"],
        settings["mqtt_pass"],
    )
    for serial, cfg in storage.get_devices().items():
        model = cfg.get("model", "")
        device_type = cfg.get("device_type", "")
        if model:
            devices.register_device(serial, model, device_type)
    devices.suppress_alerts(True)
    await devices.start()
    _LOGGER.info("Petlibro Local v%s started", VERSION)

    async def _unsuppress():
        await asyncio.sleep(60)
        devices.suppress_alerts(False)
        _LOGGER.debug("Startup alert suppression lifted")
    asyncio.ensure_future(_unsuppress())


def main():
    app = web.Application()
    app.on_startup.append(on_startup)

    app.router.add_get("/",                              handle_index)
    app.router.add_get("/icon.png",                      handle_icon)
    app.router.add_get("/locales/available",             handle_locales_available)
    app.router.add_get("/locales/{lang}.json",           handle_locale)
    app.router.add_get("/images/{name}",                 handle_device_image)
    app.router.add_get("/audio/{name}",                  handle_audio_serve)
    app.router.add_get(r"/{name:[^/]+\.(js|css)}",       handle_static)

    app.router.add_get("/api/devices",                   handle_api_devices_get)
    app.router.add_post("/api/devices",                  handle_api_device_add)
    app.router.add_post("/api/devices/{serial}",         handle_api_device_post)
    app.router.add_delete("/api/devices/{serial}",        handle_api_device_delete)
    app.router.add_post("/api/devices/{serial}/command",  handle_api_device_command)
    app.router.add_post("/api/devices/{serial}/resave-creds", handle_api_device_resave_creds)
    app.router.add_get("/api/devices/{serial}/intake",     handle_api_device_intake)
    app.router.add_get("/api/devices/{serial}/feeder-log",     handle_api_feeder_log)
    app.router.add_get("/api/devices/{serial}/fountain-log",   handle_api_fountain_log)
    app.router.add_get("/api/devices/{serial}/feeding-plans",  handle_api_feeding_plans_get)
    app.router.add_post("/api/devices/{serial}/feeding-plans", handle_api_feeding_plans_post)
    app.router.add_post("/api/devices/{serial}/image",         handle_api_device_image_upload)
    app.router.add_post("/api/devices/{serial}/push-audio",    handle_api_device_push_audio)

    app.router.add_get("/api/audio",                     handle_api_audio_list)
    app.router.add_post("/api/audio",                    handle_api_audio_upload)
    app.router.add_delete("/api/audio/{name}",           handle_api_audio_delete)

    app.router.add_get("/api/icons",                     handle_api_icons_get)
    app.router.add_post("/api/icons",                    handle_api_icons_post)
    app.router.add_put("/api/icons/{id}",                handle_api_icon_put)
    app.router.add_delete("/api/icons/{id}",             handle_api_icon_delete)

    app.router.add_get("/api/pets",                      handle_api_pets_get)
    app.router.add_post("/api/pets",                     handle_api_pet_add)
    app.router.add_post("/api/pets/{pet_id}",            handle_api_pet_post)
    app.router.add_delete("/api/pets/{pet_id}",          handle_api_pet_delete)
    app.router.add_post("/api/pets/{pet_id}/image",      handle_api_pet_image_upload)
    app.router.add_get("/api/pets/{pet_id}/log",         handle_api_pet_log)

    app.router.add_get("/api/settings",                  handle_api_settings_get)
    app.router.add_post("/api/settings",                 handle_api_settings_post)

    app.router.add_post("/api/capture/start",            handle_api_capture_start)
    app.router.add_get("/api/capture/status",            handle_api_capture_status)
    app.router.add_post("/api/capture/reset",            handle_api_capture_reset)

    app.router.add_get("/api/about",                     handle_api_about)
    app.router.add_get("/api/ha-areas",                  handle_api_ha_areas)
    app.router.add_get("/api/ha-notify-services",        handle_api_ha_notify_services)
    app.router.add_get("/api/ha-mobile-targets",         handle_api_ha_mobile_targets)
    app.router.add_get("/api/diag/mosquitto",            handle_api_diag_mosquitto)
    app.router.add_get("/api/diag/debug-capture",        handle_api_diag_debug_capture)
    app.router.add_get("/api/diag/ntp-log",              handle_api_diag_ntp_log)

    port = int(os.environ.get("INGRESS_PORT", 8765))
    _LOGGER.info("Starting Petlibro Local on port %d", port)
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
