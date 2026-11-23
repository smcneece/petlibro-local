# Networking: DNS, local MQTT and Mosquitto restarts

Back to the [README](../README.md).

- [DNS Setup](#dns-setup)
- [Keeping Devices on Local MQTT](#keeping-devices-on-local-mqtt)
- [Restarting Mosquitto Reboots Your Devices](#restarting-mosquitto-reboots-your-devices)

## DNS Setup

Before adding any device, confirm your DNS override is in place. `mqtt.us.petlibro.com` must resolve to your Home Assistant IP on your local network.

**Pi-hole:** In the Pi-hole web interface, go to **System > Settings > Local DNS Records**. Under "List of local DNS records", enter `mqtt.us.petlibro.com` in the Domain field and your Home Assistant IP in the Associated IP field. Click the "+" Icon, and the override is active immediately.

**AdGuard Home:** Go to **Filters > DNS Rewrites**, click Add Rewrite, enter `mqtt.us.petlibro.com` as the domain and your HA IP as the answer.

**Router-level DNS:** varies by router; check your router's documentation for "custom DNS records" or "local DNS override." For OpenWrt, including firewall rules and capturing a device's traffic, see [OpenWrt](openwrt.md).

You can verify the override is working from any device on your network:

```
nslookup mqtt.us.petlibro.com
```

The response should show your HA IP, not a Petlibro cloud address.

## Keeping Devices on Local MQTT

Petlibro firmware appears to cache the cloud broker's resolved IP address and will fall back to it directly, bypassing DNS entirely, if the local broker is unavailable even briefly (for example, during a Mosquitto restart). A DNS override alone may not be enough to keep the device on your local broker permanently.

The most reliable solution is to **block the device from accessing the internet entirely** at your router or firewall. With no route to the Petlibro cloud, the device has no fallback and stays on your local Mosquitto broker. Petlibro devices do not need internet access once they are on a local broker; all telemetry, commands, and heartbeats flow over MQTT on your LAN. Most routers let you block individual devices by MAC address under a firewall or access control section. Google can likely assist, please do not open issues on router support.

**A second DNS server on some firmware:** a debug log from my One RFID Smart Feeder on firmware 3.0.72 (hardware 5.0.0) showed it doing a DNS lookup that listed a fallback DNS server of `8.8.4.4` next to the one my router hands out. I saw this on one device and have not checked other firmware. If you rely on a DNS override alone, also redirect or block DNS to `8.8.4.4`, or the device may resolve the real Petlibro address through that server instead.

**If a device never picks up the DNS override at all:** a user-submitted packet capture confirmed that at least one hardware/firmware combination (One RFID Smart Feeder, `fw 2.0.47, hw 4.0.0`) never performs a DNS lookup for the broker, ever. Every reconnect goes straight to one of roughly a dozen cached/anycast cloud IPs, so a DNS override has nothing to intercept, it's simply never asked. If you suspect your device is doing this (Auto Setup capture never triggers, or a device that was working stops picking up a DNS change), a source-based NAT rule at your router works regardless of which cached IP the device dials:

```
-s <feeder-ip>/32 -p tcp --dport 1883 -j DNAT --to-destination <home-assistant-ip>:1883
```

This redirects that specific device's MQTT traffic to your Home Assistant IP no matter which destination it tries to reach. Most routers running iptables/nftables (UniFi, OPNsense, pfSense, etc.) support this; consult your router's documentation for source-based DNAT or port redirection. If your inter-VLAN firewall is allow-list style, remember the redirected packet still needs to be allowed through to the broker host/port, DNAT happens before filtering.

**If a device has a backup battery installed and doesn't reconnect after a normal power cycle:** try removing the battery too, not just cutting AC power. This is based on limited testing, not fully confirmed, but a battery-backed unit may survive a brief power interruption without doing a genuine firmware reboot, in which case it just resumes whatever connection it already had instead of re-evaluating anything. Removing the battery forces a true cold boot.

**Would you like Petlibro to add a local broker setting?** The devices are hardcoded to `mqtt.us.petlibro.com`. If you'd like to ask Petlibro for a configurable broker, [there is a ready-made email template](petlibro-feature-request.md) you can copy and personalize.

## Restarting Mosquitto Reboots Your Devices

In my testing, every time the Mosquitto broker restarts, the feeders and fountains reboot themselves (the feeder's LED goes through its start-up sequence) and reconnect within a minute or two. That is firmware behavior, not something this app controls. It matters mostly for feeders: a feeder keeps its schedule but has no clock that survives a reboot, so it can come back with a clock several minutes behind. Petlibro Local sets the One RFID feeder's clock when it asks for the time after a reboot, and on every heartbeat it checks the clock and corrects it if it is wrong. It holds a correction while jumping the clock would skip or repeat a scheduled feed, so a feed that falls inside that gap can still be late.

In my testing, restarting Home Assistant itself (not a full host reboot) or restarting Petlibro Local did not reboot my feeder, since the devices only talk to Mosquitto. So avoid restarting Mosquitto (or doing anything that restarts it, like a Home Assistant OS update or an update of the Mosquitto add-on itself) near a feeding time. **Adding or removing a device in Petlibro Local restarts Mosquitto too**, so the same applies there.

This is how the firmware I've tested behaves, and newer firmware may well fix it, I just don't know. What I've tested: One RFID Smart Feeder on firmware 3.0.72 (hardware 5.0.0), and Dockstream 2 fountains on firmware 0.0.34 (hardware 1.0.0). If yours runs a different version and behaves differently, please open an issue and tell me, it helps. Your firmware version is shown at the top of each device's modal.
