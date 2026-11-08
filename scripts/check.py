"""One local check entry point (also usable as a CI step): required core checks, then optional ones, with every skip said.

USAGE
    python scripts/check.py              # core: backend tests that need no optional package or model, the fictional product demo
    python scripts/check.py --all        # also the optional checks below, where their dependencies are present
    python scripts/check.py --list       # print the checks and what each needs, run nothing

CORE (required; no optional packages, no model files, no network)
    unit and API tests      pytest, deselecting tests that need OpenCV, the video trackers or the model file
    product demo            scripts/product_demo.py run (fictional; isolated temporary database)
OPTIONAL (--all; skipped with the reason when a dependency is missing)
    vision tests            tests marked by their imports of cv2 / numpy / trackers (requirements-vision.txt, requirements-video.txt)
    real-detector tests     need models/yolox_s.onnx (scripts/fetch_person_model.py)
    camera pipeline         scripts/verify_camera_pipeline.py (needs OpenCV; starts its own isolated engine)
    operational workflow    scripts/verify_operational_workflow.py (starts isolated engines on free loopback ports)
    session workflow        scripts/verify_session_workflow.py (session plan, preflight, start/resume/stop, startup failure)
    fatigue pilot           scripts/pilot_rehearsal.py --mode accelerated (fictional; in-process, simulated clock)
The frontend checks are browser pages in the Athletrix_Web repository (tests/timeline_confidence.test.html and
tests/video_review.test.html); this script cannot run them without a browser and says so.
Exit code 0 when every check that ran passed; skipped checks never count as passed.
"""

import argparse
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def have(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def run(cmd: list[str], out) -> bool:
    out("$ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    for line in tail:
        out("    " + line)
    return r.returncode == 0


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="also run the optional checks whose dependencies are present")
    ap.add_argument("--list", action="store_true", help="list the checks and their needs; run nothing")
    args = ap.parse_args(argv)
    vision = have("cv2") and have("numpy")
    video = vision and have("trackers") and have("supervision")
    model = (ROOT / "models" / "yolox_s.onnx").is_file()
    plan = [("core", "backend tests (no optional dependencies)", True, None),
            ("core", "fictional product demo", True, None),
            ("optional", "vision and video tests", args.all and vision, None if vision else "OpenCV/NumPy not installed (requirements-vision.txt)"),
            ("optional", "real-detector tests", args.all and video and model, None if (video and model) else "needs the video packages and models/yolox_s.onnx"),
            ("optional", "camera pipeline through an isolated engine", args.all and vision, None if vision else "OpenCV not installed"),
            ("optional", "operational workflow (isolated engines)", args.all, None),
            ("optional", "session workflow (isolated engines)", args.all, None),
            ("optional", "fatigue pilot rehearsal (accelerated, fictional)", args.all, None),
            ("frontend", "browser test pages (Athletrix_Web)", False, "needs a browser: open tests/timeline_confidence.test.html and tests/video_review.test.html from a loopback server")]
    if args.list:
        for kind, name, _, why in plan:
            out(f"{kind:9} {name}" + (f"  [{why}]" if why else ""))
        return 0
    results = []
    vision_tests = ["tests/test_camera_synthetic.py", "tests/test_person_video.py", "tests/test_bounded_processing.py", "tests/test_intrinsics.py", "tests/test_fetch_person_model.py"]
    core_cmd = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"] + [f"--ignore={t}" for t in vision_tests] + ["--deselect", "tests/test_product_demo.py"]
    results.append(("backend tests (no optional dependencies)", run(core_cmd, out)))
    with tempfile.TemporaryDirectory() as tmp:
        results.append(("fictional product demo", run([PY, "scripts/product_demo.py", "run", "--report", str(Path(tmp) / "demo.json")], out)))
    for kind, name, enabled, why in plan[2:]:
        if not enabled:
            out(f"SKIPPED {name}: {why or 'not requested (use --all)'}")
            results.append((name, None))
            continue
        if name.startswith("vision"):
            results.append((name, run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *vision_tests], out)))
        elif name.startswith("real-detector"):
            results.append((name, run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_person_video.py", "-k", "real"], out)))
        elif name.startswith("camera pipeline"):
            results.append((name, run([PY, "scripts/verify_camera_pipeline.py"], out)))
        elif name.startswith("fatigue pilot"):
            with tempfile.TemporaryDirectory() as tmp:
                results.append((name, run([PY, "scripts/pilot_rehearsal.py", "--mode", "accelerated", "--out", str(Path(tmp) / "pilot")], out)))
        elif name.startswith("operational"):
            results.append((name, run([PY, "scripts/verify_operational_workflow.py"], out)))
        else:
            results.append((name, run([PY, "scripts/verify_session_workflow.py"], out)))
    out("")
    for name, ok in results:
        out(f"{'PASSED ' if ok else 'SKIPPED' if ok is None else 'FAILED '} {name}")
    return 0 if all(ok is not False for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
