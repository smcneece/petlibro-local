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
    <p class="form-hint">This will dispense 1 portion, ${_CAL_MEASUREMENTS_NEEDED} times. After each one, weigh what came out on your own kitchen scale and enter the grams. The average becomes this feeder's portion size everywhere amounts are shown.</p>
    <p class="form-hint">Since this feeder dispenses through a fixed-volume scoop rather than a scale, individual dispenses will vary a little, this gives a real average for your specific food instead of a flat guess.</p>
    <button class="btn-primary" id="cal-start-btn" style="width:100%;margin-top:8px">Start</button>
  `;
  document.getElementById("cal-start-btn").onclick = () => _calStartRound();
}

async function _calStartRound() {
  _calStep++;
  document.getElementById("feed-cal-body").innerHTML = `
    <p class="form-hint">Step ${_calStep + 1} of ${_CAL_MEASUREMENTS_NEEDED}: dispensing 1 portion...</p>
    <button class="btn-secondary" id="cal-dispense-btn" style="width:100%" disabled>Dispensing...</button>
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
    <p class="form-hint">Step ${_calStep + 1} of ${_CAL_MEASUREMENTS_NEEDED}: weigh what was just dispensed and enter the reading.</p>
    <div class="form-row">
      <label class="form-label">Weight</label>
      <div style="display:flex;gap:8px">
        <input class="form-input" type="number" id="cal-weight-input" min="0.01" step="0.01" style="flex:1">
        <select class="form-input" id="cal-unit-select" style="width:auto">
          <option value="g"${_calUnit === "g" ? " selected" : ""}>grams</option>
          <option value="oz"${_calUnit === "oz" ? " selected" : ""}>ounces</option>
        </select>
      </div>
      <p class="form-hint" style="margin-top:6px">One portion is only about 10g (0.35 oz), so grams is more accurate if your scale can show it. A scale that only reads to 0.1 oz can be off by a few grams.</p>
    </div>
    <button class="btn-primary" id="cal-weight-next-btn" style="width:100%;margin-top:8px">
      ${_calStep + 1 < _CAL_MEASUREMENTS_NEEDED ? "Next" : "Finish"}
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
    <p class="form-hint">Measurements: ${_calMeasurements.map(m => `${m}g`).join(", ")}</p>
    <p class="form-hint">Average: <strong>${avgRounded}g (${(avgRounded / _CAL_GRAMS_PER_OZ).toFixed(2)} oz) per portion</strong></p>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn-secondary" id="cal-redo-btn" style="flex:1">Redo</button>
      <button class="btn-primary" id="cal-save-btn" style="flex:1">Save</button>
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
