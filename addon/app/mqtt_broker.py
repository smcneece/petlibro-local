"""Minimal TCP MQTT broker for one-time credential capture.

Listens on port 1883, waits for a device CONNECT packet, extracts
username, password, and client ID, sends CONNACK, then closes.

No dependencies beyond stdlib -- parses the MQTT 3.1.1 wire format manually.
"""

import asyncio
import logging
import struct

_LOGGER = logging.getLogger(__name__)

CAPTURE_PORT = 1883
CAPTURE_TIMEOUT = 60


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    multiplier = 1
    value = 0
    while True:
        byte = data[offset]
        offset += 1
        value += (byte & 0x7F) * multiplier
        multiplier *= 128
        if not (byte & 0x80):
            break
        if multiplier > 128 * 128 * 128:
            raise ValueError("Malformed variable-length integer")
    return value, offset


def _decode_utf8(data: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    return data[offset:offset + length].decode("utf-8", errors="replace"), offset + length


def _decode_bytes(data: bytes, offset: int) -> tuple[bytes, int]:
    length = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    return data[offset:offset + length], offset + length


def _parse_connect(data: bytes) -> dict | None:
    """Parse an MQTT CONNECT packet. Returns dict with client_id, username, password or None."""
    try:
        if not data or data[0] != 0x10:
            return None

        _, offset = _decode_varint(data, 1)

        proto_name, offset = _decode_utf8(data, offset)
        if proto_name not in ("MQTT", "MQIsdp"):
            return None

        _proto_level = data[offset]
        offset += 1

        connect_flags = data[offset]
        offset += 1

        has_username = bool(connect_flags & 0x80)
        has_password = bool(connect_flags & 0x40)
        has_will = bool(connect_flags & 0x04)

        offset += 2  # keepalive

        client_id, offset = _decode_utf8(data, offset)

        if has_will:
            _, offset = _decode_utf8(data, offset)
            _, offset = _decode_bytes(data, offset)

        username = ""
        password = ""

        if has_username:
            username, offset = _decode_utf8(data, offset)
        if has_password:
            pwd_bytes, offset = _decode_bytes(data, offset)
            password = pwd_bytes.decode("utf-8", errors="replace")

        return {"client_id": client_id, "username": username, "password": password}
    except Exception:
        _LOGGER.exception("Failed to parse CONNECT packet")
        return None


CONNACK_ACCEPTED = bytes([0x20, 0x02, 0x00, 0x00])

# Client ID prefixes that identify Petlibro devices
PETLIBRO_PREFIXES = ("WF", "AF", "LS", "PL")


def _is_petlibro(client_id: str) -> bool:
    upper = client_id.upper()
    return any(upper.startswith(p) for p in PETLIBRO_PREFIXES)


class _CaptureProtocol(asyncio.Protocol):
    def __init__(self, result_future: asyncio.Future, known_serials: frozenset[str] = frozenset()):
        self._future = result_future
        self._known_serials = known_serials
        self._buf = b""
        self._transport = None

    def connection_made(self, transport):
        self._transport = transport
        peer = transport.get_extra_info("peername")
        _LOGGER.info("Capture: connection from %s", peer)

    def data_received(self, data: bytes):
        self._buf += data
        if len(self._buf) < 2:
            return
        creds = _parse_connect(self._buf)
        if creds is not None:
            client_id = creds.get("client_id", "")
            # Always send CONNACK so the device doesn't error out
            self._transport.write(CONNACK_ACCEPTED)
            self._transport.close()

            if not _is_petlibro(client_id):
                _LOGGER.info(
                    "Capture: skipping non-Petlibro client '%s' — still waiting...",
                    client_id[:16],
                )
                return

            if client_id.upper() in self._known_serials:
                # Stopping Mosquitto for this capture window disconnects every
                # already-configured device too, not just the new one the user
                # is trying to add, and any of them reconnecting during the
                # window would otherwise win the capture race. Real complaint
                # from a user with multiple devices (issue #8, 2026-09-11):
                # re-adding one device kept re-capturing an already-onboarded
                # one instead, since it reconnects faster than a freshly
                # power-cycled new device.
                _LOGGER.info(
                    "Capture: skipping already-onboarded device '%s...' — still waiting...",
                    client_id[:8],
                )
                return

            _LOGGER.info(
                "Capture: Petlibro device — client_id=%s..., user=%s...",
                client_id[:8],
                creds["username"][:4] if creds["username"] else "?",
            )
            if not self._future.done():
                self._future.set_result(creds)

    def connection_lost(self, exc):
        if exc:
            _LOGGER.debug("Capture: connection lost: %s", exc)


async def capture_credentials(timeout_seconds: int = CAPTURE_TIMEOUT, known_serials=frozenset()) -> dict:
    """Run a mini MQTT broker on port 1883 and capture the first CONNECT credentials
    from a device NOT already in known_serials (already-onboarded devices reconnecting
    during this same window are skipped, not captured).

    Retries binding the port every 3 seconds until Mosquitto fully releases it.
    Returns {"client_id": ..., "username": ..., "password": ...} or {} on timeout/error.
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future = loop.create_future()
    deadline = loop.time() + timeout_seconds
    server = None
    known_serials = frozenset(s.upper() for s in known_serials)

    # Retry until port is free -- Mosquitto can take several seconds to release it
    while loop.time() < deadline:
        try:
            server = await loop.create_server(
                lambda: _CaptureProtocol(result, known_serials),
                host="0.0.0.0",
                port=CAPTURE_PORT,
            )
            break
        except OSError:
            remaining = deadline - loop.time()
            if remaining < 4:
                _LOGGER.error("Port %d never became free -- Mosquitto may not have stopped", CAPTURE_PORT)
                return {}
            _LOGGER.info("Port %d still in use, retrying in 3s...", CAPTURE_PORT)
            await asyncio.sleep(3)

    if server is None:
        return {}

    _LOGGER.info("Capture broker ready on port %d (%ds window)", CAPTURE_PORT, int(deadline - loop.time()))
    try:
        remaining = max(1.0, deadline - loop.time())
        await asyncio.wait_for(result, timeout=remaining)
        return result.result()
    except asyncio.TimeoutError:
        _LOGGER.warning("Credential capture timed out after %ds", timeout_seconds)
        return {}
    except Exception:
        _LOGGER.exception("Capture broker error")
        return {}
    finally:
        server.close()
        await server.wait_closed()
        _LOGGER.info("Capture broker stopped")
