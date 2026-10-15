// Custom dot-matrix icon editor for the OneRFID feeder display.
// Display: 5 rows × 26 columns. Editor uses a 5 × 12 grid centered in the display.
// Bit convention: bit (11-col) of rows[row] = pixel at that column (bit 11 = leftmost).
// To display: shift each row left by 7 to center in the 26-wide display.

const ICON_COLS   = 12;
const ICON_ROWS   = 5;
const DISPLAY_COLS = 26;
const ICON_SHIFT  = (DISPLAY_COLS - ICON_COLS) / 2; // 7

// Built-in presets — send-only, cannot be saved or deleted.
// Stock frames (id 5-8) are the exact frames captured from the PetLibro cloud.
// Custom frame rows use the 5-row, 12-column editor format (shift-7 to center on display).
const ICON_PRESETS = [
  {
    id: "salute",
    name: "Petlibro Salute",
    rows: [128, 128, 448, 480, 224],
    frame: null, // computed from rows
  },
  {
    id: "heart",
    name: "Heart (stock)",
    frame: [0, 55296, 130048, 130048, 130048, 63488, 28672, 8192],
  },
  {
    id: "dog",
    name: "Dog (stock)",
    frame: [0, 16384, 245760, 246016, 32256, 32256, 16896, 50688],
  },
  {
    id: "cat",
    name: "Cat (stock)",
    frame: [0, 69632, 126976, 86016, 58368, 17408, 62464, 130048],
  },
  {
    id: "elk",
    name: "Elk (stock)",
    frame: [0, 540672, 1032192, 196608, 522240, 260096, 174080, 174080],
  },
];

let _editorSerial     = null;
let _editorRows       = [0, 0, 0, 0, 0]; // current pixel data
let _editorIconId     = null;             // null = new icon, number = editing saved icon
let _editorStockFrame = null;             // non-null when a stock preset with fixed frame is loaded
let _editorIconName   = null;             // display name of currently loaded preset/saved icon
let _savedIcons       = [];               // fetched from API

function _pixelLit(row, col) {
  return (_editorRows[row] >> (ICON_COLS - 1 - col)) & 1;
}

function _setPixel(row, col, lit) {
  const bit = ICON_COLS - 1 - col;
  if (lit) {
    _editorRows[row] |= (1 << bit);
  } else {
    _editorRows[row] &= ~(1 << bit);
  }
}

function _togglePixel(row, col) {
  _setPixel(row, col, !_pixelLit(row, col));
}

function _rowToFrame(rows) {
  // Convert editor rows to [0, r0<<shift, ..., r4<<shift, 0]
  return [0, ...rows.map(r => r << ICON_SHIFT), 0];
}

// ── Render ────────────────────────────────────────────────────────────────────

function _renderGrid() {
  const grid = document.getElementById("ie-grid");
  if (!grid) return;
  grid.innerHTML = "";
  for (let r = 0; r < ICON_ROWS; r++) {
    const rowDiv = document.createElement("div");
    rowDiv.style.cssText = "display:flex;gap:3px;margin-bottom:3px";
    for (let c = 0; c < ICON_COLS; c++) {
      const cell = document.createElement("div");
      const lit = _pixelLit(r, c);
      cell.style.cssText = `width:28px;height:28px;border-radius:4px;cursor:pointer;transition:background 0.1s;background:${lit ? "var(--pl-accent)" : "var(--pl-surface-2, #2a2a3a)"}`;
      cell.dataset.r = r;
      cell.dataset.c = c;
      rowDiv.appendChild(cell);
    }
    grid.appendChild(rowDiv);
  }
}

function _renderPreview() {
  const preview = document.getElementById("ie-preview");
  if (!preview) return;
  preview.innerHTML = "";
  // Show the full 26-wide display with the icon centered
  for (let r = 0; r < ICON_ROWS; r++) {
    const displayVal = _editorRows[r] << ICON_SHIFT;
    const rowDiv = document.createElement("div");
    rowDiv.style.cssText = "display:flex;gap:1px;margin-bottom:2px";
    for (let c = DISPLAY_COLS - 1; c >= 0; c--) {
      const lit = (displayVal >> c) & 1;
      const dot = document.createElement("div");
      dot.style.cssText = `width:7px;height:7px;border-radius:1px;background:${lit ? "var(--pl-accent)" : "var(--pl-surface-2, #2a2a3a)"}`;
      rowDiv.appendChild(dot);
    }
    preview.appendChild(rowDiv);
  }
}

function _renderIconList() {
  const list = document.getElementById("ie-list");
  if (!list) return;
  list.innerHTML = "";

  // Presets
  ICON_PRESETS.forEach(preset => {
    const btn = document.createElement("button");
    btn.className = "btn-secondary";
    btn.style.cssText = "width:100%;margin-bottom:6px;text-align:left;font-size:12px;padding:6px 8px";
    btn.textContent = preset.name;
    btn.title = preset.frame ? "Built-in stock icon — send only" : "Built-in preset — send only";
    btn.onclick = () => {
      _editorIconId = null;
      _editorIconName = preset.name;
      _editorStockFrame = preset.frame ?? null;
      document.getElementById("ie-name").value = "";
      document.getElementById("ie-delete").disabled = true;
      if (preset.rows) {
        _editorRows = [...preset.rows];
      } else if (preset.frame) {
        // Extract editor rows from display frame by reversing the shift.
        // Frame is [0, r1..r6, 0] — take the first 5 data rows, strip the centering shift.
        const dataRows = preset.frame.slice(1, 6);
        _editorRows = dataRows.map(r => (r >> ICON_SHIFT) & ((1 << ICON_COLS) - 1));
      } else {
        _editorRows = [0, 0, 0, 0, 0];
      }
      _renderAll();
    };
    list.appendChild(btn);
  });

  if (_savedIcons.length) {
    const sep = document.createElement("div");
    sep.style.cssText = "border-top:1px solid var(--pl-border,#333);margin:8px 0 6px";
    list.appendChild(sep);
  }

  _savedIcons.forEach(icon => {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:4px;margin-bottom:6px;align-items:center";

    const btn = document.createElement("button");
    btn.className = "btn-secondary";
    btn.style.cssText = "flex:1;text-align:left;font-size:12px;padding:6px 8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
    btn.textContent = icon.name || "Untitled";
    btn.onclick = () => {
      _editorRows = [...(icon.rows || [0, 0, 0, 0, 0])];
      _editorIconId = icon.id;
      _editorIconName = icon.name || null;
      _editorStockFrame = null;
      document.getElementById("ie-name").value = icon.name || "";
      document.getElementById("ie-delete").disabled = false;
      _renderAll();
    };
    row.appendChild(btn);
    list.appendChild(row);
  });
}

function _renderAll() {
  _renderGrid();
  _renderPreview();
  _renderIconList();
}

// ── API ───────────────────────────────────────────────────────────────────────

async function _loadIcons() {
  try {
    _savedIcons = await api("GET", "/api/icons");
  } catch (e) {
    _savedIcons = [];
  }
}

async function _saveIcon() {
  const name = (document.getElementById("ie-name").value || "").trim() || "Untitled";
  try {
    if (_editorIconId !== null) {
      await api("PUT", `/api/icons/${_editorIconId}`, { name, rows: _editorRows });
      _savedIcons = _savedIcons.map(ic => ic.id === _editorIconId ? { ...ic, name, rows: [..._editorRows] } : ic);
    } else {
      const res = await api("POST", "/api/icons", { name, rows: _editorRows });
      _editorIconId = res.id;
      _savedIcons.push({ id: res.id, name, rows: [..._editorRows] });
    }
    document.getElementById("ie-name").value = name;
    document.getElementById("ie-delete").disabled = false;
    _renderIconList();
  } catch (e) {
    alert("Failed to save icon.");
  }
}

async function _deleteIcon() {
  if (_editorIconId === null) return;
  if (!await plConfirm({message: t("icon.delete_confirm"), confirmText: t("common.delete"), cancelText: t("common.cancel"), danger: true})) return;
  try {
    await api("DELETE", `/api/icons/${_editorIconId}`);
    _savedIcons = _savedIcons.filter(ic => ic.id !== _editorIconId);
    _editorIconId = null;
    _editorRows = [0, 0, 0, 0, 0];
    document.getElementById("ie-name").value = "";
    document.getElementById("ie-delete").disabled = true;
    _renderAll();
  } catch (e) {
    alert("Failed to delete icon.");
  }
}

async function _sendToFeeder() {
  if (!_editorSerial) return;
  const frame = _editorStockFrame ?? _rowToFrame(_editorRows);
  try {
    const name = _editorIconName || document.getElementById("ie-name").value.trim() || null;
    await api("POST", `/api/devices/${_editorSerial}/command`, { _display_frame: frame, _display_frame_name: name });
    _patchDevice(_editorSerial, { display_icon_name: name, display_icon: 0 });
    renderDevices();
    showToast("Icon sent to feeder.");
  } catch (e) {
    alert("Failed to send icon to feeder.");
  }
}

// ── Mouse drag support ────────────────────────────────────────────────────────

let _dragMode = null; // "set" | "clear"

let _gridWired = false;

function _wireGrid() {
  const grid = document.getElementById("ie-grid");
  if (!grid || _gridWired) return;   // the grid element persists, so wire it only once
  _gridWired = true;

  grid.addEventListener("mousedown", e => {
    const cell = e.target.closest("[data-r]");
    if (!cell) return;
    const r = +cell.dataset.r, c = +cell.dataset.c;
    _dragMode = _pixelLit(r, c) ? "clear" : "set";
    _setPixel(r, c, _dragMode === "set");
    _renderGrid();
    _renderPreview();
    e.preventDefault();
  });

  grid.addEventListener("mousemove", e => {
    if (_dragMode === null) return;
    const cell = e.target.closest("[data-r]");
    if (!cell) return;
    const r = +cell.dataset.r, c = +cell.dataset.c;
    _setPixel(r, c, _dragMode === "set");
    _renderGrid();
    _renderPreview();
  });

  // Touch support
  grid.addEventListener("touchstart", e => {
    const touch = e.touches[0];
    const cell = document.elementFromPoint(touch.clientX, touch.clientY)?.closest("[data-r]");
    if (!cell) return;
    const r = +cell.dataset.r, c = +cell.dataset.c;
    _dragMode = _pixelLit(r, c) ? "clear" : "set";
    _setPixel(r, c, _dragMode === "set");
    _renderGrid();
    _renderPreview();
    e.preventDefault();
  }, { passive: false });

  grid.addEventListener("touchmove", e => {
    if (_dragMode === null) return;
    const touch = e.touches[0];
    const cell = document.elementFromPoint(touch.clientX, touch.clientY)?.closest("[data-r]");
    if (!cell) return;
    const r = +cell.dataset.r, c = +cell.dataset.c;
    _setPixel(r, c, _dragMode === "set");
    _renderGrid();
    _renderPreview();
    e.preventDefault();
  }, { passive: false });
}

document.addEventListener("mouseup",   () => { _dragMode = null; });
document.addEventListener("touchend",  () => { _dragMode = null; });

// ── Modal lifecycle ───────────────────────────────────────────────────────────

function openIconEditor(serial) {
  _editorSerial     = serial;
  _editorRows       = [0, 0, 0, 0, 0];
  _editorIconId     = null;
  _editorIconName   = null;
  _editorStockFrame = null;

  const overlay = document.getElementById("icon-editor-overlay");
  overlay.classList.add("active");
  document.getElementById("ie-name").value = "";
  document.getElementById("ie-delete").disabled = true;

  _loadIcons().then(() => _renderAll());
  _wireGrid();
}

function closeIconEditor() {
  document.getElementById("icon-editor-overlay").classList.remove("active");
  _editorSerial = null;
}

// Wire static buttons on DOMContentLoaded
document.addEventListener("DOMContentLoaded", () => {
  const overlay = document.getElementById("icon-editor-overlay");

  overlay.addEventListener("click", e => {
    if (e.target === overlay) closeIconEditor();
  });

  document.getElementById("ie-btn-send").addEventListener("click", _sendToFeeder);
  document.getElementById("ie-btn-save").addEventListener("click", _saveIcon);
  document.getElementById("ie-btn-clear").addEventListener("click", () => {
    _editorRows = [0, 0, 0, 0, 0];
    _editorStockFrame = null;
    _editorIconId = null;
    document.getElementById("ie-name").value = "";
    document.getElementById("ie-delete").disabled = true;
    _renderGrid();
    _renderPreview();
  });
  document.getElementById("ie-delete").addEventListener("click", _deleteIcon);
});
