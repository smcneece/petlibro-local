# Running Petlibro Local standalone (Docker / non-HAOS)

Petlibro Local is primarily a Home Assistant add-on, but the **same image** also runs as a
plain Docker container for people who run Home Assistant in a container (or don't run HA at
all). In standalone mode you point the app at your own MQTT broker and open its web UI
directly instead of through HA ingress.

Home Assistant is **optional** standalone: without it, everything that controls feeders and
fountains over MQTT works exactly the same. The only thing you lose is routing feed alerts
*through* HA (push/email/persistent notifications) and the area / notify-service dropdowns —
and those come back if you provide `HA_BASE_URL` + `HA_TOKEN`.

## Prerequisites

- An **MQTT broker** on your network (e.g. Mosquitto). This is how the app talks to your
  devices — it is required, and it is independent of Home Assistant.
- A **dedicated broker user** for this app (separate from the per-device accounts), e.g.
  username `petlibro`.

## Quick start

1. Copy the example compose file and edit the MQTT values:

   ```bash
   cp docker-compose.example.yml docker-compose.yml
   # edit MQTT_HOST / MQTT_USER / MQTT_PASS (and TZ)
   docker compose up -d
   ```

2. Open `http://<host>:8765` and add your devices.

The image is published to `ghcr.io/mikefez/petlibro-local:latest` (multi-arch: amd64 +
arm64).

## Configuration (environment variables)

| Variable | Required | Purpose |
|---|---|---|
| `MQTT_HOST` | ✅ | Broker host/IP the app connects to |
| `MQTT_PORT` | | Broker port (default `1883`) |
| `MQTT_USER` | ✅ | Broker username for this app |
| `MQTT_PASS` | ✅ | Broker password for this app |
| `HA_BASE_URL` | | HA REST API, e.g. `http://homeassistant:8123/api` — enables HA notifications |
| `HA_TOKEN` | | HA long-lived access token (pairs with `HA_BASE_URL`) |
| `TZ` | | Timezone for schedules and logs, e.g. `America/New_York` |

When these are set they are **authoritative** and override the in-app Settings UI for the
matching fields (those fields show as read-only in the UI). Leave a variable unset to manage
that field from the Settings UI instead.

Persist state by mounting a volume at **`/data`** (devices, pets, settings, uploaded images
and audio all live there).

## Device credential capture and port 1883

Onboarding a device captures its MQTT credentials by briefly running a tiny broker on
**port 1883**. As an add-on the app stops the Mosquitto add-on automatically to free the
port; **standalone there is no Supervisor**, so you must make sure 1883 is free on this host
during capture:

- If your MQTT broker runs on a **different** host/container, just publish `1883:1883` on
  this container (as in the example) and capture works.
- If your broker runs on the **same host's** 1883, stop it for the capture step, or run the
  capture from a host where 1883 is free.

During normal operation the app is just a client of `MQTT_HOST` and does not need 1883
published.

## Home Assistant notifications (optional)

To route feed alerts through HA, create a **long-lived access token** in HA
(Profile → Security) and set:

```yaml
    environment:
      HA_BASE_URL: "http://homeassistant:8123/api"
      HA_TOKEN: "eyJ...your token..."
```

Leave them unset to run without any HA integration.
