// LED animation editor: draw frames on the 26 x 7 grid, keep a library of your
// own animations, send to the feeder, and import or export as JSON.
// A frame is 7 ints (rows top to bottom), bit 25 = leftmost pixel.

const AE_COLS = 26, AE_ROWS = 7, AE_MAX_FRAMES = 140;
let _aeSerial = null;
let _aeFrames = [[0, 0, 0, 0, 0, 0, 0]];
let _aeIdx = 0;
let _aeLibrary = [];
let _aeEditingId = null;      // id of the saved animation being edited, or null
let _aePlayTimer = null;
let _aePlayIdx = 0;

function _aeMsg(text, bad) {
  const el = document.getElementById("ae-msg");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = bad ? "var(--pl-danger)" : "";
}

function _aePixel(frame, r, c) { return (frame[r] >> (AE_COLS - 1 - c)) & 1; }
function _aeSet(frame, r, c, on) {
  const bit = 1 << (AE_COLS - 1 - c);
  frame[r] = on ? (frame[r] | bit) : (frame[r] & ~bit);
}

function _aeDrawGrid() {
  const cv = document.getElementById("ae-grid");
  if (!cv) return;
  const ctx = cv.getContext("2d");
  const cell = cv.width / AE_COLS;
  ctx.clearRect(0, 0, cv.width, cv.height);
  const f = _aeFrames[_aeIdx];
  for (let r = 0; r < AE_ROWS; r++) {
    for (let c = 0; c < AE_COLS; c++) {
      ctx.fillStyle = _aePixel(f, r, c) ? "#fff0c8" : "#262636";
      ctx.beginPath();
      ctx.arc(c * cell + cell / 2, r * cell + cell / 2, cell * 0.4, 0, Math.PI * 2);
      ctx.fill();
    }
  }
}

function _aeRenderStrip() {
  const strip = document.getElementById("ae-strip");
  if (!strip) return;
  strip.innerHTML = "";
  _aeFrames.forEach((f, i) => {
    const cv = document.createElement("canvas");
    cv.width = 104; cv.height = 28;
    cv.style.cssText = `flex-shrink:0;border-radius:4px;cursor:pointer;background:#10101a;border:2px solid ${i === _aeIdx ? "var(--pl-accent)" : "transparent"}`;
    const ctx = cv.getContext("2d");
    for (let r = 0; r < AE_ROWS; r++) for (let c = 0; c < AE_COLS; c++) {
      if (_aePixel(f, r, c)) { ctx.fillStyle = "#fff0c8"; ctx.fillRect(c * 4, r * 4, 3, 3); }
    }
    cv.onclick = () => { _aeIdx = i; _aeRefresh(); };
    strip.appendChild(cv);
  });
  const cur = strip.children[_aeIdx];
  if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: "nearest", inline: "nearest" });
  document.getElementById("ae-count").textContent =
    `${t("animed.frame")} ${_aeIdx + 1} / ${_aeFrames.length} (${t("animed.max", {n: AE_MAX_FRAMES})})`;
}

function _aeRefresh() { _aeDrawGrid(); _aeRenderStrip(); _aePlay(); }

function _aePlay() {
  if (_aePlayTimer) clearInterval(_aePlayTimer);
  const cv = document.getElementById("ae-preview");
  if (!cv) return;
  _aePlayIdx = 0;
  _aePlayTimer = setInterval(() => {
    if (!cv.isConnected || !document.getElementById("anim-editor-overlay").classList.contains("active")) {
      clearInterval(_aePlayTimer); _aePlayTimer = null; return;
    }
    _drawAnimFrame(cv, _aeFrames[_aePlayIdx++ % _aeFrames.length]);
  }, Math.max(1, +document.getElementById("ae-speed").value) * 25);
}

function _aeValidFrames(frames) {
  return Array.isArray(frames) && frames.length >= 1 && frames.length <= AE_MAX_FRAMES &&
    frames.every(f => Array.isArray(f) && f.length === AE_ROWS &&
      f.every(v => Number.isInteger(v) && v >= 0 && v < (1 << AE_COLS)));
}

// Accepts the exported object, a bare list of frames, or frames written as
// 7 text rows of 26 characters ('#' lit).
function _aeParseImport(text) {
  const data = JSON.parse(text);
  const frames = Array.isArray(data) ? data : data.frames;
  if (!Array.isArray(frames)) throw new Error(t("animed.err_format"));
  const out = frames.map(f => {
    if (Array.isArray(f) && f.length === AE_ROWS && typeof f[0] === "string") {
      return f.map(line => {
        let v = 0;
        for (let c = 0; c < AE_COLS; c++) if (line[c] === "#") v |= 1 << (AE_COLS - 1 - c);
        return v;
      });
    }
    return f;
  });
  if (!_aeValidFrames(out)) throw new Error(t("animed.err_frames", {n: AE_MAX_FRAMES}));
  return {
    frames: out,
    name: Array.isArray(data) ? "" : String(data.name || ""),
    interval: Array.isArray(data) ? 4 : (+data.interval || 4),
  };
}

function _aeLoad(entry, asCopy) {
  _aeFrames = entry.frames.map(f => [...f]);
  _aeIdx = 0;
  _aeEditingId = (entry.custom && !asCopy) ? entry.id : null;
  document.getElementById("ae-name").value =
    (entry.custom && !asCopy) ? entry.name : (entry.name ? entry.name + " (copy)" : "");
  document.getElementById("ae-speed").value = Math.min(8, Math.max(2, entry.interval || 4));
  _aeMsg("");
  _aeRefresh();
  _aeRenderList();
}

async function _aeLoadLibrary() {
  try { _aeLibrary = await api("GET", "/api/animations"); } catch { _aeLibrary = []; }
  _aeRenderList();
}

function _aeRenderList() {
  const list = document.getElementById("ae-list");
  if (!list) return;
  list.innerHTML = "";
  const mk = (entry) => {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:4px;margin-bottom:6px";
    const b = document.createElement("button");
    b.className = "btn-secondary";
    b.style.cssText = "flex:1;text-align:left;font-size:12px;padding:6px 8px" +
      (entry.custom && entry.id === _aeEditingId ? ";border-color:var(--pl-accent)" : "");
    b.textContent = entry.name;
    b.onclick = () => _aeLoad(entry, !entry.custom);
    row.appendChild(b);
    if (entry.custom) {
      const x = document.createElement("button");
      x.className = "btn-secondary";
      x.style.cssText = "padding:6px 8px;color:var(--pl-danger)";
      x.textContent = "✕";
      x.title = t("animed.del_saved");
      x.onclick = async () => {
        if (!await plConfirm({message: t("animed.del_confirm", {name: entry.name}), confirmText: t("common.delete"), cancelText: t("common.cancel"), danger: true})) return;
        await api("DELETE", `/api/animations/${entry.id}`);
        if (_aeEditingId === entry.id) _aeEditingId = null;
        _animPresets = null;
        await _aeLoadLibrary();
      };
      row.appendChild(x);
    }
    list.appendChild(row);
  };
  _aeLibrary.filter(a => a.custom).forEach(mk);
  if (_aeLibrary.some(a => a.custom)) {
    const hr = document.createElement("div");
    hr.style.cssText = "height:1px;background:var(--pl-border);margin:8px 0";
    list.appendChild(hr);
  }
  _aeLibrary.filter(a => !a.custom).forEach(mk);
}

function _aeEditFrame(fn) { fn(_aeFrames[_aeIdx]); _aeRefresh(); }

// Horizontal shifts wrap around (handy for scrolling loops); vertical ones clip.
function _aeShift(dx, dy) {
  const mask = (1 << AE_COLS) - 1;
  _aeEditFrame(f => {
    const old = [...f];
    for (let r = 0; r < AE_ROWS; r++) {
      const src = r - dy;
      let v = (src >= 0 && src < AE_ROWS) ? old[src] : 0;
      if (dx > 0) v = ((v >>> dx) | (v << (AE_COLS - dx))) & mask;
      else if (dx < 0) v = ((v << -dx) | (v >>> (AE_COLS + dx))) & mask;
      f[r] = v;
    }
  });
}

function _aeWireGrid() {
  const cv = document.getElementById("ae-grid");
  let painting = false, paintValue = 1;
  const cellAt = (e) => {
    const rect = cv.getBoundingClientRect();
    const c = Math.floor((e.clientX - rect.left) / rect.width * AE_COLS);
    const r = Math.floor((e.clientY - rect.top) / rect.height * AE_ROWS);
    return (r >= 0 && r < AE_ROWS && c >= 0 && c < AE_COLS) ? [r, c] : null;
  };
  cv.onpointerdown = (e) => {
    const rc = cellAt(e); if (!rc) return;
    cv.setPointerCapture(e.pointerId);
    painting = true;
    paintValue = _aePixel(_aeFrames[_aeIdx], rc[0], rc[1]) ? 0 : 1;
    _aeSet(_aeFrames[_aeIdx], rc[0], rc[1], paintValue);
    _aeDrawGrid();
  };
  cv.onpointermove = (e) => {
    if (!painting) return;
    const rc = cellAt(e); if (!rc) return;
    _aeSet(_aeFrames[_aeIdx], rc[0], rc[1], paintValue);
    _aeDrawGrid();
  };
  const end = () => { if (painting) { painting = false; _aeRenderStrip(); } };
  cv.onpointerup = end;
  cv.onpointercancel = end;
}

function _aeCurrent() {
  return {
    name: document.getElementById("ae-name").value.trim() || "Animation",
    interval: +document.getElementById("ae-speed").value,
    frames: _aeFrames,
  };
}

function openAnimEditor(serial) {
  _aeSerial = serial;
  _aeFrames = [[0, 0, 0, 0, 0, 0, 0]];
  _aeIdx = 0;
  _aeEditingId = null;
  document.getElementById("ae-name").value = "";
  document.getElementById("ae-json").value = "";
  document.getElementById("ae-speed").value = 4;
  _aeMsg("");
  document.getElementById("anim-editor-overlay").classList.add("active");
  _aeWireGrid();
  _aeRefresh();
  _aeLoadLibrary();
}

function closeAnimEditor() {
  document.getElementById("anim-editor-overlay").classList.remove("active");
  if (_aePlayTimer) { clearInterval(_aePlayTimer); _aePlayTimer = null; }
  _aeSerial = null;
}

function _aeIsBlank() {
  return _aeFrames.every(f => f.every(v => v === 0)) && !document.getElementById("ae-name").value.trim();
}

document.addEventListener("DOMContentLoaded", () => {
  const $ = (id) => document.getElementById(id);
  $("ae-new").onclick = async () => {
    if (!_aeIsBlank() && !await plConfirm({message: t("animed.new_confirm"), confirmText: t("animed.new_btn"), cancelText: t("common.cancel"), danger: true})) return;
    _aeFrames = [[0, 0, 0, 0, 0, 0, 0]];
    _aeIdx = 0;
    _aeEditingId = null;
    $("ae-name").value = "";
    $("ae-speed").value = 4;
    _aeMsg("");
    _aeRefresh();
    _aeRenderList();
  };
  $("ae-add").onclick = () => {
    if (_aeFrames.length >= AE_MAX_FRAMES) return _aeMsg(t("animed.err_max", {n: AE_MAX_FRAMES}), true);
    _aeFrames.splice(_aeIdx + 1, 0, [0, 0, 0, 0, 0, 0, 0]); _aeIdx++; _aeRefresh();
  };
  $("ae-dup").onclick = () => {
    if (_aeFrames.length >= AE_MAX_FRAMES) return _aeMsg(t("animed.err_max", {n: AE_MAX_FRAMES}), true);
    _aeFrames.splice(_aeIdx + 1, 0, [..._aeFrames[_aeIdx]]); _aeIdx++; _aeRefresh();
  };
  $("ae-del").onclick = () => {
    if (_aeFrames.length === 1) { _aeFrames[0] = [0, 0, 0, 0, 0, 0, 0]; }
    else { _aeFrames.splice(_aeIdx, 1); _aeIdx = Math.min(_aeIdx, _aeFrames.length - 1); }
    _aeRefresh();
  };
  $("ae-left").onclick = () => {
    if (_aeIdx > 0) {
      [_aeFrames[_aeIdx - 1], _aeFrames[_aeIdx]] = [_aeFrames[_aeIdx], _aeFrames[_aeIdx - 1]];
      _aeIdx--; _aeRefresh();
    }
  };
  $("ae-right").onclick = () => {
    if (_aeIdx < _aeFrames.length - 1) {
      [_aeFrames[_aeIdx + 1], _aeFrames[_aeIdx]] = [_aeFrames[_aeIdx], _aeFrames[_aeIdx + 1]];
      _aeIdx++; _aeRefresh();
    }
  };
  $("ae-clear").onclick = () => _aeEditFrame(f => f.fill(0));
  $("ae-sl").onclick = () => _aeShift(-1, 0);
  $("ae-sr").onclick = () => _aeShift(1, 0);
  $("ae-su").onclick = () => _aeShift(0, -1);
  $("ae-sd").onclick = () => _aeShift(0, 1);
  $("ae-speed").oninput = _aePlay;
  $("ae-save").onclick = async () => {
    const a = _aeCurrent();
    try {
      const url = _aeEditingId ? `/api/animations/${_aeEditingId}` : "/api/animations";
      const r = await api(_aeEditingId ? "PUT" : "POST", url, a);
      _aeEditingId = r.id;
      if (r.name) $("ae-name").value = r.name;
      _animPresets = null;
      await _aeLoadLibrary();
      _aeMsg(t("animed.saved"));
    } catch (e) { _aeMsg(t("animed.err_save", {error: e.message}), true); }
  };
  $("ae-send").onclick = async () => {
    const btn = $("ae-send"), orig = btn.textContent;
    btn.textContent = t("overview.sending");
    btn.disabled = true;
    try {
      await api("POST", `/api/devices/${_aeSerial}/command`,
        { _display_animation: { frames: _aeFrames, interval: +$("ae-speed").value, loop: true, name: $("ae-name").value.trim() } });
      btn.textContent = t("overview.sent");
    } catch (e) { btn.textContent = t("overview.failed"); _aeMsg(e.message, true); }
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
  };
  $("ae-export").onclick = () => {
    const a = _aeCurrent();
    const json = JSON.stringify({
      format: "petlibro-local-animation", version: 1,
      name: a.name, interval: a.interval, loop: true, frames: a.frames,
    });
    $("ae-json").value = json;
    $("ae-json").closest("details").open = true;
    try {
      const url = URL.createObjectURL(new Blob([json], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = a.name.replace(/[^\w-]+/g, "_") + ".json";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (e) { /* the JSON is in the box either way */ }
    _aeMsg(t("animed.exported"));
  };
  // Import adds the animation to your library under its own name (a number is
  // added only if that name is already taken). If the library is full it is
  // still loaded into the editor, unsaved, so nothing is lost.
  const doImport = async (text) => {
    let r;
    try { r = _aeParseImport(text); }
    catch (e) { _aeMsg(t("animed.err_import", {error: e.message}), true); return; }
    const name = r.name || t("animed.imported_name");
    try {
      const saved = await api("POST", "/api/animations", { name, interval: r.interval, frames: r.frames });
      _animPresets = null;
      await _aeLoadLibrary();
      _aeLoad({ frames: r.frames, name: saved.name, interval: r.interval, custom: true, id: saved.id }, false);
      _aeMsg(t("animed.imported_saved", {n: r.frames.length, name: saved.name}));
    } catch (e) {
      _aeLoad({ frames: r.frames, name, interval: r.interval, custom: false }, true);
      $("ae-name").value = name;
      _aeMsg(t("animed.imported_unsaved", {n: r.frames.length}), true);
    }
  };
  $("ae-import").onclick = () => doImport($("ae-json").value);
  $("ae-file").onchange = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { $("ae-json").value = reader.result; doImport(reader.result); };
    reader.readAsText(file);
    e.target.value = "";
  };
});
