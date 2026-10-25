"""Register the fictional demo roster through the athlete API.

Registers two clearly fictional athletes (athlete-1 and athlete-2) using the
existing POST /api/v1/athletes endpoint. Safe to run repeatedly:

  - 201 from the API            -> "registered"
  - 409 and stored == desired   -> "unchanged" (left alone)
  - 409 and stored != desired   -> "conflict"  (reported, never overwritten)

Exit status: 0 when every athlete is registered or unchanged, 1 if any
conflict or API error occurred, 2 if the backend could not be reached.
Registration is separate from the simulator; the simulator still works for
unregistered athlete ids.

Usage (backend must already be running):
    python scripts/register_demo_roster.py
    python scripts/register_demo_roster.py --url http://127.0.0.1:8000
"""

import argparse
import sys
from dataclasses import dataclass

import httpx

API_PREFIX = "/api/v1"

# Fictional people for the local demo only. Two different sports on purpose: the platform is multi-sport.
DEMO_ATHLETES: list[dict] = [
    {"id": "athlete-1", "name": "Alex Example", "jersey_number": 10, "sport": "football", "team_id": "demo-team"},
    {"id": "athlete-2", "name": "Sam Sample", "jersey_number": 23, "sport": "basketball", "team_id": "demo-team"},
]


@dataclass
class Result:
    athlete_id: str
    outcome: str  # registered | unchanged | conflict | error
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in ("registered", "unchanged")


def _describe(a: dict) -> str:
    return f"{a['name']} #{a['jersey_number']} ({a['sport']}, team {a['team_id']})"


def _diff(stored: dict, desired: dict) -> str:
    return ", ".join(f"{k}: stored {stored.get(k)!r} vs desired {desired[k]!r}" for k in desired if stored.get(k) != desired[k])


def register_one(client: httpx.Client, athlete: dict) -> Result:
    """Register one athlete, leaving an identical existing record alone and never overwriting a different one."""
    r = client.post(f"{API_PREFIX}/athletes", json=athlete)
    if r.status_code == 201:
        return Result(athlete["id"], "registered", _describe(athlete))
    if r.status_code == 409:
        g = client.get(f"{API_PREFIX}/athletes/{athlete['id']}")
        if g.status_code != 200:
            return Result(athlete["id"], "error", f"exists but could not be read: HTTP {g.status_code}")
        stored = g.json()
        if stored == athlete:
            return Result(athlete["id"], "unchanged", _describe(athlete))
        return Result(athlete["id"], "conflict", f"already registered with different details ({_diff(stored, athlete)}); left as is")
    return Result(athlete["id"], "error", f"HTTP {r.status_code}: {r.text[:200]}")


def register_roster(client: httpx.Client, athletes: list[dict] = DEMO_ATHLETES) -> list[Result]:
    return [register_one(client, a) for a in athletes]


def exit_code(results: list[Result]) -> int:
    return 0 if all(r.ok for r in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Backend base URL")
    args = parser.parse_args()

    print("FICTIONAL DEMO ROSTER - these athletes are not real people.")
    try:
        with httpx.Client(base_url=args.url.rstrip("/"), timeout=5.0) as client:
            results = register_roster(client)
    except httpx.HTTPError as exc:
        print(f"Cannot reach backend at {args.url}: {type(exc).__name__}: {exc}")
        return 2

    for r in results:
        print(f"{r.athlete_id:<10} {r.outcome:<11} {r.detail}")
    code = exit_code(results)
    print("All demo athletes are registered." if code == 0 else "Some athletes were not registered; see above.")
    return code


if __name__ == "__main__":
    sys.exit(main())
