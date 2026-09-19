// ── Pets ──────────────────────────────────────────────────────────────────
function renderPets() {
  const key = JSON.stringify(_pets.map(p => ({ id: p.id, name: p.name, breed: p.breed, w: p.weight_kg, img: p.image_url })));
  if (key === _lastPetRenderKey) return;
  _lastPetRenderKey = key;
  populateSortSelect();

  const list = document.getElementById("pet-list");
  if (!_pets.length) {
    list.innerHTML = `<div class="empty-state">
      <div class="empty-icon">🐾</div>
      <div class="empty-msg">${t("pet.none")}</div>
    </div>`;
    return;
  }
  list.innerHTML = _pets.map(p => `<div class="pet-card" data-pet-id="${escHtml(p.id)}" style="position:relative">
    <div class="pet-avatar">${p.image_url ? `<img src="${escHtml(p.image_url)}" alt="">` : "🐾"}</div>
    <div class="pet-info">
      <div class="pet-name">${escHtml(p.name || "Unnamed pet")}</div>
      <div class="pet-sub">${escHtml(p.breed || "")}${p.weight_kg ? ` · ${useImperial() ? (p.weight_kg * 2.20462).toFixed(1) + " lbs" : p.weight_kg + " kg"}` : ""}</div>
    </div>
    <button class="btn-pet-delete-card" data-pet-id="${escHtml(p.id)}" title="Delete pet"
      style="position:absolute;top:50%;right:10px;transform:translateY(-50%);background:none;border:none;cursor:pointer;font-size:20px;line-height:1;padding:2px;opacity:0.5"
      onmouseenter="this.style.opacity='1'"
      onmouseleave="this.style.opacity='0.5'">🗑️</button>
  </div>`).join("");

  list.querySelectorAll(".pet-card").forEach(card => {
    card.addEventListener("click", (e) => {
      if (e.target.closest(".btn-pet-delete-card")) return;
      const pet = _pets.find(p => p.id === card.dataset.petId);
      if (pet) openPetModal(pet);
    });
  });

  list.querySelectorAll(".btn-pet-delete-card").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const pet = _pets.find(p => p.id === btn.dataset.petId);
      const name = pet?.name || "this pet";
      if (!confirm(`Delete ${name}?`)) return;
      await api("DELETE", `/api/pets/${btn.dataset.petId}`);
      await refresh();
    });
  });
}

let _pendingPetPhoto = null;  // Blob set after crop, uploaded on save

function openPetModal(pet) {
  _editPetId = pet ? pet.id : null;
  _pendingPetPhoto = null;
  document.getElementById("pet-modal-title").textContent = pet ? pet.name : t("pet.add");
  document.getElementById("p-name").value = pet ? (pet.name || "") : "";
  document.getElementById("p-breed").value = pet ? (pet.breed || "") : "";
  document.getElementById("p-photo-file").value = "";

  // Weight: store in kg, display in lbs if imperial
  const imperial = useImperial();
  const weightLabel = document.getElementById("p-weight-label");
  const weightInput = document.getElementById("p-weight");
  if (imperial) {
    weightLabel.textContent = t("weight.lbs");
    weightInput.placeholder = "10.0";
    const rawKg = pet ? (pet.weight_kg || null) : null;
    weightInput.value = rawKg != null ? (rawKg * 2.20462).toFixed(1) : "";
  } else {
    weightLabel.textContent = t("weight.kg");
    weightInput.placeholder = "4.5";
    weightInput.value = pet ? (pet.weight_kg || "") : "";
  }

  // Pet photo — image_url is already a full path (includes ingress prefix), use it directly
  const preview = document.getElementById("pet-photo-preview");
  const placeholder = document.getElementById("pet-photo-placeholder");
  if (pet?.image_url) {
    preview.src = pet.image_url;
    preview.style.display = "";
    placeholder.style.display = "none";
  } else {
    preview.src = "";
    preview.style.display = "none";
    placeholder.style.display = "";
  }

  // Device assignment checkboxes
  const deviceSection = document.getElementById("p-device-section");
  const deviceList = document.getElementById("p-device-list");
  const assignedSerials = new Set(pet?.device_serials || []);
  // Also collect serials from device.pet_ids for this pet
  if (pet) {
    _devices.forEach(d => { if (d.pet_ids?.includes(pet.id)) assignedSerials.add(d.serial); });
  }
  if (_devices.length > 0) {
    deviceSection.style.display = "";
    deviceList.innerHTML = _devices.map(d => `
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px">
        <input type="checkbox" class="p-device-cb" value="${escHtml(d.serial)}"
          ${assignedSerials.has(d.serial) ? "checked" : ""}
          style="width:16px;height:16px;accent-color:var(--pl-accent)">
        <span>${escHtml(d.name || d.device_type || d.serial)}</span>
        ${d.room ? `<span style="font-size:12px;color:var(--pl-subtext)">(${escHtml(d.room)})</span>` : ""}
      </label>
    `).join("");
  } else {
    deviceSection.style.display = "none";
  }

  // Notification toggles
  document.getElementById("p-notify-eating-activity").checked = pet ? (pet.notify_eating_activity ?? true) : true;
  document.getElementById("p-notify-bell").checked   = pet ? (pet.notify_bell   ?? true)  : true;
  document.getElementById("p-notify-email").checked  = pet ? (pet.notify_email  ?? true)  : true;
  document.getElementById("p-notify-mobile").checked = pet ? (pet.notify_mobile ?? false) : false;

  // Hasn't eaten alert
  document.getElementById("p-no-eat-enabled").checked = pet ? (pet.no_eat_alert_enabled ?? false) : false;
  document.getElementById("p-no-eat-hours").value     = pet ? (pet.no_eat_alert_hours   ?? 12)    : 12;

  // RFID tag -- show section only if pet already has a tag or is assigned to an RFID feeder
  const rfidInput   = document.getElementById("p-rfid-tag");
  const rfidEye     = document.getElementById("btn-rfid-eye");
  rfidInput.value = pet?.rfid_tag || "";
  rfidInput.type  = "password";
  if (rfidEye) {
    rfidEye.onclick = () => {
      rfidInput.type = rfidInput.type === "password" ? "text" : "password";
    };
  }

  // Activity log
  const actSection = document.getElementById("p-activity-section");
  const actLog     = document.getElementById("p-activity-log");
  if (pet) {
    actSection.style.display = "";
    actLog.innerHTML = `<div style="color:var(--pl-subtext);font-size:12px">Loading...</div>`;
    api("GET", `/api/pets/${pet.id}/log?limit=30`).then(events => {
      if (!events.length) {
        actLog.innerHTML = `<div style="color:var(--pl-subtext);font-size:12px">No activity recorded yet.</div>`;
        return;
      }
      actLog.innerHTML = events.map(e => {
        const time = escHtml(fmtTime(e.ts));
        const dev  = e.device_name ? `<span style="color:var(--pl-subtext)"> · ${escHtml(e.device_name)}</span>` : "";
        let desc;
        if (e.type === "pet_eating") {
          desc = e.duration_secs ? `Ate for ${escHtml(fmtDuration(e.duration_secs))}` : "Ate";
        } else if (e.type === "pet_drinking") {
          desc = e.duration_secs ? `Drank for ${escHtml(fmtDuration(e.duration_secs))}` : "Drank";
        } else if (e.type === "pet_litter") {
          desc = e.duration_secs ? `Used litter box (${escHtml(fmtDuration(e.duration_secs))})` : "Used litter box";
        } else {
          desc = escHtml(e.type);
        }
        return `<div style="display:flex;gap:8px;align-items:baseline">
          <span style="color:var(--pl-subtext);white-space:nowrap;font-size:12px">${time}</span>
          <span>${desc}${dev}</span>
        </div>`;
      }).join("");
    }).catch(() => {
      actLog.innerHTML = `<div style="color:var(--pl-subtext);font-size:12px">Could not load activity.</div>`;
    });
  } else {
    actSection.style.display = "none";
  }

  document.getElementById("modal-pet").classList.add("open");
}

async function savePet() {
  const imperial = useImperial();
  const rawWeight = parseFloat(document.getElementById("p-weight").value);
  const weight_kg = isNaN(rawWeight) ? null : (imperial ? rawWeight / 2.20462 : rawWeight);

  const rfidVal = (document.getElementById("p-rfid-tag")?.value || "").trim();
  const body = {
    name:          document.getElementById("p-name").value.trim(),
    breed:         document.getElementById("p-breed").value.trim(),
    weight_kg:     weight_kg != null ? parseFloat(weight_kg.toFixed(3)) : null,
    rfid_tag:      rfidVal || null,
    notify_eating_activity: document.getElementById("p-notify-eating-activity").checked,
    notify_bell:          document.getElementById("p-notify-bell").checked,
    notify_email:         document.getElementById("p-notify-email").checked,
    notify_mobile:        document.getElementById("p-notify-mobile").checked,
    no_eat_alert_enabled: document.getElementById("p-no-eat-enabled").checked,
    no_eat_alert_hours:   parseInt(document.getElementById("p-no-eat-hours").value) || 12,
  };
  try {
    let savedPet;
    if (_editPetId) {
      savedPet = await api("POST", `/api/pets/${_editPetId}`, body);
    } else {
      savedPet = await api("POST", "/api/pets", body);
    }
    const petId = savedPet?.id || _editPetId;

    // Upload photo if one was cropped
    if (_pendingPetPhoto && petId) {
      try {
        const formData = new FormData();
        formData.append("image", _pendingPetPhoto, "pet.png");
        const r = await fetch(`${BASE}/api/pets/${petId}/image`, { method: "POST", body: formData });
        if (!r.ok) console.warn("Pet photo upload failed:", r.status);
      } catch(e) { console.warn("Pet photo upload error:", e); }
    }

    // Sync device assignment: update pet_ids on each device
    const checkedSerials = new Set(
      Array.from(document.querySelectorAll(".p-device-cb:checked")).map(cb => cb.value)
    );
    const allSerials = new Set(
      Array.from(document.querySelectorAll(".p-device-cb")).map(cb => cb.value)
    );
    for (const serial of allSerials) {
      const dev = _devices.find(d => d.serial === serial);
      if (!dev) continue;
      const currentIds = new Set(dev.pet_ids || []);
      const shouldHave = checkedSerials.has(serial);
      const doesHave = petId && currentIds.has(petId);
      if (shouldHave && !doesHave) {
        const newIds = [...currentIds, petId];
        await api("POST", `/api/devices/${serial}`, { pet_ids: newIds });
      } else if (!shouldHave && doesHave) {
        const newIds = [...currentIds].filter(id => id !== petId);
        await api("POST", `/api/devices/${serial}`, { pet_ids: newIds });
      }
    }

    document.getElementById("modal-pet").classList.remove("open");
    _pendingPetPhoto = null;
    await refresh();
  } catch(e) { alert(t("pet.save_failed", {error: e.message})); }
}

async function deletePet() {
  if (!confirm(t("pet.delete_confirm"))) return;
  await api("DELETE", `/api/pets/${_editPetId}`);
  document.getElementById("modal-pet").classList.remove("open");
  await refresh();
}
