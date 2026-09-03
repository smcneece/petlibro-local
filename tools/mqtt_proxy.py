"""
Petlibro MQTT Proxy - credential and packet capture tool.

Sits between your PetLibro device and the PetLibro cloud, logging all MQTT
traffic. Used to capture the device's MQTT username and password so you can
configure Petlibro Local, and to capture packet logs when adding support for
a new device type.

Requirements: Python 3.9+, no extra packages needed.

Setup overview
--------------
1. Find your device's serial number (visible in the PetLibro app under device
   settings). Set SERIAL_PREFIX below to the FULL serial, not just the first
   few characters. If you own more than one device of the same type, they
   very likely share the same short prefix, and a partial match would proxy
   and log traffic (including MQTT credentials) from whichever one connects
   first, not necessarily the one you meant to capture. A full serial still
   works fine as a prefix match (it just matches itself exactly) and is the
   only way to guarantee you're capturing the right device when you own more
   than one of the same kind.

2. Find the real PetLibro cloud IP. On a machine that is NOT being redirected by
   Pi-hole or a split-DNS rule, run:
       nslookup mqtt.us.petlibro.com 8.8.8.8
   Copy one of the returned IP addresses into CLOUD_HOST below.

3. At your router or firewall, block the target device from reaching the internet
   (or just block outbound port 1883 for that device's IP). This forces it to use
   the DNS redirect instead of any cached connection.

4. In Pi-hole (or your router's DNS), add a local DNS override:
       mqtt.us.petlibro.com -> <IP of the machine running this script>

5. Power-cycle the device, then ASAP run this script and open the PetLibro app. The device will reconnect through
   the proxy to the real cloud — the app will show it as online. The username,
   password, and full packet log will appear in the console and in mqtt_proxy.log.

NOTE: While the DNS redirect is active, any other PetLibro devices on your network
will also be redirected and will temporarily lose Petlibro Local connectivity.
Remove the DNS override when done — the device itself can (and should) remain
blocked from the internet permanently for full local-only operation.

AFTER YOU'RE DONE: point the DNS override back at Petlibro Local and let the
device reconnect, then go back into Petlibro Local and make (and save) some
change to that device's feeding schedule, even a trivial one, to force a
fresh push. While the device was talking to the real cloud during capture,
the cloud may have changed its stored schedule, and there's no other way to
be sure the device is running Petlibro Local's schedule again afterward.

PRIVACY: mqtt_proxy.log contains your device's full serial, username, and password
in plain text. Do not post it publicly or attach it to GitHub issues. If you're
sending the log to someone for a packet-format issue (not a credential-capture
issue), you can delete the "username="/"password=" lines first, they aren't
needed for that and there's no reason to hand them over if they're not required.
"""

import asyncio
import json
import struct
from datetime import datetime

# ── Configure these before running ───────────────────────────────────────────

# Your device's FULL serial number (case-insensitive), not just a short prefix.
# Only connections from a matching device are proxied; everything else is
# dropped. If you own multiple devices of the same type, they likely share
# a short prefix, so use the complete serial to avoid capturing the wrong
# one's traffic and credentials.
SERIAL_PREFIX = "XXXX"

# Real PetLibro cloud IP. Resolve mqtt.us.petlibro.com via 8.8.8.8 and paste here.
# Cannot use the hostname because this machine is receiving the redirected DNS lookup.
CLOUD_HOST = "0.0.0.0"

CLOUD_PORT  = 1883
LISTEN_PORT = 1883

# ─────────────────────────────────────────────────────────────────────────────

import os as _os
LOG_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "mqtt_proxy.log")

_log_fh = open(LOG_FILE, "w", buffering=1)


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _log(msg: str):
    _log_fh.write(msg + "\n")


def _print_and_log(msg: str):
    print(msg, flush=True)
    _log(msg)


def _parse_remaining_length(data: bytes, offset: int) -> tuple[int, int]:
    multiplier, value, i = 1, 0, 0
    while True:
        if offset + i >= len(data):
            raise ValueError("Incomplete")
        byte = data[offset + i]
        value += (byte & 0x7F) * multiplier
        multiplier *= 128
        i += 1
        if not (byte & 0x80):
            break
        if i > 4:
            raise ValueError("Bad length")
    return value, i


def _parse_utf8(data: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from(">H", data, offset)[0]
    return data[offset + 2:offset + 2 + length].decode("utf-8", errors="replace"), offset + 2 + length


def _parse_connect(packet: bytes) -> dict:
    _, c = _parse_remaining_length(packet, 1)
    off = 1 + c
    _, off = _parse_utf8(packet, off)
    off += 1
    flags = packet[off]; off += 3
    client_id, off = _parse_utf8(packet, off)
    username, password = "", ""
    if flags & 0x80:
        username, off = _parse_utf8(packet, off)
    if flags & 0x40:
        password, off = _parse_utf8(packet, off)
    return {"client_id": client_id, "username": username, "password": password}


def _parse_publish(packet: bytes) -> dict:
    qos = (packet[0] >> 1) & 0x03
    _, c = _parse_remaining_length(packet, 1)
    off = 1 + c
    topic, off = _parse_utf8(packet, off)
    if qos > 0:
        off += 2
    payload = packet[off:].decode("utf-8", errors="replace")
    return {"topic": topic, "payload": payload}


def _extract_packets(data: bytes) -> list[tuple[int, bytes]]:
    results = []
    pos = 0
    while pos < len(data):
        if pos + 1 >= len(data):
            break
        pkt_type = (data[pos] >> 4) & 0x0F
        try:
            rem_len, consumed = _parse_remaining_length(data, pos + 1)
        except ValueError:
            break
        total = 1 + consumed + rem_len
        if pos + total > len(data):
            break
        results.append((pkt_type, data[pos:pos + total]))
        pos += total
    return results


def _handle_packets(data: bytes, direction: str):
    for pkt_type, packet in _extract_packets(data):
        if pkt_type == 3:  # PUBLISH
            try:
                p = _parse_publish(packet)
                topic = p["topic"]
                payload_str = p["payload"]
                try:
                    parsed = json.loads(payload_str)
                    pretty = json.dumps(parsed, indent=2)
                except Exception:
                    pretty = payload_str[:200]

                channel = topic.split("/device/")[-1] if "/device/" in topic else topic
                line = f"[{_ts()}] {direction}  {channel}\n{pretty}"
                _log(line)

                if direction == "cloud->device":
                    _print_and_log(f"\nCLOUD->DEVICE  {channel}")
                    print(pretty, flush=True)

            except Exception as e:
                _log(f"[{_ts()}] PUBLISH parse error ({direction}): {e}")

        elif pkt_type == 1:  # CONNECT
            try:
                info = _parse_connect(packet)
                cid = info["client_id"]
                user = info["username"]
                pw = info["password"]
                _print_and_log(f"[{_ts()}] CONNECT  client_id={cid}")
                _print_and_log(f"[{_ts()}]          username={user}")
                _print_and_log(f"[{_ts()}]          password={pw}")
                _print_and_log(f"[{_ts()}] --- Copy username and password into Petlibro Local ---")
                _print_and_log(f"[{_ts()}] *** mqtt_proxy.log contains credentials — do not share publicly ***")
            except Exception:
                _log(f"[{_ts()}] CONNECT (parse error)")

        elif pkt_type == 2:  # CONNACK
            rc = packet[3] if len(packet) > 3 else -1
            rc_names = {0: "OK", 1: "bad proto", 2: "bad client_id",
                        3: "server unavail", 4: "bad credentials", 5: "not authorized"}
            _print_and_log(f"[{_ts()}] CONNACK ({direction}) rc={rc} ({rc_names.get(rc, 'unknown')})")

        elif pkt_type == 14:  # DISCONNECT
            _print_and_log(f"[{_ts()}] DISCONNECT ({direction})")


async def _pipe(reader, writer, direction: str):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                _log(f"[{_ts()}] EOF ({direction})")
                break
            _log(f"[{_ts()}] RAW ({direction}) {len(data)}b")
            _handle_packets(data, direction)
            writer.write(data)
            await writer.drain()
    except Exception as e:
        _log(f"[{_ts()}] pipe error ({direction}): {e}")
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_device(dev_r, dev_w):
    try:
        opening = await asyncio.wait_for(dev_r.read(4096), timeout=5)
    except asyncio.TimeoutError:
        dev_w.close()
        return
    if not opening:
        dev_w.close()
        return

    client_id = ""
    try:
        pkts = _extract_packets(opening)
        if pkts and pkts[0][0] == 1:
            info = _parse_connect(pkts[0][1])
            client_id = info["client_id"]
    except Exception:
        pass

    if not client_id.upper().startswith(SERIAL_PREFIX.upper()):
        _log(f"[{_ts()}] ignored client: {client_id[:16] if client_id else 'unknown'}")
        dev_w.close()
        return

    _print_and_log(f"[{_ts()}] device connected: {client_id}")
    _handle_packets(opening, "device->cloud")

    try:
        cloud_r, cloud_w = await asyncio.open_connection(CLOUD_HOST, CLOUD_PORT)
    except Exception as e:
        _print_and_log(f"[{_ts()}] ERROR: cannot reach cloud: {e}")
        dev_w.close()
        return

    _print_and_log(f"[{_ts()}] proxy active")
    cloud_w.write(opening)
    await cloud_w.drain()

    await asyncio.gather(
        _pipe(dev_r, cloud_w, "device->cloud"),
        _pipe(cloud_r, dev_w, "cloud->device"),
        return_exceptions=True,
    )
    _print_and_log(f"[{_ts()}] connection closed")


async def _shutdown_after(seconds: int):
    await asyncio.sleep(seconds)
    _print_and_log(f"\n[{_ts()}] time limit reached, stopping.")
    raise SystemExit(0)


async def _run():
    if SERIAL_PREFIX == "XXXX" or CLOUD_HOST == "0.0.0.0":
        print("ERROR: fill in SERIAL_PREFIX and CLOUD_HOST at the top of this file before running.")
        return
    server = await asyncio.start_server(handle_device, "0.0.0.0", LISTEN_PORT)
    _print_and_log(f"[{_ts()}] MQTT proxy :{LISTEN_PORT} -> {CLOUD_HOST}:{CLOUD_PORT}")
    _print_and_log(f"[{_ts()}] waiting for device (power-cycle or Wi-Fi reconnect)...")
    _print_and_log(f"[{_ts()}] full log: {LOG_FILE}")
    print()
    async with server:
        await asyncio.gather(
            server.serve_forever(),
            _shutdown_after(300),
            return_exceptions=True,
        )


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        _print_and_log("proxy stopped.")
        _log_fh.close()
