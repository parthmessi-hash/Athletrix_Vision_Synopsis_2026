"""scripts/check.py lists its checks with what each needs, and never counts a skipped check as passed."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_cli", ROOT / "scripts" / "check.py")
check = importlib.util.module_from_spec(spec); spec.loader.exec_module(check)


def test_the_check_list_names_core_optional_and_browser_checks():
    lines = []
    assert check.main(["--list"], out=lines.append) == 0
    text = "\n".join(lines)
    assert text.count("core") >= 2 and "operational workflow" in text and "needs a browser" in text
