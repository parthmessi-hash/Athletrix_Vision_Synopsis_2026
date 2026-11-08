"""The display-delay analysis: coalesced samples take the first covering update's delay, uncovered samples are missing
updates, an older response shown late never covers newer samples, and the share within 2 s."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pilot_display_delay as dd  # noqa: E402


def test_coalesced_missing_and_late_older_responses():
    # receipts every second from 1000 ms; updates: at 2500 covering 2000; at 3600 covering 3000 (coalesces 3000);
    # a late OLDER response at 5000 covering only 2000; an outage, then 9000 covering 8000
    display = [[2500, 2000, 10], [3600, 3000, 10], [5000, 2000, 10], [9000, 8000, 10], [20000, 19000, 10]]
    rec = [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000]
    r = dd.analyze(display, rec, tail_s=5)
    assert r["samples"] == 7, "receipts from the first update up to 5 s before the last"
    # 3000 -> 3600 (0.6 s); 4000..8000 -> first update covering them is at 9000 (5, 4, 3, 2, 1 s); 9000 -> 20000 (11 s)
    assert r["displayed"] == 7 and r["missing_updates"] == 0
    assert r["delay_s"]["max"] == 11.0 and r["delay_s"]["median"] == 3.0 and r["delay_s"]["share_within_2s"] == round(3 / 7, 4)
    assert r["clock_mapping_uncertainty_s"] == 0.01 and r["longest_gap_between_updates_s"] == 11.0


def test_samples_never_covered_are_missing_updates():
    r = dd.analyze([[1000, 500, 5], [10000, 600, 5]], [700, 800, 900, 1500, 2000], tail_s=1)
    assert r["missing_updates"] == r["samples"] == 2 and r["delay_s"] is None, "1500 and 2000 fall in the window and no update covers them"


def test_receipts_are_compared_at_the_browser_millisecond_resolution():
    r = dd.analyze([[1000, 500, 5], [1500, 1200, 5], [9000, 1200, 5]], [1200.198], tail_s=1)
    assert r["displayed"] == 1 and r["delay_s"]["max"] == 0.3, "a receipt with microseconds is covered by the update that shows its millisecond"


def test_samples_while_the_page_was_not_drawn_are_not_observable_never_a_delay():
    """A paint heartbeat (animation frames) separates real display delay from time the window was not drawn: a sample
    received while the display slept or the window was covered is NOT OBSERVABLE, not a 60 s delay."""
    beats = [k * 100.0 for k in range(0, 101)] + [70_000 + k * 100.0 for k in range(0, 101)]   # drawn 0-10 s, dark 10-70 s, drawn 70-80 s
    display = [[t + 300, t, 5] for t in range(1000, 10_000, 1000)] + [[71_000, 60_000, 5]] + [[t + 300, t, 5] for t in range(72_000, 80_000, 1000)]
    receipts = [float(t) for t in range(1000, 10_000, 1000)] + [30_000.0, 60_000.0] + [float(t) for t in range(72_000, 75_000, 1000)]
    r = dd.analyze(display, receipts, tail_s=1, painted=beats)
    assert r["visibility"]["samples_not_observable"] == 2 and r["displayed"] == 11   # the 1 s receipt precedes the first display
    assert r["delay_s"]["max"] == 0.3 and r["delay_s"]["share_within_2s"] == 1.0
    assert r["visibility"]["painted_s"] == 17.0   # drawn time inside the observation window 1.3-78.3 s and r["visibility"]["painted_spans"] == 2
    old = dd.analyze(display, receipts, tail_s=1)
    assert old["visibility"] is None and old["delay_s"]["max"] > 40, "without the heartbeat the dark minute looks like a delay"
