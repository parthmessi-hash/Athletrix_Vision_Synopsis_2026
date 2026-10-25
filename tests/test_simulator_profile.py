"""Effort-rest simulator profile: deterministic, mathematically consistent, delayed toy heart rate, defaults unchanged."""

import importlib.util
import math
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "simulate_athlete.py"
spec = importlib.util.spec_from_file_location("simulate_athlete", SCRIPT)
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


def test_deterministic():
    a = [sim.effort_rest_motion_at(t) for t in range(0, 200)]
    b = [sim.effort_rest_motion_at(t) for t in range(0, 200)]
    assert a == b
    assert sim.effort_rest_heart_rate_series(120) == sim.effort_rest_heart_rate_series(120)


def test_phase_values_hand_calculated():
    assert sim.effort_rest_motion_at(0) == (0.0, 0.0, 0.6)
    assert sim.effort_rest_motion_at(5) == (3.0, 7.5, 0.6)           # 0.6*5, 0.3*25
    assert sim.effort_rest_motion_at(10) == (6.0, 30.0, 0.0)
    assert sim.effort_rest_motion_at(15) == (6.0, 60.0, 0.0)
    assert sim.effort_rest_motion_at(20) == (6.0, 90.0, -0.6)
    assert sim.effort_rest_motion_at(25) == (3.0, 112.5, -0.6)       # 90 + 6*5 - 0.3*25
    assert sim.effort_rest_motion_at(30) == (0.0, 120.0, 0.0)
    assert sim.effort_rest_motion_at(35) == (0.0, 120.0, 0.0)


def test_speed_is_integral_consistent_with_acceleration_and_position():
    dt = 0.05
    for t in [x * dt for x in range(0, int(90 / dt))]:
        v0, x0, a0 = sim.effort_rest_motion_at(t)
        v1, x1, _ = sim.effort_rest_motion_at(t + dt)
        # acceleration matches the slope of speed except at the phase corners
        if all(abs((t % 40) - b) > dt * 2 for b in (10, 20, 30, 40, 0)):
            assert (v1 - v0) / dt == pytest.approx(a0, abs=0.05)
        # position advances by the trapezoid of speed
        assert x1 - x0 == pytest.approx((v0 + v1) / 2 * dt, abs=0.005)


def test_continuity_across_cycle_boundaries_and_no_position_reset():
    for boundary in (40, 80, 120):
        v_before, x_before, _ = sim.effort_rest_motion_at(boundary - 0.001)
        v_at, x_at, _ = sim.effort_rest_motion_at(boundary)
        assert v_before == pytest.approx(0.0, abs=0.01) and v_at == 0.0
        assert x_at == pytest.approx(x_before, abs=0.01)
    xs = [sim.effort_rest_motion_at(t)[1] for t in range(0, 161)]
    assert all(b >= a for a, b in zip(xs, xs[1:]))          # monotonic: never resets
    assert xs[40] == 120.0 and xs[80] == 240.0 and xs[120] == 360.0


def test_heart_rate_is_delayed_relative_to_movement():
    hr = sim.effort_rest_heart_rate_series(80)
    assert hr[0] == 70.0
    # movement starts at t=0; heart rate target only begins moving at HR_DELAY_S, so the first 5 s stay at rest
    assert all(v == 70.0 for v in hr[: sim.HR_DELAY_S])
    assert hr[sim.HR_DELAY_S + 1] > 70.0
    # the sustained phase ends at 20 s; the toy response peaks after that and declines through the stationary phase (30-40)
    peak_t = max(range(40), key=lambda t: hr[t])
    assert 20 < peak_t < 30
    assert hr[30] > hr[35] > hr[39]
    # the lag makes the heart-rate peak come after the speed peak by roughly the delay plus some response time
    speed_peak_start = next(t for t in range(40) if sim.effort_rest_motion_at(t)[0] == 6.0)
    assert peak_t - speed_peak_start >= sim.HR_DELAY_S
    # second cycle repeats the same shape, deterministically
    assert hr[40:80] != hr[0:40]  # it starts from a higher value, so not identical
    assert max(range(40, 80), key=lambda t: hr[t]) - 40 == pytest.approx(peak_t, abs=2)


def test_heart_rate_stays_within_toy_bounds():
    hr = sim.effort_rest_heart_rate_series(400)
    assert min(hr) >= sim.HR_REST_BPM and max(hr) <= sim.HR_PEAK_BPM


def test_effort_rest_at_combines_series_and_motion():
    series = sim.effort_rest_heart_rate_series(50)
    hr, speed, x, acc = sim.effort_rest_at(15, series)
    assert (hr, speed, x, acc) == (series[15], 6.0, 60.0, 0.0)


def test_default_profile_curves_unchanged():
    assert sim.heart_rate_at(0, 60) == 110.0 and sim.heart_rate_at(30, 60) == 150.0
    assert sim.motion_at(0, 60) == (0.0, 0.0, round(8.0 * math.pi / 60, 2))
    assert sim.motion_at(30, 60)[0] == 8.0
    assert (sim.DEFAULT_ATHLETE_ID, sim.DEFAULT_DEVICE_ID) == ("athlete-1", "simulator-1")


def test_cli_defaults_and_choices(capsys):
    import sys
    argv = sys.argv
    try:
        sys.argv = ["simulate_athlete.py", "--help"]
        with pytest.raises(SystemExit):
            sim.main()
    finally:
        sys.argv = argv
    out = capsys.readouterr().out
    assert "--profile {sine,effort-rest}" in out and "--athlete-id" in out and "--device-id" in out and "--with-tracking" in out
