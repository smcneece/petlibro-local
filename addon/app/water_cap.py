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


class Tracker:
    def __init__(self):
        self.last_ts = 0
        self.stable = None        # last settled weight in grams
        self.session = None       # {"tags": {tag: samples}, "start": ts, "last_tag": ts, "baseline": g}
        self.recent = []          # recent no-tag weights, newest last
        self.latest = None        # {"weight", "percent", "ts"} of the newest sample with a weight on the scale
        self.reservoir_off = False

    def _settled(self):
        r = self.recent[-STEADY_SAMPLES:]
        if len(r) < STEADY_SAMPLES:
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
            samples.append((ts, float(w), tag, s.get("weightPercent")))
        samples.sort(key=lambda x: x[0])
        for ts, w, tag, pct in samples:
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
            if tag:
                self.recent = []
                if self.session is None:
                    self.session = {"tags": {}, "start": ts, "last_tag": ts, "baseline": self.stable}
                self.session["tags"][tag] = self.session["tags"].get(tag, 0) + 1
                self.session["last_tag"] = ts
                continue
            self.recent.append(w)
            self.recent = self.recent[-STEADY_SAMPLES * 2:]
            settled = self._settled()
            if self.session is not None:
                if ts - self.session["start"] > SESSION_TIMEOUT_SECS * 1000:
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
        secs = max(1, round((s["last_tag"] - s["start"]) / 1000) + 1)
        return {
            "fluctuatingWeight": round(grams),
            "fluctuatingSeconds": secs,
            "rfids": [{"rfid": t, "count": n} for t, n in s["tags"].items()],
        }
