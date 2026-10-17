# MQTT protocol notes

Back to the [README](../README.md). This page lists the MQTT topics and messages Petlibro devices use, for anyone who wants to talk to a device without Home Assistant (for example from openHAB or Node-RED). It is built from what Petlibro Local itself sends and receives, from captures of the real Petlibro cloud, and from other people's published work. It is not official documentation.

How to read it: **observed** means I saw it in a capture of the real cloud or on a real device. **Inferred** means I read it from the data and have not proven it. **Reported** means another project says so and I have not seen it myself. Where a section is thin, the source file named at the end of it has the exact details.

## Connection

- Plain MQTT on port 1883, MQTT 3.1.1, with a username and password. The client id is the device's serial number, which is how the device is identified.
- Two fountains of the same model use the same username and password (observed). I have not shown whether that holds across owners or other models. Treat the login as weak and keep devices on a trusted network.
- The device connects to `mqtt.us.petlibro.com`. Petlibro Local points that name at your own broker with a DNS override; see [Networking](networking.md).

## Topics

Everything is under `dl/<model>/<serial>/device/`, where `<model>` is the MQTT model, not the model printed on the box:

| Device | Serial prefix | MQTT model |
|--------|--------------|------------|
| Dockstream 2 fountain | WF01, WF03 | PLWF106 |
| Dockstream 2 Cordless | WF04 | PLWF116 |
| Dockstream RFID fountain | WF02 | PLWF305 |
| One RFID feeder | AF06 | PLAF301 |
| Granary Smart Feeder | AF01 | PLAF103 |
| Polar Wet Food Feeder | AF05 | PLAF109 |
| Camera feeder (not supported here) | unknown | PLAF203, reported by other projects |

Each channel has two directions, `post` (the device sends) and `sub` (the cloud, or you, send to the device):

| Channel | Device sends on `.../post` | You send on `.../sub` |
|---------|----------------------------|-----------------------|
| `heart` | `HEARTBEAT` | nothing |
| `event` | events and state pushes | the reply to an event |
| `service` | the reply to a command | commands |
| `ntp` | `NTP` time request | the time answer, `NTP_SYNC` |
| `config`, `system`, `ota` | seen from some models | not used here |

A message is a JSON object. The usual fields are `cmd` (the message type), `ts` (sender's time in milliseconds), `msgId` (a request and its reply carry the same one) and `code` (`0` means accepted). The cloud also adds a `linkId` to what it sends.

## What the server side has to do

A device expects the cloud's part to be played. The minimum, per what Petlibro Local does:

1. **Answer `NTP`.** The device asks for the time on `ntp/post`; answer on `ntp/sub`. See below.
2. **Acknowledge event posts that expect it.** For `DEVICE_START_EVENT` reply on `event/sub` with `{"cmd":"DEVICE_START_EVENT","ts":..,"msgId":"<same>","code":0}`. For `GRAIN_OUTPUT_EVENT` and `WET_GRAIN_OUTPUT_EVENT` do the same and include the `execStep` you received.
3. **Never echo a `service/post` reply.** On the Polar, echoing a command reply made the device run the command again in an endless loop (observed). A good rule is to ignore any message that carries a `linkId`.
4. **Ask for state.** Send `ATTR_GET_SERVICE` on `service/sub` after a device connects and every few minutes; the device answers with its attributes.

The real cloud also echoes `ATTR_PUSH_EVENT` and `GET_FEEDING_PLAN_EVENT` (observed on a Polar). Petlibro Local does not echo `ATTR_PUSH_EVENT` for the feeders and fountains, and they work, so it may not be required there.

## Messages every device uses

**`HEARTBEAT`** (observed): about every 90 to 100 seconds on `heart/post`: `{"cmd":"HEARTBEAT","count":N,"rssi":-48,"wifiType":1,"ts":...}`. The Polar adds `temperature`. `count` restarts after a reboot. The `ts` is the device's own clock, so comparing it with real time shows how far the clock is off.

**`DEVICE_START_EVENT`** (observed): sent on every connect or boot, on `event/post`: `success`, `pid` (the model), `hardwareVersion`, `softwareVersion`, and on some models `restartReason`, `mac` and `uuid`. Acknowledge it as above.

**`NTP`** (observed): the device sends `{"cmd":"NTP","ts":<its clock>}` and the answer on `ntp/sub` is

```json
{"cmd":"NTP","ts":1790000000000,"code":0,"calibrationTag":false,
 "timezone":-6,"timezoneOffsetSeconds":-21600,
 "nextDSTOffsetSeconds":-25200,"nextDSTTransitionTs":1793000000000,
 "secondNextDSTOffsetSeconds":-21600,"secondNextDSTTransitionTs":1805000000000}
```

`timezone` is whole hours, the offsets are seconds, and the two transition fields are the next two daylight saving changes in milliseconds (the real cloud sends the full set). What `calibrationTag` does is not clear to me: the real cloud sends `false` to the Polar, and some One RFID firmware is reported to use it to decide whether to apply the time. A One RFID asks about once a day. A Polar asked twice, 30 minutes apart, in the one capture I have, and stayed within a second of real time while it was answered (inferred).

**`NTP_SYNC`** (observed, One RFID): the cloud can push the same fields without `code` and `calibrationTag`, plus `msgId` and `linkId`, on `ntp/sub`. The device answers on `ntp/post` with `{"cmd":"NTP_SYNC","msgId":"<same>","code":0,"ts":<its new clock>}`.

**`ATTR_GET_SERVICE`** (observed): send `{"cmd":"ATTR_GET_SERVICE","ts":..,"msgId":".."}`. The device answers with its attributes.

**`ATTR_SET_SERVICE`** (observed): send the attributes to change as extra keys, for example `{"cmd":"ATTR_SET_SERVICE","ts":..,"msgId":"..","volume":40}`. The device acks on `service/post` with `code` 0 and reports the new values as `ATTR_PUSH_EVENT` on `event/post`. Some devices only accept a whole group of settings together (the Granary always gets volume, audioUrl, enableAudio, lightSwitch, lightAgingType and disableHardwareButton in one message).

**Times in plans and windows** (observed): plan times are UTC. Sound and light windows carry both the local time and a `...Utc` twin, for example `soundStartTime` and `soundStartTimeUtc`.

## One RFID feeder (PLAF301) and Granary (PLAF103)

Commands, all sent on `service/sub`:

| `cmd` | Fields | Notes |
|-------|--------|-------|
| `MANUAL_FEEDING_SERVICE` | `grainNum` (portions) | Feed now. The device answers on `service/post`, then reports `GRAIN_OUTPUT_EVENT`. |
| `FEEDING_PLAN_SERVICE` | `plans`: list of `{planId, executionTime "HH:MM" UTC, repeatDay [1..7, Monday 1], enableAudio, audioTimes, grainNum, syncTime, skipEndTime?}` | Replaces the whole list. An empty list removes all plans. `skipEndTime` (epoch ms) skips that plan until then. The device acks with each `planId` and `syncTime`. |
| `SWITCH_DOOR_SERVICE` | `barnDoorState` true/false | Open or close the lid (One RFID only). |
| `ATTR_SET_SERVICE` | `volume`, `soundSwitch`, `childLockSwitch`, `coverOpenMode`, `coverCloseSpeed`, `closeDoorTimeSec`, `audioUrl`, `enableAudio` | One RFID settings. `audioUrl` is a plain HTTP address the feeder downloads a custom sound from. |
| `ATTR_SET_SERVICE` | `soundSwitch`, `soundAgingType` (1 all day, 2 custom), `soundStartTime`, `soundEndTime` and the `Utc` twins, `soundTimes` | Sound hours. `soundTimes` is end minus start in minutes and goes negative when the window crosses midnight. |
| `ATTR_SET_SERVICE` | `screenDisplaySwitch`, `screenDisplayAgingType`, `screenDisplayStart/EndTime` and `Utc` twins, `screenDisplayInterval` | LED display on/off and hours (One RFID). A change is sent as the switch off then on. |
| `DISPLAY_MATRIX_SERVICE` | `screenDisplayMatrix` (frames), `screenDisplayNumber`, `screenDisplayId`, `screenDisplayInterval`, `displayScene` (`USER_CUSTOM` or `PET_NAME`), `subNum`, `subIndex`, `loopDisplay` | Scrolling text or animation on the LED matrix. Frames go in chunks of 20, up to 140 in total. |

Events the device sends on `event/post`:

| `cmd` | Fields | Meaning |
|-------|--------|---------|
| `GET_FEEDING_PLAN_EVENT` | | The device asks for its plans. Answer with `GET_FEEDING_PLAN_EVENT`, `code` 0 and `plans`. |
| `GRAIN_OUTPUT_EVENT` | `finished`, `actualGrainNum`, `execStep` | Feeding progress. `finished` with a positive `actualGrainNum` is a completed dispense. |
| `WAREHOUSE_DOOR_EVENT` | `barnDoorState`, `triggerType` | Lid opened or closed. `PET_IDENTIFY` means a pet's tag opened it. |
| `PET_IDENTIFY_EVENT` | `rfid`, `type` NEAR or LEAVE | A pet's tag came into range or left (One RFID). |
| `ERROR_EVENT` | `errorCode` | 2032 is the door being blocked from closing (observed once). Other codes unknown. |
| `ATTR_PUSH_EVENT` | changed attributes | Includes `surplusGrain` (food present), `barnDoorState`, `electricQuantity`, `powerType`, `volume` and the settings above. |

`powerType` is 2 on battery and 3 on AC on the One RFID (confirmed by cutting power). The Granary and Polar only ever showed 1, so do not read it the same way there.

Source: `addon/app/devices.py` (search `send_feeding_plans`, `send_display`, `_handle_message`) and `addon/app/display_matrix.py`.

## Fountains (PLWF106, PLWF116, PLWF305)

Commands on `service/sub`, all `ATTR_SET_SERVICE` unless noted:

| Fields | Meaning |
|--------|---------|
| `waterStopSwitch` | Pump. 0 is on and 1 is off (inverted). |
| `lightSwitch` | Ring light, 1 or 0. |
| `lightAgingType` (1 static, 2 scheduled), `lightingStartTime`, `lightingEndTime`, `lightingStartTimeUtc`, `lightingEndTimeUtc`, `lightingTimes` | Light schedule. To switch it off you must resend the times with `lightAgingType` 1; the flag alone does nothing (observed). |
| `useWaterType`, `useWaterDuration`, `useWaterInterval` | A type of 1 uses the intermittent settings, in minutes. |
| `filterLedSwitch`, `filterNextReplacementTimestamp` (ms) | Filter reminder light and due date. |
| `{"cmd":"WEIGHT_CLR_SERVICE","threshold":550}` | Zero the scale. Replies use `code` values such as 3001, 3003 and 3007; I only have one example of each. |

State the device reports: `currentWeight` (grams of water), `waterStopSwitch`, `lightSwitch`, `filterNextReplacementTimestamp`, `electricQuantity` (cordless only). The RFID fountain also sends `WEIGHT_CHANGE_EVENT` with the pet tags present and the amount of the drop.

Source: `addon/app/devices.py`, search `_handle_fountain_drink` and `waterStopSwitch`.

## Polar Wet Food Feeder (PLAF109), early beta

Details and what is unconfirmed are in the [device notes](devices.md#polar-wet-food-feeder-early-beta). The messages, on `service/sub` unless noted:

- **Feed now:** `WET_GRAIN_FEEDING_PLAN_SERVICE` with one plan `{"planId":666666666,"executionTime":"HH:MM","executionDay":"YYYY-MM-DD","plate":1,"feedingDuration":N,"feedNowState":true}`, times in local time (observed). One owner reports that without `feedNowState` nothing opens.
- **Progress, on `event/post`:** `WET_GRAIN_OUTPUT_EVENT` with `execStep` of `GRAIN_THAW`, `GRAIN_START`, `OPEN_DOOR`, then `CLOSE_DOOR` and `GRAIN_END` with `finished` true (observed). Echo each one.
- **Stop:** `WET_FOOD_FEED_STOP_SERVICE` with `planId` 666666666 (observed).
- **Move the plate:** `SET_PLATE_POS_SERVICE` with `platePosition` 1, 2 or 3 (observed). A plate outside 1 to 3, including 0, makes the motor run on and on, so never send one.
- **Schedules:** a list of dated plans (`planId`, `executionTime`, `executionDay`, `plate`, `feedingDuration`, no `feedNowState`), at most three. To change anything the app sends an empty list first and then the new one (observed).
- **Pet sensor, on `event/post`:** `PET_DETECT_EVENT` with `type` NEAR or LEAVE, and `MACHINE_INFRARED_EVENT` with `irState` true or false (observed).
- **Open question:** whether `feedingDuration` is in seconds or minutes.

## Camera feeders (PLAF203)

I have not seen one, so I can't vouch for any of this. The model is documented by [rgnv/haos-petlibro-local](https://github.com/rgnv/haos-petlibro-local) and [tannerln7/Petlibro-Home-Assistant](https://github.com/tannerln7/Petlibro-Home-Assistant). One detail from rgnv matters if you build your own server: newer firmware (3.1.49 and later) is reported to wait for a reply to its `BINDING` message, sent on `system/post`, before it sends `DEVICE_START_EVENT`. The reply is `{"cmd":"BINDING","msgId":"<same>","code":0}` on `system/sub`. The video is not carried over MQTT; it uses a separate peer-to-peer connection.

## Contributing

If you see a message that is wrong or missing here, please [open an issue](https://github.com/smcneece/petlibro-local/issues) or email github@mega-city.com. Captures are the most useful thing: `tools/mqtt_proxy.py` records everything between a device and the cloud. Delete the username and password lines from a log before sharing it.

Thanks to rgnv, to gianlucariccobono (whose Polar fork reported the stop, lid and plate commands), and to the Polar owners whose captures confirmed them.
