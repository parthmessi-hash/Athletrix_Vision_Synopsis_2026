"""scripts/demo_launcher.py: roster, frontend copy, ports, feeding with a fake clock, and startup/cleanup with mocked processes.

No real backend or frontend process is started here and nothing sleeps for real. All athletes and values are fictional.
"""

import importlib.util
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dl = load("demo_launcher")


def fake_frontend(tmp_path: Path, api_base='"http://127.0.0.1:8000"') -> Path:
    src = tmp_path / "web-src"
    src.mkdir()
    (src / "index.html").write_text("<html><script>var API_BASE = String(CONFIG.apiBase || " + api_base + ");</script></html>", encoding="utf-8")
    (src / "images").mkdir()
    (src / "images" / "logo.txt").write_text("fictional logo", encoding="utf-8")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return src


# ---- roster and samples --------------------------------------------------------------------------------------------

def test_demo_roster_is_fictional_multi_sport_and_distinct():
    roster = dl.demo_roster(7)
    assert [a["id"] for a in roster] == [f"demo-athlete-{i:02d}" for i in range(1, 8)]
    assert all(a["name"].startswith("Fictional Player") and a["team_id"] == "demo-team" for a in roster)
    assert len({a["sport"] for a in roster}) >= 3 and len({a["jersey_number"] for a in roster}) == 7


def test_demo_samples_reuse_the_simulator_profile_and_are_explicitly_simulated():
    ts = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)
    a = dl.demo_samples(0, 3, 60, ts)
    b = dl.demo_samples(1, 3, 60, ts)
    assert a["physiological"]["source"] == "simulated" and a["tracking"]["source"] == "simulated"
    assert a["physiological"]["device_id"] == "fictional-wearable-01" and a["tracking"]["device_id"] == "fictional-tracker-01"
    assert a["physiological"]["heart_rate_bpm"] != b["physiological"]["heart_rate_bpm"], "athletes are phase-shifted on the same deterministic profile"
    assert a == dl.demo_samples(0, 3, 60, ts)
    st = dl.demo_status(2, 10, ts)
    assert st["device_id"] == "fictional-wearable-03" and st["connected"] is True and st["battery_percent"] == 98.0


# ---- frontend copy ----------------------------------------------------------------------------------------------------

def test_frontend_copy_is_pointed_at_the_demo_backend_and_the_source_is_untouched(tmp_path):
    src = fake_frontend(tmp_path)
    before = (src / "index.html").read_text(encoding="utf-8")
    copy = dl.prepare_frontend_copy(src, tmp_path / "web", "http://127.0.0.1:54321")
    assert '"http://127.0.0.1:54321"' in copy.read_text(encoding="utf-8") and '"http://127.0.0.1:8000"' not in copy.read_text(encoding="utf-8")
    assert (src / "index.html").read_text(encoding="utf-8") == before, "the repository copy is never edited"
    assert (tmp_path / "web" / "images" / "logo.txt").is_file() and not (tmp_path / "web" / ".git").exists(), "assets are copied, .git is not"


def test_frontend_copy_errors_are_clear(tmp_path):
    with pytest.raises(FileNotFoundError, match="no index.html"):
        dl.prepare_frontend_copy(tmp_path / "missing", tmp_path / "web", "http://127.0.0.1:1")
    src = fake_frontend(tmp_path, api_base='"http://somewhere.else:9"')
    with pytest.raises(RuntimeError, match="does not contain the default apiBase"):
        dl.prepare_frontend_copy(src, tmp_path / "web2", "http://127.0.0.1:1")


def test_real_frontend_index_can_be_pointed_at_a_demo_backend(tmp_path):
    frontend = dl.DEFAULT_FRONTEND
    if not (frontend / "index.html").is_file():
        pytest.skip("the Athletrix_Web checkout is not next to this repository")
    copy = dl.prepare_frontend_copy(frontend, tmp_path / "web", "http://127.0.0.1:54321")
    text = copy.read_text(encoding="utf-8")
    assert text.count('"http://127.0.0.1:54321"') >= 1 and '"http://127.0.0.1:8000"' not in text


# ---- ports --------------------------------------------------------------------------------------------------------------

def test_occupied_port_is_refused_not_taken_over():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        with pytest.raises(RuntimeError, match="already in use"):
            dl.choose_port(port, "backend")
    assert dl.choose_port(None, "backend") > 1024
    assert dl.port_available(dl.free_port())


# ---- feeding with a fake clock ------------------------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 50.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def test_feed_team_sends_both_streams_per_athlete_per_second_and_statuses_every_five_seconds():
    clock = FakeClock()
    sent = []
    roster = dl.demo_roster(3)
    stats = dl.feed_team("http://demo", roster, 12, with_status=True, post=lambda path, body: sent.append((path, body)) or True, clock=clock, sleep=clock.sleep,
                         now=lambda: datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC), out=lambda s: None)
    assert stats.seconds == 12 and stats.accepted == {"physiological": 36, "tracking": 36, "status": 9} and sum(stats.failed.values()) == 0
    assert sum(1 for p, _ in sent if p.endswith("/physiological")) == 36 and sum(1 for p, _ in sent if p.endswith("/tracking")) == 36
    statuses = [b for p, b in sent if p.endswith("/devices/status")]
    assert len(statuses) == 9 and all(b["device_id"].startswith("fictional-wearable-") for b in statuses)
    assert all(b["source"] == "simulated" for p, b in sent if "/samples/" in p)
    assert all(s > 0 for s in clock.slept) and len(clock.slept) == 11, "ticks after the first wait for their due time on the monotonic clock"


def test_feed_team_counts_failures_without_retrying_or_inventing_successes():
    clock = FakeClock()
    calls = {"n": 0}

    def post(path, body):
        calls["n"] += 1
        return not path.endswith("/tracking")
    stats = dl.feed_team("http://demo", dl.demo_roster(2), 3, with_status=False, post=post, clock=clock, sleep=clock.sleep, out=lambda s: None)
    assert stats.accepted == {"physiological": 6, "tracking": 0, "status": 0} and stats.failed["tracking"] == 6 and calls["n"] == 12


# ---- startup, failure and cleanup with mocked processes -----------------------------------------------------------------

class FakeBackend:
    instances = []

    def __init__(self, port=None, log_level="warning", cors_origins=None, retention_caps=None):
        self.port = port or 50000
        self.cors_origins = cors_origins
        self.retention_caps = dict(retention_caps or {})
        self.db_path = Path("/tmp/fake.db")
        self.proc = None
        self.started = False
        self.cleaned = False
        self.fail = False
        FakeBackend.instances.append(self)

    def start(self):
        if self.fail:
            raise RuntimeError("backend exited early with code 3")
        self.started = True
        self.proc = type("P", (), {"pid": 4242})()

    def cleanup(self):
        self.cleaned = True


class FakeStatic:
    instances = []

    def __init__(self, directory, port):
        self.directory, self.port, self.url = directory, port, f"http://localhost:{port}"
        self.started = False
        self.stopped = False
        FakeStatic.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def mocked(monkeypatch, tmp_path):
    FakeBackend.instances.clear(); FakeStatic.instances.clear()
    monkeypatch.setattr(dl, "IsolatedBackend", FakeBackend)
    monkeypatch.setattr(dl, "StaticServer", FakeStatic)
    monkeypatch.setattr(dl, "register_demo_roster", lambda base_url, roster, client=None: (len(roster), 0))
    monkeypatch.setattr(dl, "feed_team", lambda *a, **k: dl.FeedStats(seconds=1, accepted={"physiological": 2, "tracking": 2, "status": 0}))
    return fake_frontend(tmp_path)


def test_main_starts_both_servers_registers_feeds_prints_the_url_and_cleans_up(mocked):
    lines = []
    code = dl.main(["--frontend", str(mocked), "--athletes", "2", "--duration", "1", "--exit-after-demo"], out=lines.append)
    assert code == 0
    b, s = FakeBackend.instances[0], FakeStatic.instances[0]
    assert b.started and s.started and b.cleaned and s.stopped
    assert b.cors_origins == [f"http://localhost:{s.port}", f"http://127.0.0.1:{s.port}"], "CORS only for the demo's own frontend port"
    assert any(f"OPEN THE DASHBOARD:  http://localhost:{s.port}/#demo" in l for l in lines)
    assert any("fictional athletes" in l.lower() for l in lines) and any("Data generation finished" in l for l in lines)
    assert '"http://127.0.0.1:' + str(b.port) + '"' in (Path(s.directory) / "index.html").read_text(encoding="utf-8") if Path(s.directory).exists() else True


def test_main_missing_frontend_and_bad_arguments_start_nothing(mocked, tmp_path):
    lines = []
    assert dl.main(["--frontend", str(tmp_path / "nowhere")], out=lines.append) == 2 and any("Frontend folder not found" in l for l in lines)
    assert dl.main(["--frontend", str(mocked), "--athletes", "0"], out=lines.append) == 2
    assert FakeBackend.instances == [] and FakeStatic.instances == []


def test_main_refuses_an_occupied_port_before_starting_anything(mocked):
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0)); held.listen(1)
        port = held.getsockname()[1]
        lines = []
        assert dl.main(["--frontend", str(mocked), "--backend-port", str(port)], out=lines.append) == 1
    assert any("already in use" in l for l in lines) and FakeBackend.instances == []


def test_main_backend_start_failure_is_reported_and_cleaned_up(mocked, monkeypatch):
    original = FakeBackend.start

    def failing(self):
        raise RuntimeError("backend exited early with code 3")
    monkeypatch.setattr(FakeBackend, "start", failing)
    lines = []
    assert dl.main(["--frontend", str(mocked), "--exit-after-demo"], out=lines.append) == 1
    assert any("Startup failed: backend exited early with code 3" in l for l in lines)
    assert FakeBackend.instances[0].cleaned and FakeStatic.instances == [], "the frontend server was never started; the backend owner was cleaned up"
    monkeypatch.setattr(FakeBackend, "start", original)


def test_main_keyboard_interrupt_while_waiting_cleans_up(mocked):
    lines = []

    def interrupt():
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        dl.main(["--frontend", str(mocked), "--duration", "1"], out=lines.append, wait_for_interrupt=interrupt)
    assert FakeBackend.instances[0].cleaned and FakeStatic.instances[0].stopped
    assert any("Press Ctrl-C to stop" in l for l in lines)


def test_demo_temporary_folder_is_created_and_removed(mocked):
    demo = dl.Demo(mocked, None, None)
    assert demo.tmp_path.is_dir() and demo.backend_port != demo.frontend_port
    demo.cleanup()
    assert not demo.tmp_path.exists() and FakeBackend.instances[-1].cleaned
    demo.cleanup()  # idempotent


def test_main_passes_explicit_caps_only_to_the_demo_backend(mocked):
    lines = []
    assert dl.main(["--frontend", str(mocked), "--duration", "1", "--exit-after-demo", "--tracking-cap", "3000"], out=lines.append) == 0
    assert FakeBackend.instances[-1].retention_caps == {"tracking": 3000}
    assert any("Retention caps for this demo backend only" in l and "tracking 3000" in l for l in lines)
    assert dl.main(["--frontend", str(mocked), "--duration", "1", "--exit-after-demo"], out=lambda s: None) == 0
    assert FakeBackend.instances[-1].retention_caps == {}, "no option: the backend's own defaults"
    assert dl.main(["--frontend", str(mocked), "--physiological-cap", "0"], out=lines.append) == 2
