# OpenWrt: pointing PetLibro devices at your own server

Back to the [README](../README.md) · [Networking](networking.md) · [Running standalone](standalone.md)

How to make a PetLibro device on your LAN talk to your own Petlibro Local + Mosquitto instead of the cloud, using an
OpenWrt router. The server and the device are on the same LAN. Written for OpenWrt 22.03 and newer (`fw4`/nftables).

> Each step has a check you can run, so verify it and adjust for your setup.

Placeholders used below: `<SERVER_IP>` (the host running the compose stack), `<DEVICE_IP>` (the feeder),
`<ROUTER_IP>` (the router's LAN address, usually `192.168.1.1`).

- [1. Static addresses](#1-static-addresses)
- [2. DNS override](#2-dns-override)
- [3. Keep the device on the local broker](#3-keep-the-device-on-the-local-broker)
- [4. Order of operations](#4-order-of-operations)
- [Capturing a new device's traffic](#capturing-a-new-devices-traffic)

## 1. Static addresses

The DNS override points at the server, so the server must keep its address (a wired connection is best). In
**Network > DHCP and DNS > Static Leases**, or:

```sh
uci add dhcp host
uci set dhcp.@host[-1].name='petlibro-server'
uci set dhcp.@host[-1].mac='<SERVER_MAC>'
uci set dhcp.@host[-1].ip='<SERVER_IP>'
uci commit dhcp && /etc/init.d/dnsmasq restart
```

Do the same for the device if you want firewall rules that match its IP.

## 2. DNS override

The devices are hardcoded to `mqtt.us.petlibro.com`. Make the router's dnsmasq answer it with the server's address:

```sh
uci add dhcp domain
uci set dhcp.@domain[-1].name='mqtt.us.petlibro.com'
uci set dhcp.@domain[-1].ip='<SERVER_IP>'
uci commit dhcp && /etc/init.d/dnsmasq restart
```

Without SSH, the same entry in LuCI: **Network > DHCP and DNS > Hostnames > Add**, hostname `mqtt.us.petlibro.com`,
IP address `<SERVER_IP>`, then **Save & Apply**.

Check it from the server or any LAN computer (ask the router explicitly):

```sh
nslookup mqtt.us.petlibro.com <ROUTER_IP>              # must show <SERVER_IP>
nslookup -type=AAAA mqtt.us.petlibro.com <ROUTER_IP>   # must not return a real cloud address
```

The device only picks the change up when it reconnects: power-cycle it (and remove its backup battery briefly if it
has one).

## 3. Keep the device on the local broker

The firmware can fall back to cached cloud addresses or another DNS server (see
[Networking](networking.md#keeping-devices-on-local-mqtt)). Add the rules you need, in this order of preference.

**a) Redirect the device's DNS to the router** (defeats a hardcoded `8.8.4.4`):

```sh
uci add firewall redirect
uci set firewall.@redirect[-1].name='PetLibro DNS to router'
uci set firewall.@redirect[-1].target='dnat'
uci set firewall.@redirect[-1].src='lan'
uci set firewall.@redirect[-1].src_ip='<DEVICE_IP>'
uci set firewall.@redirect[-1].proto='tcp udp'
uci set firewall.@redirect[-1].src_dport='53'
uci set firewall.@redirect[-1].dest_port='53'
uci commit firewall && /etc/init.d/firewall reload
```

**b) Block the device's internet access** (do this once it works locally, see [order of operations](#4-order-of-operations)):

```sh
uci add firewall rule
uci set firewall.@rule[-1].name='Block PetLibro device WAN'
uci set firewall.@rule[-1].src='lan'
uci set firewall.@rule[-1].dest='wan'
uci set firewall.@rule[-1].src_ip='<DEVICE_IP>'
uci set firewall.@rule[-1].proto='all'
uci set firewall.@rule[-1].target='REJECT'
uci commit firewall && /etc/init.d/firewall reload
```

The PetLibro app and cloud no longer control the device after this. That is the point, but you can no longer see it
there.

**c) Only if the device never asks for the name** (it connects straight to a cached cloud IP, so the DNS override is
never used; a [capture](#capturing-a-new-devices-traffic) shows whether it does): redirect its port 1883 to the server.
Server and device are on the same subnet, so the reply would go straight back to the device with the wrong source
address and the connection would never work. The redirect needs a matching source NAT so the reply passes through the
router again:

```sh
uci add firewall redirect
uci set firewall.@redirect[-1].name='PetLibro MQTT to local broker'
uci set firewall.@redirect[-1].target='dnat'
uci set firewall.@redirect[-1].src='lan'
uci set firewall.@redirect[-1].dest='lan'
uci set firewall.@redirect[-1].proto='tcp'
uci set firewall.@redirect[-1].src_ip='<DEVICE_IP>'
uci set firewall.@redirect[-1].src_dport='1883'
uci set firewall.@redirect[-1].dest_ip='<SERVER_IP>'
uci set firewall.@redirect[-1].dest_port='1883'

uci add firewall redirect
uci set firewall.@redirect[-1].name='PetLibro MQTT hairpin SNAT'
uci set firewall.@redirect[-1].target='snat'
uci set firewall.@redirect[-1].src='lan'
uci set firewall.@redirect[-1].dest='lan'
uci set firewall.@redirect[-1].proto='tcp'
uci set firewall.@redirect[-1].src_ip='<DEVICE_IP>'
uci set firewall.@redirect[-1].dest_ip='<SERVER_IP>'
uci set firewall.@redirect[-1].dest_port='1883'
uci set firewall.@redirect[-1].src_dip='<ROUTER_IP>'
uci commit firewall && /etc/init.d/firewall reload
```

Check with `sudo tcpdump -ni any port 1883` on the server: the device's connection must arrive from `<ROUTER_IP>`.

**d) The server's own firewall.** The router cannot see traffic between two hosts on the same subnet, and the host
firewall of the server sits in the way. If the server runs `ufw` (Ubuntu and Debian desktops often do, with incoming
traffic dropped by default), the device's connection is silently dropped and nothing ever reaches Mosquitto, even
though DNS is right. Allow only the device (Docker's published port is filtered by `ufw` on some setups, not on
others, so test it):

```sh
sudo ufw allow from <DEVICE_IP> to any port 1883 proto tcp comment 'petlibro'
```

Without `ufw`, check `sudo iptables -S INPUT` / `sudo nft list ruleset` for a drop rule. You can see the device
knocking with `sudo tcpdump -ni any host <DEVICE_IP> and port 1883`; if SYN packets arrive but there is never a reply,
it is the firewall. Open the web UI port (8765) only to the machines you manage it from.

## 4. Order of operations

1. Static leases for the server and the device.
2. Start the stack (see [Running standalone](standalone.md)) and add the device's login in the app.
3. Allow the device through the server's firewall (3d) and add the DNS override, then power-cycle the device.
4. Check `docker compose logs -f mosquitto` and the app: the device must connect and show up.
5. Only then add the DNS redirect (3a) and the WAN block (3b).

Restarting the Mosquitto container reboots the devices (see
[Networking](networking.md#restarting-mosquitto-reboots-your-devices)), so avoid doing it near a feeding time. Never
publish port 1883 to the internet: the broker is meant for your LAN only.

## Capturing a new device's traffic

For a model the app does not know yet, or to read a device's MQTT login, use the relay in
[mqtt_proxy.py](../tools/mqtt_proxy.py), described in
[Running standalone](standalone.md#capture-a-devices-login-with-the-relay). It needs only the DNS override from
section 2 (no router shell and no packet capture on the router). Two OpenWrt-specific points:

- Look up the **real** cloud address through a public resolver (`nslookup mqtt.us.petlibro.com 8.8.8.8`), because your
  router now answers that name with the server.
- Allow the device through the server's firewall first (3d), or its connection never reaches the relay.
