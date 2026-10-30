"""Fetch a pinned person-detector model file once, explicitly (the video tracking command never downloads anything).

USAGE
    python scripts/fetch_person_model.py --list
    python scripts/fetch_person_model.py --model yolox_s [--dest models/]

Prints the URL, size and licence first, downloads to a temporary file next to the destination, checks the size and the
pinned SHA-256, and only then moves it into place (models/ is ignored by git; never commit model weights). A model
without a pinned SHA-256 is downloaded only with --allow-unpinned, and its SHA-256 is printed so you can pin it.

EXIT CODES: 0 fetched or already present and verified · 1 download or verification failed · 2 bad arguments
"""

import argparse
import hashlib
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from camera_adapter.person_video import MODELS, sha256_file  # noqa: E402


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", choices=sorted(MODELS))
    p.add_argument("--dest", type=Path, default=ROOT / "models")
    p.add_argument("--list", action="store_true")
    p.add_argument("--allow-unpinned", action="store_true")
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.list or not args.model:
        for name, m in MODELS.items():
            path = args.dest / f"{name}.onnx"
            state = "not fetched" if not path.exists() else ("present, verified" if m["sha256"] and sha256_file(path) == m["sha256"] else "present, NOT matching the pin" if m["sha256"] else "present (no pin)")
            out(f"{name}: {m['bytes'] / 1e6:.1f} MB, input {m['input_size']}, {m['licence']}; {m['source']}; sha256 {m['sha256'] or 'not pinned'}; {state}")
        return 0 if args.list else 2
    m = MODELS[args.model]
    if m["sha256"] is None and not args.allow_unpinned:
        out(f"REFUSED: {args.model} has no pinned SHA-256; rerun with --allow-unpinned to fetch it and print its SHA-256")
        return 1
    dest = args.dest / f"{args.model}.onnx"
    if dest.exists():
        digest = sha256_file(dest)
        if m["sha256"] is None or digest == m["sha256"]:
            out(f"already present: {dest} (sha256 {digest})")
            return 0
        out(f"REFUSED: {dest} exists but its SHA-256 {digest} differs from the pin; move it away first")
        return 1
    out(f"downloading {m['url']} ({m['bytes'] / 1e6:.1f} MB; licence {m['licence']}) -> {dest}")
    args.dest.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    h = hashlib.sha256()
    try:
        with httpx.stream("GET", m["url"], follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
    except (httpx.HTTPError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        out(f"FAILED: {exc}")
        return 1
    size, digest = tmp.stat().st_size, h.hexdigest()
    if size != m["bytes"] or (m["sha256"] is not None and digest != m["sha256"]):
        tmp.unlink()
        out(f"FAILED: got {size} bytes with SHA-256 {digest}; expected {m['bytes']} bytes" + (f" and {m['sha256']}" if m["sha256"] else ""))
        return 1
    tmp.rename(dest)
    out(f"fetched and verified: {dest} (sha256 {digest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
