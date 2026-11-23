# Supported device notes

Back to the [README](../README.md). The table of supported devices is there; these are the details behind it.

**Granary Smart Feeder:** No RFID and no lid on this model (confirmed via a real MQTT capture, issue #8), so pet-attributed feeding and the "Open Lid"/"Food Door" features don't apply here, only the One RFID Smart Feeder has those. Has an indicator light and a "Buttons Lock" (physical button lock) the One RFID feeder doesn't. With backup batteries fitted it reports power the same way as the One RFID (an owner's unplug tests, issue #23): `powerType` 2 while on battery, 3 on mains, and 1 when no batteries are detected.

## Polar Wet Food Feeder (early beta)

I do not own a Polar, so none of this has been run on real hardware by me. It is built from three public sources: a capture of the real Petlibro cloud talking to a Polar (the `rgnv/haos-petlibro-local` repository, firmware 2.0.32), the `gianlucariccobono/petlibro-local` fork, which reports testing on a real feeder, and tests one Polar owner ran on their own feeder. Please treat it as an experiment, stay near the feeder the first few times, and keep the Stop button handy.

**What it does:** serve plate 1, 2 or 3, stop a feed, open or close the lid, move the plate, and show status, last fed, plate position and the feeder's inside temperature. These are in the device's Overview tab, and as buttons, a select and a number in Home Assistant (see [Home Assistant entities](entities.md)). Feeds are logged in the Log tab.

**What it deliberately does not do:**
- **Feeding plans.** The feeder keeps and runs its own plans. This app neither reads nor writes them, so plans set up in the Petlibro app are left alone. A capture of the Petlibro app from a Polar owner shows how plans work: each plan is one dated occurrence (a day, a time and a bowl), there are at most three, one per bowl, and only one schedule is active at a time. Every change in the app clears the feeder's list and sends the new one. The app sends nothing for "Automatically Repeat Schedule", so my reading is that whatever plays the cloud's part sends the next days' plans. Petlibro Local replaces the cloud, so for repeating schedules it would have to do the same. Until it does, a Polar pointed at this app has nobody sending it new plans, so plans already stored would run once and then run out. I have not seen that happen, so treat it as my reading of the capture and not a result. Writing plans from this app is not built; the one thing still missing is what the feeder reports when a scheduled plan runs (see the note at the top of the [README](../README.md)).
- **Battery and power alerts.** The only values seen so far do not match what the One RFID Feeder's confirmed battery and AC values would say, so I did not guess.
- **Clock correction.** One report has a Polar's clock about 20 minutes slow. The app shows the offset but never changes the clock, because a jump could skip a plan the feeder runs by itself and this app cannot see those plans.
- Cleaning reminders and other maintenance, until someone is using it for real.

**Safety rules built in:**
- A plate number outside 1 to 3, including 0, is refused before anything is sent. On one owner's feeder, a plate outside that range made the plate motor run on and on.
- Only one feed at a time, and plate moves are spaced out and blocked during a feed.
- Commands are never retained and never queued, so a command sent while the feeder is offline is dropped, not replayed later.
- Commands are refused when the feeder is offline.

**The open question, the door time:** the feed command carries a number for how long the door stays open, and the sources disagree on whether it is seconds or minutes (both quote 240 as the Petlibro cloud's own value). Until you choose a unit for your feeder in the Overview tab, Serve sends a test value of 1, which is harmless in either unit. The app times how long the door actually stays open and shows it as "Last feed", so a test feed tells you which unit it is: about 60 seconds means minutes, a few seconds means seconds. It suggests a unit but never applies one by itself. Once you set a unit, the door time is capped at 600 seconds or 240 minutes.

**Other things I could not confirm:** the serial prefix `AF05` is from one owner's feeder (it shows in that capture's topics). Stop and plate position are now seen in a capture of the real Petlibro cloud, but opening and closing the lid with `SWITCH_DOOR_SERVICE` is still only the fork's report: the Petlibro app's own Close Now sends the Stop command, and I did not see it send `SWITCH_DOOR_SERVICE` at all, so treat Open Lid and Close Lid as unverified. On the door time, the app's 4 hour limit per bowl and its default of 240 hint at minutes, but that is a guess and not a result. If something does not work, please [open an issue](https://github.com/smcneece/petlibro-local/issues) with the add-on log (Settings, Help/About) from around the time you tried it.

Thanks to the owners of the repositories above, whose published work made this possible.

**Granary Smart Camera Feeder (PLAF203, serial starting AF03):** Basic support only, from one owner's capture. It talks on MQTT model `PLAF203` and otherwise behaves like the Granary: Feed Now, feeding plans, Last Fed and the clock correction use the same messages, but I have not tested Feed Now or the Light and Buttons Lock switches on one. The camera is not supported, so there is no video. Its many camera settings are stored but not shown, and its camera access token is never stored. Its power readings are inconsistent (three different values in one boot), so no power alerts are built. If you own one, please tell me what works and what doesn't.

## Air Smart Feeder (PLAF108, beta)

Built from one owner's capture of the real PetLibro cloud talking to a feeder (firmware 2.0.14, hardware 1.0.0), and run on that same feeder against Petlibro Local with its own broker. Serial prefix `AF04`; the MQTT model, in the topics and in `DEVICE_START_EVENT`, is `PLAF108`. It connects with plain MQTT on port 1883 like the other feeders, so the DNS override works for it. It is added as a Granary device (the app shows the Granary type, with the model name PLAF108 learned from the feeder's own messages).

**What works:**
- **Feed Now** with a number of portions, and the feeder's progress (`GRAIN_START`, `GRAIN_END`), which feeds Last Fed and the log. Manual feeds sent from Petlibro Local have run on the real feeder for several days without problems; the command and the progress handling were also checked against a simulated feeder (`tools/replay_capture.py`).
- **Feeding plans:** the same messages as the Granary. The app replaces the feeder's whole list, a disabled plan is simply not sent, and plan times are UTC on the wire. Scheduled meals have run on the real feeder for several days without problems.
- **Light:** on and off, with the state shown back, checked on the real feeder from Petlibro Local. The feeder acknowledges and reports `lightSwitch` and `enableLight`.
- **State:** food present, battery percentage, Wi-Fi signal, firmware and hardware versions, and the clock (the app answers its time requests, like for every model).

**How it differs from the Granary:**
- The light command has the shape the fountains use (`lightSwitch`, `lightAgingType`, the start and end times and their `Utc` twins, `filterLedSwitch`), not the Granary's group of settings. The default hours the cloud sent were 08:00 to 20:00 local.
- There is no volume and no Buttons Lock. The PetLibro app never sent those to this feeder, so they are hidden in the app and in Home Assistant, and commands for them are refused.
- No power alerts. At boot it reports `powerType` 2 with `batteryState` `USING` and 100 %, which could be battery or mains; I could not tell, so I did not guess.
- Black and white pictures are bundled for it (`plaf108_b.png`, `plaf108_w.png`); pick the color on the device's edit screen.

**Not built:** the cloud's public API lists more fields for this model (vacuum and pump state, sleep mode, grain outlet blocked, desiccant days remaining, cover close speed), but this firmware and the PetLibro app version the owner used never showed them, so nothing is guessed. The portions limit stays at the Granary's 12 (the cloud API suggests the Air takes up to 16).

**Before you move a feeder from the cloud to your own server:** when it first asks for its feeding plans, the app answers with its own list. If that is empty the feeder deletes the plans it had, so note them down (times in the PetLibro app are local, the feeder stores UTC) and add them in the Schedule tab right after adding the feeder. If something does not work, please [open an issue](https://github.com/smcneece/petlibro-local/issues) with the log from around the time you tried it.

**WF01 units:** Some Dockstream 2 boxes/units are labeled "PLWF105" rather than "PLWF106". A real MQTT capture (issue #8) confirmed a `WF01`-prefixed unit reports `PLWF106` over the wire regardless, so it's treated identically to `WF03`.

**Color variant encoding:** On the Dockstream 2, I think the serial number encodes the color variant. The characters at positions 10-11 appear to indicate color: `BD` = black, `BA` = white. If other owners of the Dockstream 2 fountains could also let me know by opening an issue.

**Dockstream RFID Smart Fountain:** A user-submitted `WF02` serial was initially assumed to be a plain Dockstream 2 (same prefix as an earlier report), but a real MQTT capture later showed it actually reports MQTT model `PLWF305`, a distinct, RFID-capable fountain. It reports which pet's tag was present during a drink (`WEIGHT_CHANGE_EVENT`), logged and attributed to that pet's activity the same way the One RFID feeder attributes eating sessions. Everything else (water level, filter, pump, light) works the same as the Dockstream 2. Serial prefix `WF02` now maps to this device type instead of the plain Dockstream 2, correcting the earlier assumption. Only a white variant is confirmed so far. If your `WF02` fountain doesn't have RFID tag support, or you have a black one, please open an issue so we can sort that out.

**Cordless fountain:** Feature-identical to the wired Dockstream 2 with the addition of battery level, charge state (Charging / Charged / Discharging), and battery percentage. MQTT model `PLWF116` and serial prefix `WF04` are confirmed by an owner's capture (issue #16, firmware 0.3.61, hardware 1.0.0). The `BD`/`BA` color code the wired model has at positions 11 and 12 of the serial does not seem to apply: that owner's serial has `2F` there, so pick the color in Edit Device. This fountain reports its scale as `WATER_CAP_EVENT` with a radar instead of RFID tags (see the [MQTT protocol notes](mqtt-protocol.md)).

**One RFID Smart Feeder:** Significantly different from the fountain: RFID door, desiccant tray, feeding plan, display matrix, sound, and lid controls. Serial prefix confirmed as `AF06`, MQTT model confirmed as `PLAF301`. Color variant does not appear to be encoded at positions 10-11 the way fountain serials are. Note that Petlibro's product listing shows this device as model `PLAF103`; that's the retail box/product number and is unrelated to `PLAF301`, which is the MQTT topic model this app matches on. Serials on newer hardware revisions run a few characters longer than early units but use the same `AF06` prefix and work the same way.

## Firmware versions I have seen

| Device | Firmware | Hardware |
|--------|----------|----------|
| One RFID Smart Feeder | 3.0.72 | 5.0.0 |
| One RFID Smart Feeder | 2.0.47 | 4.0.0 |
| Dockstream 2 Smart Fountain | 0.0.34 | 1.0.0 |
| Air Smart Feeder | 2.0.14 | 1.0.0 |

I have seen the cordless fountain on firmware 0.3.61, the RFID fountain on 1.0.16 and the Granary on 1.1.15 (hardware 1.0.0). If yours shows a different one (it's at the top of the device's modal), please [open an issue](https://github.com/smcneece/petlibro-local/issues) and tell me, since newer firmware may behave differently.

## Adding a new device type

Additional devices can be added by contributing a device type entry, the MQTT model string (the topic prefix after `dl/`), and the serial number prefix used for auto-detection during capture. The MQTT topic structure is consistent across the Petlibro product line.
