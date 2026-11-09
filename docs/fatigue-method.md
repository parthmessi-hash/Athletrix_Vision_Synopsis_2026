# Fatigue estimation method (2026-11-09)

Athletrix produces an **experimental** fatigue estimate, 0-100, for the player wearing the vest, relative to that
player's own baseline. The **combined** estimate (wearable + fixed sideline camera) is the product output; the
wearable-only and camera-only estimates are research comparisons. Nothing here has been tested on athletes: every
number below comes from fictional or generated data. Code: `app/core/fatigue.py`. Profile values quoted are the
FICTIONAL development profile `scripts/examples/pilot/fictional_fatigue_profile.json`; **none of them is a team
decision**.

## 1. How a score is made, in plain language

1. At a moment T, the engine takes the vest wearer's readings that it had **received by T**: heart rate from the
   assigned vest (only during that player's own wearer assignment) and their camera-measured speed. Readings from a
   timed test sprint (5 s before to 90 s after it) are never used, in any window, later or earlier.
2. It computes several **features**, each 0-100 against the player's own reference: their seated-rest heart rate and
   maximum heart rate for the wearable, and their own first minutes of tracked play in the session for the camera.
   A feature that cannot be computed is "not available" with a reason; it is never 0.
3. The **combined estimate** is the weighted mean of its available features, re-weighted over those available, but
   only when its required features are available, both streams are fresh (newest reading at most 10 s old) and the
   available weights reach 70 %. Otherwise it is "not available" with every reason; it is never replaced by a
   single-source value.
4. A saved pre-sprint estimate stores everything needed to recompute it (frozen profile text, input fingerprints,
   rules version); late readings and later corrections cannot change what was known when it was saved.

## 2. Features (current rules version 2)

| Feature | Inputs | Formula and units | Personal reference | Window | Not available when |
|---|---|---|---|---|---|
| hr_strain | wearable | mean HR (bpm) as a fraction of heart-rate **reserve**: (mean - rest) / (HRmax - rest); 0 at 0.50, 100 at 0.90 | accepted seated-rest baseline (rest HR); HRmax with its stated origin | last 120 s | fewer than 30 HR values; no accepted baseline; no HRmax; HRmax not above rest; not the active wearer |
| time_above_threshold | wearable | share of covered time with HR >= 0.85 x **HRmax** (not reserve); 0 at none, 100 at 50 % of the time | HRmax | last 300 s | less than 60 % of the window covered (gaps over 5 s not bridged) |
| recovery | wearable | HR drop (bpm) during 60 s after the latest effort >= 0.85 x HRmax; 0 when the drop reaches 30 bpm (absolute), 100 at no drop | HRmax; a fixed 30 bpm reference drop | latest qualifying effort in the last 600 s | no qualifying effort; recovery window incomplete, interrupted by a new effort, or under 70 % covered |
| hr_per_speed | wearable + camera | (mean HR / mean speed) recent against the same ratio in the reference, bpm per m/s; 100 at +30 % | the player's first 120 s of tracked play after their first properly started round | last 120 s, pairs within 0.5 s, speed >= 0.5 m/s | fewer than 20 pairs in either window; no stated camera clock alignment or uncertainty over 0.5 s; reference incomplete |
| output_change | camera | mean speed over covered time (m/s, i.e. distance per second) recent against reference; 0 when not lower, 100 at a 40 % drop | same camera reference | last 120 s | under 60 % tracked coverage in either window; no movement in the reference |
| sprint_quality | camera | mean peak speed of complete detected sprints (>= 5.5 m/s) recent against reference; 100 at a 15 % drop | same camera reference | last 300 s | no sprint in the reference; no sprint opportunity in the recent window (never read as a drop) |
| hr_speed_matched (EXPERIMENTAL) | wearable + camera | see section 4 | same camera reference | last 120 s, movement context 30 s | see section 4 |

Combined (development profile): hr_strain 0.3, time_above_threshold 0.1, recovery 0.1, hr_per_speed 0.3,
output_change 0.2; hr_strain, hr_per_speed and output_change required. Wearable-only: hr_strain 0.5, time above 0.25,
recovery 0.25. Camera-only: output_change 0.6, sprint_quality 0.4.

## 3. Differences from the research draft (flagged, not resolved)

- **Percent of maximum heart rate vs percent of heart-rate reserve.** hr_strain uses heart-rate **reserve** (needs a
  resting baseline); time_above_threshold and recovery qualification use **percent of HRmax**. The draft speaks of
  "heart-rate strain" and "a configured heart-rate threshold" without fixing the basis. The two bases are inconsistent on
  purpose until the team chooses one.
- **HRmax**: the draft wants an explicit input with its origin; the software requires `hr_max_bpm` and
  `hr_max_origin` per wearer. A measured test and an age formula are not equivalent; the team must choose.
- **Recovery reference** is an absolute 30 bpm drop, not relative to the player.
- **Camera baseline**: the draft says "per player baselines"; the camera reference is an early-session window, not a
  separate rest baseline, so it moves with each session's first minutes.
- **Physical output** is mean speed; the draft's "physical-output change" could also mean distance per minute or
  high-speed distance.
- **Sprints**: the draft names "tracked sprint efforts" (5.5 m/s); the software uses peak speed of complete sprints,
  not a count, so "no opportunity" is never a drop.
- Not inputs, and not to be added from unconfirmed vendor fields: live beat-to-beat R-R intervals / HRV, vest-reported
  jump or impact counts, sleep, soreness.

## 4. The experimental change: heart rate against movement

**Weakness found (hr_per_speed).** It divides mean heart rate by mean speed. Heart rate is not proportional to speed:
it has a large offset at rest and it lags movement by tens of seconds. So:
- a deliberately **slower drill** raises bpm-per-m/s even with a normal heart-rate response (fictional case: 100,
  the maximum);
- a **stop-start drill** keeps heart rate up while instantaneous speed drops;
- a camera clock that is off by many seconds pairs heart rate with the wrong movement.

**Candidate (hr_speed_matched, only in `scripts/examples/pilot/experimental_matched_profile.json`).** Each heart-rate
sample is paired with the player's mean speed over the preceding 30 s (lag-aware; needs 60 % of those 30 s tracked,
no bridging across gaps or test sprints). Pairs fall into speed bands (0.5-1.5, 1.5-2.5, 2.5-3.5, 3.5-5, 5-12 m/s). In
each band where both the reference and the recent window have at least 5 pairs, it takes recent mean HR minus reference
mean HR (bpm). The feature is the pair-weighted mean excess as a fraction of heart-rate reserve; 0 at no excess, 100 at
10 % of reserve (14 bpm for a 60-200 bpm player). **If matched bands hold less than 60 % of the recent pairs, it returns
"the reference does not cover this kind of movement"** instead of extrapolating. It never uses R-R intervals or vendor
event counts.

**Fictional comparison** (`scripts/fatigue_method_compare.py`; heart rate follows movement with a 25 s lag; fictional
player 60/200 bpm). "Usable" is the coverage each method actually had: heart-rate/speed pairs for the current feature,
the share of recent pairs in speed bands the reference also has for the experimental one.

| Case | Should | Current feature / usable / combined | Experimental feature / usable / combined |
|---|---|---|---|
| harder work (recent 4.0 m/s, reference up to 3.4) | low, or not enough data | 0 / 120 pairs / 12.7 | not available / 16 % comparable / not available |
| same movement, +12 bpm drift | clearly raised | 41 / 120 / 22 | **91** / 100 % / 39 |
| slower drill, no slow movement in the reference | not enough data | **100** / 120 / 56 | not available / 0 % / not available |
| slower drill, reference includes slow movement | low | **100** / 120 / 56 | **0** / 100 % / 22 |
| stop-start drill (5 s fast, 10 s slow), same average | low | 12 / 120 / 4 | 20 / 100 % / 7 |
| camera clock uncertainty 2 s | not enough data | not available | not available |
| camera clock secretly 20 s off, +12 bpm drift | close to the no-offset value | 20 / 120 / 15 | 89 / 83 % / 38 |
| camera missing | not enough data | not available | not available (now explained as a camera gap) |
| **challenge:** stop-start every 6 s (2 s fast, 4 s slow), same average | low | 9 / 120 / 3 | 12 / 100 % / 4 |
| **challenge:** heart rate still high after an abrupt slowdown | low | 12 / 120 / 4 | **64** / 100 % / **21** |
| **challenge:** step from slow to hard running (control) | low | 9 / 120 / 5 | 3 / 100 % / 3 |
| **challenge:** the same step, camera clock secretly 5 s off | close to the control | 8 / 116 / 4 | 0 / 100 % / 1 |
| **challenge:** the same step, stated clock uncertainty 2 s | not enough data | not available | not available |

What improved: a slower drill is no longer read as maximal fatigue; a genuine same-movement rise is clearer; a hidden
clock offset mattered less in these cases.

**Where the experimental method still misleads (challenge cases, 2026-11-09):**
- **Lingering heart rate after an abrupt slowdown reads as fatigue-like (64, combined 21, with no drift).** Each heart-rate
  sample is matched on the mean speed of the preceding 30 s, but heart rate takes longer than that to come down, so the
  first slow minute after hard running carries heart rate the reference's slow band does not have. This is a limitation of
  the method (context length vs heart-rate recovery), not a code defect; it is pinned by a test. A testable next step for
  the team, not done here: leave out heart-rate samples whose movement changed band within the last N seconds (steady-state
  pairs only), and check it on this case and the stop-start cases before adopting it. Do not tune context_s to make this
  one case pass.
- **A hidden clock offset is not detectable from the two signals.** With 5 s it changed little here (3 -> 0); with 20 s it
  changed little in the drift case. These are fictional shapes; on other shapes an offset could matter more. Only the
  stated alignment and its uncertainty protect against it.
- **Band edges are hard.** A steady 2.5 m/s sits on the edge between two bands and splits between them by rounding (seen in
  the camera-missing case). Movement near an edge can move pairs between bands.
- **Stop-start reads slightly higher** (12-20) because averaged movement crosses band edges; this simulator's heart rate has
  no extra cost for accelerations, so real stop-start work is not represented.
- More "not enough data" when recent movement goes beyond the reference (harder work): by design.
- **output_change still reads a deliberately slower drill as lower output** (the 22 above comes from it).

**Defects found and fixed (2026-11-09):**
- With the camera gone for most of the recent window, the experimental feature's reason said "the reference does not cover
  this kind of movement". It now names the camera gap (or missing heart rate, uncertain identity, or test-sprint exclusion)
  when lost data removed more samples than were paired. Availability and values are unchanged.
- An accepted baseline with too few heart-rate values (no resting heart rate) made hr_strain and hr_speed_matched raise a
  TypeError (HTTP 500 on the score and pilot view). They now return "not available" with the reason. Any score that did not
  raise before is computed exactly as before.

The candidate is experimental: fictional cases expose bad behaviour, they cannot show accuracy. Existing frozen profiles do
not use it, so every saved prediction reproduces exactly as before.

### 4b. Reference coverage: checking the player's reference (EXPERIMENTAL feature only)

When the session's frozen profile uses hr_speed_matched, the pilot view (`GET .../study/pilot`, field
`reference_coverage`), the Fatigue estimate card (a dashed "Reference coverage" box) and `pilot_session.py coverage` show,
for the active wearer:
- each speed band: **comparable** (both windows have at least min_pairs_per_band pairs: exactly the score's rule), **reference
  lacks this range**, **reference has only N pairs here**, **too few recent pairs**, or **no recent movement here**;
- the share of recent samples in comparable bands against the profile's min_matched_share;
- per window, heart-rate samples, seconds covered, pairs made, and what was left out and why: next to a timed test sprint (by
  design), low-confidence or predicted camera tracking (identity uncertain), no camera tracking (gap), or movement outside
  every band;
- a status that keeps apart a reference that lacks the speed range, missing heart rate, uncertain camera identity, a camera
  gap, clock alignment, an incomplete reference window, and a wearer/baseline problem; a caution when the estimate stands on
  fewer samples than the windows held; and a plain "check next" line.

It is built only from the score's own counts and thresholds and decides nothing. Heart rate counts as missing when it
covers less than context_min_coverage of a window (the feature's own threshold); a camera or test-sprint loss is named when
it removed more samples than were paired. It does not prescribe an exercise protocol: whether the reference period should
include a given movement is the team's baseline-procedure decision. With the current (default) profile it is absent.

Fictional example, **matched estimate available** (same movement, +12 bpm drift):

```
COVERED · recent samples in comparable bands: 100 % (the score needs 60 %)
  1.5-2.5 m/s: no recent movement here (reference 4 pairs)
  2.5-3.5 m/s: comparable (98 reference, 120 recent pairs)
  reference: 120 heart-rate samples, 102 paired; lost: 18 no camera tracking (the reference's first seconds have no 30 s context)
  -> feature 91 (+12.8 bpm at similar movement), combined 39
```

Fictional example, **withheld because the reference does not cover the movement** (slower drill, no slow movement in
the reference):

```
NOT_COMPARABLE · recent samples in comparable bands: 0 % (the score needs 60 %)
  0.5-1.5 m/s: reference lacks this range (0 reference pairs, 101 recent)
  1.5-2.5 m/s: reference has only 4 pairs here (needs 5); 19 recent
  2.5-3.5 m/s: no recent movement here (reference 98 pairs)
  -> combined estimate not available; next: the reference period did not include that speed; whether it should is a
     baseline-procedure decision for the team.
```

Tests: `tests/test_reference_coverage.py` (good coverage, recent effort outside the range, uneven and sparse heart rate, short
and longer tracking gaps, uncertain identity vs gap, clock alignment, incomplete reference, a wearer change through the engine
where P2 never gets P1's reference, the CLI).

## 5. The accuracy question and how it is tested

Question: does the combined estimate track independently timed 20 m sprint slowdown better than the wearable-only and
camera-only estimates? Sprint slowdown (percent increase over the player's best valid time that session) is a
practical comparison, not a measurement of fatigue.
- The estimate is saved and confirmed before the sprint starts; the sprint is timed independently (blind sheet) and
  entered afterwards; test-sprint readings and timing outcomes never reach any candidate.
- All three methods are compared on the **same eligible rounds** (each needs all three estimates and an outcome);
  each method's coverage and every excluded round with its reason are listed, with results by player and session and
  the round-order comparison (does an estimate simply follow time passing?).
- The report opens with a plain result: INCONCLUSIVE with fewer than 5 rounds, NEGATIVE when the combined estimate does
  not beat both single-source estimates, and a caution when it follows round order at least as strongly as the outcome.
  The correlation target (combined r >= 0.5) is a study target, not a promise.
- **Freeze evidence (2026-11-09).** A date typed in the manifest does not show the rules were frozen. The session entry can
  now record the study manifest's SHA-256 and the profile it names (`pilot_session.py session ... --study-manifest
  FILE`); the engine refuses the session if the manifest names a different profile from the one it freezes, and stores
  the fingerprint with its own receipt time. The report then states one of:
  **RECORDED** (every session recorded this exact manifest at setup, before the first outcome),
  **PARTLY RECORDED**, **SELF-DECLARED, NOT VERIFIED** (older exports or row files: only the typed declared_at), or
  **NONE** (no manifest). It refuses a manifest different from the one recorded, and (real data) one recorded only by a
  correction after the first outcome; for self-declared real data the old rule still applies (declared_at must precede
  the first outcome). Limits: this shows the manifest existed and was fixed when the session started on this engine; it
  cannot show nobody saw outcomes from an earlier session.
- The report's first lines also show coverage (usable rounds per method and the shared rounds compared) and the frozen
  profile and rules version; mixed rules versions are refused, so earlier results stay tied to the version they were saved
  with and are never rescored.

## 6. Decisions to freeze before an approved pilot

1. Heart-rate basis: reserve or percent of HRmax, everywhere; and how HRmax is obtained per player.
2. The features and weights of the combined estimate, including whether hr_speed_matched replaces hr_per_speed.
3. Baseline rules: the seated-rest procedure and the camera reference (length, which round opens it, and which kinds of
   movement it should contain, now visible in the reference coverage summary).
4. Freshness, coverage and minimum-data rules (when to say "not enough data").
5. The analysis: timing policy, reference methods, valid-round rule, the outcome and statistic, held-out split; and record
   the manifest at session setup (`--study-manifest`) so the freeze is RECORDED, not self-declared.

## 7. Wording in the science-fair application to check (the document is not in this repository; not edited)

Change any wording that says or implies: substitution recommendations or "who to sub / rotate"; bench rankings or
"freshest on the bench"; tactical evaluation; performance grades, ratings, MVP or "efficiency" of players; that the
system measures fatigue (it estimates, experimentally); that it learns each athlete's fatigue-performance curve; live
HRV / R-R, SpO2, temperature or jump/impact counts as inputs; the basketball framing. Describe instead: an offline
fatigue estimate from one wearable and one sideline camera, relative to each player's own baseline, explained and
marked "not enough data" when needed, tested against independently timed sprint slowdown.
