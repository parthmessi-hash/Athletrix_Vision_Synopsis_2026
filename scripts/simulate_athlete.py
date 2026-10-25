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

Usage (backend must already be running):
    python scripts/simulate_athlete.py
    python scripts/simulate_athlete.py --with-tracking
    python scripts/simulate_athlete.py --seconds 10 --url http://127.0.0.1:8000
    python scripts/simulate_athlete.py --athlete-id athlete-2 --device-id simulator-2 --with-tracking

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
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID, help="device_id for physiological samples")
    args = parser.parse_args()
    athlete_id, device_id = args.athlete_id, args.device_id

    base = args.url.rstrip("/")
    physiological_endpoint = f"{base}/api/v1/samples/physiological"
    tracking_endpoint = f"{base}/api/v1/samples/tracking"

    print("FICTIONAL DEMO DATA - not from any real athlete or device.")
    kinds = "physiological + tracking" if args.with_tracking else "physiological"
    print(f"Sending {args.seconds} {kinds} samples for athlete_id={athlete_id} device_id={device_id} to {base}")

    with httpx.Client(timeout=5.0) as client:
        for i in range(args.seconds):
            timestamp = datetime.now(UTC).isoformat()
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
                speed, x_position, acceleration = motion_at(i, args.seconds)
                tracking = {
                    "athlete_id": athlete_id,
                    "timestamp": timestamp,
                    "x_position": x_position,
                    "y_position": 0.0,
                    "speed_mps": speed,
                    "acceleration_mps2": acceleration,
                    "tracking_confidence": 1.0,
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
