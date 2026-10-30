"""scripts/fetch_person_model.py without any network access: listing, refusals and the pinned-file checks."""

import hashlib
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_person_model_cli", ROOT / "scripts" / "fetch_person_model.py")
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)


def run(argv):
    lines = []
    return cli.main(argv, out=lines.append), lines


def test_list_reports_pins_licences_and_state_without_downloading(tmp_path):
    dest = tmp_path / "models"
    code, lines = run(["--list", "--dest", str(dest)])
    assert code == 0 and any(l.startswith("yolox_s:") and "Apache-2.0" in l and "not fetched" in l for l in lines)
    assert not dest.exists()


def test_an_unpinned_model_is_refused_and_a_mismatching_file_is_never_replaced(tmp_path, monkeypatch):
    monkeypatch.setitem(cli.MODELS, "yolox_tiny", {**cli.MODELS["yolox_tiny"], "sha256": None})
    code, lines = run(["--model", "yolox_tiny", "--dest", str(tmp_path)])
    assert code == 1 and "no pinned SHA-256" in lines[-1]
    (tmp_path / "yolox_s.onnx").write_bytes(b"something else")
    monkeypatch.setattr(cli.httpx, "stream", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
    code, lines = run(["--model", "yolox_s", "--dest", str(tmp_path)])
    assert code == 1 and "differs from the pin" in lines[-1] and (tmp_path / "yolox_s.onnx").read_bytes() == b"something else"


def test_a_present_verified_file_is_not_downloaded_again(tmp_path, monkeypatch):
    data = b"pretend model"
    monkeypatch.setitem(cli.MODELS, "yolox_s", {**cli.MODELS["yolox_s"], "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    (tmp_path / "yolox_s.onnx").write_bytes(data)
    monkeypatch.setattr(cli.httpx, "stream", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
    code, lines = run(["--model", "yolox_s", "--dest", str(tmp_path)])
    assert code == 0 and lines[-1].startswith("already present")
