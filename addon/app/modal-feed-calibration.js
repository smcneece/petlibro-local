// Feed amount calibration wizard -- lets a user measure how many real grams
// their feeder actually dispenses per portion (the fixed-volume cog scoop
// varies by food density/shape), instead of assuming the flat 10g/portion
// default. Entirely optional: a device with no calibration saved just keeps
// using the 10g default everywhere fmtPortions() is called (see util.js).

const _CAL_MEASUREMENTS_NEEDED = 3;
const _CAL_GRAMS_PER_OZ = 28.349523125;

// Measurements are always stored in grams (that's what calibrated_grams_per_portion
// is); the unit is only what the user types their scale's reading in.
let _calUnit = "g";

let _calDevice = null;
let _calMeasurements = [];
let _calStep = 0; // 0-indexed dispense/weigh round, or -1 for intro

function openFeedCalibration(device) {
  _calDevice = device;
  _calMeasurements = [];
  _calStep = -1;
  _calUnit = useImperial() ? "oz" : "g";
  document.getElementById("feed-cal-overlay").classList.add("active");
  _calRenderIntro();
}

function closeFeedCalibration() {
  document.getElementById("feed-cal-overlay").classList.remove("active");
  _calDevice = null;
  _calMeasurements = [];
  _calStep = 0;
}

function _calRenderIntro() {
  document.getElementById("feed-cal-body").innerHTML = `
    <p class="form-hint">${t("cal.intro1", {n: _CAL_MEASUREMENTS_NEEDED})}</p>
    <p class="form-hint">${t("cal.intro2")}</p>
    <button class="btn-primary" id="cal-start-btn" style="width:100%;margin-top:8px">${t("cal.start")}</button>
  `;
  document.getElementById("cal-start-btn").onclick = () => _calStartRound();
}

async function _calStartRound() {
  _calStep++;
  document.getElementById("feed-cal-body").innerHTML = `
    <p class="form-hint">${t("cal.step_dispensing", {step: _calStep + 1, total: _CAL_MEASUREMENTS_NEEDED})}</p>
    <button class="btn-secondary" id="cal-dispense-btn" style="width:100%" disabled>${t("cal.dispensing")}</button>
  `;
  try {
    await api("POST", `/api/devices/${_calDevice.serial}/command`, {
      cmd: "MANUAL_FEEDING_SERVICE",
      grainNum: 1,
    });
  } catch (e) {
    // fall through to the weigh step regardless -- the user can still weigh
    // whatever came out, and retry the whole wizard if the feed didn't fire
  }
  _calRenderWeighStep();
}

function _calRenderWeighStep() {
  document.getElementById("feed-cal-body").innerHTML = `
    <p class="form-hint">${t("cal.step_weigh", {step: _calStep + 1, total: _CAL_MEASUREMENTS_NEEDED})}</p>
    <div class="form-row">
      <label class="form-label">${t("cal.weight")}</label>
      <div style="display:flex;gap:8px">
        <input class="form-input" type="number" id="cal-weight-input" min="0.01" step="0.01" style="flex:1">
        <select class="form-input" id="cal-unit-select" style="width:auto">
          <option value="g"${_calUnit === "g" ? " selected" : ""}>${t("cal.grams")}</option>
          <option value="oz"${_calUnit === "oz" ? " selected" : ""}>${t("cal.ounces")}</option>
        </select>
      </div>
      <p class="form-hint" style="margin-top:6px">${t("cal.unit_hint")}</p>
    </div>
    <button class="btn-primary" id="cal-weight-next-btn" style="width:100%;margin-top:8px">
      ${_calStep + 1 < _CAL_MEASUREMENTS_NEEDED ? t("cal.next") : t("cal.finish")}
    </button>
  `;
  const btn = document.getElementById("cal-weight-next-btn");
  const input = document.getElementById("cal-weight-input");
  document.getElementById("cal-unit-select").onchange = e => { _calUnit = e.target.value; };
  btn.onclick = () => {
    const reading = parseFloat(input.value);
    if (!reading || reading <= 0) {
      input.style.borderColor = "var(--pl-danger)";
      return;
    }
    const grams = Math.round((_calUnit === "oz" ? reading * _CAL_GRAMS_PER_OZ : reading) * 10) / 10;
    _calMeasurements.push(grams);
    if (_calMeasurements.length < _CAL_MEASUREMENTS_NEEDED) {
      _calStartRound();
    } else {
      _calRenderSummary();
    }
  };
}

function _calRenderSummary() {
  const avg = _calMeasurements.reduce((a, b) => a + b, 0) / _calMeasurements.length;
  const avgRounded = Math.round(avg * 10) / 10;
  document.getElementById("feed-cal-body").innerHTML = `
    <p class="form-hint">${t("cal.measurements", {list: _calMeasurements.map(m => `${m}g`).join(", ")})}</p>
    <p class="form-hint">${t("cal.average", {grams: avgRounded, oz: (avgRounded / _CAL_GRAMS_PER_OZ).toFixed(2)})}</p>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn-secondary" id="cal-redo-btn" style="flex:1">${t("cal.redo")}</button>
      <button class="btn-primary" id="cal-save-btn" style="flex:1">${t("cal.save")}</button>
    </div>
  `;
  document.getElementById("cal-redo-btn").onclick = () => {
    _calMeasurements = [];
    _calStep = -1;
    _calStartRound();
  };
  document.getElementById("cal-save-btn").onclick = async () => {
    await api("POST", `/api/devices/${_calDevice.serial}`, { calibrated_grams_per_portion: avgRounded });
    _patchDevice(_calDevice.serial, { calibrated_grams_per_portion: avgRounded });
    closeFeedCalibration();
    renderDeviceTab("schedule");
  };
}

document.addEventListener("DOMContentLoaded", () => {
  const overlay = document.getElementById("feed-cal-overlay");
  if (overlay) {
    overlay.addEventListener("click", e => {
      if (e.target === overlay) closeFeedCalibration();
    });
  }
});
