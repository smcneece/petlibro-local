#!/usr/bin/env python3
"""Play the DEVICE side of a PetLibro session against your own broker, so Petlibro Local can be tested
without the real feeder (and without risking a real feed).

Two modes:

  * With --proxy-log (a tools/mqtt_proxy.py log): replays the messages a real device sent, keeping their
    timing. The serial and the model in the topics are rewritten to --serial / --model.
  * Without --proxy-log: plays a small made-up Granary-style session (boot, NTP request, attributes,
    heartbeat, plan request) for the given serial and model.

While it runs it prints everything Petlibro Local sends to the device (the `.../sub` topics) and, with
--simulate-feeder, answers a Feed Now (MANUAL_FEEDING_SERVICE) with an acknowledgement and a finished
GRAIN_OUTPUT_EVENT like a Granary does.

Examples (broker and app running, device login already added to the broker, device added in the app):

    python3 tools/replay_capture.py --serial AF01TEST00001 --model PLAF108 --user USER --password PASS
    python3 tools/replay_capture.py --proxy-log mqtt_proxy_*.log --serial AF01TEST00001 --user USER --password PASS

This talks to the broker you point it at, never to the PetLibro cloud. Requires Python 3.9+ and aiomqtt
(`pip install aiomqtt`, the version the app itself uses is pinned in addon/Dockerfile).
"""
import argparse
import asyncio
import json
import re
import time
import uuid

_TOPIC = re.compile(r"^dl/([^/]+)/([^/]+)/device/([^/]+)/(post|sub)$")


def _now_ms():
    return int(time.time() * 1000)


def _built_in(model):
    ts = _now_ms()
    return [
        (0.0, "event", {"cmd": "DEVICE_START_EVENT", "ts": ts, "msgId": uuid.uuid4().hex, "success": True,
                        "pid": model, "hardwareVersion": "1.0.0", "softwareVersion": "1.0.0"}),
        (0.5, "ntp", {"cmd": "NTP", "ts": ts}),
        (1.0, "event", {"cmd": "ATTR_PUSH_EVENT", "ts": ts, "surplusGrain": True, "grainOutletState": True,
                        "electricQuantity": 100, "powerType": 3, "volume": 40}),
        (1.5, "heart", {"cmd": "HEARTBEAT", "count": 1, "rssi": -50, "wifiType": 1, "ts": ts}),
        (2.0, "event", {"cmd": "GET_FEEDING_PLAN_EVENT", "ts": ts, "msgId": uuid.uuid4().hex}),
    ]


def _rewrite(messages, serial, model):
    """messages: [(seconds, topic, payload)] of what the device sent. Returns the steps to replay."""
    out, orig_serial, orig_model, t0 = [], None, None, None
    for ts, topic, payload in messages:
        m = _TOPIC.match(topic)
        if not m or m.group(4) != "post":
            continue
        orig_model, orig_serial = orig_model or m.group(1), orig_serial or m.group(2)
        t0 = t0 if t0 is not None else ts
        new_topic = "dl/%s/%s/device/%s/post" % (model or m.group(1), serial, m.group(3))
        out.append((ts - t0, new_topic, payload.replace(orig_serial, serial)))
    return out, orig_serial, orig_model


_PROXY_HEADER = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] device->cloud\s+(dl/\S+)\s*$")
_PROXY_ANY = re.compile(r"^\[\d\d:\d\d:\d\d\] ")


def _from_proxy_log(path, serial, model):
    """Read a tools/mqtt_proxy.py log: a `[HH:MM:SS] device->cloud <topic>` line, then the JSON body."""
    messages, topic, secs, body = [], None, 0, []

    def flush():
        if topic and body:
            try:
                payload = json.dumps(json.loads("\n".join(body)))
            except ValueError:
                return
            messages.append((secs, topic, payload))

    day, last = 0, None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            m = _PROXY_HEADER.match(line)
            if m or _PROXY_ANY.match(line):
                flush()
                topic, body = None, []
                if m:
                    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                    if last is not None and secs + day < last:
                        day += 86400
                    secs += day
                    last = secs
                    topic = m.group(4)
            elif topic is not None and line.strip():
                body.append(line)
    flush()
    return _rewrite(messages, serial, model)


async def run(args):
    import aiomqtt

    model, serial = args.model, args.serial
    source = args.proxy_log
    if source:
        steps, orig_serial, orig_model = _from_proxy_log(source, serial, model)
        model = model or orig_model
        if not steps:
            raise SystemExit("no device->broker publishes found in " + source)
        print("replaying %d messages from %s (model %s)" % (len(steps), source, model))
    else:
        model = model or "PLAF108"
        steps = [(dt, "dl/%s/%s/device/%s/post" % (model, serial, chan), json.dumps(body))
                 for dt, chan, body in _built_in(model)]

    async with aiomqtt.Client(args.host, args.port, username=args.user, password=args.password,
                              identifier=serial) as client:
        await client.subscribe("dl/%s/%s/device/+/sub" % (model, serial), qos=1)
        print("connected as %s, listening on dl/%s/%s/device/+/sub" % (serial, model, serial))

        async def publish():
            last = 0.0
            for dt, topic, payload in steps:
                wait = min((dt - last) / args.speed if args.speed > 0 else 0, 10)
                last = dt
                if wait > 0:
                    await asyncio.sleep(wait)
                await client.publish(topic, payload, qos=1)
                print("DEVICE -> %s  %s" % (topic.split("/device/")[-1], payload[:300]))
            await asyncio.sleep(args.listen)

        async def receive():
            async for msg in client.messages:
                text = msg.payload.decode("utf-8", "replace")
                print("APP    -> %s  %s" % (str(msg.topic).split("/device/")[-1], text[:300]))
                if not args.simulate_feeder:
                    continue
                try:
                    body = json.loads(text)
                except ValueError:
                    continue
                if body.get("cmd") == "MANUAL_FEEDING_SERVICE":
                    base = "dl/%s/%s/device/" % (model, serial)
                    msg_id, grains = body.get("msgId"), body.get("grainNum", 1)
                    ack = {"cmd": "MANUAL_FEEDING_SERVICE", "ts": _now_ms(), "msgId": msg_id, "code": 0}
                    await client.publish(base + "service/post", json.dumps(ack), qos=1)
                    for finished, actual, step in ((False, 0, "GRAIN_START"), (True, grains, "GRAIN_END")):
                        step_msg = {"cmd": "GRAIN_OUTPUT_EVENT", "ts": _now_ms(), "msgId": msg_id,
                                    "finished": finished, "type": 2, "actualGrainNum": actual,
                                    "expectGrainNum": grains, "execStep": step}
                        await client.publish(base + "event/post", json.dumps(step_msg), qos=1)
                        await asyncio.sleep(1)
                    print("DEVICE -> simulated feed of %s portion(s)" % grains)

        publisher = asyncio.ensure_future(publish())
        receiver = asyncio.ensure_future(receive())
        await publisher
        receiver.cancel()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proxy-log", help="replay the device messages of a tools/mqtt_proxy.py log instead of the "
                    "built-in session (the login in that log is ignored)")
    ap.add_argument("--serial", required=True, help="serial to play (also the MQTT client id); use a fake one")
    ap.add_argument("--model", help="model used in the topics (default: the capture's, or PLAF108)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--speed", type=float, default=1.0, help="timing factor for --proxy-log (0 = no delays)")
    ap.add_argument("--listen", type=float, default=30.0, help="seconds to keep listening after the replay")
    ap.add_argument("--simulate-feeder", action="store_true",
                    help="answer MANUAL_FEEDING_SERVICE like the real feeders do (acknowledgement, then GRAIN_START and GRAIN_END)")
    args = ap.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
