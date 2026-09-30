# Using Acta with a real strap

Acta doesn't talk to the strap itself. It reads the SQLite database that
[Gadgetbridge](https://gadgetbridge.org/) (an open-source Android companion app) keeps on
the phone. You get that file to the machine running Acta, and `acta ingest` does the rest.

```
strap --Bluetooth--> Gadgetbridge (phone) --auto-export--> Gadgetbridge.db --sync--> Acta
```

**Supported devices:** Huami/Zepp straps and watches (Amazfit). Acta reads their tables
(`HUAMI_*`) and their sleep-session format, so other brands won't work without new parsing
code. It was built and tested on an Amazfit Helio Strap.

## 1. Install Gadgetbridge and pair the device

1. Install Gadgetbridge on the phone. It's on [F-Droid](https://f-droid.org/packages/nodomain.freeyourgadget.gadgetbridge/).
2. Recent Huami/Zepp devices need an **auth key** before Gadgetbridge can pair. The key
   comes from the manufacturer's servers, via the official Zepp app. Gadgetbridge's
   documentation explains how to get it for your model; do this first, it's the step
   people get stuck on.
3. Add the device in Gadgetbridge, enter the auth key, and let it pair. Once Gadgetbridge
   works, don't let the official app connect to the device as well, or the two will fight
   over it.

## 2. Turn on the measurements Acta uses

In Gadgetbridge's settings for the device, enable:

| Setting | Used for |
|---|---|
| Heart rate monitoring, **every minute** | biocharge, zones, workout detection, calories, the vitals charts |
| Sleep monitoring (with sleep breathing / respiratory rate if offered) | sleep score, recharge, respiratory rate |
| Stress monitoring | biocharge drain, the stress chart |
| SpO₂ | vitals |
| HRV (on devices that record it at night) | sleep score, readiness |

Anything missing is simply blank in the dashboard. Heart rate every minute is the one
that really matters.

## 3. Export the database automatically

Gadgetbridge can write a copy of its database on a schedule. Turn on the database
**auto-export** in the app's settings (the menu has moved between versions; look for
"Auto export" under settings or automations), choose a folder, and set an interval.
Hourly works well.

The export only contains what the phone has fetched from the strap. Enable automatic
fetching (or pull down on the device card now and then), otherwise the export can be
hours behind. The dashboard says "No data · please upload" when today's data is missing.

## 4. Get the file to Acta

Sync the export folder to the machine running Acta with any file-sync tool.
[Syncthing](https://syncthing.net/) is a good fit: it's peer-to-peer and runs on Android.
Then point Acta at the synced file:

```bash
export ACTA_GADGETBRIDGE_DB=~/sync/gadgetbridge/Gadgetbridge.db
acta ingest
```

The first run scores every night in the history and prints how many it wrote. After
that, run it on a timer (see [`deploy/`](../deploy/)). Runs where nothing changed cost a
single `stat()` call. Acta opens the export read-only and hashes a private copy, so a sync
rewriting the file mid-run can't corrupt anything.

## 5. Things the strap can't see

Log these in the app (the **Log** button, or the Workout tab). They feed the models too:

- **coffee and alcohol:** they change the energy curve, and alcohol also changes the sleep
  score;
- **workouts without the strap** (swimming, for example): priced from the logged duration
  and intensity;
- **workouts the strap saw but you didn't log:** Acta proposes them from the heart-rate
  data, and you confirm or dismiss each one.

## First weeks

Most scores compare you with **your own** recent history (usually the last 21 nights).
For the first week the sleep score uses general defaults. Baselines settle after about
three weeks, and the analysis features (lag mining, experiments, the illness fingerprint)
need months of data before they say anything. See [algorithms.md](algorithms.md) for
what's personal and what's fixed.
