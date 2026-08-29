# Petlibro Local Changelog

For full release notes and details on each version, see the [GitHub Releases page](https://github.com/smcneece/petlibro-local/releases).

## Unreleased

## 2026.08.8
- Fixed the fountain Controls tab kicking you back to Overview after toggling Pump, Light, or the filter indicator light. The action itself always worked, the tab display just didn't know it had moved off Overview
- Device cards now show up to 5 pet photos before overflowing to a "+N" badge, up from 3 with no overflow indicator at all before (anything past 3 just silently didn't show). Also fixed a real bug where mobile was actually showing the full desktop-size avatars instead of the smaller mobile size that was always intended, now properly scaled down. If you've got a full house of cats on one device, curious how this looks for you, 5 felt like a reasonable number but happy to adjust it
- Fixed a fountain's "Days Remaining" fields (filter, cleaning) always showing one less than what you actually typed, and the same bug for a feeder's desiccant, bowl, and housing cleaning countdowns. Typing 21 would immediately show 20, since the countdown was rounded down and a just-saved value is always a hair under a full day by the time it's redisplayed
- Removed the Save Threshold buttons on a fountain's Maintenance tab (Alert Threshold, Minimum Drink Size); those fields save as soon as you change them now. The interval and life-days fields next to the Reset/Record buttons (Filter Life, Clean-Up Interval, Bowl/Housing Cleaning Interval, Desiccant Life) also now save on their own the moment you change them, instead of only taking effect when you click the action button next to them
- Removed the One RFID Smart Feeder's "Save Settings" button on the Overview tab. Lid Mode, Close Speed, Auto-Close Seconds, Sound, Volume, and Child Lock each save the instant you change them now. This also fixes the feeder announcing "sound settings changed" out loud every time you changed something unrelated like Child Lock, since Sound and Volume no longer get resent unless you actually touch them
- Volume and Auto-Close Seconds wait about 2 seconds after your last click before actually sending anything, so rapidly clicking a number field's up/down arrows sends one command with the final value instead of one command per click (which made the feeder repeatedly announce "sound settings changed")
- Updated the `mqtt_proxy.py` capture tool's guidance to use a device's full serial number instead of just the first few characters. If you own more than one device of the same type, they likely share a short prefix, so a partial match could proxy and log traffic, including MQTT credentials, from the wrong one
- Clarified what the fountain's "Filter Indicator" toggle does: it turns off the red status LED, but only has a visible effect while the LED is actually lit for something (filter overdue, etc). Confirmed by hardware testing that it does nothing when nothing is currently wrong, which is expected since there's nothing lit to suppress, added a hint to that effect in the app
- Fountain Controls tab: added a Light Schedule (set the time the light turns on and off each day, alongside the existing plain on/off toggle) and a Calibrate Scale button, matching features available in the official PetLibro app
- Calibrate Scale now reports what actually happened instead of just "Sent": if the water container is still on the scale it tells you to remove it and try again, and confirms once it actually finishes calibrating
- New "Last Drink" sensor on every fountain, so you can see (or automate on) when any pet last drank without building your own template sensor
- Fountains, and any device really, now get an immediate attribute query the moment they come online instead of waiting up to 60 seconds for the next periodic check. Some fields (like a fountain's current water level) don't appear to ride along on a device's own connect-time report at all, only on an actual weight change or a query like this one, so a freshly added or reconnected device could otherwise sit with no reading for a while
- Fixed fountain drinks occasionally getting logged twice for the same event (identical volume, same minute, both fountain types). A weight change that gets reported in more than one step, whether that's a real drink or something like lifting the water container off the fountain, could previously trigger a separate log entry for each step. A second qualifying reading within 15 seconds of one already logged is now treated as the same event and skipped

## 2026.08.7
- Fountain device modal reorganized: Pump, Light, and flow controls moved off the Overview tab onto their own new Controls tab, for every fountain type. On the Dockstream RFID Smart Fountain specifically, the Overview tab now shows a Pet Activity section for any linked pets instead, each with their photo, name, how long and how many times they drank today, and total volume, mirroring the official PetLibro app's per-pet breakdown. Scrolls after the first few pets so a household with several cats doesn't get an oversized tab. Plain Dockstream 2 fountains have no RFID hardware, so they don't show this section
- One RFID Smart Feeder's backup battery low threshold moved from the Maintenance tab to the Alerts tab, right next to the alert it controls, freeing up space under Maintenance. It's now a single number field instead of a separate checkbox plus number field: set it to 0 to turn the alert off, or any other value to both turn it on and set the percentage it fires at
- Removed the accurate power lost/restored notification added in 2026.08.6, sourced from the feeder's internal io device log. Real-world testing showed that log entry can fire for reasons unrelated to an actual power loss, producing false "power lost" alerts. The lost/restored notification still works, and still reports exact times and duration, just now timed from the same powerType signal already used for the AC/battery display, which has held up to direct testing

## 2026.08.6
- Custom feed sounds, confirmed working end to end on real hardware: a new Settings → Audio tab holds one shared sound library used by every feeder, upload a file (any common format, converted automatically) or record one, with playback preview before saving and a play button on each saved sound in the library. From a feeder's Maintenance tab, pick a sound and push it to play on that feeder's scheduled feeds instead of the default chime. One-time "Local Audio Base URL" setting required in the Audio tab. Only plays on actual scheduled feeds, PetLibro's own protocol doesn't support sound on manual Feed Now
- In-browser recording needs HTTPS or `localhost` to access your microphone, a browser restriction most home setups reaching Home Assistant over plain HTTP won't meet. The Home Assistant Companion App works for recording even on local HTTP, that's the easiest option; recording externally (Windows Sound Recorder, Mac Voice Memos) and uploading the file works everywhere regardless
- Fixed a bug where an uploaded sound could play back noticeably sped up on the feeder. Custom sounds are now transcoded to exactly 44100 Hz stereo, matching PetLibro's own audio files, instead of inheriting whatever sample rate the source recording used
- If scheduled feeds have silently stopped firing on your feeder, a physical power cycle of the feeder has resolved this in testing; root cause not fully confirmed but appears tied to a stuck device-side state rather than anything in the app
- New `FEEDING_PLAN_SERVICE` diagnostic logging so schedule push/ack issues are visible in the add-on log instead of silent
- One RFID Smart Feeder backup battery support: the device card, modal header, and Overview tab now show battery charge and AC/battery status together (e.g. "100% AC") instead of one or the other. Exposed to Home Assistant as a Battery sensor and an On AC Power binary sensor. Two new alerts in the feeder's Notifications tab: "Running on battery power (AC lost)" and a configurable "Backup battery low" percentage threshold (Maintenance tab). Note: this feeder appears to drop Wi-Fi shortly after losing AC to save power, so the AC-lost alert is opportunistic (fires when we catch the transition in time) rather than a live guarantee; a real outage will usually also show up as the existing offline alert
- Fixed the AC/battery status persistently showing "Battery" even when genuinely on AC power (and a false "running on battery" notification firing after any add-on restart). Two bugs stacked: the app had the power reading backwards (the feeder rarely if ever reports the value we were treating as "AC"), and a stale reading could get resurrected from disk on every restart. Card, modal, and the Home Assistant sensor now correctly read the feeder's actual AC/battery reporting, and no longer resurrect a stale value after a restart
- New: when power is lost and later restored, you'll get an accurate notification stating when it was actually lost and restored and for how long ("Power lost at 6:54:16 PM, restored at 6:57:49 PM (3m33s)"), sourced from a log the feeder buffers internally and reports once it reconnects. This is more reliable than the feeder's live "just lost power" message, which isn't always sent before it drops Wi-Fi to save the battery
- Alerts that stay true for days until you act on them (cleaning/filter/desiccant/bowl/housing due, low battery, low food, low water) are now capped to firing at most once every 24 hours per device. Previously, restarting the add-on reset its memory of which alerts had already been sent, so anyone restarting the app multiple times in a day (e.g. while testing) could get the same reminder repeatedly
- Fixed a device's Alerts tab Save button silently doing nothing: it shared the same button ID as the global Settings → Notifications page, so the click was being wired to the wrong (offscreen) button. Per-device alert preferences now actually save
- Download Debug Capture now redacts WiFi network name and any local custom audio server URL before download. It still includes your device serial numbers, so please email it rather than posting it publicly
- New device: Dockstream RFID Smart Fountain (MQTT model `PLWF305`, serial prefix `WF02`, white only so far). Same water/filter/pump/light monitoring as the Dockstream 2, plus it reports which pet's RFID tag was present during a drink. That drink is now logged and attributed to the pet, showing up in their Recent Activity the same way the One RFID feeder attributes eating sessions. Correction: `WF02` was previously assumed to be a plain Dockstream 2 (`PLWF106`); a real MQTT capture showed this exact serial actually reports `PLWF305`, so Auto Setup now maps `WF02` to this new RFID-capable device type instead

## 2026.08.5
- Critical fix: an MQTT self-loop introduced by the 2026.08.4 debug capture feature could cause the app to repeatedly receive and reprocess its own outbound acknowledgments (device boot acks, feeding plan responses, grain dispense acks) as if the feeder had sent them, flooding the log and starving real message processing. This could cause scheduled feedings to silently fail to fire. Message handling is now correctly scoped to only the feeder's own outbound telemetry topics
- Feeding schedule list now displays sorted by time of day for readability. Storage order was unaffected, but the plans sent to the feeder over MQTT are now also sorted by time of day, in case the feeder's own scheduler is sensitive to plan order

## 2026.08.4
- Dockstream 2 Smart Fountain: added `WF02` as a recognized serial prefix for Auto Setup, confirmed by a user-submitted device. Same `PLWF106` MQTT model as `WF03` units, just a different hardware/serial revision
- New "Download Debug Capture" button in Help/About: the app now continuously keeps a rolling log of raw dl/ MQTT traffic in the background, including from devices it doesn't recognize yet (marked UNRECOGNIZED in the download). Since these devices only check in occasionally, capturing continuously instead of during a fixed window means intermittent devices still get caught. Makes it much easier to diagnose an unsupported or misbehaving device without walking through a manual `mosquitto_sub` capture

## 2026.08.3
- Hasn't eaten alert: per-pet configurable alert that fires via bell, email, and mobile push if no eating session is detected within a set number of hours. Enabled in the pet profile and auto-dismisses when an eating session is logged
- New Home Assistant sensor: Last Eating Duration (seconds). Updates after every qualifying door session — RFID or manual — so automations can check how long a pet was at the feeder to decide whether to skip or dispense the next meal
- Minimum drink detection threshold is now configurable per fountain in the Maintenance tab. Default is 5g (≈ 1 tsp), down from the previous hardcoded 10g. Lower it for kittens or small sips; raise it if pump turbulence triggers false readings
- Fountain drink log: each detected drinking event (weight drop between 10g and 800g) is now recorded with a timestamp and volume. The Log tab on a fountain device card now shows a timestamped drink activity list instead of the 7-day bar chart. Daily totals are still shown on the device card and in the overview tab
- Next meal time on the feeder card now shows the time only (3:00 AM) without a Tomorrow or weekday prefix
- Storage writes are now atomic: data is written to a temporary file and replaced in place, preventing configuration loss on ungraceful host power loss or reboot
- Clearing all feeding plans now correctly clears the Next Meal sensor in Home Assistant instead of leaving the previous timestamp displayed indefinitely
- Editing or saving feeding plans now immediately pushes the updated Next Meal state to Home Assistant; previously the sensor stayed stale until the next device MQTT message
- Adding, renaming, or deleting a custom display icon now immediately updates the Display Icon select entity options in Home Assistant without requiring an app restart
- Selecting a display icon via a Home Assistant automation or dashboard now correctly saves the icon name, so the app card reflects the active icon
- Active custom display icon name now correctly reported to the Home Assistant select entity after app restart; previously the select would revert to "None" if a user-created icon was active
- Bowl cleaning, manual lid access, and other non-feeding door events no longer update the "Last Fed" timestamp in the app and Home Assistant sensor
- Device alert notifications now use unique IDs per device and alert type, so a second feeder going offline no longer overwrites the first feeder's notification in the HA bell panel
- Pet eating notifications also carry unique IDs, so Finn's notification and Zoey's notification appear as separate bell entries
- Offline alerts auto-dismiss from the HA bell and mobile push when a device comes back online
- Hardcoded English strings in the device detail modal (LED Display, Scrolling Text, door open log entries, notification channel labels) are now routed through the i18n system

## 2026.08.2
- Offline watchdog: devices are now marked offline in Home Assistant and the app if no MQTT message is received within 5 minutes, catching power loss and Wi-Fi drops without requiring a broker restart
- Offline and back-online notifications now fire correctly via email, mobile push, and HA persistent notification when a device goes silent or reconnects
- Notification subject lines now include the alert type so the reason is visible without opening the email
- Alert bell in the app header now opens a dropdown listing all active alerts by device; clicking an alert opens that device's detail modal
- RFID tag detection: confirmed field names from live capture (`rfid` for tag, `type` for NEAR/LEAVE action). Eating sessions are logged with duration and linked to the pet profile. RFID tag number auto-detected on first scan and stored on the pet profile, or entered manually behind an eye toggle
- RFID-only pet meal tracking: pet Recent Activity shows only RFID-confirmed eating sessions. Duration is measured from feeder door open to close (accurate eating time), but only when the door was triggered by an RFID scan. Manual access, bowl cleaning, and scheduled dispenses are excluded. Door-only events appear in the device log as "Door open for Xs"
- 30-second minimum for RFID eating sessions; short tag passes and walk-bys are ignored. Duplicate entries from the same session are deduplicated automatically
- Pet activity notifications: when an RFID eating session is detected, a notification fires via the same channels as device alerts (bell, email, mobile push) — e.g. "Zoey ate for 3m25s at Zoey's Feeder". Per-pet notification toggles stored on the pet profile
- Pet profiles now show a Recent Activity log. Foundation is in place to extend pet timelines to fountains and litter boxes as RFID-capable devices are added
- Delete pet moved from the modal footer to a trash icon on the pet card, preventing accidental deletion confusion with the activity log
- Pet modal Save button is now compact and centered rather than full-width
- Timestamps in activity logs now zero-pad single-digit hours (07:03 AM instead of 7:03 AM) for consistent column alignment
- Pet list on desktop is now centered and max-width constrained instead of stretching full screen width

## 2026.08.1
- Initial public beta
- Local MQTT support for **Dockstream 2** and **Dockstream 2 Cordless** fountains. Completely offline, no PetLibro cloud required
- Local MQTT support for **OneRFID Smart Feeder (PLAF301)**
- Fountain monitoring: real-time water level (grams/ml/oz), filter days remaining, cleaning days remaining, pump on/off switch, light on/off switch, battery level (cordless model)
- Feeder monitoring: food level (OK/Low), lid state, desiccant days remaining, last fed time, next scheduled meal time
- Feeder controls: Feed Now button, Open Door button, volume slider (0 to 100), display text (up to 20 characters), display icon (None, Heart, Dog, Cat, Elk)
- **Custom Icon Editor**: 5 × 12 pixel grid with click-and-drag drawing and live 26-wide display preview. Save up to 12 named icons. Built-in presets include the stock PetLibro icons and the Petlibro Salute easter egg. All sends work fully offline over local MQTT
- Feeding schedule management: view, add, edit, enable/disable, and skip scheduled meals. Times entered in local time and converted to UTC automatically
- Pet profiles: name, photo, breed, weight, linked devices. Pet avatar badges overlay device cards
- Maintenance tracking: filter replacement, fountain cleaning, bowl cleaning, housing cleaning. Configurable intervals with overdue alerts
- Notifications: in-app bell, email (any HA notify service), mobile push. Configurable per device and per alert type
- Home Assistant MQTT Discovery: all devices, sensors, switches, buttons, numbers, selects, and text entities auto-created in HA. Survives restarts and device renames. Availability tracking (online/offline)
- Feeder timezone: auto-detected from browser and HA Supervisor on first install. Named timezone select with DST-aware automatic conversion, no manual UTC offset needed
- Water intake tracking: daily grams logged per fountain, shown on device card and in detail modal
- Dark/light theme following system preference
- Mobile layout: floating action buttons for Add Device and Alerts on narrow screens, responsive card grid
