const BASE = window.__BASE__;
const VERSION = window.__VERSION__;

// Delays calling fn until ms have passed since the last call. Used for
// auto-save fields where rapid repeated changes (e.g. clicking a number
// input's spinner arrows) would otherwise send one command per click.
function _debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// ── i18n ──────────────────────────────────────────────────────────────────
let _t = {};

async function loadLocale(lang) {
  try {
    const r = await fetch(`${BASE}/locales/${lang}.json`);
    if (r.ok) _t = await r.json();
  } catch {}
}

function t(key, vars = {}) {
  let s = _t[key] || key;
  for (const [k, v] of Object.entries(vars)) s = s.replace(`{${k}}`, v);
  return s;
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach(el => {
    const key = el.dataset.i18n;
    if (key) el.textContent = t(key);
  });
}

// ── Toast ─────────────────────────────────────────────────────────────────
function showToast(msg, durationMs = 2500) {
  const el = document.createElement("div");
  el.textContent = msg;
  el.style.cssText = "position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--pl-surface-2,#2a2a3a);border:1px solid var(--pl-border,#333);border-radius:8px;padding:10px 18px;font-size:13px;z-index:9999;max-width:90vw;text-align:center;pointer-events:none";
  document.body.appendChild(el);
  setTimeout(() => el.remove(), durationMs);
}

// ── State ─────────────────────────────────────────────────────────────────
let _devices = [];
let _pets = [];
let _settings = {};
let _notifyServices = [];
let _currentDevice = null;
let _currentDeviceTab = "overview";
let _editPetId = null;
let _wizardStep = 0;
let _capturePolling = null;
let _lastDeviceRenderKey = "";
let _lastPetRenderKey = "";
let _deviceSort = localStorage.getItem("pl_device_sort") || "name";

const DEVICE_ICONS = {
  dockstream2: "💧",
  dockstream2_cordless: "💧",
  dockstream_rfid: "💧",
  one_rfid: "🐾",
};

const DEVICE_REQUIRES_PET = {
  one_rfid: true,
};

function deviceIcon(d) {
  return DEVICE_ICONS[d.device_type] || "🥣";
}

// ── Unit conversion ───────────────────────────────────────────────────────
function useImperial() {
  const u = _settings.units || "auto";
  if (u === "imperial") return true;
  if (u === "metric") return false;
  // auto: en-US → imperial, everything else → metric
  const lang = (navigator.languages?.[0] || navigator.language || "en").toLowerCase();
  return lang === "en-us" || lang.startsWith("en-us");
}

function gramsToDisplay(grams) {
  if (grams == null) return null;
  if (useImperial()) {
    return { value: (grams * 0.033814).toFixed(2), unit: "fl oz" };
  }
  return { value: Math.round(grams).toString(), unit: "ml" };
}

const _DISPLAY_ICONS = { 5: "❤ Heart", 6: "🐕 Dog", 7: "🐱 Cat", 8: "🦌 Elk" };
const _DISPLAY_ICON_EMOJIS = { 5: "❤️", 6: "🐕", 7: "🐱", 8: "🦌" };
function _fmtDisplayLabel(d) {
  if (d.display_icon_name) return d.display_icon_name;
  if (d.display_text) return d.display_text;
  if (d.display_icon) return _DISPLAY_ICON_EMOJIS[d.display_icon] || "Icon " + d.display_icon;
  return "";
}

function fmtWater(grams) {
  if (grams == null) return "—";
  const d = gramsToDisplay(grams);
  return `${d.value} ${d.unit}`;
}

function fmtDays(days) {
  if (days == null) return "—";
  return t("unit.days", {n: days});
}

function fmtDuration(secs) {
  const m = Math.floor(secs / 60), s = secs % 60;
  return m > 0 ? `${m}m${String(s).padStart(2, "0")}s` : `${s}s`;
}

function fmtTime(ts) {
  return new Date(ts).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function fmtDate(ts) {
  const d = new Date(ts);
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return t("time.today");
  if (d.toDateString() === yesterday.toDateString()) return t("time.yesterday");
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function fmtTime12(hhmm) {
  if (!hhmm) return "--";
  const [h, m] = hhmm.split(":").map(Number);
  const ampm = h < 12 ? t("time.am") : t("time.pm");
  const h12 = h % 12 || 12;
  return `${h12}:${String(m).padStart(2, "0")} ${ampm}`;
}

// The feeder's FEEDING_PLAN_SERVICE executionTime field is always UTC,
// regardless of the NTP timezone sent to the feeder. The PetLibro app
// silently converts local time to UTC before sending plans. We do the
// same here: display and time-picker use local time; stored value is UTC.
function _tzOffsetHours(tz) {
  // Returns current UTC offset in hours for an IANA timezone name.
  // Uses Intl to format the same moment in both zones, then diffs the strings.
  if (!tz) return -7;
  try {
    const now = new Date();
    const fmt = (zone) => new Intl.DateTimeFormat("sv-SE", {
      timeZone: zone,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit"
    }).format(now).replace(" ", "T") + "Z";
    return (new Date(fmt(tz)) - new Date(fmt("UTC"))) / 3600000;
  } catch(e) {
    return _settings.feeder_tz_offset ?? -7;
  }
}
function _schedTzOffset() {
  const tz = _settings.feeder_timezone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  return _tzOffsetHours(tz);
}
function _utcToLocal(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  const mins = ((h * 60 + m + _schedTzOffset() * 60) % 1440 + 1440) % 1440;
  return `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;
}
function _localToUtc(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  const mins = ((h * 60 + m - _schedTzOffset() * 60) % 1440 + 1440) % 1440;
  return `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;
}

// Rounded, not floored: a value just saved as "N days" is always read back a
// few milliseconds later, so flooring would show N-1 essentially every time
// (20.99999 days floors to 20 even though nothing meaningfully elapsed).
function filterDaysRemaining(d) {
  const ts = d.filterNextReplacementTimestamp;
  if (ts == null) return null;
  return Math.max(0, Math.round((ts - Date.now()) / 86400000));
}

function cleaningDaysRemaining(d) {
  const ts = d.last_cleaned_ts;
  const interval = d.cleaning_interval_days ?? 30;
  if (ts == null) return null;
  const due = ts + interval * 86400000;
  return Math.round((due - Date.now()) / 86400000);
}

function desiccantDaysRemaining(d) {
  const ts = d.last_desiccant_ts;
  const interval = d.desiccant_life_days ?? 14;
  if (ts == null) return null;
  const due = ts + interval * 86400000;
  return Math.round((due - Date.now()) / 86400000);
}
function bowlDaysRemaining(d) {
  const ts = d.last_bowl_cleaned_ts;
  const interval = d.bowl_cleaning_interval_days ?? 7;
  if (ts == null) return null;
  return Math.round((ts + interval * 86400000 - Date.now()) / 86400000);
}
function housingDaysRemaining(d) {
  const ts = d.last_housing_cleaned_ts;
  const interval = d.housing_cleaning_interval_days ?? 30;
  if (ts == null) return null;
  return Math.round((ts + interval * 86400000 - Date.now()) / 86400000);
}
function _fmt12h(h, m) {
  const ampm = h >= 12 ? t("time.pm") : t("time.am");
  return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${ampm}`;
}
const _WDAY_KEYS = ["time.wday_sun","time.wday_mon","time.wday_tue","time.wday_wed","time.wday_thu","time.wday_fri","time.wday_sat"];
function nextMealLabel(plans) {
  if (!plans || !plans.length) return null;
  const enabled = plans.filter(p => p._enabled !== false);
  if (!enabled.length) return null;
  const now = new Date();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  for (let off = 0; off < 8; off++) {
    const day = new Date(now); day.setDate(day.getDate() + off);
    const plDay = day.getDay() === 0 ? 7 : day.getDay(); // PetLibro: 1=Mon…7=Sun
    const candidates = enabled
      .filter(p => (p.repeatDay || []).includes(plDay))
      .map(p => ({ ...p, localTime: _utcToLocal(p.executionTime) }))
      .sort((a, b) => a.localTime.localeCompare(b.localTime));
    for (const plan of candidates) {
      const [h, m] = plan.localTime.split(":").map(Number);
      if (off === 0 && h * 60 + m <= nowMin) continue;
      return _fmt12h(h, m);
    }
  }
  return null;
}

function rssiClass(rssi) {
  if (rssi == null) return "rssi-na";
  if (rssi >= -65) return "rssi-good";
  if (rssi >= -80) return "rssi-ok";
  return "rssi-poor";
}

function rssiLabel(rssi) {
  if (rssi == null) return t("signal.none");
  if (rssi >= -65) return `${rssi} dBm — ${t("signal.good")}`;
  if (rssi >= -80) return `${rssi} dBm — ${t("signal.fair")}`;
  return `${rssi} dBm — ${t("signal.poor")}`;
}
