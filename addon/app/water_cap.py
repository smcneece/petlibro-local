"""Dockstream RFID Smart Fountain (PLWF305), newer firmware: WATER_CAP_EVENT.

OBSERVED in one owner's capture (firmware 1.0.16, issue #12): instead of answering
ATTR_GET_SERVICE or sending WEIGHT_CHANGE_EVENT, this firmware sends

    {"cmd":"WATER_CAP_EVENT","msgId":"..","content":[
        {"weightPercent":100,"weight":2384,"ts":1792337525840,"rfid":""}, ...]}

about every 15 seconds, with roughly 15 samples one second apart. `weight` is grams on
the scale, `rfid` is empty unless a pet's tag is near the bowl (then it is the tag
number). Seen: 2384 steady, then samples carrying a tag while the weight wandered
(2378, 2380, ...), then a new steady 2372 (a 12 g drink); with the reservoir lifted off,
`weight` 0 and `weightPercent` 10; after a refill 3561.

The Dockstream 2 Cordless (PLWF116, firmware 0.3.61, issue #16) sends the same message
with no `rfid`, and `"radarDetected": true` while something is in front of the fountain
(about 45 samples per message). Its radar flickers on and off during one drink, and the
weight keeps falling for 20 to 60 seconds after the radar goes quiet (water draining
back), so a radar session only ends once the radar has been quiet for RADAR_QUIET_SECS
and the weight has held still for RADAR_STEADY_SAMPLES. Short radar blips with no
weight change are common and give no drink. Its `weightPercent` stayed at 70 all day.

This module turns that stream into (a) the latest reading and (b) finished drinks in
the shape the WEIGHT_CHANGE_EVENT handler already takes, so pet attribution and the log
are shared. It is pure (no MQTT, no storage) so it can be tested without a fountain.
How a drink ends is my reading of the capture (tag gone and the weight steady again),
not something the firmware states.
"""

STEADY_SAMPLES = 3          # consecutive no-tag samples that count as a settled weight
STEADY_TOLERANCE_G = 2      # grams they may differ by
SESSION_TIMEOUT_SECS = 300  # give up on a session that never settles
MAX_DRINK_G = 800           # same ceiling the other fountain path uses
RADAR_STEADY_SAMPLES = 10   # radar fountains: a settled weight needs 10 s still (it drains slowly)
RADAR_QUIET_SECS = 45       # and the radar quiet this long, so flickers join into one drink
RADAR_MIN_DRINK_G = 3       # radar blips move the scale by a gram or two; below this is noise


class Tracker:
    def __init__(self):
        self.last_ts = 0
        self.stable = None        # last settled weight in grams
        self.session = None       # {"tags": {tag: samples}, "start": ts, "last_tag": ts, "baseline": g, "radar": samples}
        self.recent = []          # recent no-tag weights, newest last
        self.latest = None        # {"weight", "percent", "ts"} of the newest sample with a weight on the scale
        self.reservoir_off = False

    def _settled(self, n=STEADY_SAMPLES):
        r = self.recent[-n:]
        if len(r) < n:
            return None
        return r[-1] if max(r) - min(r) <= STEADY_TOLERANCE_G else None

    def feed(self, content) -> list:
        """Takes a message's `content` list. Returns finished drinks:
        [{"fluctuatingWeight": grams, "fluctuatingSeconds": s, "rfids": [{"rfid": tag, "count": n}]}]."""
        drinks = []
        if not isinstance(content, list):
            return drinks
        samples = []
        for s in content:
            if not isinstance(s, dict):
                continue
            w, ts = s.get("weight"), s.get("ts")
            if isinstance(w, bool) or not isinstance(w, (int, float)) or isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            tag = s.get("rfid")
            tag = str(tag).strip() if isinstance(tag, (str, int)) and not isinstance(tag, bool) else ""
            samples.append((ts, float(w), tag, s.get("weightPercent"), s.get("radarDetected") is True))
        samples.sort(key=lambda x: x[0])
        for ts, w, tag, pct, radar in samples:
            if ts <= self.last_ts:
                continue            # already seen (a repeated or overlapping message)
            self.last_ts = ts
            if w <= 0:
                # Reservoir lifted off: not a drink. Forget the baseline until it settles again.
                self.reservoir_off = True
                self.session = None
                self.stable = None
                self.recent = []
                continue
            if self.reservoir_off:
                self.reservoir_off = False
                self.recent = []
            self.latest = {"weight": w, "percent": pct if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None, "ts": ts}
            if tag or radar:
                self.recent = []
                if self.session is None:
                    self.session = {"tags": {}, "start": ts, "last_tag": ts, "baseline": self.stable, "radar": 0}
                if tag:
                    self.session["tags"][tag] = self.session["tags"].get(tag, 0) + 1
                else:
                    self.session["radar"] += 1
                self.session["last_tag"] = ts
                continue
            self.recent.append(w)
            self.recent = self.recent[-RADAR_STEADY_SAMPLES * 2:]
            s = self.session
            if s is not None and s["radar"] and not s["tags"]:
                # radar only: wait for the radar to stay quiet and the drain to stop
                settled = self._settled(RADAR_STEADY_SAMPLES)
                if ts - s["last_tag"] > SESSION_TIMEOUT_SECS * 1000:
                    self.session = None
                elif settled is not None and ts - s["last_tag"] >= RADAR_QUIET_SECS * 1000:
                    drink = self._finish(settled)
                    if drink and drink["fluctuatingWeight"] >= RADAR_MIN_DRINK_G:
                        drinks.append(drink)
                    self.session = None
                    self.stable = settled
                continue
            settled = self._settled()
            if s is not None:
                if ts - s["start"] > SESSION_TIMEOUT_SECS * 1000:
                    self.session = None
                elif settled is not None:
                    drink = self._finish(settled)
                    if drink:
                        drinks.append(drink)
                    self.session = None
            if settled is not None:
                self.stable = settled
        return drinks

    def _finish(self, final):
        s = self.session
        if s is None or s["baseline"] is None:
            return None
        grams = s["baseline"] - final
        if grams <= 0 or grams > MAX_DRINK_G:
            return None             # a refill, or noise
        # radar only: the seconds the radar saw something (gaps between flickers left out)
        secs = s["radar"] if not s["tags"] else max(1, round((s["last_tag"] - s["start"]) / 1000) + 1)
        return {
            "fluctuatingWeight": round(grams),
            "fluctuatingSeconds": secs,
            "rfids": [{"rfid": t, "count": n} for t, n in s["tags"].items()],
        }
