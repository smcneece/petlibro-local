// ── Edit device modal ─────────────────────────────────────────────────────
function openEditDevice() {
  const device = _currentDevice;
  if (!device) return;
  document.getElementById("e-name").value = device.name || "";
  populateRoomSelect("e-room", device.room || "");
  populateVariantSelect("e-variant", device.device_type, device.variant || "b");
  document.getElementById("e-image").value = "";
  document.getElementById("e-cred-serial").value = device.serial || "";
  document.getElementById("e-cred-user").value = device.mqtt_user || "";
  document.getElementById("e-cred-pass").value = device.mqtt_pass || "";
  document.getElementById("e-cred-hint").style.display = "none";
  document.getElementById("e-cred-hint").textContent = "";
  ["e-cred-serial", "e-cred-user", "e-cred-pass"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.type = "password";
    const btn = document.querySelector(`[data-target="${id}"]`);
    if (btn) btn.textContent = "👁";
  });

  // Pet assignment checkboxes
  const petSection = document.getElementById("e-pet-section");
  const petList = document.getElementById("e-pet-list");
  if (_pets.length > 0) {
    petSection.style.display = "";
    const assignedIds = new Set(device.pet_ids || []);
    petList.innerHTML = _pets.map(p => `
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px">
        <input type="checkbox" class="e-pet-cb" value="${escHtml(p.id)}"
          ${assignedIds.has(p.id) ? "checked" : ""}
          style="width:16px;height:16px;accent-color:var(--pl-accent)">
        <span>${escHtml(p.name || "Unnamed pet")}</span>
      </label>
    `).join("");
  } else {
    petSection.style.display = "none";
  }

  const eatingSection = document.getElementById("e-eating-section");
  if (device.device_type === "one_rfid") {
    eatingSection.style.display = "";
    document.getElementById("e-min-eating-secs").value = device.min_eating_secs ?? 30;
  } else {
    eatingSection.style.display = "none";
  }

  document.getElementById("modal-edit-device").classList.add("open");
}

async function saveEditDevice() {
  const device = _currentDevice;
  if (!device) return;
  const serial = device.serial;
  const name = document.getElementById("e-name").value.trim();
  const room = document.getElementById("e-room").value;
  const variant = document.getElementById("e-variant").value;
  const imageFile = document.getElementById("e-image").files[0];

  // Compute image URL from variant (built-in images)
  let image_url = deviceImageUrl(device.device_type, variant) || device.image_url || "";

  // Custom image upload takes priority
  if (imageFile) {
    try {
      const formData = new FormData();
      formData.append("image", imageFile);
      const r = await fetch(`${BASE}/api/devices/${serial}/image`, { method: "POST", body: formData });
      if (r.ok) {
        const data = await r.json();
        image_url = data.image_url;
      } else {
        alert(t("overview.upload_failed"));
        return;
      }
    } catch(e) {
      alert(t("overview.upload_error", {error: e.message}));
      return;
    }
  }

  const petIds = Array.from(document.querySelectorAll(".e-pet-cb:checked")).map(cb => cb.value);

  try {
    const payload = { name: name || device.name, room, variant, image_url, pet_ids: petIds };
    if (device.device_type === "one_rfid") {
      const minSecs = parseInt(document.getElementById("e-min-eating-secs").value);
      if (!isNaN(minSecs) && minSecs >= 10) payload.min_eating_secs = minSecs;
    }
    await api("POST", `/api/devices/${serial}`, payload);
    document.getElementById("modal-edit-device").classList.remove("open");
    _lastDeviceRenderKey = "";
    await refresh();
    // Reopen device modal with updated data
    const updated = _devices.find(d => d.serial === serial);
    if (updated) openDeviceModal(updated);
  } catch(e) { alert(t("settings.save_failed", {error: e.message})); }
}

// ── HA Notify Services ────────────────────────────────────────────────────
let _mobileTargets = [];  // [{service, label}] with HA friendly names

async function loadNotifyServices() {
  try {
    [_notifyServices, _mobileTargets] = await Promise.all([
      api("GET", "/api/ha-notify-services"),
      api("GET", "/api/ha-mobile-targets"),
    ]);
  } catch {
    try { _notifyServices = await api("GET", "/api/ha-notify-services"); } catch { _notifyServices = []; }
    _mobileTargets = _notifyServices
      .filter(s => s.startsWith("mobile_app_"))
      .map(s => ({ service: s, label: s.replace("mobile_app_", "").replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()) }));
  }
  _populateNotifyDropdowns();
}

function _mobileOptions(savedValue) {
  return `<option value="">-- None --</option>` +
    _mobileTargets.map(t =>
      `<option value="${escHtml(t.service)}"${t.service === savedValue ? " selected" : ""}>${escHtml(t.label)}</option>`
    ).join("") +
    (savedValue && !_mobileTargets.find(t => t.service === savedValue)
      ? `<option value="${escHtml(savedValue)}" selected>${escHtml(savedValue)}</option>` : "");
}

function _populateNotifyDropdowns() {
  const emailExclude = ["persistent_notification", "notify"];
  const emailExcludePfx = ["mobile_app_", "alexa_media_"];
  const emailSvcs = _notifyServices.filter(s => !emailExclude.includes(s) && !emailExcludePfx.some(p => s.startsWith(p)));

  const emailSel = document.getElementById("s-notify-email-service");
  if (emailSel) {
    const savedEmail = _settings.notify_email_service || "";
    emailSel.innerHTML = `<option value="">-- None --</option>` +
      emailSvcs.map(s => `<option value="${escHtml(s)}"${s === savedEmail ? " selected" : ""}>${escHtml(s)}</option>`).join("");
    if (savedEmail && !emailSvcs.includes(savedEmail)) {
      emailSel.innerHTML += `<option value="${escHtml(savedEmail)}" selected>${escHtml(savedEmail)}</option>`;
    }
  }
  const mobileSel = document.getElementById("s-notify-mobile-service");
  if (mobileSel) {
    mobileSel.innerHTML = _mobileOptions(_settings.notify_mobile_default_service || "");
  }
}

// ── HA Areas ──────────────────────────────────────────────────────────────
let _haAreas = [];

async function loadHaAreas() {
  try {
    _haAreas = await api("GET", "/api/ha-areas");
  } catch { _haAreas = []; }
}

function populateRoomSelect(selectId, currentValue) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  sel.innerHTML = ["", ..._haAreas].map(a =>
    `<option value="${escHtml(a)}"${a === currentValue ? " selected" : ""}>${a || t("room.select")}</option>`
  ).join("");
}

// ── Device type detection ─────────────────────────────────────────────────
const DEVICE_TYPE_MAP = {
  // WF02 was previously mapped to dockstream2 (PLWF106) based on the device
  // appearing as "Dockstream 2" in the PetLibro app; a real MQTT capture
  // (GitHub issue #3) later showed this exact serial actually reports
  // PLWF305 (RFID-capable), correcting the earlier assumption.
  // WF01 (issue #8): box/device label reads "PLWF105", but a real MQTT
  // capture confirmed it reports PLWF106 over the wire, identical to WF03,
  // so it's handled as the same dockstream2 device type.
  "WF01": { device_type: "dockstream2",          model: "WF01" },
  "WF02": { device_type: "dockstream_rfid",      model: "WF02" },
  "WF03": { device_type: "dockstream2",         model: "WF03" },
  "WF04": { device_type: "dockstream2_cordless", model: "WF04" },
  "AF06": { device_type: "one_rfid",             model: "AF06" },
  // AF01 (issue #8): Granary Smart Feeder, no RFID, no lid. Confirmed via a
  // real MQTT capture -- wire model is PLAF103, same as the box number this
  // time (no discrepancy).
  "AF01": { device_type: "granary",              model: "AF01" },
};
const DEVICE_MQTT_MODELS = {
  "dockstream2":          "plwf106",
  "dockstream2_cordless": "plwf116",
  "dockstream_rfid":      "plwf305",
  "one_rfid":             "plaf301",
  "granary":              "plaf103",
};
const DEVICE_VARIANTS = {
  "dockstream2":          [{ value: "b", label: "Black" }, { value: "w", label: "White" }],
  "dockstream2_cordless": [{ value: "b", label: "Black" }, { value: "w", label: "White" }],
  // Only white confirmed so far, not certain there is a black variant. Add
  // a black option back here if one turns up.
  "dockstream_rfid":      [{ value: "w", label: "White" }],
  "one_rfid":             [{ value: "b", label: "Black" }, { value: "w", label: "White" }],
  // Danny's own unit (issue #8) is white; black is a real retail color too
  // (Petlibro sells almost all their devices in both black and white).
  "granary":              [{ value: "b", label: "Black" }, { value: "w", label: "White" }],
};

function deviceImageUrl(deviceType, variant) {
  const model = DEVICE_MQTT_MODELS[deviceType];
  if (!model || !variant) return "";
  return `${BASE}/images/${model}_${variant}`;
}

function populatePetSection(sectionId, listId, optionalLabelId, deviceType) {
  const section = document.getElementById(sectionId);
  const list = document.getElementById(listId);
  const optLabel = document.getElementById(optionalLabelId);
  if (!section || !list) return;
  if (!_pets.length) { section.style.display = "none"; return; }
  section.style.display = "";
  if (optLabel) optLabel.style.display = deviceType === "one_rfid" ? "none" : "";
  list.innerHTML = _pets.map(p => `
    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;color:var(--pl-text)">
      <input type="checkbox" class="add-pet-cb" data-list="${escHtml(listId)}" value="${escHtml(p.id)}"
        style="width:16px;height:16px;accent-color:var(--pl-accent)">
      <span>${escHtml(p.name || "Unnamed pet")}</span>
    </label>
  `).join("");
}

function selectedPetIds(listId) {
  return [...document.querySelectorAll(`#${listId} .add-pet-cb:checked`)].map(cb => cb.value);
}

function detectVariantFromSerial(serial, deviceType) {
  if (deviceType === "dockstream_rfid") return "w"; // only white confirmed so far
  const isFountain = deviceType === "dockstream2" || deviceType === "dockstream2_cordless";
  if (isFountain && serial && serial.length >= 12) {
    const code = serial.substring(10, 12).toUpperCase();
    if (code === "BA") return "w";
    if (code === "BD") return "b";
  }
  return "b"; // default — user can correct in the Color dropdown
}

function populateVariantSelect(selectId, deviceType, currentVariant) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  const variants = DEVICE_VARIANTS[deviceType] || [];
  if (!variants.length) {
    sel.closest(".form-row").style.display = "none";
    return;
  }
  sel.closest(".form-row").style.display = "";
  sel.innerHTML = variants.map(v =>
    `<option value="${v.value}"${v.value === currentVariant ? " selected" : ""}>${v.label}</option>`
  ).join("");
}

function detectFromClientId(clientId) {
  if (!clientId) return {};
  const upper = clientId.toUpperCase();
  for (const [prefix, info] of Object.entries(DEVICE_TYPE_MAP)) {
    if (upper.startsWith(prefix)) return { serial: upper, ...info };
  }
  return { serial: upper };
}
