// ── Help / About ──────────────────────────────────────────────────────────
async function openAbout() {
  document.getElementById("modal-about").classList.add("open");
  try {
    const info = await api("GET", "/api/about");
    document.getElementById("about-version").textContent = info.version || "—";
    document.getElementById("about-ha-version").textContent = info.ha_version || "—";
  } catch {
    document.getElementById("about-version").textContent = "—";
    document.getElementById("about-ha-version").textContent = "—";
  }
}

// Has to be fetch(), not a plain navigation/window.open: these endpoints are
// served through Home Assistant's ingress, and ingress auth is carried by
// same-document requests but does NOT reliably carry over to a fresh
// top-level navigation (confirmed -- a plain window.open() attempt here
// got a 401). So we fetch the file with the page's own credentials, then
// hand it to the browser via a Blob + synthetic <a download> click.
//
// CONFIRMED LIMITATION: the Home Assistant Companion App's built-in
// browser doesn't support triggering a file save this way at all --
// no prompt, no file, no error, on both Android and iOS as tested. A
// same-window navigation to the blob URL was also tried as a fix and made
// things worse (desktop browsers just displayed the raw text instead of
// saving it, and it still didn't work on the Companion App either), so
// that was reverted. Use a regular desktop or mobile browser for these
// buttons instead of the Companion App until/unless a real fix turns up.
async function _downloadViaFetch(btnId, path, fallbackFilename) {
  const btn = document.getElementById(btnId);
  const originalText = btn.textContent;
  btn.disabled = true;
  try {
    const r = await fetch(`${BASE}${path}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const disposition = r.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : fallbackFilename;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch(e) {
    alert(t("about.debug_capture_failed", {error: e.message}));
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function downloadDebugCapture() {
  await _downloadViaFetch("btn-debug-capture", "/api/diag/debug-capture", "petlibro-debug-capture.log");
}

async function downloadNtpLog() {
  await _downloadViaFetch("btn-ntp-log", "/api/diag/ntp-log", "ntp-debug.log");
}

// ── Audio library (Settings → Audio tab) ────────────────────────────────────
let _audioPreviewEl = null;
let _audioPreviewPlayingName = null;

function _toggleAudioPreview(name, btn) {
  if (!_audioPreviewEl) {
    _audioPreviewEl = new Audio();
    _audioPreviewEl.onended = () => _setPlayButtonState(null);
  }
  if (_audioPreviewPlayingName === name) {
    _audioPreviewEl.pause();
    _setPlayButtonState(null);
    return;
  }
  _audioPreviewEl.src = `${BASE}/audio/${encodeURIComponent(name)}`;
  _audioPreviewEl.play().catch(() => {});
  _setPlayButtonState(name);
}

function _setPlayButtonState(playingName) {
  _audioPreviewPlayingName = playingName;
  document.querySelectorAll(".btn-audio-play").forEach(b => {
    b.textContent = b.dataset.name === playingName ? "⏸" : "▶";
  });
}

async function renderAudioLibrary() {
  const list = document.getElementById("audio-library-list");
  if (!list) return;
  try {
    const names = await api("GET", "/api/audio");
    const sorted = [...names].sort((a, b) => a.localeCompare(b));
    list.innerHTML = sorted.length
      ? sorted.map(n => `
        <div style="display:flex;align-items:center;justify-content:space-between;background:var(--pl-surface2);border:1px solid var(--pl-border);border-radius:8px;padding:8px 12px;gap:8px">
          <span style="flex:1">${escHtml(n)}</span>
          <button class="btn-audio-play" data-name="${escHtml(n)}" title="${t("settings.audio_play")}" style="background:none;border:none;cursor:pointer;padding:4px;color:var(--pl-accent);font-size:16px;line-height:1;flex-shrink:0">▶</button>
          <button class="btn-audio-delete" data-name="${escHtml(n)}" title="${t("settings.audio_delete")}" style="background:none;border:none;cursor:pointer;padding:4px;color:var(--pl-danger,#e05252);font-size:16px;line-height:1;opacity:0.8">&#x2715;</button>
        </div>`).join("")
      : `<p class="form-hint">${t("settings.audio_library_empty")}</p>`;
    list.querySelectorAll(".btn-audio-play").forEach(btn => {
      btn.onclick = () => _toggleAudioPreview(btn.dataset.name, btn);
    });
    list.querySelectorAll(".btn-audio-delete").forEach(btn => {
      btn.onclick = async () => {
        if (!confirm(t("settings.audio_delete_confirm", {name: btn.dataset.name}))) return;
        if (_audioPreviewPlayingName === btn.dataset.name) { _audioPreviewEl?.pause(); _setPlayButtonState(null); }
        try {
          await api("DELETE", `/api/audio/${encodeURIComponent(btn.dataset.name)}`);
          await renderAudioLibrary();
        } catch(e) { alert(t("settings.audio_delete_failed", {error: e.message})); }
      };
    });
  } catch {
    list.innerHTML = `<p class="form-hint">${t("settings.audio_library_load_failed")}</p>`;
  }
}

async function uploadAudioBlob(blob, filename, statusEl, nameInput, defaultName) {
  const typed = (nameInput.value || "").trim().toLowerCase().replace(/[^a-z0-9_-]/g, "");
  const fallback = (defaultName || "").trim().toLowerCase().replace(/[^a-z0-9_-]/g, "");
  const name = typed || fallback;
  if (!name) { statusEl.textContent = t("maint.name_required"); return false; }
  statusEl.textContent = t("maint.uploading");
  try {
    const formData = new FormData();
    formData.append("name", name);
    formData.append("file", blob, filename);
    const r = await fetch(`${BASE}/api/audio`, { method: "POST", body: formData });
    if (!r.ok) throw new Error(await r.text());
    statusEl.textContent = t("maint.upload_success");
    nameInput.value = "";
    await renderAudioLibrary();
    return true;
  } catch(e) {
    statusEl.textContent = t("maint.upload_failed", {error: e.message});
    return false;
  }
}

function wireAudioSettingsControls() {
  const uploadBtn  = document.getElementById("btn-audio-upload-file");
  const fileInput  = document.getElementById("audio-file-input");
  const recordBtn  = document.getElementById("btn-audio-record");
  const nameInput  = document.getElementById("audio-new-name");
  const statusEl   = document.getElementById("audio-status");

  if (uploadBtn && fileInput) {
    uploadBtn.onclick = () => fileInput.click();
    fileInput.onchange = () => {
      const file = fileInput.files[0];
      if (file) {
        // Default to the file's own name (extension stripped) if the New
        // Sound Name field is left blank, no reason to force typing one.
        const stem = file.name.replace(/\.[^.]+$/, "");
        uploadAudioBlob(file, file.name, statusEl, nameInput, stem);
      }
      fileInput.value = "";
    };
  }

  if (recordBtn) recordBtn.onclick = openRecordAudioModal;

  const closeRecordBtn = document.getElementById("btn-close-record-audio");
  if (closeRecordBtn) closeRecordBtn.onclick = closeRecordAudioModal;
}

// ── Record Sound modal ───────────────────────────────────────────────────
let _recordMediaRecorder = null;
let _recordChunks = [];
let _recordBlob = null;

function openRecordAudioModal() {
  _recordBlob = null;
  document.getElementById("record-audio-name").value = "";
  document.getElementById("record-audio-status").textContent = "";
  document.getElementById("record-audio-preview-wrap").style.display = "none";
  document.getElementById("record-audio-preview").src = "";
  const toggleBtn = document.getElementById("btn-record-toggle");
  toggleBtn.textContent = t("maint.record");
  toggleBtn.disabled = false;
  document.getElementById("btn-record-save").disabled = true;

  const statusEl = document.getElementById("record-audio-status");
  // Same secure-context check as before, just surfaced up front in the
  // modal instead of only after clicking Record.
  if (!navigator.mediaDevices?.getUserMedia) {
    statusEl.textContent = t("settings.audio_record_insecure");
    toggleBtn.disabled = true;
  }

  toggleBtn.onclick = async () => {
    if (_recordMediaRecorder && _recordMediaRecorder.state === "recording") {
      _recordMediaRecorder.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      _recordChunks = [];
      // Mobile browsers (especially iOS Safari/WebKit) often don't support
      // MediaRecorder's default mimeType at all, and can silently produce
      // zero data instead of throwing. Explicitly pick the first format the
      // browser actually claims to support rather than trusting the default.
      const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/aac", "audio/ogg;codecs=opus"];
      const mimeType = candidates.find(c => window.MediaRecorder?.isTypeSupported?.(c));
      _recordMediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      _recordMediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) _recordChunks.push(e.data); };
      _recordMediaRecorder.onstop = () => {
        stream.getTracks().forEach(track => track.stop());
        toggleBtn.textContent = t("maint.record");
        _recordBlob = new Blob(_recordChunks, { type: _recordMediaRecorder.mimeType || mimeType || "audio/webm" });
        if (_recordBlob.size === 0) {
          statusEl.textContent = t("settings.audio_record_empty");
          document.getElementById("record-audio-preview-wrap").style.display = "none";
          document.getElementById("btn-record-save").disabled = true;
          return;
        }
        const preview = document.getElementById("record-audio-preview");
        preview.src = URL.createObjectURL(_recordBlob);
        document.getElementById("record-audio-preview-wrap").style.display = "";
        document.getElementById("btn-record-save").disabled = false;
        statusEl.textContent = "";
      };
      // Timeslice so data flushes periodically instead of only at the very
      // end -- some mobile browsers handle very short single-chunk
      // recordings unreliably otherwise.
      _recordMediaRecorder.start(500);
      toggleBtn.textContent = t("maint.recording_stop");
      statusEl.textContent = t("maint.recording_in_progress");
    } catch(e) {
      statusEl.textContent = t("maint.mic_denied", {error: e.message});
    }
  };

  document.getElementById("btn-record-save").onclick = async () => {
    if (!_recordBlob) return;
    const nameInput = document.getElementById("record-audio-name");
    const ok = await uploadAudioBlob(_recordBlob, "recording.webm", statusEl, nameInput, "");
    if (ok) closeRecordAudioModal();
  };

  document.getElementById("modal-record-audio").classList.add("open");
}

function closeRecordAudioModal() {
  if (_recordMediaRecorder && _recordMediaRecorder.state === "recording") {
    _recordMediaRecorder.stop();
  }
  document.getElementById("modal-record-audio").classList.remove("open");
}

// ── Settings ──────────────────────────────────────────────────────────────
function renderSettings() {
  document.getElementById("s-mqtt-host").value = _settings.mqtt_host || "";
  document.getElementById("s-mqtt-port").value = _settings.mqtt_port || 1883;
  document.getElementById("s-mqtt-user").value = _settings.mqtt_user || "";
  document.getElementById("s-mqtt-pass").value = _settings.mqtt_pass || "";
  const unitsSel = document.getElementById("s-units");
  if (unitsSel) {
    for (const opt of unitsSel.options) {
      if (opt.value === (_settings.units || "auto")) { opt.selected = true; break; }
    }
  }
  const bellCb = document.getElementById("s-notify-bell");
  if (bellCb) bellCb.checked = _settings.notify_bell_enabled !== false;

  const emailTo = document.getElementById("s-notify-email-to");
  if (emailTo) emailTo.value = _settings.notify_email_to || "";

  _populateNotifyDropdowns();

  const langSel = document.getElementById("s-language");
  if (_settings.language) {
    for (const opt of langSel.options) {
      if (opt.value === _settings.language) { opt.selected = true; break; }
    }
  }
  const tzSel = document.getElementById("s-feeder-tz");
  if (tzSel) {
    const tzVal = _settings.feeder_timezone || Intl.DateTimeFormat().resolvedOptions().timeZone;
    for (const opt of tzSel.options) {
      if (opt.value === tzVal) { opt.selected = true; break; }
    }
  }
  const audioUrlEl = document.getElementById("s-local-audio-base-url");
  if (audioUrlEl) audioUrlEl.value = _settings.local_audio_base_url || "";
}


async function saveSettings(fromTab) {
  const body = {
    mqtt_host: document.getElementById("s-mqtt-host").value.trim(),
    mqtt_port: parseInt(document.getElementById("s-mqtt-port").value) || 1883,
    mqtt_user: document.getElementById("s-mqtt-user").value.trim(),
    mqtt_pass: document.getElementById("s-mqtt-pass").value,
    language: document.getElementById("s-language").value,
    units: document.getElementById("s-units").value,
    feeder_timezone: document.getElementById("s-feeder-tz")?.value || "America/Denver",
    notify_bell_enabled: document.getElementById("s-notify-bell")?.checked !== false,
    notify_email_service: document.getElementById("s-notify-email-service")?.value || "",
    notify_email_to: (document.getElementById("s-notify-email-to")?.value || "").trim(),
    notify_mobile_default_service: document.getElementById("s-notify-mobile-service")?.value || "",
    local_audio_base_url: (document.getElementById("s-local-audio-base-url")?.value || "").trim(),
  };
  const statusEl = document.getElementById("mqtt-status");
  statusEl.className = "mqtt-status";
  try {
    const result = await api("POST", "/api/settings", body);
    _settings = result;
    const ind = document.getElementById(`save-indicator-${fromTab}`);
    if (ind) { ind.classList.add("show"); setTimeout(() => ind.classList.remove("show"), 2000); }
    if (result.mqtt_ok === true) {
      statusEl.className = "mqtt-status ok";
      statusEl.textContent = t("settings.mqtt_ok");
      document.getElementById("setup-banner").classList.remove("show");
    } else if (result.mqtt_ok === false) {
      statusEl.className = "mqtt-status fail";
      statusEl.textContent = t("settings.mqtt_fail");
    }
    // Refresh device render since units may have changed
    _lastDeviceRenderKey = "";
    renderDevices();
  } catch(e) { alert(t("settings.save_failed", {error: e.message})); }
}

async function loadLanguages() {
  try {
    const langs = await api("GET", "/locales/available");
    const sel = document.getElementById("s-language");
    sel.innerHTML = langs.map(l => `<option value="${escHtml(l)}">${l.toUpperCase()}</option>`).join("");
  } catch {}
}

// ── MQTT setup check ──────────────────────────────────────────────────────
function isMqttConfigured() {
  return !!((_settings.mqtt_user || "").trim() && (_settings.mqtt_host || "").trim());
}

function checkMqttSetup() {
  const banner = document.getElementById("setup-banner");
  if (!isMqttConfigured()) {
    banner.classList.add("show");
    switchTab("settings");
    switchSettingsTab("mqtt");
  } else {
    banner.classList.remove("show");
  }
}

function checkPetSetup() {
  const banner = document.getElementById("pet-setup-banner");
  if (!banner) return;
  if (!isMqttConfigured() || !_devices.length || _pets.length > 0) {
    banner.style.display = "none";
    return;
  }
  banner.style.display = "";
}

// ── Crop modal ────────────────────────────────────────────────────────────
let _cropState = null;
let _cropCallback = null;

function openCropModal(file, onCrop) {
  _cropCallback = onCrop;
  const reader = new FileReader();
  reader.onload = e => {
    const img = new Image();
    img.onload = () => {
      const SIZE = 280;
      const minScale = Math.max(SIZE / img.width, SIZE / img.height);
      _cropState = {
        img,
        scale: minScale,
        minScale,
        ox: 0, oy: 0,
        dragging: false, lastX: 0, lastY: 0,
      };
      document.getElementById("crop-zoom").min = minScale;
      document.getElementById("crop-zoom").max = minScale * 6;
      document.getElementById("crop-zoom").step = minScale * 0.005;
      document.getElementById("crop-zoom").value = minScale;
      _cropDraw();
      document.getElementById("modal-crop").classList.add("open");
    };
    img.src = e.target.result;
  };
  reader.readAsDataURL(file);
}

function _cropDraw() {
  const canvas = document.getElementById("crop-canvas");
  const ctx = canvas.getContext("2d");
  const SIZE = 280;
  const { img, scale, ox, oy } = _cropState;
  const w = img.width * scale;
  const h = img.height * scale;
  const x = (SIZE - w) / 2 + ox;
  const y = (SIZE - h) / 2 + oy;
  ctx.clearRect(0, 0, SIZE, SIZE);
  ctx.drawImage(img, x, y, w, h);
}

function _cropClamp() {
  const SIZE = 280;
  const { img, scale } = _cropState;
  const w = img.width * scale;
  const h = img.height * scale;
  const maxOx = Math.max(0, (w - SIZE) / 2);
  const maxOy = Math.max(0, (h - SIZE) / 2);
  _cropState.ox = Math.max(-maxOx, Math.min(maxOx, _cropState.ox));
  _cropState.oy = Math.max(-maxOy, Math.min(maxOy, _cropState.oy));
}

function _applyCrop() {
  const SIZE = 280;
  const EXPORT = 400;
  const ratio = EXPORT / SIZE;
  const offCanvas = document.createElement("canvas");
  offCanvas.width = EXPORT; offCanvas.height = EXPORT;
  const ctx = offCanvas.getContext("2d");
  // Circular clip
  ctx.beginPath();
  ctx.arc(EXPORT / 2, EXPORT / 2, EXPORT / 2, 0, Math.PI * 2);
  ctx.clip();
  const { img, scale, ox, oy } = _cropState;
  const w = img.width * scale * ratio;
  const h = img.height * scale * ratio;
  const x = (EXPORT - w) / 2 + ox * ratio;
  const y = (EXPORT - h) / 2 + oy * ratio;
  ctx.drawImage(img, x, y, w, h);
  offCanvas.toBlob(blob => {
    document.getElementById("modal-crop").classList.remove("open");
    if (_cropCallback) _cropCallback(blob);
  }, "image/png");
}

function _wireCropModal() {
  const wrap = document.getElementById("crop-canvas-wrap");
  const canvas = document.getElementById("crop-canvas");

  // Mouse drag
  wrap.addEventListener("mousedown", e => {
    if (!_cropState) return;
    _cropState.dragging = true;
    _cropState.lastX = e.clientX;
    _cropState.lastY = e.clientY;
  });
  window.addEventListener("mousemove", e => {
    if (!_cropState?.dragging) return;
    _cropState.ox += e.clientX - _cropState.lastX;
    _cropState.oy += e.clientY - _cropState.lastY;
    _cropState.lastX = e.clientX;
    _cropState.lastY = e.clientY;
    _cropClamp();
    _cropDraw();
  });
  window.addEventListener("mouseup", () => { if (_cropState) _cropState.dragging = false; });

  // Touch drag
  let _lastTouchX = 0, _lastTouchY = 0;
  wrap.addEventListener("touchstart", e => {
    if (!_cropState || e.touches.length !== 1) return;
    _lastTouchX = e.touches[0].clientX;
    _lastTouchY = e.touches[0].clientY;
    e.preventDefault();
  }, { passive: false });
  wrap.addEventListener("touchmove", e => {
    if (!_cropState || e.touches.length !== 1) return;
    _cropState.ox += e.touches[0].clientX - _lastTouchX;
    _cropState.oy += e.touches[0].clientY - _lastTouchY;
    _lastTouchX = e.touches[0].clientX;
    _lastTouchY = e.touches[0].clientY;
    _cropClamp();
    _cropDraw();
    e.preventDefault();
  }, { passive: false });

  // Zoom slider
  document.getElementById("crop-zoom").addEventListener("input", e => {
    if (!_cropState) return;
    _cropState.scale = parseFloat(e.target.value);
    _cropClamp();
    _cropDraw();
  });

  document.getElementById("btn-crop-apply").addEventListener("click", _applyCrop);
  document.getElementById("btn-crop-cancel").addEventListener("click", () => {
    document.getElementById("modal-crop").classList.remove("open");
  });
}
