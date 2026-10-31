// ── API ───────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  let r = await fetch(`${BASE}${path}`, opts);
  // Standalone (non-HAOS) deployments protect /api/* with AUTH_TOKEN. A 401 means
  // we need to log in; prompt for the token, then retry the request once.
  if (r.status === 401 && path !== "/api/login") {
    await promptLogin();
    r = await fetch(`${BASE}${path}`, opts);
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// Shared across concurrent 401s so we only show one login box.
let _loginPromise = null;

function promptLogin() {
  if (_loginPromise) return _loginPromise;
  _loginPromise = new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay active pl-confirm";
    const box = document.createElement("div");
    box.className = "modal";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");

    const h = document.createElement("div");
    h.className = "modal-title";
    h.textContent = "Sign in";
    const msg = document.createElement("div");
    msg.className = "pl-confirm-msg";
    msg.textContent = "Enter the access token (AUTH_TOKEN) to use Petlibro Local.";
    const input = document.createElement("input");
    input.className = "form-input";
    input.type = "password";
    input.autocomplete = "current-password";
    input.style.marginTop = "8px";
    const err = document.createElement("div");
    err.style.cssText = "color:var(--pl-danger);font-size:12px;margin-top:6px;display:none";
    err.textContent = "Invalid token.";
    const row = document.createElement("div");
    row.className = "pl-confirm-actions";
    const okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "btn-primary";
    okBtn.textContent = "Sign in";
    row.appendChild(okBtn);
    box.append(h, msg, input, err, row);
    overlay.appendChild(box);

    const submit = async () => {
      err.style.display = "none";
      okBtn.disabled = true;
      try {
        const r = await fetch(`${BASE}/api/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: input.value }),
        });
        if (r.ok) {
          overlay.remove();
          _loginPromise = null;
          resolve();
          return;
        }
      } catch {}
      okBtn.disabled = false;
      err.style.display = "";
      input.select();
    };
    okBtn.addEventListener("click", submit);
    input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    document.body.appendChild(overlay);
    input.focus();
  });
  return _loginPromise;
}

async function loadAll() {
  try {
    [_devices, _pets, _settings] = await Promise.all([
      api("GET", "/api/devices"),
      api("GET", "/api/pets"),
      api("GET", "/api/settings"),
    ]);
  } catch(e) {
    console.error("Load error", e);
  }
}

// ── Alerts & notifications ────────────────────────────────────────────────
function deviceAlerts(device) {
  const notif = device.notifications || {};
  const alerts = [];
  if (device.device_type === "one_rfid" || device.device_type === "granary") {
    if (notif.food_low !== false && device.online && device.surplusGrain === false) alerts.push("food_low");
    if (notif.desiccant_due !== false && maintDueSoon("desiccant_due", desiccantDaysRemaining(device), device.desiccant_life_days ?? 14)) alerts.push("desiccant_due");
    if (notif.bowl_due !== false && maintDueSoon("bowl_due", bowlDaysRemaining(device), device.bowl_cleaning_interval_days ?? 7)) alerts.push("bowl_due");
    if (notif.housing_due !== false && maintDueSoon("housing_due", housingDaysRemaining(device), device.housing_cleaning_interval_days ?? 30)) alerts.push("housing_due");
  } else if (device.device_type === "polar") {
    // nothing besides the offline check below
  } else {
    if (notif.water_low !== false) {
      const threshold = device.lowWater ?? device.low_water_grams ?? 500;
      if (device.online && device.currentWeight != null && device.currentWeight < threshold) {
        alerts.push("water_low");
      }
    }
    if (notif.filter_due !== false) {
      const days = filterDaysRemaining(device);
      if (days != null && days <= 3) alerts.push("filter_due");
    }
    if (notif.cleaning_due !== false) {
      const days = cleaningDaysRemaining(device);
      if (days != null && days <= 0) alerts.push("cleaning_due");
    }
  }
  if (notif.offline !== false && !device.online) alerts.push("offline");
  return alerts;
}

function checkAlerts() {
  let totalAlerts = 0;
  for (const device of _devices) totalAlerts += deviceAlerts(device).length;
  const badge = document.getElementById("bell-badge");
  const bellBtn = document.getElementById("btn-bell");
  const fabBell = document.getElementById("fab-bell");
  const fabBadge = document.getElementById("fab-bell-badge");
  if (totalAlerts > 0) {
    badge.textContent = totalAlerts;
    badge.style.display = "flex";
    bellBtn.style.display = "";
    fabBell.style.display = "";
    fabBell.classList.add("has-alerts");
    fabBadge.textContent = totalAlerts;
    fabBadge.style.display = "flex";
  } else {
    badge.style.display = "none";
    fabBell.style.display = "none";
    fabBell.classList.remove("has-alerts");
    fabBadge.style.display = "none";
  }
}

// Alert keys -> display text, looked up in the locale files (alert.<key>) so
// they translate; the English fallbacks here only cover a missing locale key.
const _ALERT_LABELS = {
  offline:      "Device offline",
  food_low:     "Food level low",
  water_low:    "Low water",
  filter_due:   "Filter replacement due",
  cleaning_due: "Cleaning overdue",
  bowl_due:     "Bowl cleaning due soon",
  housing_due:  "Housing cleaning due soon",
  desiccant_due: "Desiccant replacement due soon",
};

function alertLabel(key) {
  const s = t(`alert.${key}`);
  return s !== `alert.${key}` ? s : (_ALERT_LABELS[key] || key);
}

function openDeviceBySerial(serial) {
  const device = _devices.find(d => d.serial === serial);
  if (device) openDeviceModal(device);
}

function toggleAlertPanel() {
  const panel = document.getElementById("alert-panel");
  if (!panel) return;
  if (panel.style.display !== "none") { panel.style.display = "none"; return; }

  const rows = [];
  for (const d of _devices) {
    const alerts = deviceAlerts(d);
    if (!alerts.length) continue;
    const name = escHtml(d.name || d.serial?.slice(0, 8) || "Device");
    for (const a of alerts) {
      const label = escHtml(alertLabel(a));
      const color = a === "offline" ? "var(--pl-danger)" : "var(--pl-warn, #e09a30)";
      rows.push(`<div class="alert-row" data-serial="${escHtml(d.serial)}" style="padding:8px 16px;border-bottom:1px solid var(--pl-border);cursor:pointer">
        <div style="font-size:13px;font-weight:600">${name}</div>
        <div style="font-size:12px;color:${color};margin-top:2px">${label}</div>
      </div>`);
    }
  }
  if (!rows.length) {
    panel.innerHTML = `<div style="padding:12px 16px;font-size:13px;color:var(--pl-subtext)">No active alerts</div>`;
  } else {
    panel.innerHTML = rows.join("");
    panel.querySelectorAll(".alert-row").forEach(row => {
      row.onclick = () => { openDeviceBySerial(row.dataset.serial); panel.style.display = "none"; };
    });
  }
  panel.style.display = "block";

  const close = (e) => {
    if (!panel.contains(e.target) && !e.target.closest("#btn-bell, #fab-bell, #bell-badge, #fab-bell-badge")) {
      panel.style.display = "none";
      document.removeEventListener("click", close, true);
    }
  };
  setTimeout(() => document.addEventListener("click", close, true), 0);
}
