# Tools

Developer and diagnostic scripts. Not required for normal use of Petlibro Local.

Open each script in a text editor — full setup instructions are at the top of the file.

---

## mqtt_proxy.py — Credential and packet capture

Transparent MQTT proxy. Captures your device's MQTT username and password and logs
all traffic between the device and the PetLibro cloud. Used for adding a device
manually and for reverse-engineering new device types.

Requires Python 3.9+, no extra packages.

> **Privacy:** the `mqtt_proxy_<date>_<time>.log` files contain credentials and device data in plain text.
> Do not post it publicly or attach it to GitHub issues.

---

## replay_capture.py — Play a device against your own broker

Plays the device side of a session (a made-up Granary-style one, or the messages from a `mqtt_proxy.py`
log, with a fake serial) against your own broker, prints everything Petlibro Local
sends back and can simulate a finished feed (acknowledgement, then `GRAIN_START` and `GRAIN_END`). Add the fake
serial as a device in the app first, using a login the broker already knows. Use it to test support for a model without the real feeder. It never talks to the
PetLibro cloud.

Requires Python 3.9+ and aiomqtt (`pip install aiomqtt`).

---

## feeder_probe.py — Interactive feeder command tool

Connects to your local Mosquitto broker and lets you send MQTT commands to a feeder
interactively. Useful for testing commands on a new device model.

Requires Python 3.9+ and paho-mqtt (`pip install paho-mqtt`).

---

If you capture useful packet logs for a device that is not yet supported,
please open an issue or pull request on the
[Petlibro Local GitHub repository](https://github.com/smcneece/petlibro-local).
