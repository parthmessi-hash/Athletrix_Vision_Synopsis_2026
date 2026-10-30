"""Vendor-independent wearable integration foundation: provider readers -> normalization -> engine submission.

  readers      (wearable/providers/) turn one provider's output into RawRecord objects and know nothing about Athletrix.
  mapping      (mapping.py) a documented, declarative field/unit mapping from a provider's fields into PhysiologicalSample.
  assignments  (assignments.py) explicit, time-bounded device-to-athlete assignments, including changes.
  clock        (clock.py) how an observation time becomes UTC, with its stated uncertainty, kept apart from receipt time.
  normalize    (normalize.py) applies all of the above, handling missing and invalid values, duplicates, delayed and
               out-of-order records, disconnects and reconnects, and keeps a provenance record per sample and a
               separate device-status history. It never invents a sample or fills a value.
Submission reuses the engine's interchange format and scripts/replay_samples.py. The engine never imports this package.
The providers shipped here are FICTIONAL test formats; no real device is supported yet (see docs/zephyr-integration-checklist.md).
"""
