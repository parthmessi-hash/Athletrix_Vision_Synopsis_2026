"""Demo simulator: posts fictional samples for one athlete, once per second.

ALL VALUES ARE FICTIONAL DEMO DATA. They are generated from simple curves and
exist only to exercise the local API; they do not come from any real person,
sensor or device.

By default only a PhysiologicalSample (heart rate) is sent each second. With
--with-tracking a TrackingSample is sent alongside it, sharing the same
athlete_id and UTC timestamp, modelling movement along a straight line:

  - speed_mps rises from 0 to a peak at the midpoint and falls back to 0
  - x_position is the distance covered so far (the integral of speed), metres
  - y_position stays at 0
  - acceleration_mps2 is the rate of change of speed (negative while slowing)
  - tracking_confidence is 1.0

--profile effort-rest replaces those curves with a repeating 40 s cycle
(see effort_rest_at for the exact piecewise rules):

  phase        seconds in cycle   speed                       acceleration
  accelerate   0 - 10             0 -> 6 m/s linearly          +0.6 m/s^2
  sustained    10 - 20            6 m/s                        0
  slow         20 - 30            6 -> 0 m/s linearly          -0.6 m/s^2
  stationary   30 - 40            0                            0

  Position is the exact integral of speed and keeps accumulating across
  cycles (120 m per cycle); it never resets. Heart rate is a toy delayed
  response: target(t) = 70 + 100 * speed(t - 5) / 6 bpm, followed through a
  first-order lag with a 5 s time constant, stepped once per second:
  hr(t) = hr(t-1) + (target(t) - hr(t-1)) * (1 - exp(-1/5)), hr(0) = 70.
  So heart rate starts rising about 5 s after movement, peaks a few seconds
  after the sustained phase ends, and declines through the stationary
  phase. This is fictional data built to demonstrate timing on a chart; it
  is not a validated physiological model.

Usage (backend must already be running):
    python scripts/simulate_athlete.py
    python scripts/simulate_athlete.py --with-tracking
    python scripts/simulate_athlete.py --seconds 10 --url http://127.0.0.1:8000
    python scripts/simulate_athlete.py --athlete-id athlete-2 --device-id simulator-2 --with-tracking
    python scripts/simulate_athlete.py --profile effort-rest --with-tracking --seconds 120   # three full cycles

Several instances can run at once with different --athlete-id / --device-id
values; the backend keeps their data separate. Registration is not required.
"""

import argparse
import math
import time
from datetime import UTC, datetime

import httpx

DEFAULT_ATHLETE_ID = "athlete-1"
DEFAULT_DEVICE_ID = "simulator-1"
RESTING_BPM = 110.0
PEAK_RISE_BPM = 40.0
PEAK_SPEED_MPS = 8.0

# ---- effort-rest profile (fictional, for demonstrating timing) ----
CYCLE_SECONDS = 40.0
ACCEL_END, SUSTAIN_END, SLOW_END = 10.0, 20.0, 30.0   # phase boundaries within a cycle; 30-40 is stationary
EFFORT_PEAK_SPEED_MPS = 6.0
EFFORT_ACCEL_MPS2 = EFFORT_PEAK_SPEED_MPS / ACCEL_END   # 0.6
CYCLE_DISTANCE_M = 0.5 * EFFORT_PEAK_SPEED_MPS * ACCEL_END + EFFORT_PEAK_SPEED_MPS * (SUSTAIN_END - ACCEL_END) + 0.5 * EFFORT_PEAK_SPEED_MPS * (SLOW_END - SUSTAIN_END)  # 120 m
HR_REST_BPM, HR_PEAK_BPM = 70.0, 170.0
HR_DELAY_S = 5          # heart-rate target follows the speed from this many seconds earlier
HR_TAU_S = 5.0          # first-order lag time constant, seconds


def heart_rate_at(elapsed_s: float, duration_s: float) -> float:
    """Rise from resting to a peak at the midpoint, then fall back to resting."""
    return round(RESTING_BPM + PEAK_RISE_BPM * math.sin(math.pi * elapsed_s / duration_s), 1)


def motion_at(elapsed_s: float, duration_s: float) -> tuple[float, float, float]:
    """Return (speed m/s, x position m, acceleration m/s^2) for straight-line motion.

    speed(t) = PEAK * sin(pi * t / D); position and acceleration are its exact
    integral and derivative, so the three values are always consistent.
    """
    phase = math.pi * elapsed_s / duration_s
    speed = PEAK_SPEED_MPS * math.sin(phase)
    x_position = PEAK_SPEED_MPS * duration_s / math.pi * (1 - math.cos(phase))
    acceleration = PEAK_SPEED_MPS * math.pi / duration_s * math.cos(phase)
    return round(speed, 2), round(x_position, 2), round(acceleration, 2)


def effort_rest_motion_at(elapsed_s: float) -> tuple[float, float, float]:
    """(speed m/s, cumulative x position m, acceleration m/s^2) at `elapsed_s` for the effort-rest cycle.

    Piecewise within each 40 s cycle (tau = seconds since the cycle started):
      0-10  speed = 0.6 tau,            x = 0.3 tau^2,                        a = +0.6
      10-20 speed = 6,                  x = 30 + 6 (tau - 10),                a = 0
      20-30 speed = 6 - 0.6 (tau - 20), x = 90 + 6 u - 0.3 u^2 (u = tau-20), a = -0.6
      30-40 speed = 0,                  x = 120,                              a = 0
    plus 120 m for every completed cycle, so position is continuous and never resets.
    """
    cycles, tau = divmod(elapsed_s, CYCLE_SECONDS)
    v, a_ = EFFORT_PEAK_SPEED_MPS, EFFORT_ACCEL_MPS2
    if tau < ACCEL_END:
        speed, x, acc = a_ * tau, 0.5 * a_ * tau * tau, a_
    elif tau < SUSTAIN_END:
        speed, x, acc = v, 0.5 * v * ACCEL_END + v * (tau - ACCEL_END), 0.0
    elif tau < SLOW_END:
        u = tau - SUSTAIN_END
        speed, x, acc = v - a_ * u, 0.5 * v * ACCEL_END + v * (SUSTAIN_END - ACCEL_END) + v * u - 0.5 * a_ * u * u, -a_
    else:
        speed, x, acc = 0.0, CYCLE_DISTANCE_M, 0.0
    return round(speed, 3), round(cycles * CYCLE_DISTANCE_M + x, 3), round(acc, 3)


def effort_rest_heart_rate_series(seconds: int) -> list[float]:
    """Toy delayed heart-rate response for t = 0 .. seconds-1, one value per second (deterministic).

    target(t) = HR_REST + (HR_PEAK - HR_REST) * speed(t - HR_DELAY_S) / peak speed  (speed before t=0 is 0)
    hr(0) = HR_REST ;  hr(t) = hr(t-1) + (target(t) - hr(t-1)) * (1 - exp(-1 / HR_TAU_S))
    """
    gain = 1.0 - math.exp(-1.0 / HR_TAU_S)
    hr = HR_REST_BPM
    series = []
    for t in range(seconds):
        lagged_speed = effort_rest_motion_at(t - HR_DELAY_S)[0] if t - HR_DELAY_S >= 0 else 0.0
        target = HR_REST_BPM + (HR_PEAK_BPM - HR_REST_BPM) * lagged_speed / EFFORT_PEAK_SPEED_MPS
        hr = hr + (target - hr) * gain
        series.append(round(hr, 1))
    return series


def effort_rest_at(elapsed_s: int, hr_series: list[float]) -> tuple[float, float, float, float]:
    """(heart rate bpm, speed m/s, x position m, acceleration m/s^2) at whole second `elapsed_s`."""
    speed, x, acc = effort_rest_motion_at(elapsed_s)
    return hr_series[elapsed_s], speed, x, acc


def post(client: httpx.Client, endpoint: str, payload: dict) -> str:
    """POST and return the HTTP status, or a short error description."""
    try:
        return str(client.post(endpoint, json=payload).status_code)
    except httpx.HTTPError as exc:
        return f"error({type(exc).__name__})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Backend base URL")
    parser.add_argument("--seconds", type=int, default=60, help="How long to run")
    parser.add_argument("--with-tracking", action="store_true", help="Also send a TrackingSample each second")
    parser.add_argument("--athlete-id", default=DEFAULT_ATHLETE_ID, help="athlete_id for all samples")
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID, help="device_id for every sample (physiological and tracking)")
    parser.add_argument("--profile", choices=("sine", "effort-rest"), default="sine",
                        help="sine: single rise-and-fall over the run (default); effort-rest: repeating 40 s accelerate/sustain/slow/stationary cycle with a delayed toy heart-rate response")
    args = parser.parse_args()
    athlete_id, device_id = args.athlete_id, args.device_id

    base = args.url.rstrip("/")
    physiological_endpoint = f"{base}/api/v1/samples/physiological"
    tracking_endpoint = f"{base}/api/v1/samples/tracking"

    print("FICTIONAL DEMO DATA - not from any real athlete or device.")
    kinds = "physiological + tracking" if args.with_tracking else "physiological"
    print(f"Sending {args.seconds} {kinds} samples for athlete_id={athlete_id} device_id={device_id} to {base} (profile={args.profile})")
    hr_series = effort_rest_heart_rate_series(args.seconds) if args.profile == "effort-rest" else None

    with httpx.Client(timeout=5.0) as client:
        for i in range(args.seconds):
            timestamp = datetime.now(UTC).isoformat()
            if args.profile == "effort-rest":
                hr, speed, x_position, acceleration = effort_rest_at(i, hr_series)
            else:
                hr = heart_rate_at(i, args.seconds)
            physiological = {
                "athlete_id": athlete_id,
                "timestamp": timestamp,
                "source": "simulated",
                "device_id": device_id,
                "heart_rate_bpm": hr,
            }
            line = (
                f"[{athlete_id}/{device_id}] t={i:02d}s  heart_rate_bpm={hr:6.1f}  "
                f"physiological={post(client, physiological_endpoint, physiological)}"
            )

            if args.with_tracking:
                if args.profile != "effort-rest":
                    speed, x_position, acceleration = motion_at(i, args.seconds)
                tracking = {
                    "athlete_id": athlete_id,
                    "timestamp": timestamp,
                    "x_position": x_position,
                    "y_position": 0.0,
                    "speed_mps": speed,
                    "acceleration_mps2": acceleration,
                    "tracking_confidence": 1.0,
                    "source": "simulated",   # explicit: generated here, from no device
                    "device_id": device_id,
                }
                line += (
                    f"  |  speed_mps={speed:5.2f}  x_position={x_position:7.2f}  "
                    f"acceleration_mps2={acceleration:6.2f}  tracking={post(client, tracking_endpoint, tracking)}"
                )

            print(line)
            if i < args.seconds - 1:
                time.sleep(1)


if __name__ == "__main__":
    main()
