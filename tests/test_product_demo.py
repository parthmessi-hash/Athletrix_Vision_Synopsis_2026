"""The FICTIONAL full-product demo (scripts/product_demo.py) end to end in an isolated temporary database: every
scenario output, the hand-worked checks, persistence, corrections, configuration changes, export/import and removal."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_product_demo_runs_every_scenario_and_check():
    spec = importlib.util.spec_from_file_location("product_demo_test", ROOT / "scripts" / "product_demo.py")
    demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
    lines = []
    assert demo.main(["run"], out=lines.append) == 0, "\n".join(lines)
    assert sum("[ok]" in x for x in lines) == 24 and not any("[FAIL]" in x for x in lines)
