# Running Petlibro Local standalone (Docker, non-HAOS)

Contributed by [@MikeFez](https://github.com/MikeFez) (PR #21), who tested it end to end with his own devices.

Petlibro Local is mainly a Home Assistant add-on, but the same image also runs as a plain Docker container, for people who run Home Assistant in a container or don't run it at all. In standalone mode you point the app at your own MQTT broker and open its web UI directly instead of through Home Assistant.

Home Assistant is optional standalone. Without it, controlling feeders and fountains over MQTT works the same. What you lose is sending alerts through Home Assistant (push, email and persistent notifications) and the area and notify-service dropdowns. Those come back if you set `HA_BASE_URL` and `HA_TOKEN`. If Home Assistant uses the same MQTT broker, the devices' Home Assistant entities show up there as usual.

There are two ways to run it:

- **[All-in-one stack with its own Mosquitto](#all-in-one-stack-with-its-own-mosquitto)** (`docker-compose.standalone.yml`): one `docker compose up` gives you the broker and the app, and the app manages the devices' broker logins for you. Pick this if you have no MQTT broker yet, or want this one only for your PetLibro devices.
- **[Your own MQTT broker](#your-own-mqtt-broker)** (`docker-compose.example.yml`): the app is only a client of a broker you already run, and you add the device logins to it by hand.

## All-in-one stack with its own Mosquitto

Written for a server on the same LAN as the devices, with the DNS override done in the router (see [networking.md](networking.md)). The stack is two containers:

- `mosquitto` (the official `eclipse-mosquitto:2` image) publishes port 1883. Anonymous access is off; every login is a line in a password file.
- `petlibro-local` publishes the web UI on port 8765. It shares the broker's process namespace and the password file volume, so when you add or remove a device it writes the login to the file and sends Mosquitto a reload signal (`SIGHUP`). No restart, so the other devices stay connected.

### Start it

```bash
git clone https://github.com/smcneece/petlibro-local.git
cd petlibro-local
cp .env.example .env
```

Edit `.env`: set `AUTH_TOKEN` (`openssl rand -hex 32`), `MQTT_PASS` (`openssl rand -hex 16`) and `TZ`. `MQTT_USER` is the app's own login and can stay `petlibro`; the app creates it in the password file on its first start. Then:

```bash
docker compose -f docker-compose.standalone.yml up -d --build
```

Open `http://<server>:8765` and enter your `AUTH_TOKEN`. Your devices, pets and settings are in `./data`, the broker's password file and retained data in two named volumes (`mosquitto-auth`, `mosquitto-data`). Back up `./data` and `.env`.

### Add a device

The device must first connect to this broker, and for that the broker needs the device's own login (each model has one that the PetLibro cloud gave it). You read it once from the device's conversation with the real cloud:

1. Capture it with [`tools/mqtt_proxy.py`](../tools/mqtt_proxy.py), a relay between the device and the real cloud that logs the username and password. The steps are [below](#capture-a-devices-login-with-the-relay). The relay needs port 1883, so the stack is stopped while you capture.
2. In the web UI choose **Add Device > Manual Setup**, enter the serial and the captured username and password. The app saves the device and adds the login to the broker's password file at once; the device connects on its next reconnect (power-cycle it) without a broker restart.

Devices of the same model share one login, so once you have captured it you can add every device of that model with Manual Setup, with no more captures.

**Auto Setup is not available in this setup.** It runs a small capture broker on port 1883, which this stack's own broker already holds, so the app answers "use Manual Setup + `tools/mqtt_proxy.py`" instead of waiting.

### Capture a device's login with the relay

[`tools/mqtt_proxy.py`](../tools/mqtt_proxy.py) sits between the device and the real PetLibro cloud and logs everything in both directions, including the device's username and password. It needs only the DNS override (see [networking.md](networking.md)):

1. Stop the stack (`docker compose -f docker-compose.standalone.yml stop`) so port 1883 is free. Your data is kept.
2. Resolve the **real** cloud address with `nslookup mqtt.us.petlibro.com 8.8.8.8` (not through your own DNS, which answers with this server) and set it as `CLOUD_HOST` in a copy of the script. Set `SERIAL_PREFIX` to the device's full serial (it is in the PetLibro app, in the device settings; copy it exactly, because a single wrong character makes the proxy ignore the device). Keep the copy outside the repository, since its log contains the device's login.
3. Allow the device through the server's firewall (see [Things to know](#things-to-know)), make sure the DNS override points at this server, and power-cycle the device.
4. Run the script, use the PetLibro app on your phone (for example feed one portion, edit a plan, toggle the light) and stop it with Ctrl+C. The log shows every message in both directions.
5. Start the stack again (`docker compose -f docker-compose.standalone.yml up -d`) and add the device with **Manual Setup**, using that username and password.

Never share the log without removing the username, password, serial and Wi-Fi name.

### Things to know

- **Adopting a device that already has plans in the PetLibro cloud replaces them.** When the device first asks for its feeding plans, the app answers with its own list, which is empty at that point, and the device deletes what it had. Write the plans again right after adding the device (Schedule tab; note them down beforehand, plan times in the PetLibro app are your local time).
- **Restarting the Mosquitto container reboots the devices** (see [networking.md](networking.md#restarting-mosquitto-reboots-your-devices)), and also restarts the app, which shares its process namespace. Avoid it near a feeding time. `docker compose up -d --build` after changing only the app does not touch the broker.
- **The server's firewall.** If the server runs `ufw` or another host firewall that drops incoming traffic, the device's connection to port 1883 never arrives and nothing says why. Allow the device (`sudo ufw allow from <device-ip> to any port 1883 proto tcp`).
- **LAN only.** Port 1883 has no encryption, and every device of a model shares one login, with no per-device restrictions. Never forward it from the internet, and keep the devices off the internet once they work locally (blocking the device's internet access in your router does it). Port 8765 sits behind the `AUTH_TOKEN`, but it is plain HTTP; publish it on a LAN address only (`"192.168.1.10:8765:8765"` in the compose file) if the server is reachable from elsewhere.
- **Give the server a fixed address.** The DNS override points at it. Use a DHCP reservation or a static IP, and a wired connection if you can.
- **Home Assistant** is optional here. Set `HA_BASE_URL` and `HA_TOKEN` in the compose file's environment to send alerts through it (see the last section). Discovery messages are still published to the broker; they do nothing unless Home Assistant connects to it.

## Your own MQTT broker

The rest of this page: the app connects to a broker you already run, and doesn't create logins there.

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
| `TZ` | No | Timezone for schedules and logs, for example `America/New_York` |

An MQTT variable that is set wins over the app's Settings page, and that field shows read-only there. Leave a variable unset to manage that field from the Settings page instead.

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
