# Zephyr integration checklist

Status on 2026-10-30: **Zephyr is the team's preferred wearable. It has not been purchased, and no API access, sample
data or hardware is available to the project.** The team is waiting for the vendor's response before confirming the
remaining hardware, cost and API/access arrangements. Nothing in Athletrix communicates with any Zephyr software.

Supported path (as described by the vendor): **BioModule → ECHO → OmniSense → OmniSense Live API (local) → Athletrix
adapter.** Athletrix does not communicate with ECHO directly.

Code boundary: `wearable/providers/zephyr.py` (refuses to run and lists what is missing). The shared, vendor-independent
foundation it will plug into (readers, mapping, assignments, clock, normalization, export, replay) is in `wearable/`
and is exercised by two fictional provider formats (`scripts/wearable_tool.py`).

## 1. What the vendor has told us (email from Zephyr/Medtronic sales, 2026-10-05, answering the team's questions)

Correspondence, not verified behaviour:

- 11–20 BioModules can be monitored at the same time through ECHO, with calculated physiological data at 1 Hz.
- The Live API provides calculated parameters such as heart rate, breathing rate, posture/activity and other derived
  metrics. The vendor offered a specification listing every field.
- Individual R-R intervals are **not** available live. They can be captured and exported afterwards for HRV
  analysis. The live data includes two HRV calculations (SDNN and RMSSD, as stated in the email).
- Breathing rate is available at 1 Hz. Raw or higher-frequency respiration and accelerometry are **not** available
  through the Live API.
- A third-party application can consume the OmniSense Live API locally. OmniSense is required; the ECHO gateway is not
  a third-party interface.
- API access is included with an OmniSense Cloud subscription; the vendor quoted a yearly price plus per-seat and
  administrator fees depending on the deployment.
- Data received through the API may be stored in Athletrix's own database and used as inputs to derived metrics.
- A technical evaluation before buying an 11-player system is possible. The vendor attached API documentation and
  said sample data would follow.

## 2. What actual documentation and sample data establish

- **Documentation:** the vendor's API document (version 2.0.4, dated 2018) arrived as an attachment. It carries a
  confidentiality notice, so it is not reproduced or summarised field by field in this public repository. At a high
  level it describes a subscription service that OmniSense Live hosts on the same computer or local network. Its
  transport is specific to the .NET platform, so a vendor-supported client or a small bridge on the OmniSense
  computer will probably be needed. It does not state units, value ranges, missing-value conventions or timestamp
  semantics. It predates the "OmniSense Live API" named in the email, and may not match the current version.
- **Sample payloads:** none. No mapping can be implemented or tested against real data.
- Team members with permission can keep field-level notes in `docs/private/` (ignored by git, never committed).

## 3. Unanswered or unavailable (needed before writing the reader)

1. Permission to use the API documentation for this project, and confirmation that it is the current version.
2. Real Live API sample payloads covering normal data, missing values, a disconnect and reconnect, and low signal.
3. The supported client or transport for a third-party application. Can it be used from Python, or does it need a
   vendor-supported bridge on the OmniSense computer?
4. Units, ranges and missing-value conventions for every field Athletrix would map.
5. Timestamp semantics: whose clock, which time zone, what resolution, and how it relates to receipt time. Also how
   to align it with the camera's clock.
6. Which HRV statistic each live value is (RMSSD or SDNN), its window and its unit. A calculated HRV value is never
   treated as beat intervals.
7. Posture and activity definitions, plus the meaning of any confidence, reliability or "worn" indicators. These
   decide whether such indicators map to `signal_quality`, a skip rule, or provenance only.
8. Temperature: any temperature the API offers is mapped to `skin_temperature_c` only if it is a measured skin
   temperature. An estimated core temperature stays unmapped.
9. Connection, reconnection, battery and signal reporting, for the device-status history.
10. Subscription, seat and licence terms for an evaluation and for a product, and the cost of the hardware.
11. Whether an evaluation kit can be borrowed for the science-fair timeline.

## 4. When the answers arrive

Write the reader, mapping and time parsing in `wearable/providers/zephyr.py` from the documentation only. Build test
fixtures from the real sample payloads, and only if the team may store them in a public repository; otherwise keep
them in `docs/private/` and test locally. Run `scripts/wearable_tool.py validate` and `replay` against an isolated
engine. Report the result as fixture verification until a live OmniSense connection has actually been tested.
