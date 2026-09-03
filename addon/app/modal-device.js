// ── Device detail modal ────────────────────────────────────────────────────
function openDeviceModal(device) {
  _currentDevice = device;
  _currentDeviceTab = "overview";

  const imgEl = document.getElementById("detail-img");
  imgEl.innerHTML = device.image_url
    ? `<img src="${escHtml(device.image_url)}" alt="">`
    : `<span>${deviceIcon(device)}</span>`;

  document.getElementById("detail-name").textContent = device.name || device.device_type || device.serial;
  document.getElementById("detail-room").textContent = device.room || "";

  const badge = document.getElementById("detail-status-badge");
  badge.textContent = device.online ? t("device.online") : t("device.offline");
  badge.className = `status-badge ${device.online ? "online" : "offline"}`;

  const rssiEl = document.getElementById("detail-rssi");
  rssiEl.className = `rssi-badge ${rssiClass(device.rssi)}`;
  rssiEl.textContent = rssiLabel(device.rssi);
  const fwEl = document.getElementById("detail-fw");
  if (device.softwareVersion) {
    fwEl.textContent = `fw: ${device.softwareVersion}`;
    fwEl.style.display = "";
  } else {
    fwEl.style.display = "none";
  }

  const hwEl = document.getElementById("detail-hw");
  if (device.hardwareVersion) {
    hwEl.textContent = `hw: ${device.hardwareVersion}`;
    hwEl.style.display = "";
  } else {
    hwEl.style.display = "none";
  }

  const powerEl = document.getElementById("detail-power");
  if (device.device_type === "one_rfid" && device.electricQuantity != null && device.electricQuantity > 0) {
    const onAC = device.powerType !== 2;
    powerEl.textContent = onAC ? `🔌 ${t("power.ac")}` : `🔋 ${device.electricQuantity}%`;
    powerEl.style.display = "";
  } else {
    powerEl.style.display = "none";
  }

  const isFeeder = device.device_type === "one_rfid";
  const isFountain = device.device_type?.startsWith("dockstream");
  document.querySelectorAll(".dtab").forEach(b => {
    b.classList.toggle("active", b.dataset.dtab === "overview");
    if (b.dataset.dtab === "log") b.style.display = "";
    if (b.dataset.dtab === "schedule") b.style.display = isFeeder ? "" : "none";
    if (b.dataset.dtab === "controls") b.style.display = isFountain ? "" : "none";
  });

  renderDeviceTab("overview");

  document.getElementById("btn-detail-delete").onclick = () => deleteDevice(device.serial);
  document.getElementById("btn-detail-edit").onclick = openEditDevice;
  document.getElementById("modal-device").classList.add("open");
}

function renderDeviceTab(tabName) {
  _currentDeviceTab = tabName;
  const content = document.getElementById("dtab-content");
  if (tabName === "overview") content.innerHTML = buildOverviewTab(_currentDevice);
  else if (tabName === "controls") content.innerHTML = buildControlsTab(_currentDevice);
  else if (tabName === "maintenance") content.innerHTML = buildMaintenanceTab(_currentDevice);
  else if (tabName === "log") {
    content.innerHTML = `<p style="color:var(--pl-subtext);padding:16px 0;text-align:center">Loading...</p>`;
    if (_currentDevice.device_type === "one_rfid") {
      api("GET", `/api/devices/${_currentDevice.serial}/feeder-log`)
        .then(log => { content.innerHTML = buildFeederLogTab(_currentDevice, log); })
        .catch(() => { content.innerHTML = `<p style="color:var(--pl-danger);padding:16px 0">${t("device_modal.load_failed_log")}</p>`; });
    } else if (_currentDevice.device_type?.startsWith("dockstream")) {
      api("GET", `/api/devices/${_currentDevice.serial}/fountain-log`)
        .then(log => { content.innerHTML = buildFountainLogTab(_currentDevice, log); })
        .catch(() => { content.innerHTML = `<p style="color:var(--pl-danger);padding:16px 0">${t("device_modal.load_failed_log")}</p>`; });
    } else {
      api("GET", `/api/devices/${_currentDevice.serial}/intake?days=7`)
        .then(history => { content.innerHTML = buildLogTab(_currentDevice, history); })
        .catch(() => { content.innerHTML = `<p style="color:var(--pl-danger);padding:16px 0">${t("device_modal.load_failed_intake")}</p>`; });
    }
    return;
  }
  else if (tabName === "schedule") {
    content.innerHTML = `<p style="color:var(--pl-subtext);padding:16px 0;text-align:center">Loading...</p>`;
    api("GET", `/api/devices/${_currentDevice.serial}/feeding-plans`)
      .then(plans => {
        _schedPlans = plans;
        content.innerHTML = buildScheduleTab(plans);
        wireScheduleTabHandlers();
      })
      .catch(() => { content.innerHTML = `<p style="color:var(--pl-danger);padding:16px 0">${t("device_modal.load_failed_schedule")}</p>`; });
    return;
  }
  else if (tabName === "notifications") content.innerHTML = buildNotificationsTab(_currentDevice);
  wireDeviceTabHandlers(tabName);
}

// ── Overview tab ──────────────────────────────────────────────────────────
function buildOverviewTab(device) {
  const filterDays = filterDaysRemaining(device);
  const filterClass = filterDays != null && filterDays <= 3 ? "danger" : "accent";
  const cleanDays = cleaningDaysRemaining(device);
  const cleanClass = cleanDays != null && cleanDays < 1 ? "danger" : "";
  const intakeHtml = device.intake_today_grams > 0
    ? `<div class="detail-stat">
        <div class="detail-stat-label">${t("overview.intake_today")}</div>
        <div class="detail-stat-value accent">${escHtml(fmtWater(device.intake_today_grams))}</div>
      </div>`
    : "";
  const cleanHtml = cleanDays != null
    ? `<div class="detail-stat">
        <div class="detail-stat-label">${t("overview.next_clean")}</div>
        <div class="detail-stat-value ${cleanClass}">${escHtml(fmtDays(Math.max(0, cleanDays)))}</div>
      </div>`
    : "";

  let petIntakeHtml = "";
  if (device.device_type === "dockstream_rfid") {
    const pets = device.pets || [];
    if (pets.length) {
      const rows = pets.map(p => `
      <div class="intake-row">
        <div class="intake-avatar">${p.image_url ? `<img src="${escHtml(p.image_url)}" alt="${escHtml(p.name)}">` : "🐾"}</div>
        <div class="intake-info">
          <div class="intake-name">${escHtml(p.name || t("pet.unnamed"))}</div>
          <div class="intake-stats">${p.visits ? t("overview.pet_intake_stats", {duration: fmtDuration(p.duration_secs || 0), n: p.visits}) : t("overview.pet_intake_none")}</div>
        </div>
        <div class="intake-value">${escHtml(fmtWater(p.grams || 0))}</div>
      </div>`).join("");
      petIntakeHtml = `
  <div class="tab-section-heading" style="margin-top:18px">${t("overview.pet_activity")}</div>
  <div class="intake-list">${rows}</div>`;
    }
  }

  if (device.device_type === "one_rfid") {
    const foodOk     = device.surplusGrain;
    const foodClass  = foodOk === false ? "danger" : "accent";
    const powerIsAC  = device.powerType !== 2;
    const hasBattery = device.electricQuantity != null && device.electricQuantity > 0;
    const lowPct     = device.battery_low_pct ?? 20;
    const battClass  = hasBattery && device.electricQuantity <= lowPct ? "danger" : "";
    const closeSpeed = device.coverCloseSpeed ?? "FAST";
    const openMode   = device.coverOpenMode   ?? "CUSTOM";
    const closeSec   = device.closeDoorTimeSec ?? 10;
    const childLock  = device.childLockSwitch  ?? false;
    const soundOn    = device.soundSwitch !== false;
    const volume     = device.volume ?? 50;
    const foodLabel = foodOk === false ? t("food.low") : foodOk === true ? t("food.ok") : "—";
    const battPct  = hasBattery
      ? `${device.electricQuantity}% (${powerIsAC ? t("power.ac") : t("power.battery")})`
      : (powerIsAC ? t("power.ac") : "—");
    return `<div class="detail-stats">
      <div class="detail-stat" style="padding:8px 14px">
        <div class="detail-stat-label">${t("overview.food_level")}</div>
        <div class="detail-stat-value ${foodClass}">${foodLabel}</div>
      </div>
      <div class="detail-stat" style="padding:8px 14px">
        <div class="detail-stat-label">${t("overview.battery")}</div>
        <div class="detail-stat-value ${battClass}">${battPct}</div>
      </div>
    </div>
    <div style="margin-top:16px">
      <div class="tab-section-heading">${t("overview.controls")}</div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <label style="white-space:nowrap;font-size:13px;color:var(--pl-subtext)">${t("overview.portions")}</label>
        <select class="form-input" id="feeder-portions" style="width:80px">
          <option value="1" selected>1</option>
          <option value="2">2</option>
          <option value="3">3</option>
          <option value="4">4</option>
          <option value="5">5</option>
        </select>
        <button class="btn-primary" id="btn-feed-now" style="flex:1">${t("overview.feed_now")}</button>
      </div>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button class="btn-secondary" id="btn-open-lid" style="flex:1">${t("overview.open_lid")}</button>
        <button class="btn-secondary" id="btn-close-lid" style="flex:1">${t("overview.close_lid")}</button>
      </div>
    </div>
    <div style="margin-top:16px">
      <div class="form-row">
        <label class="form-label">${t("overview.lid_mode")}</label>
        <select class="form-input" id="feeder-cover-open-mode">
          <option value="KEEP_CLOSE" ${openMode === "KEEP_CLOSE" ? "selected" : ""}>${t("overview.lid_always_closed")}</option>
          <option value="CUSTOM"     ${openMode === "CUSTOM"     ? "selected" : ""}>${t("overview.lid_auto_close")}</option>
          <option value="KEEP_OPEN"  ${openMode === "KEEP_OPEN"  ? "selected" : ""}>${t("overview.lid_stay_open")}</option>
        </select>
      </div>
      <div style="display:flex;gap:8px">
        <div style="flex:1">
          <label class="form-label">${t("overview.close_speed")}</label>
          <select class="form-input" id="feeder-cover-close-speed">
            <option value="FAST" ${closeSpeed === "FAST" ? "selected" : ""}>${t("overview.close_speed_fast")}</option>
            <option value="SLOW" ${closeSpeed === "SLOW" ? "selected" : ""}>${t("overview.close_speed_slow")}</option>
          </select>
        </div>
        <div style="flex:1">
          <label class="form-label">${t("overview.auto_close_sec")}</label>
          <input class="form-input" id="feeder-close-door-sec" type="number" min="1" max="10" value="${closeSec}">
        </div>
      </div>
      <div class="form-row" style="align-items:center;gap:16px;flex-wrap:wrap;margin-top:8px">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="feeder-sound-switch" ${soundOn ? "checked" : ""}> ${t("overview.sound")}
          <input class="form-input" id="feeder-volume" type="number" min="0" max="100" value="${volume}" style="width:72px;margin-left:4px">
        </label>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="feeder-child-lock" ${childLock ? "checked" : ""}> ${t("overview.child_lock")}
        </label>
      </div>
    </div>
    <div style="margin-top:16px">
      <div class="tab-section-heading">${t("overview.led_display")}</div>
      <div class="form-row">
        <label class="form-label">${t("overview.scrolling_text")}</label>
        <div style="display:flex;gap:8px">
          <input class="form-input" id="feeder-display-text" type="text" maxlength="20" placeholder="e.g. MOCHI" value="${escHtml(device.display_text || "")}" oninput="this.value=this.value.toUpperCase()">
          <button class="btn-secondary" id="btn-send-display-text" style="flex-shrink:0">Send</button>
        </div>
        <p class="form-hint">${t("overview.display_text_hint")}</p>
      </div>
      <button class="btn-secondary" id="btn-open-icon-editor" style="width:100%;margin-top:4px">${t("overview.icon_editor_btn")}</button>
    </div>
    ${(!device.pets?.length) ? `
    <div style="background:rgba(224,168,85,.12);border:1px solid rgba(224,168,85,.5);color:var(--pl-warning);border-radius:var(--pl-radius-sm);padding:10px 12px;font-size:13px;margin-top:14px;line-height:1.5">
      ${t("overview.no_pets")}
    </div>` : ""}`;
  }

  return `<div class="detail-stats">
    <div class="detail-stat">
      <div class="detail-stat-label">${t("overview.water_level")}</div>
      <div class="detail-stat-value accent">${escHtml(fmtWater(device.currentWeight ?? null))}</div>
    </div>
    <div class="detail-stat">
      <div class="detail-stat-label">${t("overview.filter_due")}</div>
      <div class="detail-stat-value ${filterClass}">${escHtml(fmtDays(filterDays))}</div>
    </div>
    ${intakeHtml}
    ${cleanHtml}
  </div>${petIntakeHtml}`;
}

// Polls /api/devices for a scale-calibration ack newer than sentAt, up to
// ~6s. Returns "container" (code 3007 -- water container still on the
// scale), "ok" (a response carrying the "zero_standar" baseline msg), or
// "unknown" if nothing definitive showed up in the window.
async function _pollCalibrationResult(serial, sentAt) {
  for (let i = 0; i < 6; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const list = await api("GET", "/api/devices");
      const dev = list.find(x => x.serial === serial);
      const cal = dev && dev._scale_calibration;
      if (cal && cal.ts >= sentAt - 2000) {
        if (cal.code === 3007) return "container";
        if ((cal.msg || "").includes("zero_standar")) return "ok";
      }
    } catch(e) {}
  }
  return "unknown";
}

// ── Controls tab (fountains) ──────────────────────────────────────────────
function buildControlsTab(device) {
  const pumpOn = !device.waterStopSwitch;
  const lightOn = !!device.lightSwitch;
  const filterLedOn = !!device.filterLedSwitch;
  const waterType = device.useWaterType ?? 0;
  const waterInterval = device.useWaterInterval ?? 15;
  const waterDuration = device.useWaterDuration ?? 15;
  const lightStart = device.light_start_time || "08:00";
  const lightEnd = device.light_end_time || "20:00";
  return `
  <div class="toggle-list">
    <div class="toggle-row">
      <span class="toggle-label">${t("overview.pump")}</span>
      <button class="toggle-switch ${pumpOn ? "sw-on" : "sw-off"}" id="ctrl-pump"></button>
    </div>
    <div class="toggle-row">
      <span class="toggle-label">${t("overview.light")}</span>
      <button class="toggle-switch ${lightOn ? "sw-on" : "sw-off"}" id="ctrl-light"></button>
    </div>
    <div class="toggle-row">
      <span class="toggle-label">${t("overview.filter_indicator")}</span>
      <button class="toggle-switch ${filterLedOn ? "sw-on" : "sw-off"}" id="ctrl-filter-led"></button>
    </div>
  </div>
  <p class="form-hint">${t("overview.filter_indicator_hint")}</p>
  <div class="form-row">
    <label class="form-label">${t("overview.flow_mode")}</label>
    <select class="form-select" id="ctrl-water-type">
      <option value="0" ${waterType === 0 ? "selected" : ""}>${t("overview.flow_continuous")}</option>
      <option value="1" ${waterType === 1 ? "selected" : ""}>${t("overview.flow_intermittent")}</option>
      <option value="2" ${waterType === 2 ? "selected" : ""}>${t("overview.flow_smart")}</option>
    </select>
  </div>
  <div id="intermittent-settings" style="${waterType === 1 ? "" : "display:none"}">
    <div class="form-row">
      <label class="form-label">${t("overview.on_duration")}</label>
      <input class="form-input" id="ctrl-water-duration" type="number" min="1" max="60" value="${waterDuration}">
    </div>
    <div class="form-row">
      <label class="form-label">${t("overview.off_interval")}</label>
      <input class="form-input" id="ctrl-water-interval" type="number" min="1" max="120" value="${waterInterval}">
    </div>
    <button class="btn-primary" id="btn-apply-schedule">${t("overview.apply_schedule")}</button>
  </div>
  <div class="tab-section-heading" style="margin-top:18px">${t("overview.light_schedule")}</div>
  <div class="form-row">
    <label class="form-label">${t("overview.light_start")}</label>
    <input class="form-input" id="ctrl-light-start" type="time" value="${lightStart}">
  </div>
  <div class="form-row">
    <label class="form-label">${t("overview.light_end")}</label>
    <input class="form-input" id="ctrl-light-end" type="time" value="${lightEnd}">
  </div>
  <button class="btn-primary" id="btn-apply-light-schedule">${t("overview.apply_light_schedule")}</button>

  <div class="tab-section-heading" style="margin-top:18px">${t("overview.calibrate_scale")}</div>
  <button class="btn-secondary" id="btn-calibrate-scale">${t("overview.calibrate_scale")}</button>
  <p class="form-hint">${t("overview.calibrate_scale_hint")}</p>`;
}

// ── Maintenance tab ───────────────────────────────────────────────────────
function buildMaintenanceTab(device) {
  if (device.device_type === "one_rfid") {
    const dDays    = desiccantDaysRemaining(device);
    const bowlDays = bowlDaysRemaining(device);
    const housDays = housingDaysRemaining(device);
    const dCol  = dDays    != null && dDays    <= 3 ? "var(--pl-danger)" : "var(--pl-accent)";
    const bCol  = bowlDays != null && bowlDays <= 0 ? "var(--pl-danger)" : "var(--pl-accent)";
    const hCol  = housDays != null && housDays <= 0 ? "var(--pl-danger)" : "var(--pl-accent)";
    return `
    <div class="tab-section-heading">${t("maint.desiccant")}</div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
      <span style="font-size:13px;color:var(--pl-subtext);white-space:nowrap">${t("maint.days_remaining")}</span>
      <input class="form-input" id="maint-desiccant-days" type="number" min="0" max="730"
        value="${dDays != null ? Math.max(0, Math.round(dDays)) : ""}"
        placeholder="${dDays != null ? "" : t("maint.desiccant_not_set")}"
        style="max-width:90px;color:${dCol}">
    </div>
    <label class="form-label">${t("maint.desiccant_replace_every")}</label>
    <div style="display:flex;gap:8px;margin-bottom:18px">
      <input class="form-input" id="maint-desiccant-life" type="number" min="1" max="365" value="${device.desiccant_life_days ?? 14}" style="flex:1">
      <button class="btn-secondary" id="btn-reset-desiccant" style="flex-shrink:0">${t("maint.reset_desiccant")}</button>
    </div>

    <div class="tab-section-heading">${t("maint.bowl")}</div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
      <span style="font-size:13px;color:var(--pl-subtext);white-space:nowrap">${t("maint.next_clean_in")}</span>
      <input class="form-input" id="maint-bowl-days" type="number" min="0" max="365"
        value="${bowlDays != null ? Math.max(0, Math.round(bowlDays)) : ""}"
        placeholder="${bowlDays != null ? "" : t("maint.bowl_not_set")}"
        style="max-width:90px;color:${bCol}">
    </div>
    <label class="form-label">${t("maint.bowl_clean_every")}</label>
    <div style="display:flex;gap:8px;margin-bottom:18px">
      <input class="form-input" id="maint-bowl-interval" type="number" min="1" max="90" value="${device.bowl_cleaning_interval_days ?? 7}" style="flex:1">
      <button class="btn-secondary" id="btn-record-bowl" style="flex-shrink:0">${t("maint.record_bowl")}</button>
    </div>

    <div class="tab-section-heading">${t("maint.housing")}</div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
      <span style="font-size:13px;color:var(--pl-subtext);white-space:nowrap">${t("maint.next_clean_in")}</span>
      <input class="form-input" id="maint-housing-days" type="number" min="0" max="365"
        value="${housDays != null ? Math.max(0, Math.round(housDays)) : ""}"
        placeholder="${housDays != null ? "" : t("maint.bowl_not_set")}"
        style="max-width:90px;color:${hCol}">
    </div>
    <label class="form-label">${t("maint.bowl_clean_every")}</label>
    <div style="display:flex;gap:8px;margin-bottom:18px">
      <input class="form-input" id="maint-housing-interval" type="number" min="1" max="365" value="${device.housing_cleaning_interval_days ?? 30}" style="flex:1">
      <button class="btn-secondary" id="btn-record-housing" style="flex-shrink:0">${t("maint.record_housing")}</button>
    </div>

    <div class="tab-section-heading">${t("maint.feed_sounds")}</div>
    <p class="form-hint">${t("maint.feed_sounds_hint")}</p>
    <div class="form-row">
      <label class="form-label">${t("maint.feed_sound_select")}</label>
      <select class="form-select" id="maint-audio-select">
        <option value="" data-i18n="maint.feed_sound_loading">${t("maint.feed_sound_loading")}</option>
      </select>
    </div>
    <button class="btn-secondary" id="btn-push-audio" style="width:100%">${t("maint.push_sound")}</button>
    <p class="form-hint" id="maint-audio-status"></p>`;
  }

  const filterDays = filterDaysRemaining(device);
  const filterLifeDays = device.filter_life_days ?? 30;
  const cleanDays = cleaningDaysRemaining(device);
  const cleanInterval = device.cleaning_interval_days ?? 30;
  const imperial = useImperial();
  const lowWaterGrams = device.lowWater ?? device.low_water_grams ?? 500;
  const lowWaterDisplay = imperial ? (lowWaterGrams * 0.033814).toFixed(1) : lowWaterGrams;
  const lowWaterUnit = imperial ? "fl oz" : "ml";

  const filterClass = filterDays != null && filterDays <= 3 ? "danger" : "accent";
  const cleanClass = cleanDays != null && cleanDays < 1 ? "danger" : "";

  const fCol = filterDays != null && filterDays <= 3 ? "var(--pl-danger)" : "var(--pl-accent)";
  const cCol = cleanDays  != null && cleanDays  <  1 ? "var(--pl-danger)" : "";
  return `
  <div class="tab-section-heading">${t("maint.filter")}</div>
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
    <span style="font-size:13px;color:var(--pl-subtext);white-space:nowrap">${t("maint.days_remaining")}</span>
    <input class="form-input" id="maint-filter-days" type="number" min="0" max="730"
      value="${filterDays != null ? Math.max(0, Math.round(filterDays)) : ""}"
      style="max-width:90px;${fCol ? `color:${fCol}` : ""}">
  </div>
  <label class="form-label">${t("maint.filter_life")}</label>
  <div style="display:flex;gap:8px;margin-bottom:18px">
    <input class="form-input" id="maint-filter-life" type="number" min="1" max="365" value="${filterLifeDays}" style="flex:1">
    <button class="btn-secondary" id="btn-reset-filter" style="flex-shrink:0">${t("maint.reset_filter")}</button>
  </div>

  <div class="tab-section-heading">${t("maint.cleaning")}</div>
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
    <span style="font-size:13px;color:var(--pl-subtext);white-space:nowrap">${t("maint.next_cleanup_in")}</span>
    <input class="form-input" id="maint-clean-days" type="number" min="0" max="365"
      value="${cleanDays != null ? Math.max(0, Math.round(cleanDays)) : ""}"
      style="max-width:90px;${cCol ? `color:${cCol}` : ""}">
  </div>
  <label class="form-label">${t("maint.cleanup_interval")}</label>
  <div style="display:flex;gap:8px;margin-bottom:18px">
    <input class="form-input" id="maint-clean-interval" type="number" min="1" max="90" value="${cleanInterval}" style="flex:1">
    <button class="btn-secondary" id="btn-record-clean" style="flex-shrink:0">${t("maint.record_cleaning")}</button>
  </div>

  <div class="tab-section-heading">${t("maint.low_water")}</div>
  <div class="form-row">
    <label class="form-label">${t("maint.low_water_threshold", {unit: escHtml(lowWaterUnit)})}</label>
    <input class="form-input" id="maint-low-water" type="number" min="0" value="${lowWaterDisplay}">
  </div>

  <div class="tab-section-heading">${t("maint.drink_detection")}</div>
  <div class="form-row">
    <label class="form-label">${t("maint.min_drink_label")}</label>
    <input class="form-input" id="maint-min-drink" type="number" min="1" max="200" value="${device.min_drink_grams ?? 5}">
  </div>
  <p class="form-hint">${t("maint.min_drink_hint")}</p>`;
}

// ── Custom feed sounds (feeder maintenance tab) ────────────────────────────
async function _wireFeedSoundControls(device, selectEl) {
  const statusEl = document.getElementById("maint-audio-status");
  const pushBtn  = document.getElementById("btn-push-audio");

  async function refreshList() {
    try {
      const names = await api("GET", "/api/audio");
      selectEl.innerHTML = names.length
        ? names.map(n => `<option value="${escHtml(n)}">${escHtml(n)}</option>`).join("")
        : `<option value="">${t("maint.feed_sound_none")}</option>`;
    } catch {
      selectEl.innerHTML = `<option value="">${t("maint.feed_sound_none")}</option>`;
    }
  }
  await refreshList();

  if (pushBtn) {
    pushBtn.onclick = async () => {
      const name = selectEl.value;
      if (!name) { statusEl.textContent = t("maint.feed_sound_select_first"); return; }
      pushBtn.disabled = true;
      statusEl.textContent = t("maint.pushing");
      try {
        await api("POST", `/api/devices/${device.serial}/push-audio`, { name });
        statusEl.textContent = t("maint.push_success");
      } catch(e) {
        statusEl.textContent = t("maint.push_failed", {error: e.message});
      } finally {
        pushBtn.disabled = false;
      }
    };
  }
}

// ── Log tab ───────────────────────────────────────────────────────────────
function buildLogTab(device, history) {
  const hasData = history && history.some(d => d.grams > 0);
  if (!hasData) {
    return `<div style="text-align:center;padding:32px 0">
      <div style="font-size:40px;margin-bottom:8px">💧</div>
      <p class="form-hint">${t("log.no_intake")}</p>
    </div>`;
  }
  const maxGrams = Math.max(...history.map(d => d.grams), 1);
  const today = new Date().toISOString().split("T")[0];
  const yesterday = new Date(Date.now() - 86400000).toISOString().split("T")[0];

  return `<div class="tab-section-heading">${t("log.water_heading")}</div>
  <div class="intake-log">
    ${history.map(d => {
      let label;
      if (d.date === today) label = t("time.today");
      else if (d.date === yesterday) label = t("time.yesterday");
      else label = new Date(d.date + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
      const pct = d.grams > 0 ? Math.max(4, Math.round(d.grams / maxGrams * 100)) : 0;
      const val = d.grams > 0 ? fmtWater(d.grams) : "—";
      return `<div class="intake-row">
        <div class="intake-label">${escHtml(label)}</div>
        <div class="intake-bar-wrap"><div class="intake-bar" style="width:${pct}%"></div></div>
        <div class="intake-value">${escHtml(val)}</div>
      </div>`;
    }).join("")}
  </div>`;
}

// ── Feeder log tab ────────────────────────────────────────────────────────
function buildFeederLogTab(device, entries) {
  if (!entries || !entries.length) {
    return `<div style="text-align:center;padding:32px 0">
      <div style="font-size:40px;margin-bottom:8px">🐾</div>
      <p class="form-hint">${t("log.no_activity")}</p>
    </div>`;
  }
  const petName = (device.pets && device.pets.length === 1) ? device.pets[0].name : null;

  let lastDate = null;
  const rows = entries.map(e => {
    const dateLabel = fmtDate(e.ts);
    const dateHeader = dateLabel !== lastDate
      ? `<div class="tab-section-heading" style="margin-top:${lastDate ? "16px" : "0"}">${escHtml(dateLabel)}</div>`
      : "";
    lastDate = dateLabel;

    let line;
    if (e.type === "food_dispensed") {
      const portions = e.portions === 1 ? t("log.portion_one") : t("log.portions_n", {n: e.portions});
      line = `<span style="color:var(--pl-subtext)">${escHtml(fmtTime(e.ts))}</span> ${t("log.food_dispensed", {portions: escHtml(portions)})}`;
    } else if (e.type === "pet_eating") {
      const who = petName ? escHtml(petName) : t("pet.unnamed");
      line = `<span style="color:var(--pl-subtext)">${escHtml(fmtTime(e.ts))}</span> ${t("log.pet_ate", {name: who, duration: escHtml(fmtDuration(e.duration_secs))})}`;
    } else if (e.type === "door_open") {
      line = `<span style="color:var(--pl-subtext)">${escHtml(fmtTime(e.ts))}</span> ${t("log.door_open", {duration: escHtml(fmtDuration(e.duration_secs))})}`;
    } else {
      line = `<span style="color:var(--pl-subtext)">${escHtml(fmtTime(e.ts))}</span> ${escHtml(e.type)}`;
    }
    return `${dateHeader}<div style="padding:6px 0;border-bottom:1px solid var(--pl-border);font-size:0.9rem">${line}</div>`;
  }).join("");

  return `<div class="tab-section-heading">${t("log.activity_heading")}</div>${rows}`;
}

// ── Fountain log tab ──────────────────────────────────────────────────────
function buildFountainLogTab(device, entries) {
  if (!entries || !entries.length) {
    return `<div style="text-align:center;padding:32px 0">
      <div style="font-size:40px;margin-bottom:8px">💧</div>
      <p class="form-hint">${t("log.no_activity")}</p>
    </div>`;
  }

  const solePetName = (device.pets && device.pets.length === 1) ? device.pets[0].name : null;

  let lastDate = null;
  const rows = entries.map(e => {
    const dateLabel = fmtDate(e.ts);
    const dateHeader = dateLabel !== lastDate
      ? `<div class="tab-section-heading" style="margin-top:${lastDate ? "16px" : "0"}">${escHtml(dateLabel)}</div>`
      : "";
    lastDate = dateLabel;

    let line;
    if (e.type === "drink" && e.rfid_tag) {
      const matched = (device.pets || []).find(p => p.id === e.pet_id);
      const who = matched ? matched.name : (solePetName || t("pet.unnamed"));
      line = `<span style="color:var(--pl-subtext)">${escHtml(fmtTime(e.ts))}</span> ${t("log.pet_drank", {name: escHtml(who), volume: escHtml(fmtWater(e.grams))})}`;
    } else if (e.type === "drink") {
      line = `<span style="color:var(--pl-subtext)">${escHtml(fmtTime(e.ts))}</span> ${t("log.drink", {volume: escHtml(fmtWater(e.grams))})}`;
    } else {
      line = `<span style="color:var(--pl-subtext)">${escHtml(fmtTime(e.ts))}</span> ${escHtml(e.type)}`;
    }
    return `${dateHeader}<div style="padding:6px 0;border-bottom:1px solid var(--pl-border);font-size:0.9rem">${line}</div>`;
  }).join("");

  return `<div class="tab-section-heading">${t("log.activity_heading")}</div>${rows}`;
}

// ── Schedule tab ──────────────────────────────────────────────────────────
const _DAY_KEYS = ["time.day_mon","time.day_tue","time.day_wed","time.day_thu","time.day_fri","time.day_sat","time.day_sun"];
function _DAYS() { return _DAY_KEYS.map(k => t(k)); }
// repeatDay uses 1=Mon...6=Sat,7=Sun
const _RDAY = [1,2,3,4,5,6,7];

function _schedDayLabel(repeatDay) {
  if (!repeatDay || repeatDay.length === 0) return t("schedule.no_days");
  if (repeatDay.length === 7) return t("schedule.daily");
  const sorted = [...repeatDay].sort((a,b)=>a-b);
  if (sorted.join(",") === "1,2,3,4,5") return t("schedule.weekdays");
  if (sorted.join(",") === "6,7") return t("schedule.weekends");
  const days = _DAYS();
  return sorted.map(d => days[d===7?6:d-1]).join(", ");
}

function buildScheduleTab(plans) {
  // Display in chronological (local time) order for readability, but keep
  // data-idx pointing at each plan's real position in the underlying array --
  // that's what edit/delete/toggle and the push-to-feeder order are keyed on,
  // and nothing about storage or the feeder protocol requires sorted order.
  const order = plans.map((_, i) => i).sort((a, b) =>
    _utcToLocal(plans[a].executionTime || "00:00").localeCompare(_utcToLocal(plans[b].executionTime || "00:00"))
  );
  const rows = order.map(i => {
    const plan = plans[i];
    const enabled = plan._enabled !== false;
    const dayLabel = _schedDayLabel(plan.repeatDay);
    const portions = plan.grainNum || 1;
    const sound = plan.enableAudio ? ` · ${t("schedule.sound_times", {n: plan.audioTimes||1})}` : "";
    return `<div class="sched-row${enabled?"":' disabled'}" data-idx="${i}">
      <div class="sched-time">${escHtml(fmtTime12(_utcToLocal(plan.executionTime||"00:00")))}</div>
      <div class="sched-meta">${escHtml(dayLabel)} · ${portions === 1 ? t("schedule.portion_one") : t("schedule.portions_n", {n: portions})}${escHtml(sound)}</div>
      <div class="sched-actions">
        <button class="sched-toggle${enabled?" on":""}" data-idx="${i}" title="${enabled?"Disable":"Enable"}"></button>
        <button class="sched-edit-btn" data-idx="${i}" title="Edit">✏️</button>
        <button class="sched-del-btn" data-idx="${i}" title="Delete" style="background:none;border:none;cursor:pointer;padding:4px;color:var(--pl-danger,#e05252);font-size:16px;line-height:1;opacity:0.8">&#x2715;</button>
      </div>
    </div>`;
  }).join("");

  const days = _DAYS();
  const form = `<div class="sched-form" id="sched-form">
    <div class="form-row">
      <label>${t("schedule.time")} <span id="sf-tz-hint" style="font-weight:400;color:var(--pl-subtext)"></span></label>
      <input type="time" class="form-input" id="sf-time" value="08:00">
    </div>
    <div class="form-row">
      <label>${t("schedule.days")}</label>
      <div class="day-chips" id="sf-days">
        ${days.map((d,i)=>`<div class="day-chip on" data-rday="${_RDAY[i]}">${d}</div>`).join("")}
      </div>
    </div>
    <div class="form-row">
      <label>${t("schedule.portions")}</label>
      <div class="portion-chips" id="sf-portions">
        ${[1,2,3,4,5].map(n=>`<div class="portion-chip${n===1?" on":""}" data-n="${n}">${n}</div>`).join("")}
      </div>
    </div>
    <div class="form-row" style="display:flex;align-items:center;gap:10px">
      <input type="checkbox" id="sf-sound" style="width:16px;height:16px;accent-color:var(--pl-accent);flex-shrink:0">
      <label for="sf-sound" style="margin:0;flex-shrink:0">${t("schedule.sound")}</label>
      <span id="sf-sound-times-wrap" style="display:flex;align-items:center;gap:6px;margin-left:6px">
        <label style="margin:0;flex-shrink:0;font-size:12px;color:var(--pl-subtext)">×</label>
        <input type="number" class="form-input" id="sf-sound-times" value="2" min="1" max="5" style="width:52px;padding:4px 8px">
      </span>
    </div>
    <div style="display:flex;gap:8px;margin-top:12px">
      <button class="btn-primary" id="sf-save" style="flex:1">${t("schedule.save")}</button>
      <button class="btn-secondary" id="sf-cancel" style="flex:1">${t("schedule.cancel")}</button>
    </div>
    <input type="hidden" id="sf-edit-idx" value="">
  </div>`;

  const addBtn = `<button class="btn-secondary" id="sched-add-btn" style="width:100%;margin-bottom:10px">${t("schedule.add")}</button>`;

  return `<div class="tab-section-heading">${t("schedule.heading")}</div>
    ${addBtn}
    ${form}
    <div class="sched-list" id="sched-list">${rows || `<p style="color:var(--pl-subtext);text-align:center;padding:12px 0">${t("schedule.no_plans")}</p>`}</div>
    <p style="font-size:11px;color:var(--pl-subtext);margin-top:4px">${t("schedule.sync_note")}</p>`;
}

// In-memory plans for schedule tab editing
let _schedPlans = [];

async function _saveSchedule() {
  const ok = await api("POST", `/api/devices/${_currentDevice.serial}/feeding-plans`, _schedPlans).catch(() => null);
  return ok;
}

function wireScheduleTabHandlers() {
  const list = document.getElementById("sched-list");
  const form = document.getElementById("sched-form");
  if (!list || !form) return;

  function openForm(idx) {
    const plan = idx >= 0 ? _schedPlans[idx] : null;
    document.getElementById("sf-time").value = plan ? _utcToLocal(plan.executionTime || "08:00") : "08:00";
    document.getElementById("sf-edit-idx").value = idx >= 0 ? idx : "";
    // Days
    const rdays = new Set(plan?.repeatDay || _RDAY);
    document.querySelectorAll(".day-chip").forEach(c => c.classList.toggle("on", rdays.has(+c.dataset.rday)));
    // Portions
    const portions = plan?.grainNum || 1;
    document.querySelectorAll(".portion-chip").forEach(c => c.classList.toggle("on", +c.dataset.n === portions));
    // Sound — new plans default to on (matches feeder factory default)
    const soundOn = plan ? (plan.enableAudio ?? true) : true;
    const soundCheck = document.getElementById("sf-sound");
    soundCheck.checked = soundOn;
    document.getElementById("sf-sound-times").value = plan?.audioTimes ?? 2;
    document.getElementById("sf-sound-times-wrap").style.display = soundOn ? "flex" : "none";
    // Timezone hint
    const tzHint = document.getElementById("sf-tz-hint");
    if (tzHint) {
      const off = _schedTzOffset();
      const label = _settings.feeder_timezone || `UTC${off >= 0 ? "+" : ""}${off}`;
      tzHint.textContent = `(${label}, UTC${off >= 0 ? "+" : ""}${Math.round(off)})`;
    }
    form.classList.add("open");
    document.getElementById("sf-time").focus();
  }

  function closeForm() { form.classList.remove("open"); }

  function rerender() {
    const tmp = document.createElement("div");
    tmp.innerHTML = buildScheduleTab(_schedPlans);
    const newList = tmp.querySelector("#sched-list");
    if (newList) list.innerHTML = newList.innerHTML;
    wireListHandlers();
  }

  function wireListHandlers() {
    list.querySelectorAll(".sched-toggle").forEach(btn => {
      btn.onclick = async () => {
        const idx = +btn.dataset.idx;
        _schedPlans[idx]._enabled = !(_schedPlans[idx]._enabled !== false);
        await _saveSchedule();
        rerender();
      };
    });
    list.querySelectorAll(".sched-edit-btn").forEach(btn => {
      btn.onclick = () => openForm(+btn.dataset.idx);
    });
    list.querySelectorAll(".sched-del-btn").forEach(btn => {
      btn.onclick = async () => {
        const idx = +btn.dataset.idx;
        const plan = _schedPlans[idx];
        if (!confirm(t("schedule.delete_confirm", {time: plan.executionTime}))) return;
        _schedPlans.splice(idx, 1);
        await _saveSchedule();
        rerender();
      };
    });
  }

  // Sound checkbox toggles the ×N input
  document.getElementById("sf-sound").addEventListener("change", e => {
    document.getElementById("sf-sound-times-wrap").style.display = e.target.checked ? "flex" : "none";
  });

  // Day chip toggle
  form.querySelectorAll(".day-chip").forEach(c => {
    c.onclick = () => c.classList.toggle("on");
  });
  // Portion chip select
  form.querySelectorAll(".portion-chip").forEach(c => {
    c.onclick = () => {
      form.querySelectorAll(".portion-chip").forEach(x => x.classList.remove("on"));
      c.classList.add("on");
    };
  });

  document.getElementById("sched-add-btn").onclick = () => openForm(-1);
  document.getElementById("sf-cancel").onclick = closeForm;

  document.getElementById("sf-save").onclick = async () => {
    const time = document.getElementById("sf-time").value;
    if (!time) { alert(t("schedule.err_no_time")); return; }
    const repeatDay = Array.from(form.querySelectorAll(".day-chip.on")).map(c => +c.dataset.rday);
    if (repeatDay.length === 0) { alert(t("schedule.err_no_days")); return; }
    const grainNum = +form.querySelector(".portion-chip.on")?.dataset.n || 1;
    const enableAudio = document.getElementById("sf-sound").checked;
    const audioTimes = +document.getElementById("sf-sound-times").value || 2;
    const editIdx = document.getElementById("sf-edit-idx").value;
    const planData = {
      planId: editIdx !== "" ? _schedPlans[+editIdx].planId : (5000000 + Math.floor(Math.random() * 1000000)),
      executionTime: _localToUtc(time),
      repeatDay,
      enableAudio,
      audioTimes,
      grainNum,
      syncTime: Date.now(),
      _enabled: editIdx !== "" ? (_schedPlans[+editIdx]._enabled !== false) : true,
    };
    if (editIdx !== "") {
      _schedPlans[+editIdx] = planData;
    } else {
      _schedPlans.push(planData);
    }
    const result = await _saveSchedule();
    closeForm();
    rerender();
    if (result?.mqtt === false) {
      // Non-blocking toast
      const msg = document.createElement("div");
      msg.textContent = t("schedule.offline_toast");
      msg.style.cssText = "position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--pl-surface2);border:1px solid var(--pl-border);border-radius:8px;padding:10px 16px;font-size:12px;z-index:9999;max-width:90vw;text-align:center";
      document.body.appendChild(msg);
      setTimeout(() => msg.remove(), 3500);
    }
  };

  wireListHandlers();
}

// ── Notifications tab ─────────────────────────────────────────────────────
function buildNotificationsTab(device) {
  const notif = device.notifications || {};
  const isFeeder = device.device_type === "one_rfid";
  const checks = isFeeder ? [
    { key: "food_low",      label: t("notif.food_low") },
    { key: "desiccant_due", label: t("notif.desiccant_due") },
    { key: "bowl_due",      label: t("notif.bowl_due") },
    { key: "housing_due",   label: t("notif.housing_due") },
    { key: "power_battery", label: t("notif.power_battery") },
    { key: "offline",       label: t("notif.offline") },
  ] : [
    { key: "water_low",    label: t("notif.water_low") },
    { key: "filter_due",   label: t("notif.filter_due") },
    { key: "cleaning_due", label: t("notif.cleaning_due") },
    { key: "offline",      label: t("notif.offline") },
  ];
  const devMobileOptions = `<option value="">-- Default from Settings --</option>` +
    _mobileTargets.map(t =>
      `<option value="${escHtml(t.service)}"${device.notify_mobile_service === t.service ? " selected" : ""}>${escHtml(t.label)}</option>`
    ).join("") +
    (device.notify_mobile_service && !_mobileTargets.find(t => t.service === device.notify_mobile_service)
      ? `<option value="${escHtml(device.notify_mobile_service)}" selected>${escHtml(device.notify_mobile_service)}</option>` : "");

  const batteryLowRow = isFeeder ? `
    <div class="toggle-row">
      <span class="toggle-label">${t("notif.battery_low")}</span>
      <input class="form-input" id="notif-battery-low-pct" type="number" min="0" max="99" value="${device.battery_low_pct ?? 20}" style="width:70px;text-align:center">
    </div>` : "";

  return `
  <div class="tab-section-heading">${t("notif.alert_conditions")}</div>
  <div class="toggle-list">
    ${checks.map(c => `<div class="toggle-row">
      <span class="toggle-label">${c.label}</span>
      <input type="checkbox" class="notif-check" data-key="${c.key}" ${notif[c.key] !== false ? "checked" : ""}>
    </div>`).join("")}${batteryLowRow}
  </div>
  ${isFeeder ? `<p class="form-hint">${t("notif.battery_low_hint")}</p>` : ""}

  <div class="tab-section-heading" style="margin-top:18px">${t("notif.channels")}</div>
  <div class="toggle-list" style="margin-bottom:14px">
    <div class="toggle-row">
      <span class="toggle-label">${t("notif.ha_bell")}</span>
      <input type="checkbox" class="notif-check" id="nchan-bell" ${device.notify_bell !== false ? "checked" : ""}>
    </div>
    <div class="toggle-row">
      <span class="toggle-label">${t("notif.email")}</span>
      <input type="checkbox" class="notif-check" id="nchan-email" ${device.notify_email !== false ? "checked" : ""}>
    </div>
    <div class="toggle-row">
      <span class="toggle-label">${t("notif.mobile")}</span>
      <input type="checkbox" class="notif-check" id="nchan-mobile" ${device.notify_mobile ? "checked" : ""}>
    </div>
  </div>
  <div id="nchan-email-section" style="${device.notify_email !== false ? "" : "display:none"}">
    <div class="form-row">
      <label class="form-label">${t("notif.email_override")}</label>
      <input class="form-input" id="nchan-email-addr" type="email" placeholder="${t("notif.email_placeholder")}" value="${escHtml(device.notify_email_address || "")}">
    </div>
  </div>
  <div id="nchan-mobile-section" style="${device.notify_mobile ? "" : "display:none"}">
    <div class="form-row">
      <label class="form-label">${t("notif.mobile_service")}</label>
      <select class="form-select" id="nchan-mobile-svc">${devMobileOptions}</select>
    </div>
  </div>

  <button class="btn-primary" id="btn-save-device-notifications">${t("notif.save")}</button>`;
}

// ── Wire device tab handlers ──────────────────────────────────────────────
function wireDeviceTabHandlers(tabName) {
  const d = _currentDevice;
  if (tabName === "controls") {
    const pump = document.getElementById("ctrl-pump");
    const light = document.getElementById("ctrl-light");
    const filterLed = document.getElementById("ctrl-filter-led");
    const waterType = document.getElementById("ctrl-water-type");
    const intermittent = document.getElementById("intermittent-settings");
    const applySchedule = document.getElementById("btn-apply-schedule");
    const applyLightSchedule = document.getElementById("btn-apply-light-schedule");
    const calibrateScale = document.getElementById("btn-calibrate-scale");

    if (pump) pump.onclick = () => togglePump(d);
    if (light) light.onclick = () => toggleLight(d);
    if (filterLed) filterLed.onclick = () => toggleFilterLed(d);
    if (waterType) {
      waterType.onchange = async () => {
        const val = parseInt(waterType.value);
        if (intermittent) intermittent.style.display = val === 1 ? "" : "none";
        try {
          await api("POST", `/api/devices/${d.serial}/command`, { useWaterType: val });
          _patchDevice(d.serial, { useWaterType: val });
        } catch(e) { alert(t("overview.cmd_failed", {error: e.message})); }
      };
    }
    if (applySchedule) {
      applySchedule.onclick = async () => {
        const duration = parseInt(document.getElementById("ctrl-water-duration").value) || 15;
        const interval = parseInt(document.getElementById("ctrl-water-interval").value) || 15;
        try {
          await api("POST", `/api/devices/${d.serial}/command`, {
            useWaterDuration: duration,
            useWaterInterval: interval,
          });
          _patchDevice(d.serial, { useWaterDuration: duration, useWaterInterval: interval });
          applySchedule.textContent = t("overview.applied");
          setTimeout(() => { applySchedule.textContent = t("overview.apply_schedule"); }, 1500);
        } catch(e) { alert(t("overview.cmd_failed", {error: e.message})); }
      };
    }
    if (applyLightSchedule) {
      applyLightSchedule.onclick = async () => {
        const start = document.getElementById("ctrl-light-start").value || "08:00";
        const end = document.getElementById("ctrl-light-end").value || "20:00";
        try {
          await api("POST", `/api/devices/${d.serial}/command`, { _light_schedule: { start, end } });
          _patchDevice(d.serial, { light_start_time: start, light_end_time: end });
          applyLightSchedule.textContent = t("overview.applied");
          setTimeout(() => { applyLightSchedule.textContent = t("overview.apply_light_schedule"); }, 1500);
        } catch(e) { alert(t("overview.cmd_failed", {error: e.message})); }
      };
    }
    if (calibrateScale) {
      calibrateScale.onclick = async () => {
        const orig = calibrateScale.textContent;
        calibrateScale.textContent = t("overview.calibrating");
        calibrateScale.disabled = true;
        const sentAt = Date.now();
        try {
          await api("POST", `/api/devices/${d.serial}/command`, { cmd: "WEIGHT_CLR_SERVICE", threshold: 550 });
          const result = await _pollCalibrationResult(d.serial, sentAt);
          calibrateScale.textContent =
            result === "container" ? t("overview.calibrate_scale_container") :
            result === "ok"        ? t("overview.calibrate_scale_done") :
                                      t("overview.sent");
        } catch(e) {
          calibrateScale.textContent = t("overview.failed");
        }
        setTimeout(() => { calibrateScale.textContent = orig; calibrateScale.disabled = false; }, 3500);
      };
    }
  }
  else if (tabName === "overview") {
    // Feeder controls
    const feedNow = document.getElementById("btn-feed-now");
    const openLid = document.getElementById("btn-open-lid");
    const closeLid = document.getElementById("btn-close-lid");

    if (feedNow) {
      feedNow.onclick = async () => {
        const portions = parseInt(document.getElementById("feeder-portions").value) || 1;
        feedNow.textContent = t("overview.feeding");
        feedNow.disabled = true;
        try {
          await api("POST", `/api/devices/${d.serial}/command`, {
            cmd: "MANUAL_FEEDING_SERVICE",
            grainNum: portions,
          });
          feedNow.textContent = t("overview.sent");
        } catch(e) {
          feedNow.textContent = t("overview.failed");
        }
        setTimeout(() => { feedNow.textContent = t("overview.feed_now"); feedNow.disabled = false; }, 2000);
      };
    }
    if (openLid) {
      openLid.onclick = async () => {
        const orig = openLid.textContent;
        openLid.textContent = t("overview.opening");
        openLid.disabled = true;
        try {
          await api("POST", `/api/devices/${d.serial}/command`, {
            cmd: "SWITCH_DOOR_SERVICE",
            barnDoorState: true,
          });
          openLid.textContent = t("overview.sent");
        } catch(e) {
          openLid.textContent = t("overview.failed");
          alert(t("overview.cmd_failed", {error: e.message}));
        }
        setTimeout(() => { openLid.textContent = orig; openLid.disabled = false; }, 2000);
      };
    }
    if (closeLid) {
      closeLid.onclick = async () => {
        const orig = closeLid.textContent;
        closeLid.textContent = t("overview.closing");
        closeLid.disabled = true;
        try {
          await api("POST", `/api/devices/${d.serial}/command`, {
            cmd: "SWITCH_DOOR_SERVICE",
            barnDoorState: false,
          });
          closeLid.textContent = t("overview.sent");
        } catch(e) {
          closeLid.textContent = t("overview.failed");
          alert(t("overview.cmd_failed", {error: e.message}));
        }
        setTimeout(() => { closeLid.textContent = orig; closeLid.disabled = false; }, 2000);
      };
    }
    // Each feeder setting auto-saves independently on its own change, rather
    // than one bundled "Save Settings" button that resent every field
    // together -- that meant toggling Child Lock also re-sent Sound/Volume
    // every time, which made the feeder audibly announce "sound setting
    // saved" for a setting that hadn't actually changed.
    const feederAutoSave = (elId, getPayload, debounceMs) => {
      const el = document.getElementById(elId);
      if (!el) return;
      const send = async () => {
        const payload = getPayload(el);
        try {
          await api("POST", `/api/devices/${d.serial}/command`, payload);
          _patchDevice(d.serial, payload);
        } catch(e) { alert(t("overview.cmd_failed", {error: e.message})); }
      };
      // Debounced fields: a number input's spinner arrows fire onchange on
      // every single click, so rapid clicking would otherwise send one
      // command per click -- for volume specifically, that made the feeder
      // repeatedly announce "sound settings changed" out loud.
      el.onchange = debounceMs ? _debounce(send, debounceMs) : send;
    };
    feederAutoSave("feeder-cover-open-mode",  el => ({ coverOpenMode: el.value }));
    feederAutoSave("feeder-cover-close-speed", el => ({ coverCloseSpeed: el.value }));
    feederAutoSave("feeder-close-door-sec",   el => ({ closeDoorTimeSec: Math.min(10, Math.max(1, parseInt(el.value) || 10)) }), 2000);
    feederAutoSave("feeder-sound-switch",     el => ({ soundSwitch: el.checked }));
    feederAutoSave("feeder-volume",           el => ({ volume: parseInt(el.value) || 50 }), 2000);
    feederAutoSave("feeder-child-lock",       el => ({ childLockSwitch: el.checked }));

    const sendDisplayText = document.getElementById("btn-send-display-text");
    if (sendDisplayText) {
      sendDisplayText.onclick = async () => {
        const text = (document.getElementById("feeder-display-text").value || "").trim().toUpperCase();
        if (!text) return;
        const orig = sendDisplayText.textContent;
        sendDisplayText.textContent = "Sending...";
        sendDisplayText.disabled = true;
        try {
          await api("POST", `/api/devices/${d.serial}/command`, { _display_text: text });
          _patchDevice(d.serial, { display_text: text, display_icon: 0, display_icon_name: null });
          renderDevices();
          sendDisplayText.textContent = "Sent!";
        } catch(e) {
          sendDisplayText.textContent = "Failed";
        }
        setTimeout(() => { sendDisplayText.textContent = orig; sendDisplayText.disabled = false; }, 2000);
      };
    }

    const openIconEditorBtn = document.getElementById("btn-open-icon-editor");
    if (openIconEditorBtn) {
      openIconEditorBtn.onclick = () => openIconEditor(d.serial);
    }
  }
  else if (tabName === "maintenance") {
    // Helper: back-calculate a last_ts from (days remaining, interval)
    function _lastTsFromDays(daysRemaining, intervalDays) {
      const elapsed = Math.max(0, intervalDays - daysRemaining);
      return Date.now() - elapsed * 86400000;
    }

    // Feeder: editable days-remaining inputs auto-save on change
    const desiccantDaysEl = document.getElementById("maint-desiccant-days");
    if (desiccantDaysEl) {
      desiccantDaysEl.onchange = async () => {
        const days = parseInt(desiccantDaysEl.value);
        if (isNaN(days)) return;
        const life = parseInt(document.getElementById("maint-desiccant-life").value) || 14;
        const last_ts = _lastTsFromDays(days, life);
        await api("POST", `/api/devices/${d.serial}`, { last_desiccant_ts: last_ts, desiccant_life_days: life });
        _patchDevice(d.serial, { last_desiccant_ts: last_ts, desiccant_life_days: life });
      };
    }
    const bowlDaysEl = document.getElementById("maint-bowl-days");
    if (bowlDaysEl) {
      bowlDaysEl.onchange = async () => {
        const days = parseInt(bowlDaysEl.value);
        if (isNaN(days)) return;
        const interval = parseInt(document.getElementById("maint-bowl-interval").value) || 7;
        const last_ts = _lastTsFromDays(days, interval);
        await api("POST", `/api/devices/${d.serial}`, { last_bowl_cleaned_ts: last_ts, bowl_cleaning_interval_days: interval });
        _patchDevice(d.serial, { last_bowl_cleaned_ts: last_ts, bowl_cleaning_interval_days: interval });
      };
    }
    const housingDaysEl = document.getElementById("maint-housing-days");
    if (housingDaysEl) {
      housingDaysEl.onchange = async () => {
        const days = parseInt(housingDaysEl.value);
        if (isNaN(days)) return;
        const interval = parseInt(document.getElementById("maint-housing-interval").value) || 30;
        const last_ts = _lastTsFromDays(days, interval);
        await api("POST", `/api/devices/${d.serial}`, { last_housing_cleaned_ts: last_ts, housing_cleaning_interval_days: interval });
        _patchDevice(d.serial, { last_housing_cleaned_ts: last_ts, housing_cleaning_interval_days: interval });
      };
    }
    // Fountain: editable days-remaining inputs auto-save on change
    const filterDaysEl = document.getElementById("maint-filter-days");
    if (filterDaysEl) {
      filterDaysEl.onchange = async () => {
        const days = parseInt(filterDaysEl.value);
        if (isNaN(days)) return;
        const newTs = Date.now() + days * 86400000;
        await api("POST", `/api/devices/${d.serial}/command`, { filterNextReplacementTimestamp: newTs });
        _patchDevice(d.serial, { filterNextReplacementTimestamp: newTs });
      };
    }
    const cleanDaysEl = document.getElementById("maint-clean-days");
    if (cleanDaysEl) {
      cleanDaysEl.onchange = async () => {
        const days = parseInt(cleanDaysEl.value);
        if (isNaN(days)) return;
        const interval = parseInt(document.getElementById("maint-clean-interval").value) || 30;
        const last_ts = _lastTsFromDays(days, interval);
        await api("POST", `/api/devices/${d.serial}`, { last_cleaned_ts: last_ts, cleaning_interval_days: interval });
        _patchDevice(d.serial, { last_cleaned_ts: last_ts, cleaning_interval_days: interval });
      };
    }

    const resetDesiccant = document.getElementById("btn-reset-desiccant");
    if (resetDesiccant) {
      resetDesiccant.onclick = async () => {
        const lifeDays = parseInt(document.getElementById("maint-desiccant-life").value) || 14;
        const now = Date.now();
        await api("POST", `/api/devices/${d.serial}`, { last_desiccant_ts: now, desiccant_life_days: lifeDays });
        _patchDevice(d.serial, { last_desiccant_ts: now, desiccant_life_days: lifeDays });
        resetDesiccant.textContent = t("maint.reset");
        setTimeout(() => { resetDesiccant.textContent = t("maint.reset_desiccant"); renderDeviceTab("maintenance"); }, 1200);
      };
    }
    const recordBowl = document.getElementById("btn-record-bowl");
    if (recordBowl) {
      recordBowl.onclick = async () => {
        const interval = parseInt(document.getElementById("maint-bowl-interval").value) || 7;
        const now = Date.now();
        await api("POST", `/api/devices/${d.serial}`, { last_bowl_cleaned_ts: now, bowl_cleaning_interval_days: interval });
        _patchDevice(d.serial, { last_bowl_cleaned_ts: now, bowl_cleaning_interval_days: interval });
        recordBowl.textContent = t("maint.recorded");
        setTimeout(() => { recordBowl.textContent = t("maint.record_bowl"); renderDeviceTab("maintenance"); }, 1200);
      };
    }
    const recordHousing = document.getElementById("btn-record-housing");
    if (recordHousing) {
      recordHousing.onclick = async () => {
        const interval = parseInt(document.getElementById("maint-housing-interval").value) || 30;
        const now = Date.now();
        await api("POST", `/api/devices/${d.serial}`, { last_housing_cleaned_ts: now, housing_cleaning_interval_days: interval });
        _patchDevice(d.serial, { last_housing_cleaned_ts: now, housing_cleaning_interval_days: interval });
        recordHousing.textContent = t("maint.recorded");
        setTimeout(() => { recordHousing.textContent = t("maint.record_housing"); renderDeviceTab("maintenance"); }, 1200);
      };
    }

    const audioSelect = document.getElementById("maint-audio-select");
    if (audioSelect) _wireFeedSoundControls(d, audioSelect);

    // Interval/life-days fields auto-save independently of the action
    // buttons below, so you can update just the interval without it also
    // claiming the maintenance task was just performed.
    const filterLifeEl = document.getElementById("maint-filter-life");
    if (filterLifeEl) {
      filterLifeEl.onchange = async () => {
        const lifeDays = parseInt(filterLifeEl.value) || 30;
        await api("POST", `/api/devices/${d.serial}`, { filter_life_days: lifeDays });
        _patchDevice(d.serial, { filter_life_days: lifeDays });
        renderDeviceTab("maintenance");
      };
    }
    const cleanIntervalEl = document.getElementById("maint-clean-interval");
    if (cleanIntervalEl) {
      cleanIntervalEl.onchange = async () => {
        const interval = parseInt(cleanIntervalEl.value) || 30;
        await api("POST", `/api/devices/${d.serial}`, { cleaning_interval_days: interval });
        _patchDevice(d.serial, { cleaning_interval_days: interval });
        renderDeviceTab("maintenance");
      };
    }
    const bowlIntervalEl = document.getElementById("maint-bowl-interval");
    if (bowlIntervalEl) {
      bowlIntervalEl.onchange = async () => {
        const interval = parseInt(bowlIntervalEl.value) || 7;
        await api("POST", `/api/devices/${d.serial}`, { bowl_cleaning_interval_days: interval });
        _patchDevice(d.serial, { bowl_cleaning_interval_days: interval });
        renderDeviceTab("maintenance");
      };
    }
    const housingIntervalEl = document.getElementById("maint-housing-interval");
    if (housingIntervalEl) {
      housingIntervalEl.onchange = async () => {
        const interval = parseInt(housingIntervalEl.value) || 30;
        await api("POST", `/api/devices/${d.serial}`, { housing_cleaning_interval_days: interval });
        _patchDevice(d.serial, { housing_cleaning_interval_days: interval });
        renderDeviceTab("maintenance");
      };
    }
    const desiccantLifeEl = document.getElementById("maint-desiccant-life");
    if (desiccantLifeEl) {
      desiccantLifeEl.onchange = async () => {
        const lifeDays = parseInt(desiccantLifeEl.value) || 14;
        await api("POST", `/api/devices/${d.serial}`, { desiccant_life_days: lifeDays });
        _patchDevice(d.serial, { desiccant_life_days: lifeDays });
        renderDeviceTab("maintenance");
      };
    }

    const resetFilter = document.getElementById("btn-reset-filter");
    const recordClean = document.getElementById("btn-record-clean");

    if (resetFilter) {
      resetFilter.onclick = async () => {
        const lifeDays = parseInt(document.getElementById("maint-filter-life").value) || 30;
        await api("POST", `/api/devices/${d.serial}`, { filter_life_days: lifeDays });
        const newTs = Date.now() + lifeDays * 86400000;
        try {
          await api("POST", `/api/devices/${d.serial}/command`, { filterNextReplacementTimestamp: newTs });
          _patchDevice(d.serial, { filterNextReplacementTimestamp: newTs, filter_life_days: lifeDays });
          resetFilter.textContent = t("maint.reset");
          setTimeout(() => {
            resetFilter.textContent = t("maint.reset_filter");
            renderDeviceTab("maintenance");
          }, 1200);
        } catch(e) { alert(t("overview.cmd_failed", {error: e.message})); }
      };
    }
    if (recordClean) {
      recordClean.onclick = async () => {
        const cleanInterval = parseInt(document.getElementById("maint-clean-interval").value) || 30;
        const now = Date.now();
        await api("POST", `/api/devices/${d.serial}`, { last_cleaned_ts: now, cleaning_interval_days: cleanInterval });
        _patchDevice(d.serial, { last_cleaned_ts: now, cleaning_interval_days: cleanInterval });
        recordClean.textContent = t("maint.recorded");
        setTimeout(() => {
          recordClean.textContent = t("maint.record_cleaning");
          renderDeviceTab("maintenance");
        }, 1200);
      };
    }
    const lowWaterEl = document.getElementById("maint-low-water");
    if (lowWaterEl) {
      lowWaterEl.onchange = async () => {
        const inputVal = parseFloat(lowWaterEl.value) || 0;
        const grams = useImperial() ? Math.round(inputVal / 0.033814) : Math.round(inputVal);
        await api("POST", `/api/devices/${d.serial}`, { low_water_grams: grams });
        try {
          await api("POST", `/api/devices/${d.serial}/command`, { lowWater: grams });
        } catch {}
        _patchDevice(d.serial, { low_water_grams: grams, lowWater: grams });
      };
    }
    const minDrinkEl = document.getElementById("maint-min-drink");
    if (minDrinkEl) {
      minDrinkEl.onchange = async () => {
        const val = Math.max(1, parseInt(minDrinkEl.value) || 5);
        await api("POST", `/api/devices/${d.serial}`, { min_drink_grams: val });
        _patchDevice(d.serial, { min_drink_grams: val });
      };
    }
  }
  else if (tabName === "notifications") {
    // Wire show/hide for email address and mobile service sections
    const emailCb = document.getElementById("nchan-email");
    const mobileCb = document.getElementById("nchan-mobile");
    const emailSec = document.getElementById("nchan-email-section");
    const mobileSec = document.getElementById("nchan-mobile-section");
    if (emailCb && emailSec) emailCb.onchange = () => { emailSec.style.display = emailCb.checked ? "" : "none"; };
    if (mobileCb && mobileSec) mobileCb.onchange = () => { mobileSec.style.display = mobileCb.checked ? "" : "none"; };

    const saveBtn = document.getElementById("btn-save-device-notifications");
    if (saveBtn) {
      saveBtn.onclick = async () => {
        const notif = {};
        document.querySelectorAll(".notif-check[data-key]").forEach(cb => {
          notif[cb.dataset.key] = cb.checked;
        });
        const battLowEl = document.getElementById("notif-battery-low-pct");
        const payload = {
          notifications: notif,
          notify_bell: document.getElementById("nchan-bell")?.checked !== false,
          notify_email: document.getElementById("nchan-email")?.checked !== false,
          notify_email_address: (document.getElementById("nchan-email-addr")?.value || "").trim(),
          notify_mobile: !!document.getElementById("nchan-mobile")?.checked,
          notify_mobile_service: document.getElementById("nchan-mobile-svc")?.value || "",
          ...(battLowEl ? { battery_low_pct: Math.max(0, Math.min(99, parseInt(battLowEl.value) || 0)) } : {}),
        };
        await api("POST", `/api/devices/${d.serial}`, payload);
        _patchDevice(d.serial, { ...payload });
        saveBtn.textContent = t("notif.saved");
        setTimeout(() => { saveBtn.textContent = t("notif.save"); }, 1500);
      };
    }
  }
}

function _patchDevice(serial, patch) {
  const d = _devices.find(d => d.serial === serial);
  if (d) Object.assign(d, patch);
  if (_currentDevice && _currentDevice.serial === serial) Object.assign(_currentDevice, patch);
}
