// ── Polar Wet Food Feeder (PLAF109), EARLY BETA ────────────────────────────
// Everything for the Polar in the web UI lives here; the other files only call
// buildPolarOverview, wirePolarOverview, polarCardStats and polarLogLine.
// What the commands rest on, and what is unknown, is in polar_control.py.

const POLAR_LIMITS = { seconds: [1, 600], minutes: [1, 240] };
let _polarTimer = null;

function _polarUnitLabel(unit) {
  return unit === "seconds" ? t("polar.unit_seconds") : unit === "minutes" ? t("polar.unit_minutes") : t("polar.unit_unset");
}

function _polarStatusText(d) {
  if (!d.online) return t("polar.offline");
  if (d.polar_feed_active) return d.polar_feed_step ? t("polar.feeding_step", { step: d.polar_feed_step }) : t("polar.feeding");
  return t("polar.idle");
}

function _polarLastFeedText(d) {
  const f = d.polar_last_feed;
  if (!f) return t("polar.no_feed_yet");
  const who = f.source === "requested" ? t("polar.src_requested") : t("polar.src_feeder");
  const open = f.door_open_secs != null ? t("polar.door_open_for", { secs: f.door_open_secs }) : t("polar.door_open_unknown");
  let text = `${who}, ${t("polar.plate_n", { n: f.plate ?? "?" })}, ${open}`;
  if (f.stopped) text += `, ${t("polar.stopped_early")}`;
  if (f.suggested_unit) text += `. ${t("polar.suggests", { unit: _polarUnitLabel(f.suggested_unit).toLowerCase() })}`;
  return text;
}

function buildPolarOverview(d) {
  const unit = d.polar_duration_unit || "";
  const [lo, hi] = POLAR_LIMITS[unit] || [1, 600];
  const duration = d.polar_serve_duration ?? (unit === "seconds" ? 240 : unit === "minutes" ? 30 : "");
  const plate = d.platePosition;
  const temp = typeof d.temperature === "number" ? `${d.temperature.toFixed(1)} °C` : "—";
  return `
  <div style="background:rgba(224,168,85,.12);border:1px solid rgba(224,168,85,.5);color:var(--pl-warning);border-radius:var(--pl-radius-sm);padding:10px 12px;font-size:13px;line-height:1.5;margin-bottom:14px">
    ${t("polar.beta_notice")}
  </div>
  <div class="detail-stats">
    <div class="detail-stat" style="padding:8px 14px">
      <div class="detail-stat-label">${t("polar.status")}</div>
      <div class="detail-stat-value accent" id="polar-status">${escHtml(_polarStatusText(d))}</div>
    </div>
    <div class="detail-stat" style="padding:8px 14px">
      <div class="detail-stat-label">${t("polar.plate")}</div>
      <div class="detail-stat-value" id="polar-plate">${plate >= 1 && plate <= 3 ? plate : "—"}</div>
    </div>
    <div class="detail-stat" style="padding:8px 14px">
      <div class="detail-stat-label">${t("polar.temperature")}</div>
      <div class="detail-stat-value" id="polar-temp">${escHtml(temp)}</div>
    </div>
  </div>
  <div class="form-hint" id="polar-lastfeed" style="margin-top:8px">${t("polar.last_feed")}: ${escHtml(_polarLastFeedText(d))}</div>

  <div class="tab-section-heading" style="margin-top:16px">${t("polar.serve")}</div>
  <div style="display:flex;gap:8px;margin-bottom:8px">
    ${[1, 2, 3].map(n => `<button class="btn-primary polar-serve" data-plate="${n}" style="flex:1;margin-top:0">${t("polar.plate_n", { n })}</button>`).join("")}
  </div>
  <div style="display:flex;gap:8px;margin-bottom:8px">
    <button class="btn-danger" id="polar-stop" style="flex:1">${t("polar.stop")}</button>
    <button class="btn-secondary" id="polar-open-lid" style="flex:1">${t("polar.open_lid")}</button>
    <button class="btn-secondary" id="polar-close-lid" style="flex:1">${t("polar.close_lid")}</button>
  </div>

  <div class="tab-section-heading" style="margin-top:16px">${t("polar.move_plate")}</div>
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
    <select class="form-input" id="polar-move-plate" style="width:110px">
      ${[1, 2, 3].map(n => `<option value="${n}"${plate === n ? " selected" : ""}>${t("polar.plate_n", { n })}</option>`).join("")}
    </select>
    <button class="btn-secondary" id="polar-move" style="flex:1">${t("polar.move")}</button>
  </div>

  <div class="tab-section-heading" style="margin-top:16px">${t("polar.door_time")}</div>
  <div class="form-hint" style="margin-bottom:8px;line-height:1.5">${unit ? t("polar.unit_known_hint") : t("polar.unit_unknown_hint")}</div>
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
    <select class="form-input" id="polar-unit" style="width:150px">
      <option value=""${unit ? "" : " selected"}>${t("polar.unit_unset")}</option>
      <option value="seconds"${unit === "seconds" ? " selected" : ""}>${t("polar.unit_seconds")}</option>
      <option value="minutes"${unit === "minutes" ? " selected" : ""}>${t("polar.unit_minutes")}</option>
    </select>
    <input class="form-input" id="polar-duration" type="number" min="${lo}" max="${hi}" step="1" value="${escHtml(duration)}" style="width:90px"${unit ? "" : " disabled"}>
    <button class="btn-secondary" id="polar-save-door">${t("polar.save")}</button>
  </div>`;
}

function polarCardStats(d) {
  const temp = typeof d.temperature === "number" ? `${d.temperature.toFixed(1)}°` : "—";
  let lastFed = "";
  if (d.last_fed_ts) {
    const lf = new Date(d.last_fed_ts * 1000);
    const diffH = (new Date() - lf) / 3600000;
    const label = diffH < 1 ? t("time.ago_minutes", { n: Math.round(diffH * 60) })
      : diffH < 24 ? _fmt12h(lf.getHours(), lf.getMinutes())
      : t(_WDAY_KEYS[lf.getDay()]) + " " + _fmt12h(lf.getHours(), lf.getMinutes());
    lastFed = `<div class="card-stat"><div class="stat-label">${t("card.last_fed")}</div><div class="stat-value">${escHtml(label)}</div></div>`;
  }
  return `
    <div class="card-stat">
      <div class="stat-label">${t("polar.status")}</div>
      <div class="stat-value accent">${escHtml(_polarStatusText(d))}</div>
    </div>
    ${lastFed}
    <div class="card-stat stat-secondary">
      <div class="stat-label">${t("polar.temperature")}</div>
      <div class="stat-value">${escHtml(temp)}</div>
    </div>`;
}

function polarLogLine(e) {
  const when = `<span style="color:var(--pl-subtext)">${escHtml(fmtTime(e.ts))}</span>`;
  const who = e.source === "requested" ? t("polar.src_requested") : t("polar.src_feeder");
  let text = `${who}, ${t("polar.plate_n", { n: e.plate ?? "?" })}`;
  if (e.door_open_secs != null) text += `, ${t("polar.door_open_for", { secs: e.door_open_secs })}`;
  if (e.stopped) text += `, ${t("polar.stopped_early")}`;
  return `${when} ${t("polar.log_feed")}: ${escHtml(text)}`;
}

async function _polarRefreshLive(serial) {
  // Updates the live fields in place (never re-renders, so the inputs are not clobbered).
  if (!document.getElementById("polar-status")) { clearInterval(_polarTimer); _polarTimer = null; return; }
  try {
    const list = await api("GET", "/api/devices");
    const fresh = list.find(x => x.serial === serial);
    if (!fresh) return;
    _patchDevice(serial, fresh);
    document.getElementById("polar-status").textContent = _polarStatusText(fresh);
    const p = document.getElementById("polar-plate");
    if (p) p.textContent = fresh.platePosition >= 1 && fresh.platePosition <= 3 ? fresh.platePosition : "—";
    const tp = document.getElementById("polar-temp");
    if (tp) tp.textContent = typeof fresh.temperature === "number" ? `${fresh.temperature.toFixed(1)} °C` : "—";
    const lf = document.getElementById("polar-lastfeed");
    if (lf) lf.textContent = `${t("polar.last_feed")}: ${_polarLastFeedText(fresh)}`;
  } catch {}
}

function wirePolarOverview(d) {
  if (_polarTimer) clearInterval(_polarTimer);
  _polarTimer = setInterval(() => _polarRefreshLive(d.serial), 3000);

  const send = async (body, okMsg) => {
    try {
      const r = await api("POST", `/api/devices/${d.serial}/command`, body);
      if (okMsg) showToast(okMsg);
      setTimeout(() => _polarRefreshLive(d.serial), 800);
      return r;
    } catch (e) {
      showToast(String(e.message || e).replace(/^\d+\s*/, ""), 5000);
      return null;
    }
  };

  document.querySelectorAll(".polar-serve").forEach(btn => {
    btn.onclick = async () => {
      const plate = parseInt(btn.dataset.plate);
      const cur = _devices.find(x => x.serial === d.serial) || d;
      const unit = cur.polar_duration_unit || "";
      const msg = unit
        ? t("polar.confirm_serve", { plate, duration: cur.polar_serve_duration ?? (unit === "seconds" ? 240 : 30), unit: _polarUnitLabel(unit).toLowerCase() })
        : t("polar.confirm_test", { plate });
      if (!await plConfirm({ title: t("polar.serve"), message: msg, confirmText: t("polar.serve_btn"), cancelText: t("common.cancel"), focus: "confirm" })) return;
      const r = await send({ _polar_serve: { plate } });
      if (r && r.test) showToast(t("polar.test_started"), 6000);
    };
  });
  const stop = document.getElementById("polar-stop");
  if (stop) stop.onclick = () => send({ _polar_stop: true }, t("polar.stop_sent"));
  const openLid = document.getElementById("polar-open-lid");
  if (openLid) openLid.onclick = () => send({ _polar_door: true }, t("polar.sent"));
  const closeLid = document.getElementById("polar-close-lid");
  if (closeLid) closeLid.onclick = () => send({ _polar_door: false }, t("polar.sent"));
  const move = document.getElementById("polar-move");
  if (move) move.onclick = () => send({ _polar_plate_position: parseInt(document.getElementById("polar-move-plate").value) }, t("polar.sent"));

  const unitEl = document.getElementById("polar-unit");
  const durEl = document.getElementById("polar-duration");
  if (unitEl && durEl) {
    unitEl.onchange = () => {
      const [lo, hi] = POLAR_LIMITS[unitEl.value] || [1, 600];
      durEl.min = lo; durEl.max = hi; durEl.disabled = !unitEl.value;
      if (unitEl.value && !durEl.value) durEl.value = unitEl.value === "seconds" ? 240 : 30;
    };
  }
  const save = document.getElementById("polar-save-door");
  if (save) {
    save.onclick = async () => {
      const unit = unitEl.value;
      const body = { polar_duration_unit: unit || null };
      if (unit) {
        const v = parseInt(durEl.value);
        const [lo, hi] = POLAR_LIMITS[unit];
        if (!(v >= lo && v <= hi)) { showToast(t("polar.bad_duration", { lo, hi, unit: _polarUnitLabel(unit).toLowerCase() }), 5000); return; }
        body.polar_serve_duration = v;
      }
      try {
        await api("POST", `/api/devices/${d.serial}`, body);
        _patchDevice(d.serial, body);
        showToast(t("polar.saved"));
        renderDeviceTab("overview");
      } catch (e) {
        showToast(String(e.message || e).replace(/^\d+\s*/, ""), 5000);
      }
    };
  }
}
