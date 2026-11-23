# Running Petlibro Local standalone (Docker, non-HAOS)

Contributed by [@MikeFez](https://github.com/MikeFez) (PR #21), who tested it end to end with his own devices.

Petlibro Local is mainly a Home Assistant add-on, but the same image also runs as a plain Docker container, for people who run Home Assistant in a container or don't run it at all. In standalone mode you point the app at your own MQTT broker and open its web UI directly instead of through Home Assistant.

Home Assistant is optional standalone. Without it, controlling feeders and fountains over MQTT works the same. What you lose is sending alerts through Home Assistant (push, email and persistent notifications) and the area and notify-service dropdowns. Those come back if you set `HA_BASE_URL` and `HA_TOKEN`. To get alerts without Home Assistant, send them to an [ntfy](https://ntfy.sh) topic or a webhook (see [Notifications without Home Assistant](#notifications-without-home-assistant)). If Home Assistant uses the same MQTT broker, the devices' Home Assistant entities show up there as usual.

## Prerequisites

- An MQTT broker on your network, for example Mosquitto. This is how the app talks to your devices, so it's required, with or without Home Assistant.
- A dedicated broker user for this app, separate from the device logins, for example `petlibro`.

## Quick start

There is no published image. You build it yourself from this repo, the same as my Battery Sentinel Plus add-on.

1. Clone the repo and copy the example compose file:

   ```bash
   git clone https://github.com/smcneece/petlibro-local.git
   cd petlibro-local
   cp docker-compose.example.yml docker-compose.yml
   ```

2. Edit `docker-compose.yml`: set a strong `AUTH_TOKEN` (see below), your `MQTT_HOST`, `MQTT_USER` and `MQTT_PASS`, and `TZ`. Then build and start it:

   ```bash
   docker compose up -d --build
   ```

   With plain Docker instead:

   ```bash
   docker build -t petlibro-local ./addon
   docker run -d --name petlibro-local --restart unless-stopped \
     -p 8765:8765 -p 1883:1883 -v "$PWD/data:/data" \
     -e AUTH_TOKEN="$(openssl rand -hex 32)" \
     -e MQTT_HOST=192.168.1.10 -e MQTT_USER=petlibro -e MQTT_PASS=change-me \
     -e TZ=America/New_York \
     petlibro-local
   ```

   `-p 1883:1883` is only needed for credential capture, and it only works if 1883 is free on this host. If your broker already runs here, leave that flag out and capture elsewhere (see [Device credential capture and port 1883](#device-credential-capture-and-port-1883)).

3. Open `http://<host>:8765`, enter your `AUTH_TOKEN` when asked, and add your devices: **Auto Setup** to capture their logins, or **Manual Setup** if you already have them (see below).

## Access token (`AUTH_TOKEN`, required)

As an add-on, Home Assistant's ingress sits in front of the UI and handles sign-in. Standalone there is no ingress, so `/api/*`, which can return your MQTT broker credentials, is protected by a shared `AUTH_TOKEN` instead. The app refuses to start without one, so it's never left open.

Use a long random value, for example:

```bash
openssl rand -hex 32
```

`/api/login` has no limit on attempts, so a short or guessable token could be brute-forced. The UI asks for the token the first time and keeps it in a cookie.

## Configuration (environment variables)

| Variable | Required | Purpose |
|---|---|---|
| `AUTH_TOKEN` | Yes | Token protecting the web UI and `/api/*`. Use a long random value (`openssl rand -hex 32`). The app won't start without it |
| `MQTT_HOST` | Yes | Broker host or IP the app connects to |
| `MQTT_PORT` | No | Broker port, default `1883` |
| `MQTT_USER` | Yes | Broker username for this app |
| `MQTT_PASS` | Yes | Broker password for this app |
| `HA_BASE_URL` | No | Home Assistant's REST API, for example `http://homeassistant:8123/api`. Turns on Home Assistant notifications |
| `HA_TOKEN` | No | Home Assistant long-lived access token, used with `HA_BASE_URL` |
| `NOTIFY_PUSH_URL` | No | ntfy topic URL or webhook URL for alerts without Home Assistant. Used only while the URL is blank on the Settings page |
| `NOTIFY_PUSH_FORMAT` | No | `ntfy` (default) or `webhook` |
| `NOTIFY_PUSH_TOKEN` | No | Access token (sent as a Bearer token) or `user:password` (Basic authentication) |
| `TZ` | No | Timezone for schedules and logs, for example `America/New_York` |

An MQTT variable that is set wins over the app's Settings page, and that field shows read-only there. Leave a variable unset to manage that field from the Settings page instead. The `NOTIFY_PUSH_*` variables work the other way round: the Settings page wins, and a variable only fills in a field that is blank there.

Mount a volume at `/data` to keep your devices, pets, settings, and uploaded images and audio.

## Point the device at this host

Petlibro devices are hardcoded to reach the cloud broker at `mqtt.us.petlibro.com` on plain TCP port 1883. Nothing works, neither capture nor normal use, until you redirect that hostname on your network with a DNS override (Pi-hole, AdGuard Home, or your router's DNS). The steps are in [networking.md](networking.md); standalone, read its "your Home Assistant IP" as the IP of the host you're pointing the device at.

Standalone, that host can differ between the two phases:

- **During capture**, point `mqtt.us.petlibro.com` at the host running Petlibro Local, where the small capture broker runs on 1883 (next section).
- **In normal use**, point it at the host running your MQTT broker (`MQTT_HOST`).

If Petlibro Local and your broker are on the same host, it's one IP and never changes. If they're on different hosts, switch the DNS override to the Petlibro Local host for capture, then back to the broker host afterward. The device always dials the same hostname; only the IP it resolves to changes.

## Device credential capture and port 1883

Adding a device captures its MQTT login by briefly running a small broker on port 1883. As an add-on, the app stops the Mosquitto add-on to free the port. Standalone there is no Supervisor to do that, so 1883 has to be free on this host during capture:

- If your broker runs on a different host or container, publish `1883:1883` on this container (as in the example), point the DNS override here for the capture, and capture works.
- If your broker uses 1883 on the same host, stop it for the capture, or capture from a host where 1883 is free (next section).

In normal use the app is only a client of `MQTT_HOST` and doesn't need 1883 published.

## Broker on the same host? Capture once elsewhere, then add by hand

The most common standalone setup, broker and Petlibro Local on the same host, is the one where capture can't run, because your broker already owns 1883. You don't have to move your real setup. Capture once on a throwaway instance, then type the login into your real one:

1. On any host where 1883 is free, start a temporary Petlibro Local (the `docker run` above is fine), point `mqtt.us.petlibro.com` at it, and run **Auto Setup** for the device. You only need it to read the captured username and password, shown after capture and in the device's Edit form. Then remove that instance.
2. On your real instance, choose **Add Device → Manual Setup** and enter the device's serial and that username and password. This needs no capture, no free 1883 and no DNS change.
3. Add that login to your broker (next section). Standalone, Manual Setup can't do it for you either.

Devices of the same model share one login, so once you've captured it you can add every device of that model with Manual Setup.

## Add each device's login to your broker

Add Device and Auto Setup save the device, but standalone they can't add the device's MQTT login to your broker. As an add-on the app writes the login into the Mosquitto add-on through the Supervisor; standalone that step is skipped (it's logged, and the device is still saved).

So for each device, add its username and password to your own broker by hand. For Mosquitto, for example:

```bash
mosquitto_passwd -b /path/to/passwordfile <device-username> <device-password>
# then reload or restart your broker
```

The username and password are shown after capture and in the device's Edit form. Devices of the same model share one login, so you only add it once per model.

## Home Assistant notifications (optional)

To send alerts through Home Assistant, create a long-lived access token in Home Assistant (Profile → Security) and set:

```yaml
    environment:
      HA_BASE_URL: "http://homeassistant:8123/api"
      HA_TOKEN: "eyJ...your token..."
```

Leave them unset to run without Home Assistant.

## Notifications without Home Assistant

Alerts can go straight to your phone or any service that takes a webhook, without Home Assistant. In Settings → Notifications, under **Push (ntfy / webhook)**, set the URL, or set the `NOTIFY_PUSH_*` variables above. **Send test notification** saves the form and sends one message, and shows the error if it fails.

- **ntfy** (default format): the URL is the full topic URL, for example `https://ntfy.sh/your-secret-topic` or `http://ntfy.lan/petlibro` on your own ntfy server. The alert text is the message and the alert title is the title. On ntfy.sh the topic name is the only protection, so use one nobody can guess. A self-hosted server with logins takes an access token (`tk_…`) or `user:password` as the token.
- **Webhook**: the app sends a JSON POST with `title`, `message`, `id` (one per alert type and device) and `source` (`petlibro-local`). The token, if set, goes in an `Authorization` header.

Every alert, pet activity and hasn't-eaten notification goes through it, and each device and pet has a **Push** switch to turn it off, like the other channels. The URL must be `http://` or `https://` and is never followed through a redirect: if the server redirects http to https, use the https URL. The Home Assistant bell, email and mobile channels are skipped while no Home Assistant is set. Nothing is sent when the alert clears (there is no notification to dismiss); a device coming back online sends its own message.
