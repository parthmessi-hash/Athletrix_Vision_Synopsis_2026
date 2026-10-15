"""Demo simulator: posts one fictional PhysiologicalSample per second for one athlete.

Heart rate follows a smooth rise-and-fall curve. All values are made up and
exist only to exercise the local API.

Usage (backend must already be running):
    python scripts/simulate_athlete.py
    python scripts/simulate_athlete.py --seconds 10 --url http://127.0.0.1:8000
"""

import argparse
import math
import time
from datetime import UTC, datetime

import httpx

ATHLETE_ID = "athlete-1"
DEVICE_ID = "simulator-1"
RESTING_BPM = 110.0
PEAK_RISE_BPM = 40.0


def heart_rate_at(elapsed_s: float, duration_s: float) -> float:
    """Rise from resting to a peak at the midpoint, then fall back to resting."""
    return round(RESTING_BPM + PEAK_RISE_BPM * math.sin(math.pi * elapsed_s / duration_s), 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Backend base URL")
    parser.add_argument("--seconds", type=int, default=60, help="How long to run")
    args = parser.parse_args()

    endpoint = f"{args.url.rstrip('/')}/api/v1/samples/physiological"
    print(f"Sending {args.seconds} samples for {ATHLETE_ID} to {endpoint}")

    with httpx.Client(timeout=5.0) as client:
        for i in range(args.seconds):
            hr = heart_rate_at(i, args.seconds)
            payload = {
                "athlete_id": ATHLETE_ID,
                "timestamp": datetime.now(UTC).isoformat(),
                "source": "simulated",
                "device_id": DEVICE_ID,
                "heart_rate_bpm": hr,
            }
            try:
                response = client.post(endpoint, json=payload)
                print(f"t={i:02d}s  heart_rate_bpm={hr:6.1f}  status={response.status_code}")
            except httpx.HTTPError as exc:
                print(f"t={i:02d}s  heart_rate_bpm={hr:6.1f}  error={exc}")
            if i < args.seconds - 1:
                time.sleep(1)


if __name__ == "__main__":
    main()
