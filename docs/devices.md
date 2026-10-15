# Supported device notes

Back to the [README](../README.md). The table of supported devices is there; these are the details behind it.

**Granary Smart Feeder:** No RFID and no lid on this model (confirmed via a real MQTT capture, issue #8), so pet-attributed feeding and the "Open Lid"/"Food Door" features don't apply here, only the One RFID Smart Feeder has those. Has an indicator light and a "Buttons Lock" (physical button lock) the One RFID feeder doesn't.

## Polar Wet Food Feeder (early beta)

I do not own a Polar, so none of this has been run on real hardware by me. It is built from three public sources: a capture of the real Petlibro cloud talking to a Polar (the `rgnv/haos-petlibro-local` repository, firmware 2.0.32), the `gianlucariccobono/petlibro-local` fork, which reports testing on a real feeder, and tests one Polar owner ran on their own feeder. Please treat it as an experiment, stay near the feeder the first few times, and keep the Stop button handy.

**What it does:** serve plate 1, 2 or 3, stop a feed, open or close the lid, move the plate, and show status, last fed, plate position and the feeder's inside temperature. These are in the device's Overview tab, and as buttons, a select and a number in Home Assistant (see [Home Assistant entities](entities.md)). Feeds are logged in the Log tab.

**What it deliberately does not do:**
- **Feeding plans.** The feeder keeps and runs its own plans. This app neither reads nor writes them, so plans set up in the Petlibro app are left alone. I have not tested whether a Polar that no longer reaches the Petlibro cloud keeps running them. Writing plans is not built because the sources disagree on how it should work and I cannot test it; a capture of the Petlibro app creating, editing and deleting a schedule would settle that (see the note at the top of the [README](../README.md)).
- **Battery and power alerts.** The only values seen so far do not match what the One RFID Feeder's confirmed battery and AC values would say, so I did not guess.
- **Clock correction.** One report has a Polar's clock about 20 minutes slow. The app shows the offset but never changes the clock, because a jump could skip a plan the feeder runs by itself and this app cannot see those plans.
- Cleaning reminders and other maintenance, until someone is using it for real.

**Safety rules built in:**
- A plate number outside 1 to 3, including 0, is refused before anything is sent. On one owner's feeder, a plate outside that range made the plate motor run on and on.
- Only one feed at a time, and plate moves are spaced out and blocked during a feed.
- Commands are never retained and never queued, so a command sent while the feeder is offline is dropped, not replayed later.
- Commands are refused when the feeder is offline.

**The open question, the door time:** the feed command carries a number for how long the door stays open, and the sources disagree on whether it is seconds or minutes (both quote 240 as the Petlibro cloud's own value). Until you choose a unit for your feeder in the Overview tab, Serve sends a test value of 1, which is harmless in either unit. The app times how long the door actually stays open and shows it as "Last feed", so a test feed tells you which unit it is: about 60 seconds means minutes, a few seconds means seconds. It suggests a unit but never applies one by itself. Once you set a unit, the door time is capped at 600 seconds or 240 minutes.

**Other things I could not confirm:** the serial prefix `AF05` comes from one owner's serial, the stop, lid and plate-position commands come from the fork's report and not from a capture of the real cloud, and the first connection and the feed steps have not been seen by me. If something does not work, please [open an issue](https://github.com/smcneece/petlibro-local/issues) with the add-on log (Settings, Help/About) from around the time you tried it.

Thanks to the owners of the repositories above, whose published work made this possible.

**WF01 units:** Some Dockstream 2 boxes/units are labeled "PLWF105" rather than "PLWF106". A real MQTT capture (issue #8) confirmed a `WF01`-prefixed unit reports `PLWF106` over the wire regardless, so it's treated identically to `WF03`.

**Color variant encoding:** On the Dockstream 2, I think the serial number encodes the color variant. The characters at positions 10-11 appear to indicate color: `BD` = black, `BA` = white. If other owners of the Dockstream 2 fountains could also let me know by opening an issue.

**Dockstream RFID Smart Fountain:** A user-submitted `WF02` serial was initially assumed to be a plain Dockstream 2 (same prefix as an earlier report), but a real MQTT capture later showed it actually reports MQTT model `PLWF305`, a distinct, RFID-capable fountain. It reports which pet's tag was present during a drink (`WEIGHT_CHANGE_EVENT`), logged and attributed to that pet's activity the same way the One RFID feeder attributes eating sessions. Everything else (water level, filter, pump, light) works the same as the Dockstream 2. Serial prefix `WF02` now maps to this device type instead of the plain Dockstream 2, correcting the earlier assumption. Only a white variant is confirmed so far. If your `WF02` fountain doesn't have RFID tag support, or you have a black one, please open an issue so we can sort that out.

**Cordless fountain:** Feature-identical to the wired Dockstream 2 with the addition of battery level, charge state (Charging / Charged / Discharging), and battery percentage. MQTT model `PLWF116` is taken from the cloud HA integration. The serial prefix `WF04` and color encoding (`BD`/`BA`) are inferred by analogy with the wired model, not confirmed from a real device. If you own one, please open an issue with your serial number so we can confirm or correct these.

**One RFID Smart Feeder:** Significantly different from the fountain: RFID door, desiccant tray, feeding plan, display matrix, sound, and lid controls. Serial prefix confirmed as `AF06`, MQTT model confirmed as `PLAF301`. Color variant does not appear to be encoded at positions 10-11 the way fountain serials are. Note that Petlibro's product listing shows this device as model `PLAF103`; that's the retail box/product number and is unrelated to `PLAF301`, which is the MQTT topic model this app matches on. Serials on newer hardware revisions run a few characters longer than early units but use the same `AF06` prefix and work the same way.

## Firmware versions I have seen

| Device | Firmware | Hardware |
|--------|----------|----------|
| One RFID Smart Feeder | 3.0.72 | 5.0.0 |
| One RFID Smart Feeder | 2.0.47 | 4.0.0 |
| Dockstream 2 Smart Fountain | 0.0.34 | 1.0.0 |

I don't have a firmware version for the Granary, the cordless fountain, or the RFID fountain. If yours shows a different one (it's at the top of the device's modal), please [open an issue](https://github.com/smcneece/petlibro-local/issues) and tell me, since newer firmware may behave differently.

## Adding a new device type

Additional devices can be added by contributing a device type entry, the MQTT model string (the topic prefix after `dl/`), and the serial number prefix used for auto-detection during capture. The MQTT topic structure is consistent across the Petlibro product line.
