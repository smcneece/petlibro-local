# Features

Back to the [README](../README.md).

- [Fountains](#fountains)
- [Feeders](#feeders): [monitoring](#feeder-monitoring), [schedules](#feeding-schedules), [sound](#sound), [LED display](#led-display-one-rfid-feeder), [food amount unit](#food-amount-unit), [calibration](#feed-amount-calibration-optional)
- [Device images](#device-images)
- [Pets](#pets)
- [Notifications](#notifications)
- [Settings](#settings)

## Fountains

### Monitoring
- Real-time water level shown in fl oz or ml based on your unit preference
- Filter replacement countdown computed from the device's own timestamp
- WiFi signal strength (RSSI) shown next to the online status badge, color-coded: green above -65 dBm, yellow above -80 dBm, red below that
- Online/offline status with automatic detection
- Drink log: each detected drink is recorded with a timestamp and volume, viewable as a list in the device's Log tab. Daily totals still show on the card and in the overview
- The weight drop used to detect a drink is configurable per fountain in the Maintenance tab, defaulting to 5 grams (about a teaspoon)

### Controls
- Pump on/off toggle
- Light on/off toggle, and a light schedule (start and end time, with its own on/off switch)
- Filter indicator light toggle
- All controls send directly to the device over local MQTT using the standard Petlibro service protocol

## Feeders

### Feeder Monitoring
- Next meal time pulled from the active feeding schedule and shown on the device card, converted to your local timezone
- Last Fed time: updates whenever food is actually dispensed (scheduled or manual). On the One RFID feeder it also updates when a qualifying eating session is detected, meaning the feeder door opens and closes after at least the configured minimum eating duration (default 30 seconds, adjustable per device)
- Maintenance reminders: food tank refill (every 14 days by default), desiccant replacement, bowl cleaning (every 7 days), and housing cleaning (every 30 days), with per-device notification toggles and a dedicated Maintenance tab
- Food tank refill tracking: press "Filled Food Tank" whenever you top it off, and Petlibro Local tracks days since on a configurable interval, independent of the feeder's own grain sensor (which isn't always reliable)
- Signal strength (RSSI) and firmware version shown in the device modal header
- Automatic clock correction: every device reports its own clock in each heartbeat, so a clock that has drifted or come back stale after a reboot is noticed and corrected. On the One RFID feeder, Petlibro Local also sends the same time-sync message the Petlibro cloud sends when the feeder asks for the time after a boot. A correction is held while jumping the clock would skip or repeat a scheduled feed. The modal header shows "Clock in sync" (or how far off it is), and there is a diagnostic Clock Offset sensor in Home Assistant
- One RFID Smart Feeder backup battery: current charge and AC/battery status shown on the device card, modal header, and Overview tab. Exposed to Home Assistant as a Battery sensor and an "On AC Power" binary sensor. Notifications for "running on battery" (fires when caught before the feeder drops Wi-Fi to save power, since this model appears to disconnect shortly after losing AC) and a configurable low-battery percentage threshold in the Maintenance tab

> **Tip:** If you're setting up battery devices around your home in general, not just this feeder, check out my other project, [Battery Sentinel](https://github.com/smcneece/battery-sentinel), a Home Assistant app for monitoring battery devices with an alert system, battery type lookup, and a lot more. Genuinely one of my favorite things I've built.

### Feeder Controls
- Feed Now button: dispenses one portion immediately (pick up to 5 portions in the app)
- Open Door: opens the feeder lid on demand (the feeder closes it automatically after its configured delay)
- Volume: slider from 0-100 for the feeder's built-in speaker
- All controls send directly to the device over local MQTT using the standard Petlibro service protocol

### Feeding Schedules

The Schedule tab lists every meal with its time, days, amount and sound, and lets you add, edit, enable, disable and delete plans. Changes sync to the feeder immediately.

- **Run feeding schedules:** a switch at the top of the Schedule tab. Turn it off to pause every schedule at once (for example while you feed by hand) and turn it back on to resume. Your plans are kept; while paused the feeder is sent an empty schedule, so it will not feed on its own. Next Meal, the missed-feed alert and the clock correction all treat a paused feeder as having no schedule.
- **Skip:** each row has a Skip button that skips that plan's next feeding, which is later today if the plan's time is still ahead, otherwise the next day it runs. The row shows "Skipped today" or "Skipped tomorrow" with an Undo button. It uses the same per-plan skip the Petlibro app sets, so a skip made in either place shows in both, and it ends at the end of that day. While a plan is skipped, Next Meal, the missed-feed alert and the clock correction ignore that feed.

> **Note:** Existing feeding schedules created in the Petlibro app will likely continue to run on the feeder, but it is recommended to recreate them in Petlibro Local to ensure they are managed and visible here. Schedules created in the cloud app may not survive a feeder reboot once the device is running locally.

### Sound

- **Device Sound hours** (One RFID, Overview tab): sounds play all day, or only inside a custom time window, like the Device Sound setting in the Petlibro app. Times are your local time.
- **Custom feed sounds**: upload an audio file (any common format, MP3/WAV/M4A/etc., converted automatically) or record one from your microphone, in the Settings → Audio tab. All feeders share one sound library there. From a feeder's Maintenance tab, pick a sound and push it to play on that feeder's feeds instead of the default chime. Requires a one-time "Local Audio Base URL" setting (a plain LAN address for this app, since the feeder fetches the file directly and can't use your logged-in browser session). Plays on scheduled feeds and on a manual Feed Now

> **Note on recording custom sounds:** browsers only allow microphone access ("secure context") over HTTPS or `localhost`. Most people reach Home Assistant locally over plain HTTP, so the in-app Record button often won't work there, no error, it just won't be available. Three options, easiest first:
> 1. **Use the Home Assistant Companion App** on your phone. Recording works there even for local (non-HTTPS) connections.
> 2. **Record with your phone or computer's own voice recorder app** (Windows' built-in Sound Recorder, or Voice Memos on Mac, both save as `.m4a`), then use Upload File instead of Record. Any common format works and is converted automatically.
> 3. **Last resort:** some browsers let you manually allow microphone access on an insecure origin via an advanced/experimental flag (for example, Chrome's `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, where you add your Home Assistant address). This varies by browser and isn't something this app can configure for you.

### LED Display (One RFID feeder)

Everything for the feeder's LED screen is on its own **LED** tab:

- **Show display** switch, and **LED Display hours** (all day, or a custom time window in your local time), like the LED Display screen in the Petlibro app
- **Scrolling text**: push your own text, such as a pet's name, to the display
- **Icons**: send a built-in icon (heart, dog, cat, elk) or one of your own. The **Custom Icon Editor** lets you draw pixel art on a 5 × 12 grid, preview it at full 26-pixel display width, save up to 12 named icons, and send them with one tap. It includes a "Petlibro Salute" built-in preset
- **Animations**: the display is 26 pixels wide and 7 tall, and it can loop an animation on its idle screen. Pick one of the built-in animations (a kitten chasing a ball, a kitten watching a bird fly away, a catnap with floating z's, a swimming fish, a heartbeat, and a display test pattern), preview it in the app, set the speed and send it. Animations are limited to 140 frames
- **Animation editor**: draw your own frame by frame. Add, duplicate, delete and reorder frames, shift a frame one pixel in any direction (left and right wrap around, which makes scrolling easy), preview it, and send it to the feeder. Save up to 20 of your own animations to a library, and export or import them as JSON to share. Import also accepts frames written as 7 text rows of 26 characters (`#` is lit, `.` is dark)

Sending scrolling text or an icon again replaces whatever animation is playing. The animation format follows a Reddit user's description of the feeder's protocol and works on my One RFID feeder; I have only tested one feeder.

### Food Amount Unit

Choose how feed amounts are shown for each feeder (cup, oz, g or ml, like the Petlibro app's Food Amount setting), in its Schedule tab. One portion is 1/12 cup or 20 ml. Cups and ml are the feeder's fixed scoop volume, while grams and ounces depend on your food (10 g per portion unless you calibrate). "Automatic" follows Settings → Units.

### Feed Amount Calibration (Optional)

The One RFID and Granary feeders dispense through a fixed-volume scoop rather than a scale, so one "portion" doesn't weigh the same for every food. Out of the box, Petlibro Local assumes 10g per portion everywhere it shows an amount (Feed Now, schedules, activity log). If you'd rather see real numbers for your specific food, you can calibrate.

**This is entirely optional.** If you never calibrate, nothing changes and everything keeps using 10g per portion. Calibration only changes how amounts are *displayed*; it does not change how much food the feeder dispenses.

To calibrate:
1. Open the feeder's device modal, go to the **Schedule** tab, and tap **Calibrate** in the Feed Amount Calibration section.
2. The feeder dispenses one portion. Catch it in a small dish or cup and weigh it on your kitchen scale.
3. Enter the reading in grams or ounces (ounces is the default if you use imperial units, but grams is more accurate if your scale can show it, since one portion is only about 10g or 0.35 oz).
4. Repeat for 3 test feeds. You'll see the average, then choose **Save** or **Redo**.

**Reset to Default** in the same section puts it back to 10g per portion at any time.

## Device Images

Device images are stored locally in the app. Image filenames follow the convention `{mqtt_model}_{color}.png` (for example, `plwf106_b.png` for a black Dockstream 2). Images with transparent backgrounds are recommended for best appearance against the dark UI. Multiple color variants per device type are supported.

## Pets

Add pet profiles with photo, name, breed, and weight. Assign pets to one or more devices. Pet profiles are stored locally and are independent of the Petlibro cloud account.

**RFID tag tracking:** On the One RFID Smart Feeder, the pet's RFID tag number is captured automatically on the first detected eating session and saved to the pet profile. Tags can also be entered manually behind the eye toggle on the pet's profile.

**Recent Activity:** Each pet's profile shows a per-pet activity timeline of eating sessions confirmed by RFID (with duration), and in future releases, drinking sessions from RFID fountains and litter box visits. Device-level feeding events without RFID confirmation remain in the device log and do not appear in the pet activity view.

## Notifications

Petlibro Local sends notifications through the channels you turn on:

- **In-app alert bell**: the bell icon in the app header shows a badge for active alerts. Clicking it opens a panel listing every active alert by device name and type. Clicking a row opens that device's detail modal.
- **HA persistent notification**: appears in the Home Assistant notification panel and survives app restarts.
- **Email**: sent via any Home Assistant `notify` service. Configure the service name in the device's notification settings.
- **Mobile (Home Assistant Companion app)**: a push notification sent through the Home Assistant Companion app on your phone, not a text message. It uses a Home Assistant `notify` service that targets your phone, which you pick in Settings or per device.

**Device alerts** are configurable per device in the device's detail modal under the Alerts tab. Alert types include:

| Alert | Trigger |
|-------|---------|
| Device offline | No MQTT message received for 5 minutes |
| Device back online | Device reconnects after being offline |
| Food level low | Feeder grain sensor reports low |
| Food tank refill due | Manual refill interval you set has elapsed (backup for the feeder's own low-food sensor, which isn't always reliable) |
| Low water | Fountain water level drops below threshold |
| Filter replacement due | Filter days remaining reaches 3 or fewer |
| Cleaning overdue | Fountain cleaning interval exceeded |
| Desiccant, bowl or housing due soon | Feeder reminders: desiccant and housing 3 days before they are due, bowl 1 day before (never more than half the interval early) |
| Food door jammed | One RFID Smart Feeder reports its door couldn't close, usually a pet blocking it |
| Scheduled feed missed | An enabled feeding plan is more than 5 minutes past its time and nothing was dispensed (One RFID and Granary feeders) |
| Food dispensed | Optional, off by default: every dispense, labeled Manual or Scheduled |

**Pet activity notifications** fire when an RFID eating session is detected: "Zoey ate for 3m25s at Zoey's Feeder." These use the same notification channels as device alerts and can be enabled or disabled per pet. Cats often step away and come back several times per meal, so each pet also has a "Combine visits within N minutes" setting: with it set, the visits of one meal send a single notification with the total once N minutes pass with no new visit, for example "Luna ate for 3m46s at Luna foder (5 visits over 4 min)". 0, the default, sends one per visit.

**Hasn't-eaten alerts** are a separate per-pet setting: choose a number of hours, and if no eating session is detected within that window, a notification fires through the same channels. It clears itself automatically the next time that pet eats, so there's nothing to dismiss by hand. It remembers which meal it already alerted for, so restarting or rebuilding the app does not send the same alert again.

## Settings

- MQTT broker host, port, username, and password for the app's own broker connection
- Connection test runs automatically on save and shows a green or red indicator
- Language selection (English included; additional locales can be contributed)
- All configuration is done in the Settings tab; no YAML to edit
