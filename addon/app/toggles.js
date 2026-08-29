// ── Toggle controls ───────────────────────────────────────────────────────
async function togglePump(device) {
  const newVal = !device.waterStopSwitch ? 1 : 0;
  try {
    await api("POST", `/api/devices/${device.serial}/command`, { waterStopSwitch: newVal });
    _patchDevice(device.serial, { waterStopSwitch: newVal });
    renderDeviceTab(_currentDeviceTab);
  } catch(e) { alert(t("overview.cmd_failed", {error: e.message})); }
}

async function toggleLight(device) {
  const newVal = device.lightSwitch ? 0 : 1;
  try {
    await api("POST", `/api/devices/${device.serial}/command`, { lightSwitch: newVal });
    _patchDevice(device.serial, { lightSwitch: newVal });
    renderDeviceTab(_currentDeviceTab);
  } catch(e) { alert(t("overview.cmd_failed", {error: e.message})); }
}

async function toggleFilterLed(device) {
  const newVal = device.filterLedSwitch ? 0 : 1;
  try {
    await api("POST", `/api/devices/${device.serial}/command`, { filterLedSwitch: newVal });
    _patchDevice(device.serial, { filterLedSwitch: newVal });
    renderDeviceTab(_currentDeviceTab);
  } catch(e) { alert(t("overview.cmd_failed", {error: e.message})); }
}

async function deleteDevice(serial) {
  if (!confirm(t("device_modal.delete_confirm"))) return;
  const device = _devices.find(d => d.serial === serial);
  let removeCreds = false;
  if (device?.mqtt_user) {
    removeCreds = confirm(t("device_modal.delete_creds_confirm"));
  }
  const url = removeCreds ? `/api/devices/${serial}?remove_creds=1` : `/api/devices/${serial}`;
  await api("DELETE", url);
  document.getElementById("modal-device").classList.remove("open");
  await refresh();
}
