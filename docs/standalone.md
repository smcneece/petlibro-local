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

There is no published image — you build it yourself from this repo (same as the
Battery Sentinel Plus add-on).

1. Clone the repo and copy the example compose file:

   ```bash
   git clone https://github.com/smcneece/petlibro-local.git
   cd petlibro-local
   cp docker-compose.example.yml docker-compose.yml
   ```

2. Edit `docker-compose.yml`: set a strong `AUTH_TOKEN` (see below) and your
   `MQTT_HOST` / `MQTT_USER` / `MQTT_PASS` (and `TZ`), then build and start it:

   ```bash
   docker compose up -d --build
   ```

   Prefer plain Docker? Build and run it directly instead:

   ```bash
   docker build -t petlibro-local ./addon
   docker run -d --name petlibro-local --restart unless-stopped \
     -p 8765:8765 -p 1883:1883 -v "$PWD/data:/data" \
     -e AUTH_TOKEN="$(openssl rand -hex 32)" \
     -e MQTT_HOST=192.168.1.10 -e MQTT_USER=petlibro -e MQTT_PASS=change-me \
     -e TZ=America/New_York \
     petlibro-local
   ```

3. Open `http://<host>:8765`, enter your `AUTH_TOKEN` when prompted, and add your
   devices.

## Access token (`AUTH_TOKEN`, required)

As an add-on, Home Assistant's ingress proxy sits in front of the UI and handles
authentication. Standalone there is no ingress, so `/api/*` — which can return your
MQTT broker credentials — is instead gated behind a shared **`AUTH_TOKEN`**. The app
**refuses to start** without one, so it is never left wide open.

Set it to a long random value, for example:

```bash
openssl rand -hex 32
```

`/api/login` has **no limit on attempts**, so a short or guessable token could be
brute-forced — use a long random one. The UI prompts for the token on first use and
stores it in a cookie.

## Configuration (environment variables)

| Variable | Required | Purpose |
|---|---|---|
| `AUTH_TOKEN` | ✅ | Shared token protecting the web UI / `/api/*`. Use a long random value (`openssl rand -hex 32`). The app refuses to start without it. |
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

## Add each device's login to your broker (standalone)

After capture, **Add Device** (and Auto Setup) save the device, but they **cannot add the
device's MQTT login to your broker standalone**. As an add-on the app writes the login into
the Mosquitto add-on through the Supervisor; standalone there is no Supervisor, so that step
is skipped (it is logged, and the device is still saved).

So for each device you onboard, add its captured username/password to your own MQTT broker by
hand. For Mosquitto, for example:

```bash
mosquitto_passwd -b /path/to/passwordfile <device-username> <device-password>
# then reload or restart your broker
```

The device's username and password are shown in the app after capture (and on the device's
edit form). Devices of the same model share one login, so you only add it once per model.

## Home Assistant notifications (optional)

To route feed alerts through HA, create a **long-lived access token** in HA
(Profile → Security) and set:

```yaml
    environment:
      HA_BASE_URL: "http://homeassistant:8123/api"
      HA_TOKEN: "eyJ...your token..."
```

Leave them unset to run without any HA integration.
