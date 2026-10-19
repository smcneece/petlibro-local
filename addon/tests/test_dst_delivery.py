"""DST delivery/double-feed regression tests: real storage and handlers, fake MQTT
and clock only. Run: python -m pytest -q addon/tests/test_dst_delivery.py"""
import asyncio
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import devices
import storage
import pytest


def ms(iso):
    return int(dt.datetime.fromisoformat(iso + "+00:00").timestamp() * 1000)


def plan(t="05:00", pid=1, **kw):
    kw.setdefault("repeatDay", list(range(1, 8)))
    kw.setdefault("grainNum", 2)
    return dict(planId=pid, executionTime=t, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setattr(devices, "_devices", {"TEST": ("PLAF301", "one_rfid")})
    monkeypatch.setattr(devices, "_online", {"TEST": True})
    sent = []
    class MQTT:
        async def publish(self, topic, payload, **kw):
            sent.append(json.loads(payload))
    monkeypatch.setattr(devices, "_client_ref", MQTT())
    async def noop(*args):
        pass
    monkeypatch.setattr(devices, "publish_ha_state", noop)
    monkeypatch.setattr(devices, "republish_ha_discovery", noop)
    storage.save_settings({"feeder_timezone": "Europe/Copenhagen"})
    def at(iso):
        monkeypatch.setattr(time, "time", lambda: ms(iso) / 1000)
    def save(plans, offset=7200):
        storage.save_device_feeding_plans("TEST", plans, tz_basis={
            "tz": "Europe/Copenhagen", "offset": offset, "at": ms("2026-10-20T12:00:00")})
    return at, save, sent


def tick():
    asyncio.run(devices.reconcile_plan_timezones())


def stored():
    return storage.get_device_feeding_plans("TEST")


def basis():
    return storage.get_feeding_plans_tz_basis("TEST")


class Request:
    match_info = {"serial": "TEST"}
    def __init__(self, body=None, query=None):
        self.body, self.query = body, query or {}
    async def json(self):
        return self.body


# ── double-feed protection ─────────────────────────────────────────────────

def test_normal_shift_at_night_needs_no_skip(env):
    at, save, sent = env
    save([plan(), plan("16:00", pid=2)])
    at("2026-10-25T01:01:00")
    tick()
    assert [p["executionTime"] for p in sent[-1]["plans"]] == ["06:00", "17:00"]
    assert not any("skipEndTime" in p for p in sent[-1]["plans"])


def test_meal_in_repeated_hour_is_not_fed_twice(env):
    # 02:30 local: ran at 00:30Z (CEST); would run again at 01:30Z (CET)
    at, save, sent = env
    save([plan("00:30")])
    at("2026-10-25T01:01:00")
    tick()
    assert sent[-1]["plans"][0]["skipEndTime"] == ms("2026-10-25T01:31:00")


def test_meal_at_the_transition_instant_is_not_fed_twice(env):
    at, save, sent = env
    save([plan("01:00")])
    at("2026-10-25T01:00:00")
    tick()
    assert sent[-1]["plans"][0]["skipEndTime"] == ms("2026-10-25T02:01:00")


def test_partner_across_utc_midnight(env):
    at, save, sent = env
    save([plan("23:30", repeatDay=[7])])
    at("2026-10-25T23:45:00")
    tick()
    p = sent[-1]["plans"][0]
    assert (p["executionTime"], p["repeatDay"], p["skipEndTime"]) == ("00:30", [1], ms("2026-10-26T00:31:00"))


def test_skip_only_covers_that_one_meal(env):
    at, save, sent = env
    save([plan("00:30")])
    at("2026-10-25T01:01:00")
    tick()
    p = stored()[0]
    assert storage.occurrence_skipped(p, ms("2026-10-25T01:30:00") / 1000)
    assert not storage.occurrence_skipped(p, ms("2026-10-26T01:30:00") / 1000)


def test_meal_already_skipped_by_user_does_not_count_as_run(env):
    at, save, sent = env
    save([plan(skipEndTime=ms("2026-10-25T05:30:00"))])
    at("2026-10-25T05:45:00")
    tick()
    assert sent[-1]["plans"][0].get("skipEndTime", 0) < ms("2026-10-25T06:00:00")


def test_disabled_meal_does_not_count_as_run(env):
    at, save, sent = env
    save([plan("00:30", _enabled=False), plan("16:00", pid=2)])
    at("2026-10-25T01:01:00")
    tick()
    assert "skipEndTime" not in stored()[0]


def test_late_spring_forward_meal_is_logged(env, caplog):
    at, save, sent = env
    save([plan("00:30")], offset=3600)
    at("2027-03-28T23:45:00")   # new time 23:30Z already passed, old 00:30Z ahead
    tick()
    assert "already past; no catch-up meal" in caplog.text
    assert "skipEndTime" not in stored()[0]


# ── delivery until acked ───────────────────────────────────────────────────

def test_offline_feeder_gets_shift_when_it_asks_late(env):
    # feeder offline at the change, still ran 05:00Z, asks at 05:30Z
    at, save, sent = env
    devices._online["TEST"] = False
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    assert not sent and stored()[0]["executionTime"] == "06:00"
    at("2026-10-25T05:30:00")
    asyncio.run(devices._respond_feeding_plan("TEST", "dl/PLAF301/TEST/device/event/post"))
    assert sent[-1]["plans"][0]["skipEndTime"] == ms("2026-10-25T06:01:00")


def test_unacked_resend_rechecks_and_gets_new_sync(env):
    at, save, sent = env
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()                                   # sent, ack lost
    first = sent[-1]["plans"][0]["syncTime"]
    at("2026-10-25T05:30:00")
    tick()
    latest = sent[-1]["plans"][0]
    assert latest["skipEndTime"] == ms("2026-10-25T06:01:00")
    assert latest["syncTime"] > first
    devices._plan_ack_received("TEST", [{"planId": 1, "syncTime": first}])
    assert basis()["pending_sync"] == latest["syncTime"]   # old ack does not count


def test_resend_every_5_min_until_full_ack(env):
    at, save, sent = env
    save([plan(), plan("16:00", pid=2), plan("20:00", pid=3, _enabled=False)])
    at("2026-10-25T01:01:00")
    tick()
    at("2026-10-25T01:03:00"); tick()
    assert len(sent) == 1
    at("2026-10-25T01:06:30"); tick()
    assert len(sent) == 2
    sync = basis()["pending_sync"]
    devices._plan_ack_received("TEST", [{"planId": 1, "syncTime": sync}])
    assert basis().get("pending_sync") == sync        # partial ack
    devices._plan_ack_received("TEST", [{"planId": 1, "syncTime": sync}, {"planId": 2, "syncTime": sync}])
    assert not basis().get("pending_sync") and "prev" not in basis()
    at("2026-10-25T01:30:00"); tick()
    assert len(sent) == 2
    assert [p["executionTime"] for p in stored()] == ["06:00", "17:00", "21:00"]


def test_disabled_only_list_does_not_retry(env):
    at, save, sent = env
    save([plan(_enabled=False)])
    at("2026-10-25T01:01:00")
    tick()
    assert stored()[0]["executionTime"] == "06:00"
    assert not basis().get("pending_sync") and not sent


def test_paused_schedules_send_nothing(env):
    at, save, sent = env
    storage.save_device("TEST", {"schedules_paused": True})
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    assert stored()[0]["executionTime"] == "06:00" and not sent


def test_resume_after_late_pause_keeps_protection(env):
    import main
    at, save, sent = env
    storage.save_device("TEST", {"schedules_paused": True})
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    at("2026-10-25T05:30:00")
    asyncio.run(main.handle_api_schedules_paused(Request({"paused": False})))
    assert sent[-1]["plans"][0]["skipEndTime"] == ms("2026-10-25T06:01:00")


@pytest.mark.parametrize("zone", ["", "Not/AZone", "Europe/London"])
def test_settings_timezone_change_keeps_pending_delivery(env, zone):
    at, save, sent = env
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    storage.save_settings({"feeder_timezone": zone})
    at("2026-10-25T05:30:00")
    tick()
    assert basis().get("pending_sync")
    assert sent[-1]["plans"][0]["skipEndTime"] == ms("2026-10-25T06:01:00")


def test_two_changes_without_ack_compare_with_original_list(env):
    at, save, sent = env
    devices._online["TEST"] = False
    save([plan()])
    at("2026-10-25T01:01:00"); tick()           # -> 06:00, never delivered
    at("2027-03-28T01:01:00"); tick()           # -> back to 05:00
    b = basis()
    assert stored()[0]["executionTime"] == "05:00"
    assert b["prev"]["delta"] == 0 and b["prev"]["plans"][0]["executionTime"] == "05:00"


# ── editor (revision) ────────────────────────────────────────────────────

def get(serial="TEST"):
    import main
    r = asyncio.run(main.handle_api_feeding_plans_get(Request()))
    return json.loads(r.text), r.headers["X-Plan-Revision"]


def post(plans, revision=None):
    import main
    q = {"revision": revision} if revision is not None else {}
    return asyncio.run(main.handle_api_feeding_plans_post(Request(plans, q))).status


def test_editor_load_shows_shifted_plans(env):
    at, save, sent = env
    save([plan()])
    at("2026-10-25T01:00:30")    # before the watchdog has run
    plans, _ = get()
    assert plans[0]["executionTime"] == "06:00"


def test_tab_opened_before_the_change_is_refused(env):
    at, save, sent = env
    save([plan()])
    at("2026-10-24T22:00:00")
    plans, rev = get()
    at("2026-10-25T01:01:00")
    tick()
    sent.clear()
    assert post(plans, rev) == 409
    assert stored()[0]["executionTime"] == "06:00" and not sent


def test_due_shift_not_yet_run_is_applied_before_comparing(env):
    at, save, sent = env
    save([plan()])
    at("2026-10-24T22:00:00")
    plans, rev = get()
    at("2026-10-25T01:00:20")    # offset changed, watchdog has not run
    assert post(plans, rev) == 409
    assert stored()[0]["executionTime"] == "06:00"   # reconciled, edit not written


def test_tab_opened_before_a_guard_cannot_remove_it(env):
    at, save, sent = env
    devices._online["TEST"] = False
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    plans, rev = get()                       # 06:00, no skip yet
    at("2026-10-25T05:30:00")                # old 05:00 may have run
    devices._online["TEST"] = True
    tick()                                   # guard added and sent
    devices._plan_ack_received("TEST", [{"planId": 1, "syncTime": basis()["pending_sync"]}])
    assert not basis().get("pending_sync")
    plans[0]["grainNum"] = 3                 # portions-only edit in the old tab
    assert post(plans, rev) == 409
    assert stored()[0]["skipEndTime"] == ms("2026-10-25T06:01:00")


def test_deliberate_unskip_with_current_revision_works(env):
    at, save, sent = env
    save([plan(skipEndTime=ms("2026-10-24T20:01:00"))])
    at("2026-10-24T12:00:00")
    plans, rev = get()
    del plans[0]["skipEndTime"]
    assert post(plans, rev) == 200
    assert "skipEndTime" not in stored()[0]
    plans2, rev2 = get()
    assert rev2 != rev and post(plans2, rev2) == 200    # next edit with the fresh pair


def test_timezone_setting_change_changes_revision(env):
    at, save, sent = env
    save([plan()])
    at("2026-10-24T12:00:00")
    plans, rev = get()
    storage.save_settings({"feeder_timezone": "Europe/London"})
    assert post(plans, rev) == 409
    assert stored()[0]["executionTime"] == "05:00"


def test_old_client_without_revision_still_saves(env):
    at, save, sent = env
    save([plan()])
    at("2026-10-24T12:00:00")
    assert post([plan("05:30")]) == 200


def test_edit_while_pending_keeps_protection(env, monkeypatch):
    # shift at 01:01, feeder offline, old 05:00 may run; portions edit at 05:30 whose
    # publish fails; the feeder asks for its plans at 05:40
    at, save, sent = env
    devices._online["TEST"] = False
    save([plan(), plan("16:00", pid=2)])
    at("2026-10-25T01:01:00")
    tick()
    at("2026-10-25T05:30:00")
    plans, rev = get()
    plans[1]["grainNum"] = 3
    mqtt = devices._client_ref
    monkeypatch.setattr(devices, "_client_ref", None)      # publish fails
    assert post(plans, rev) == 200
    b = basis()
    assert b.get("pending_sync") and b.get("prev")
    monkeypatch.setattr(devices, "_client_ref", mqtt)
    at("2026-10-25T05:40:00")
    asyncio.run(devices._respond_feeding_plan("TEST", "dl/PLAF301/TEST/device/event/post"))
    p = sent[-1]["plans"]
    assert p[0]["skipEndTime"] == ms("2026-10-25T06:01:00") and p[1]["grainNum"] == 3
    devices._plan_ack_received("TEST", [{"planId": i, "syncTime": basis()["pending_sync"]} for i in (1, 2)])
    assert not basis().get("pending_sync")


def test_edit_while_pending_does_not_guard_a_moved_meal(env):
    at, save, sent = env
    devices._online["TEST"] = False
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    at("2026-10-25T05:30:00")
    plans, rev = get()
    plans[0]["executionTime"] = "06:30"; plans[0].pop("skipEndTime", None)   # user moves it
    assert post(plans, rev) == 200
    assert "skipEndTime" not in stored()[0]


def test_edit_while_pending_with_changed_weekdays_is_not_guarded(env):
    at, save, sent = env
    devices._online["TEST"] = False
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    at("2026-10-25T05:30:00")
    plans, rev = get()
    plans[0]["repeatDay"] = [7]; plans[0].pop("skipEndTime", None)
    assert post(plans, rev) == 200
    assert "skipEndTime" not in stored()[0]


# ── fake device: list applied after the old deadline ─────────────────────

class FakeFeeder:
    """Runs the list it has installed; applies a sent list `delay_ms` later."""
    def __init__(self, plans):
        self.plans, self.queue, self.fired = plans, [], []
    def receive(self, plans, now_ms, delay_ms):
        self.queue.append((now_ms + delay_ms, plans))
    def run(self, start_ms, end_ms):
        for t in range(start_ms, end_ms, 1000):
            for at_ms, pl in [q for q in self.queue if q[0] <= t]:
                self.plans = pl; self.queue.remove((at_ms, pl))
            for p in self.plans:
                h, m = map(int, p["executionTime"].split(":"))
                day = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc)
                if (day.hour, day.minute, day.second) == (h, m, 0) and not storage.occurrence_skipped(p, t / 1000):
                    self.fired.append((p["planId"], t))


def test_list_sent_just_before_old_meal_but_applied_after(env):
    at, save, sent = env
    devices._online["TEST"] = False
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    feeder = FakeFeeder([plan()])                      # still runs 05:00
    devices._online["TEST"] = True
    at("2026-10-25T04:59:00")                          # resend 1 min before the old meal
    tick()
    feeder.receive(sent[-1]["plans"], ms("2026-10-25T04:59:00"), delay_ms=90000)   # lands 05:00:30
    feeder.run(ms("2026-10-25T04:59:00"), ms("2026-10-25T06:05:00"))
    assert len(feeder.fired) == 1                      # fed once (at 05:00), not again at 06:00


@pytest.mark.parametrize("bad", ["12:30\n", "１２:３０", "1٢:30"])
def test_wire_time_requires_ascii_fullmatch(bad):
    assert not devices._plans_valid([plan(bad)])


# ── shifting and baselines ────────────────────────────────────────────────

@pytest.mark.parametrize("t,days,delta,want", [
    ("05:00", None, -60, ("06:00", list(range(1, 8)))),       # CEST -> CET
    ("06:00", None, +60, ("05:00", list(range(1, 8)))),       # CET -> CEST
    ("23:30", [1, 3], -60, ("00:30", [2, 4])),                 # wraps forward a day
    ("00:15", [1], +60, ("23:15", [7])),                       # wraps back a day
    ("23:30", None, -60, ("00:30", list(range(1, 8)))),       # every day: days unchanged
    ("10:00", None, +30, ("09:30", list(range(1, 8)))),       # half-hour zones
])
def test_shift_plan_times(t, days, delta, want):
    src = [plan(t, **({"repeatDay": days} if days else {}), enableAudio=True)]
    q = devices.shift_plan_times(src, delta)[0]
    assert (q["executionTime"], q["repeatDay"]) == want
    assert src[0]["executionTime"] == t and q["enableAudio"] is True   # input untouched


@pytest.mark.parametrize("bad", [plan("24:00"), plan("5:00"), plan("05:00:00"),
                                 plan(repeatDay=[0]), plan(repeatDay=[8]), plan(repeatDay=["1"]), "x"])
def test_invalid_plans(bad):
    assert not devices._plans_valid([bad])


def test_polar_and_malformed_devices_are_left_alone(env, monkeypatch):
    at, save, sent = env
    monkeypatch.setattr(devices, "_devices", {"TEST": ("PLAF301", "one_rfid"), "POLAR": ("PLAF109", "polar"),
                                              "BAD": ("PLAF301", "one_rfid")})
    b = {"tz": "Europe/Copenhagen", "offset": 7200, "at": ms("2026-10-20T12:00:00")}
    storage.save_device_feeding_plans("POLAR", [plan()], tz_basis=b)
    storage.save_device_feeding_plans("BAD", [plan("25:99")], tz_basis=b)
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    assert storage.get_device_feeding_plans("POLAR")[0]["executionTime"] == "05:00"
    assert storage.get_device_feeding_plans("BAD")[0]["executionTime"] == "25:99"
    assert stored()[0]["executionTime"] == "06:00"          # one bad device blocks nothing


def test_nothing_happens_before_the_change(env):
    at, save, sent = env
    save([plan()])
    at("2026-10-25T00:59:00")
    tick()
    assert not sent and stored()[0]["executionTime"] == "05:00"


def test_legacy_plans_are_baselined_not_shifted(env):
    at, save, sent = env
    storage.save_device_feeding_plans("TEST", [plan()])       # saved before this feature
    at("2026-10-25T12:00:00")
    tick()
    assert stored()[0]["executionTime"] == "05:00" and basis()["offset"] == 3600 and not sent


def test_timezone_change_keeps_utc_times(env):
    at, save, sent = env
    save([plan()])
    storage.save_settings({"feeder_timezone": "Europe/London"})
    at("2026-10-24T12:00:00")
    tick()
    assert stored()[0]["executionTime"] == "05:00" and basis()["tz"] == "Europe/London" and not sent


@pytest.mark.parametrize("zone", ["", "Not/AZone"])
def test_no_usable_settings_timezone(env, zone):
    import main
    at, save, sent = env
    storage.save_settings({"feeder_timezone": zone})
    assert devices.current_plan_tz_basis() is None
    save([plan()])
    at("2026-10-24T12:00:00")
    asyncio.run(main.handle_api_feeding_plans_post(Request([plan()])))
    assert basis() is None                                   # unknown basis: never shifted


# ── round-4 conformance fixes ─────────────────────────────────────────────

def test_disable_all_while_pending_keeps_protection_for_reenable(env, monkeypatch):
    at, save, sent = env
    devices._online["TEST"] = False
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    at("2026-10-25T04:00:00")
    plans, rev = get()
    plans[0]["_enabled"] = False
    mqtt = devices._client_ref
    monkeypatch.setattr(devices, "_client_ref", None)        # publish fails
    assert post(plans, rev) == 200
    devices._plan_ack_received("TEST", [])                   # empty ack must not clear it
    assert basis().get("pending_sync") and basis().get("prev")
    monkeypatch.setattr(devices, "_client_ref", mqtt)
    devices._online["TEST"] = True
    at("2026-10-25T05:30:00")                                # old 05:00 may have run
    plans, rev = get()
    plans[0]["_enabled"] = True
    assert post(plans, rev) == 200
    assert sent[-1]["plans"][0]["skipEndTime"] == ms("2026-10-25T06:01:00")


def test_disabled_while_pending_does_not_resend(env):
    at, save, sent = env
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    plans, rev = get()
    plans[0]["_enabled"] = False
    assert post(plans, rev) == 200
    sent.clear()
    at("2026-10-25T01:30:00")
    tick()
    assert not sent


def test_zero_cumulative_shift_never_skips(env):
    at, save, sent = env
    devices._online["TEST"] = False
    save([plan()])
    at("2026-10-25T01:01:00"); tick()
    at("2027-03-28T01:01:00"); tick()                        # back to 05:00, delta 0
    devices._online["TEST"] = True
    at("2027-03-28T04:59:00")                                # inside the margin
    tick()
    assert "skipEndTime" not in sent[-1]["plans"][0]


def test_removing_an_active_guard_is_refused_while_pending(env):
    at, save, sent = env
    devices._online["TEST"] = False
    save([plan()])
    at("2026-10-25T01:01:00")
    tick()
    at("2026-10-25T05:30:00")
    plans, rev = get()
    assert plans[0]["skipEndTime"] == ms("2026-10-25T06:01:00")
    del plans[0]["skipEndTime"]                              # user un-skips
    assert post(plans, rev) == 409
    assert stored()[0]["skipEndTime"] == ms("2026-10-25T06:01:00")


def test_guard_logs_do_not_claim_certainty(env, caplog):
    import logging
    caplog.set_level(logging.INFO)
    at, save, sent = env
    save([plan("00:30")])
    at("2026-10-25T01:01:00")
    tick()
    assert "may have run" in caplog.text and "has passed" not in caplog.text
