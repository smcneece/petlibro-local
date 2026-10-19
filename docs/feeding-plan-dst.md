# Feeding plans and daylight saving time

Feeding plan times are UTC on the wire (confirmed on a One RFID, fw 3.0.72). The
schedule editor converts local time to UTC with the offset of the moment you save,
so without help every meal would move by an hour at each DST change.

The add-on remembers the Settings timezone and UTC offset each device's plans were
saved for. When that offset changes, it shifts the UTC times (and weekdays, if a time
crosses midnight) and re-sends the plans every 5 minutes until the feeder acks them.
Polar feeders use dated plans and are not touched.

## Behaviour around the change

- **Fall back** (meals move later): if a meal's old time has passed (or is less than
  2 minutes away) but its new time has not, the new time is skipped once so the cat
  is not fed twice. This covers a meal inside the repeated hour and a feeder that
  gets the shift late. Until the feeder acks, the check is repeated before every
  send, also when you edit the schedule meanwhile (for meals you did not move).
- **Spring forward** (meals move earlier): a meal whose new time passed before the
  shift reached the add-on does not run that day. This is logged; there is no
  catch-up feed.
- **Editor**: the schedule carries a revision. Saving from a tab opened before the
  schedule changed (a DST shift, a skip added by the check above, a timezone change,
  another tab) is refused and the schedule reloads, so it cannot undo that change.

## Limitations (best effort, not exactly-once)

- Set **Feeder Timezone** in Settings. Without it the editor uses the browser's
  timezone, which the server cannot know, so plans are not shifted.
- Plans saved before this feature are taken as correct for the current offset. If
  you upgrade after a DST change, check the displayed meal times and fix them once.
- Changing the timezone in Settings keeps the UTC times, so displayed local times
  change. Check them afterwards.
- The double-feed check errs towards skipping: if the feeder applied the shift but
  its ack was lost, or the shift reached it just before the old meal time, a meal
  can be skipped instead. A meal you moved yourself while the shift was unacked is
  not checked.
- While a shift is unacked, a skip added by this check cannot be removed in the
  editor (the save is refused with an explanation); it ends by itself after that meal.
- Verified on a One RFID (fw 3.0.72): plans fire at their UTC time even when the
  time sync announces a DST change or a different offset, so the firmware does not
  adjust them itself. Other models are assumed to behave the same.
- A feeder that does not ack for months, across two DST changes, is compared with
  its oldest unacked list only.
- An editor that loads the schedule within seconds of the change itself can still
  convert with the wrong offset.
- The missed-feed alert compares with the stored plans, so it can be wrong for a
  meal around the change while the feeder has not acked the shift yet.

## Tests

`addon/tests/test_dst_delivery.py` (pytest; fake MQTT, clock and feeder, temporary
storage):

```sh
python -m pytest -q addon/tests
```
