// ── Tab switching ─────────────────────────────────────────────────────────
function switchSettingsTab(name) {
  document.querySelectorAll(".stab").forEach(b => b.classList.toggle("active", b.dataset.stab === name));
  document.querySelectorAll(".stab-panel").forEach(p => p.classList.toggle("active", p.id === `stab-${name}`));
  if (name === "audio") renderAudioLibrary();
}

function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.toggle("active", p.id === `panel-${name}`));
  const sw = document.getElementById("header-sort-wrap");
  if (sw) sw.style.display = name === "devices" ? "flex" : "none";
}

// ── Refresh ───────────────────────────────────────────────────────────────
async function refresh() {
  _lastDeviceRenderKey = "";
  _lastPetRenderKey = "";
  await loadAll();
  renderDevices();
  renderPets();
  renderSettings();
  checkAlerts();
  checkPetSetup();
}

async function pollRefresh() {
  await loadAll();
  renderDevices();
  renderPets();
  checkAlerts();
  // Never re-render settings while user may be typing
}

// ── Utils ─────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Language auto-detect ──────────────────────────────────────────────────
async function detectLanguage(availableLangs) {
  const stored = _settings.language;
  if (stored && stored !== "auto" && availableLangs.includes(stored)) return stored;
  const browserLangs = Array.from(navigator.languages || [navigator.language || "en"]);
  for (const lang of browserLangs) {
    const code = lang.split("-")[0].toLowerCase();
    if (availableLangs.includes(code)) return code;
  }
  return "en";
}

// ── Init ──────────────────────────────────────────────────────────────────
(async () => {
  // Load settings first (needed for language + units)
  try {
    _settings = await api("GET", "/api/settings");
  } catch {}

  // Auto-detect language
  let availableLangs = ["en"];
  try { availableLangs = await api("GET", "/locales/available"); } catch {}
  const lang = await detectLanguage(availableLangs);
  await loadLocale(lang);
  applyI18n();

  // Populate language dropdown
  const langSel = document.getElementById("s-language");
  langSel.innerHTML = availableLangs.map(l =>
    `<option value="${escHtml(l)}"${l === lang ? " selected" : ""}>${l.toUpperCase()}</option>`
  ).join("");

  // Load HA areas and notify services
  await Promise.all([loadHaAreas(), loadNotifyServices()]);

  // Wire app tabs
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // Wire device modal
  document.getElementById("btn-close-device").addEventListener("click", () => {
    document.getElementById("modal-device").classList.remove("open");
  });
  document.querySelectorAll(".dtab").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".dtab").forEach(b => b.classList.toggle("active", b === btn));
      renderDeviceTab(btn.dataset.dtab);
    });
  });

  // Wire edit device modal
  document.getElementById("btn-close-edit").addEventListener("click", () => {
    document.getElementById("modal-edit-device").classList.remove("open");
  });
  document.getElementById("btn-resave-creds").addEventListener("click", async () => {
    const btn = document.getElementById("btn-resave-creds");
    const hint = document.getElementById("e-cred-hint");
    btn.disabled = true;
    btn.textContent = t("edit_device.resave_saving");
    hint.style.display = "none";
    try {
      const r = await api("POST", `/api/devices/${_currentDevice.serial}/resave-creds`);
      hint.textContent = r.result === "added" ? t("edit_device.resave_added") : t("edit_device.resave_exists");
      hint.style.color = "var(--pl-success)";
    } catch (e) {
      hint.textContent = t("edit_device.resave_failed", {error: e.message || "check add-on logs"});
      hint.style.color = "var(--pl-danger)";
    }
    hint.style.display = "";
    btn.disabled = false;
    btn.textContent = t("edit_device.resave_creds");
  });

  document.getElementById("btn-edit-cancel").addEventListener("click", () => {
    document.getElementById("modal-edit-device").classList.remove("open");
  });
  document.getElementById("btn-edit-save").addEventListener("click", saveEditDevice);

  // Wire add device
  document.getElementById("btn-close-add").addEventListener("click", () => {
    document.getElementById("modal-add-device").classList.remove("open");
    stopCapturePolling();
  });
  document.getElementById("btn-add-device").addEventListener("click", openAddDevice);
  document.getElementById("btn-method-auto").addEventListener("click", () => {
    showAddView("auto"); showAutoStep(0);
  });
  document.getElementById("btn-method-manual").addEventListener("click", () => {
    populateVariantSelect("w-variant", document.getElementById("w-device-type").value, "b");
    showAddView("manual"); _wizardStep = 0; updateWizard();
  });
  document.getElementById("a-device-type").addEventListener("change", e => {
    populateVariantSelect("a-variant", e.target.value, "b");
  });
  document.getElementById("w-device-type").addEventListener("change", e => {
    populateVariantSelect("w-variant", e.target.value, "b");
  });
  document.getElementById("btn-auto-back-intro").addEventListener("click", () => showAddView("method"));
  document.getElementById("btn-auto-start").addEventListener("click", startAutoCapture);
  document.getElementById("btn-auto-finish").addEventListener("click", finishAutoDevice);
  document.getElementById("btn-wizard-next").addEventListener("click", wizardNext);
  document.getElementById("btn-wizard-back").addEventListener("click", wizardBack);

  // Wire pet modal
  document.getElementById("btn-close-pet").addEventListener("click", () => {
    document.getElementById("modal-pet").classList.remove("open");
  });
  document.getElementById("btn-add-pet").addEventListener("click", () => openPetModal(null));

  // Eyeball toggle — works for any .btn-eye button with data-target pointing to an input id
  document.addEventListener("click", e => {
    const btn = e.target.closest(".btn-eye");
    if (!btn) return;
    const input = document.getElementById(btn.dataset.target);
    if (!input) return;
    input.type = input.type === "password" ? "text" : "password";
    btn.textContent = input.type === "password" ? "👁" : "🙈";
  });

  document.getElementById("btn-about").addEventListener("click", openAbout);
  document.getElementById("btn-close-about").addEventListener("click", () => {
    document.getElementById("modal-about").classList.remove("open");
  });
  document.getElementById("btn-debug-capture").addEventListener("click", downloadDebugCapture);
  document.getElementById("btn-ntp-log").addEventListener("click", downloadNtpLog);
  document.getElementById("btn-pet-save").addEventListener("click", savePet);

  // Wire pet photo button → file input → crop modal
  document.getElementById("btn-pet-photo").addEventListener("click", () => {
    document.getElementById("p-photo-file").click();
  });
  document.getElementById("p-photo-file").addEventListener("change", e => {
    const file = e.target.files?.[0];
    if (!file) return;
    openCropModal(file, blob => {
      _pendingPetPhoto = blob;
      const url = URL.createObjectURL(blob);
      const preview = document.getElementById("pet-photo-preview");
      const placeholder = document.getElementById("pet-photo-placeholder");
      preview.src = url;
      preview.style.display = "";
      placeholder.style.display = "none";
    });
  });

  // Wire crop modal
  _wireCropModal();

  // Wire settings tabs and per-tab save buttons
  document.querySelectorAll(".stab").forEach(btn => {
    btn.addEventListener("click", () => switchSettingsTab(btn.dataset.stab));
  });
  document.getElementById("btn-save-general").addEventListener("click", () => saveSettings("general"));
  document.getElementById("btn-save-notifications").addEventListener("click", () => saveSettings("notifications"));
  document.getElementById("btn-save-mqtt").addEventListener("click", () => saveSettings("mqtt"));
  document.getElementById("btn-save-audio-settings").addEventListener("click", () => saveSettings("audio"));
  wireAudioSettingsControls();
  document.getElementById("btn-banner-mqtt").addEventListener("click", () => {
    switchTab("settings");
    switchSettingsTab("mqtt");
  });
  document.getElementById("btn-banner-pets").addEventListener("click", () => {
    switchTab("pets");
    openPetModal(null);
  });
  document.getElementById("device-sort-select").addEventListener("change", e => {
    _deviceSort = e.target.value;
    localStorage.setItem("pl_device_sort", _deviceSort);
    _lastDeviceRenderKey = "";
    renderDevices();
  });
  document.getElementById("btn-bell").addEventListener("click", toggleAlertPanel);
  document.getElementById("fab-add-device").addEventListener("click", openAddDevice);
  document.getElementById("fab-bell").addEventListener("click", toggleAlertPanel);

  // Backdrop click to close modals
  document.querySelectorAll(".modal-backdrop").forEach(bd => {
    bd.addEventListener("click", e => {
      if (e.target !== bd) return;
      // Recording modal needs its own close path so a click-outside while
      // actively recording actually stops the mic stream, not just hides it.
      if (bd.id === "modal-record-audio") { closeRecordAudioModal(); return; }
      bd.classList.remove("open");
      stopCapturePolling();
    });
  });

  // Initial load
  await loadAll();
  populateSortSelect();
  renderDevices();
  renderPets();
  renderSettings();
  checkAlerts();
  checkMqttSetup();
  checkPetSetup();
  // If MQTT is set up, devices exist, but no pets have been added yet, nudge user to Pets tab
  if (isMqttConfigured() && _devices.length > 0 && _pets.length === 0) {
    switchTab("pets");
  }

  setInterval(pollRefresh, 8000);
})();