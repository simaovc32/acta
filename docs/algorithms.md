# Algorithms

How each number on the dashboard is computed. The source for each section is named in its
heading; the constants below are the ones in the code.

A few principles apply throughout:

- **Personal baselines.** Almost every component compares a value with *your own*
  rolling median (usually 21 nights) and spread (MAD), not with a population norm. A
  resting HR of 50 is great for one person and a warning sign for another.
- **Forward-only changes.** When a formula changes, it takes effect from a gate date
  (`SLEEP_SCORE_V2_START`, `HR_ZONE_TOP_START`, `NAP_DETECT_START`, …). Earlier days keep
  the model they were computed under, so a full replay reproduces history exactly.
- **No fitting to self-reports.** Mood and "how did I sleep" ratings are shown, described
  and correlated, never used as a training target or a score input.

---

## BioCharge: `engine/biocharge.py`

A per-minute level from 0 to 100 that carries over from one day to the next (a cold start
is 40).

**Recharge (sleep).** The night's sleep score sets a wake-up target:

```
target = 15 + 0.8 × sleep_score            # score 0 → 15, score 100 → 95
```

The gap between the level at bedtime and that target is spread over the sleeping minutes
in proportion to stage weight: deep 2.0, REM 1.5, light 1.0, awake 0. A deep minute
recharges twice as much as a light one. The target is then scaled by a resting-HR factor
(`baseline_RHR / measured_RHR`, clamped to 0.85–1.10), so a night with an elevated heart
rate recharges less than its score alone suggests.

**Drain (awake).** Every awake minute costs:

```
drain = 0.010                                  # base
      + zone_drain(HR − resting_HR_baseline)   # 0 … 0.17 pts/min across 6 zones
        × ambient_gate                         # 0.35 when HR is up but the body isn't moving
      + stress_drain                           # up to 0.05 from the strap's stress reading
      × modifiers                              # alcohol raises drain, coffee suppresses it for ~2.5 h
```

Zones are measured in beats above your own resting baseline, not in absolute beats per
minute. Above zone 5 the cost keeps rising at the curve's own slope up to your measured
maximum HR, so a 186-bpm interval costs more than a 161-bpm tempo run. The **ambient
gate** stops a loud bar or a scary film from being billed as exercise: raised HR with
almost no movement keeps only 35% of the zone cost, unless a workout was logged for that
window.

**Naps and returning to sleep.** The strap writes no sleep-stage data for a nap, but it
does mark those minutes as sleep. Blocks of 20 minutes or more, starting at least 2.5 h
after waking, recharge at 0.145 pts/min. That rate is the median rate of all night-sleep
minutes, so a nap minute is treated as an average sleep minute. Naps are capped at 120
minutes and 18 points.

## Sleep score: `engine/sleep_score.py`, `pipeline/ingest.py`

Five components, each compared with your trailing baseline:

| Component | Weight | Measures |
|---|---|---|
| Efficiency | 25 | asleep ÷ time in bed; ≥ 92% is full marks. A first awakening under 10 min is forgiven. |
| Regularity | 20 | sleep midpoint vs the 14-night median: full within 30 min, zero beyond 90 |
| Duration | 15 | minutes asleep vs the 21-night median |
| Stages | 25 | deep % and REM % vs their 21-night medians |
| Physiology | 15 | resting HR 45%, HRV 40%, respiratory-rate variability 15%, each as a MAD z-score vs baseline |

Then two penalties: time awake after first falling asleep (WASO) above 20 min costs up to
−6, and alcohol before bed costs −2 per unit (capped at −9). Each night also gets a "why"
line from its best and worst component.

For the first week there's no baseline, so duration is centred on a typical adult night
(7.5 h, full marks between roughly 7 and 8 h). From night 8 on it's judged against your
own 21-night median. That's also why the score never says "sleep more": it measures
whether a night was normal *for you*.

The physiology weights come from data. The first version averaged five sub-scores equally.
On one heavy-drinking night, resting HR rose by 8 bpm and HRV fell by 44%, and the
component lost only 0.3 points. The reason was `hr_dip` (resting HR minus the night's
minimum), which turned out to be *positively* correlated with a bad night (r = +0.73 with
resting HR over 133 nights), so it cancelled out the real signal. It was dropped, and the
two signals that do track recovery now carry the weight.

The strap stops recording sleep stages once you get up, even if you go back to sleep.
Ingest recovers those minutes from the raw activity stream (a minute flagged as sleep,
with no steps) and adds them to duration without inventing a stage breakdown for them.

## Readiness and recovery alarm: `engine/night_physio.py`

```
readiness = 0.40 × sleep_score
          + 0.25 × clamp(50 + 5 × (RHR_baseline − RHR), 0, 100)
          + 0.20 × clamp(50 + 250 × (HRV / HRV_baseline − 1), 0, 100)
          + 0.15 × load
```

`load` is yesterday's session-RPE (RPE × minutes; cardio RPE comes from the logged
intensity) against your own median session: 100 for a rest day, down to 20 for a very hard
one. Below 55, the Workout tab offers to cut the day's sets by 20, 40 or 60%, as an override
for that day only, never a change to the plan.

The **recovery alarm** watches the same signals for two shapes:

- **Sustained:** 2 or 3 of the last 3 nights past threshold (RHR ≥ baseline + 5 bpm,
  HRV ≤ 90% of baseline, or sleep 5 points below baseline).
- **Acute:** 2 consecutive nights getting worse, with HRV ≤ 85% of baseline (80% on the
  latest night) or RHR +3 bpm (+5 on the latest).

When the alarm fires hard, the illness fingerprint takes a snapshot of the preceding days,
so the pattern can be recognised next time.

## Workout detection: `engine/pai.py`

The strap computes PAI, a daily activity score from time in heart-rate zones. It never
records *what* you did. On a day with at least 5 PAI points, the detector finds the
bout(s) in the per-minute HR series:

- anchor on **exertion** minutes (HR more than 78 bpm above resting), not merely active
  ones, so an evening of pottering about can't chain into one fake 9-hour session;
- bridge lulls up to 10 min (half-time, rest between sets);
- trim the cool-down at the end by cadence (the effort ends where steps drop below
  100/min), because HR stays up during recovery after the work has stopped;
- wait 20 quiet minutes before proposing today's bout, so a run isn't frozen halfway;
- skip anything overlapping a logged workout.

Each bout is **proposed**, not written. You confirm it as football, a run, padel and so on,
or dismiss it. Unanswered proposals expire after 30 days. They are dismissed, never
deleted, so they can still be restored.

## VO₂max and heart-rate recovery: `engine/fitness.py`

Two published estimators, reported as a range rather than one false-precision number:

- **Uth et al. (2004):** `15.3 × HRmax / RHR`, using the strap's measured max HR and a
  28-night median resting HR.
- **Nes et al. (2011, HUNT):** a non-exercise model from age, sex, waist, resting HR and an
  activity index built from PAI zone minutes and confirmed workouts.

The headline value is `0.55·Uth + 0.45·Nes`, with a percentile from the FRIEND registry.
When the two estimates disagree by more than about 10 points, the gap is shown instead of
hidden. **Heart-rate recovery** (the drop 1 and 2 minutes after the effort ends, median of
recent confirmed runs) is a separate readout. A step-test HRR equation was tried as a third
estimator and rejected: after a maximal field effort it read about 20 points low.

## Calories: `engine/calories.py`

Each minute is classified and priced by mode instead of one formula for the whole day:

- **sleep:** 0.95 × BMR (Mifflin–St Jeor, or Katch–McArdle when body fat is known);
- **sedentary:** 1.25 × BMR plus a step-rate increment;
- **active:** percent of HR reserve → VO₂ → kcal (5 kcal per litre of O₂), using the
  measured max HR and the rolling resting HR, so it adapts as fitness changes;
- **strength:** a fixed MET of 5.0, because HR is a poor proxy for energy under load.

Population equations such as Keytel's are anchored to absolute heart rate and fitted on
people with a resting HR around 65–70. For a very fit user they over-bill exactly the band
where most minutes are spent. The estimate is display-only and never fed back into
biocharge, since both come from the same HR stream.

## Lag mining: `insights/lag_mining.py`

Every objective predictor (17 of them) is tested against every target (10), at lags of 0,
1 and 2 nights, over the whole history. That is about 350 Pearson correlations per run. A
finding must:

1. have at least 20 paired nights,
2. clear |r| ≥ 0.15 (a size floor, not the test), and
3. survive **Benjamini–Hochberg** at a false-discovery rate of 10%. This is the actual
   test: the bar tightens as more pairs are tested.

Survivors are reported with a bootstrap 95% interval. Pairs that are correlated by
construction (RHR vs HRV at lag 0, which come from the same overnight heart-rate signal; a
score vs its own inputs) are listed explicitly in `DEFINITIONAL` and skipped. The strongest
"finding" of the first version, r = −0.90, was one of those.

## Auspex: `insights/auspex.py`, `insights/explore.py`

Preset questions ("what drove last night's score?", "is my resting HR trending?", "what
will tonight turn on?"). The split of work is strict:

- **Python decides.** Each question has a builder that computes the facts and the verdict:
  trend tests, the cascade that produced the wake-up level, ranked worst nights with what
  they share.
- **The model narrates.** A language model gets only those computed facts and phrases
  them. It is told the verdict is authoritative and must not re-derive it.

`explore` goes a step further. The model *proposes* hypotheses in a fixed vocabulary of
feature keys, Python tests them with the same gates as lag mining, and only the ones that
pass come back as findings.

---

## Calibration

I developed Acta on one person's data: mine (about 7 h of sleep, a resting HR
around 50, near-daily training). What's personal and what's fixed:

| | Adapts to you | Fixed (tuned on me) |
|---|---|---|
| Sleep score | duration, stages, regularity and physiology vs your own rolling medians | component weights; the first-week defaults (7.5 h, 25% deep, 25% REM); the WASO and alcohol penalties |
| BioCharge | zones are measured from *your* resting-HR baseline; the wake target follows your sleep score | zone widths and costs, `HR_MAX` (195) where the top zone stops climbing, nap recharge rate (0.145/min), coffee and alcohol effects |
| Readiness | RHR, HRV and training load vs your own medians | the 40/25/20/15 weights; the training-load reference before you have 3 rated sessions |
| VO₂max | uses your measured max HR, resting HR, waist and activity | the published equations themselves |
| Calories | your BMR, max HR and resting HR | the calibration factor `k` (1.0, uncalibrated) |

The fixed values live as named constants at the top of each module, with a comment saying
where each one came from. Nothing tunes them automatically. That's deliberate: a model
that fitted itself to whatever it saw would drift towards telling you what you already
believe.
