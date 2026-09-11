// ── Add device flow ────────────────────────────────────────────────────────
function openAddDevice() {
  if (!isMqttConfigured()) { checkMqttSetup(); return; }
  stopCapturePolling();
  _wizardStep = 0;
  ["w-device-type","w-serial","w-mqtt-user","w-mqtt-pass","w-name","w-room",
   "a-serial","a-name","a-room"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
  showAddView("method");
  document.getElementById("modal-add-device").classList.add("open");
}

function showAddView(view) {
  document.getElementById("add-method-select").style.display  = view === "method" ? "" : "none";
  document.getElementById("add-auto-flow").style.display      = view === "auto"   ? "" : "none";
  document.getElementById("add-manual-flow").style.display    = view === "manual" ? "" : "none";
}

let _countdownTimer = null;

function showAutoStep(n) {
  [0, 1, 2].forEach(i => {
    document.getElementById(`auto-step-${i}`).style.display = i === n ? "" : "none";
  });
}

async function startAutoCapture() {
  showAutoStep(1);
  let remaining = 62;
  const el = document.getElementById("auto-countdown");
  const tick = () => {
    remaining--;
    if (remaining > 0) el.textContent = t("add_device.countdown", {n: remaining});
    else { el.textContent = ""; clearInterval(_countdownTimer); }
  };
  el.textContent = t("add_device.countdown", {n: remaining});
  _countdownTimer = setInterval(tick, 1000);
  try {
    await api("POST", "/api/capture/start");
    _capturePolling = setInterval(pollAutoCapture, 2000);
  } catch(e) {
    clearInterval(_countdownTimer);
    showAutoStep(0);
    alert(t("add_device.err_start", {error: e.message}));
  }
}

async function pollAutoCapture() {
  try {
    const s = await api("GET", "/api/capture/status");
    if (s.status === "captured") {
      stopCapturePolling();
      clearInterval(_countdownTimer);
      const detected = detectFromClientId(s.result.client_id || "");
      document.getElementById("a-serial").value = detected.serial || s.result.client_id || "";
      const sel = document.getElementById("a-device-type");
      if (detected.device_type) {
        for (const opt of sel.options) {
          if (opt.value === detected.device_type) { opt.selected = true; break; }
        }
        document.getElementById("a-type-warn").style.display = "none";
      } else {
        // Unrecognized serial prefix -- leave the placeholder selected rather
        // than silently defaulting to whatever option happens to be first in
        // the list, and warn so it's obvious manual selection is needed
        // (previously this defaulted to Dockstream 2 with no indication at
        // all, see issue #8).
        sel.value = "";
        document.getElementById("a-type-warn").style.display = "";
      }
      const capturedUser = s.result.username || "";
      const capturedPass = s.result.password || "";
      document.getElementById("a-serial").dataset.mqttUser = capturedUser;
      document.getElementById("a-serial").dataset.mqttPass = capturedPass;
      document.getElementById("a-cred-user").value = capturedUser;
      document.getElementById("a-cred-pass").value = capturedPass;
      const capturedSerial = detected.serial || s.result.client_id || "";
      const detectedVariant = detectVariantFromSerial(capturedSerial, detected.device_type || "");
      populateVariantSelect("a-variant", detected.device_type || "", detectedVariant);
      populateRoomSelect("a-room", "");
      populatePetSection("a-pet-section", "a-pet-list", "a-pet-optional", detected.device_type || "");
      await api("POST", "/api/capture/reset");
      showAutoStep(2);
    } else if (s.status === "timeout" || s.status === "error") {
      stopCapturePolling();
      clearInterval(_countdownTimer);
      showAutoStep(0);
      alert(s.status === "timeout"
        ? t("add_device.timeout")
        : (s.detail || t("add_device.capture_failed")));
    }
  } catch {}
}

async function finishAutoDevice() {
  const serialEl = document.getElementById("a-serial");
  const serial = serialEl.value.trim().toUpperCase();
  if (!serial) { alert(t("add_device.err_serial")); return; }
  const typeEl = document.getElementById("a-device-type");
  if (!typeEl.value) { alert(t("add_device.err_type")); return; }
  const modelOpt = typeEl.options[typeEl.selectedIndex];
  const aVariant = document.getElementById("a-variant").value;
  const body = {
    serial,
    device_type: typeEl.value,
    model: modelOpt.dataset.model || "",
    mqtt_user: serialEl.dataset.mqttUser || "",
    mqtt_pass: serialEl.dataset.mqttPass || "",
    name: document.getElementById("a-name").value.trim() || t("add_device.default_name"),
    room: document.getElementById("a-room").value.trim(),
    variant: aVariant,
    image_url: deviceImageUrl(typeEl.value, aVariant),
    pet_ids: selectedPetIds("a-pet-list"),
  };
  try {
    await api("POST", "/api/devices", body);
    document.getElementById("modal-add-device").classList.remove("open");
    await refresh();
  } catch(e) { alert(t("add_device.err_add", {error: e.message})); }
}

function updateWizard() {
  [0, 1, 2].forEach(i => {
    document.getElementById(`wizard-step-${i}`).style.display = i === _wizardStep ? "" : "none";
    const dot = document.querySelector(`.wizard-step[data-mstep="${i}"]`);
    if (dot) dot.className = "wizard-step" + (i < _wizardStep ? " done" : i === _wizardStep ? " active" : "");
  });
  document.getElementById("btn-wizard-next").textContent = _wizardStep === 2 ? "Add Device" : "Next";
}

async function wizardNext() {
  if (_wizardStep === 0) {
    if (!document.getElementById("w-device-type").value) { alert(t("add_device.err_type")); return; }
    _wizardStep = 1; updateWizard();
  } else if (_wizardStep === 1) {
    if (!document.getElementById("w-serial").value.trim()) { alert(t("add_device.err_serial")); return; }
    if (!document.getElementById("w-mqtt-user").value.trim()) { alert(t("add_device.err_user")); return; }
    populateRoomSelect("w-room", "");
    populatePetSection("w-pet-section", "w-pet-list", "w-pet-optional", document.getElementById("w-device-type").value);
    _wizardStep = 2; updateWizard();
  } else if (_wizardStep === 2) {
    await finishManualDevice();
  }
}

function wizardBack() {
  if (_wizardStep === 0) showAddView("method");
  else { _wizardStep--; updateWizard(); }
}

async function finishManualDevice() {
  const typeEl = document.getElementById("w-device-type");
  const modelOpt = typeEl.options[typeEl.selectedIndex];
  const serial = document.getElementById("w-serial").value.trim().toUpperCase();
  const wVariant = document.getElementById("w-variant").value;
  const body = {
    serial,
    device_type: typeEl.value,
    model: modelOpt.dataset.model || "",
    mqtt_user: document.getElementById("w-mqtt-user").value.trim(),
    mqtt_pass: document.getElementById("w-mqtt-pass").value,
    name: document.getElementById("w-name").value.trim() || t("add_device.default_name"),
    room: document.getElementById("w-room").value.trim(),
    variant: wVariant,
    image_url: deviceImageUrl(typeEl.value, wVariant),
    pet_ids: selectedPetIds("w-pet-list"),
  };
  try {
    await api("POST", "/api/devices", body);
    document.getElementById("modal-add-device").classList.remove("open");
    await refresh();
  } catch(e) { alert(t("add_device.err_add", {error: e.message})); }
}

function stopCapturePolling() {
  if (_capturePolling) { clearInterval(_capturePolling); _capturePolling = null; }
  if (_countdownTimer) { clearInterval(_countdownTimer); _countdownTimer = null; }
}
