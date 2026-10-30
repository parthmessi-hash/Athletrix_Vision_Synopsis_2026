"""Movement quality policy: ground-contact evidence apart from detector confidence, the optional noise rules of the
estimator, value_basis provenance and the engine's handling of predicted samples. Fictional data only."""

import json
import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.core.distance import estimate_distance
from app.models import TrackingSample
from camera_adapter import calibration as cal
from camera_adapter.estimator import AthleteEstimator, EstimatorConfig, Point
from camera_adapter.observations import Observation, ObservationError, ObservationFile, Video, parse
from camera_adapter.person_video import VideoTrackingConfig, ground_status
from camera_adapter.pipeline import AdapterConfig, TimeMapping, process, records

T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
H = [[18.0, 4.0, 120.0], [0.5, -9.0, 420.0], [0.0, 0.004, 1.0]]
FIELD = [(0, 0), (40, 0), (40, 20), (0, 20), (20, 10), (10, 15)]


def image(x, y):
    u, v, _ = cal.apply_h(H, x, y)
    return u, v


@pytest.fixture
def calibration():
    return cal.fit(960, 540, [{"label": f"p{i}", "image": list(image(*f)), "field": list(f)} for i, f in enumerate(FIELD)], [], camera_id="test")


def run(calibration, obs, **cfg):
    config = AdapterConfig(**cfg)
    res = process(ObservationFile(Video(960, 540, 10.0, "t"), "test", obs), calibration, config)
    return res, [r["sample"] for r in records(res, TimeMapping(T0), config, "tag")]


# ---- observation format -----------------------------------------------------------------------------------------------

def header(version):
    return json.dumps({"format": "athletrix-camera-observations", "format_version": version, "video": {"width": 960, "height": 540, "fps": 10.0, "name": "t"},
                       "time_base": "video_relative", "extractor": "test"})


def test_ground_contact_is_optional_in_version_2_and_refused_in_version_1():
    row = {"frame": 0, "video_time_s": 0.0, "track_id": "trk-1", "u_px": 10, "v_px": 10, "confidence": 0.9, "athlete_id": None}
    f = parse([header(2), json.dumps({**row, "ground_contact": "suspect"}), json.dumps({**row, "frame": 1, "video_time_s": 0.1})])
    assert [o.ground_contact for o in f.observations] == ["suspect", None]
    assert parse([header(1), json.dumps(row)]).observations[0].ground_contact is None, "version 1 files are still read"
    with pytest.raises(ObservationError):
        parse([header(1), json.dumps({**row, "ground_contact": "suspect"})])
    with pytest.raises(ObservationError):
        parse([header(2), json.dumps({**row, "ground_contact": "probably"})])


# ---- ground-contact evidence (box-height consistency) --------------------------------------------------------------------

def test_box_height_rule_flags_a_sudden_loss_of_height_and_keeps_its_reference_clean():
    cfg, ref = VideoTrackingConfig(), []
    statuses = [ground_status(ref, h, cfg) for h in [100, 101, 99, 100, 102, 100, 70, 68, 66, 99, 98]]
    assert statuses[:5] == ["unknown"] * 5, "too little reference at the start of a track"
    assert statuses[5] == "consistent" and statuses[6:9] == ["suspect"] * 3 and statuses[9:] == ["consistent", "consistent"]
    assert min(ref) >= 98, "suspect heights never enter the reference, so a long occlusion stays suspect"
    assert ground_status([], 50, VideoTrackingConfig(ground_contact_rule="none")) is None


def walk(n, *, speed=2.0, suspect=(), fps=10.0, bias_px=0.0):
    obs = []
    for f in range(n):
        u, v = image(5.0 + speed * f / fps, 8.0)
        hidden = f in suspect
        obs.append(Observation(f, f / fps, "trk-1", u, v - (bias_px if hidden else 0.0), 0.9, "ath-1", "suspect" if hidden else "consistent"))
    return obs


def test_a_suspect_ground_point_is_excluded_with_a_reason_and_starts_a_new_segment(calibration):
    obs = walk(60, suspect=range(25, 27), bias_px=6.0)           # 0.2 s: shorter than the estimator's gap rule
    res, samples = run(calibration, obs)
    assert res.skipped["ground_contact_suspect"] == 2 and res.per_athlete["ath-1"]["ground_contact_suspect"] == 2
    assert [s["started_by"] for s in res.segments] == ["first_sample", "ground_contact_suspect"]
    model = [TrackingSample.model_validate(s) for s in samples]
    assert estimate_distance(model, 6).continuity_break_count == 1
    assert all(s["value_basis"] == "estimated" for s in samples)
    worst = max(math.dist((s["x_position"], s["y_position"]), (5.0 + 2.0 * o.video_time_s, 8.0)) for o, s in zip(res.outputs, samples))
    assert worst < 0.05, "no biased point reached an estimate"
    res2, samples2 = run(calibration, walk(60, suspect=range(25, 40), bias_px=6.0), ground_contact_policy="use_all")   # a steady 1.5 s offset fits well
    worst2 = max(math.dist((s["x_position"], s["y_position"]), (5.0 + 2.0 * o.video_time_s, 8.0)) for o, s in zip(res2.outputs, samples2))
    assert "ground_contact_suspect" not in res2.skipped and worst2 > 0.3, "detector confidence alone lets the biased ground point through"


# ---- estimator noise rules -----------------------------------------------------------------------------------------------

def noisy_points(speed, sigma, seed=3, n=40, fps=25.0):
    rnd = random.Random(seed)
    return [Point(f / fps, speed * f / fps + rnd.gauss(0, sigma), 5.0 + rnd.gauss(0, sigma), 0.9, "trk-1") for f in range(n)]


@pytest.mark.parametrize("rule", ["significance", "debias"])
def test_noise_rules_never_fire_on_noise_free_motion(rule):
    est = AthleteEstimator(EstimatorConfig(stationary_rule=rule))
    for p in noisy_points(0.4, 0.0):
        est.add(p)
    e, _ = est.estimate()
    assert not e.stationary_within_noise and e.speed_mps == pytest.approx(0.4, abs=1e-6) and e.speed_sigma_mps < 1e-9


def test_noise_rules_are_explicit_on_a_jittering_stationary_target():
    def last(rule):
        est = AthleteEstimator(EstimatorConfig(stationary_rule=rule))
        for p in noisy_points(0.0, 0.05):
            est.add(p)
        return est.estimate()[0]
    plain, sig, deb = last("none"), last("significance"), last("debias")
    assert plain.speed_mps > 0.05 and not plain.stationary_within_noise, "the default reports jitter as motion"
    assert sig.stationary_within_noise and sig.speed_mps == 0.0 and sig.acceleration_mps2 == 0.0
    assert deb.speed_mps < plain.speed_mps and (deb.stationary_within_noise or deb.speed_mps > 0)
    assert sig.x == plain.x and sig.y == plain.y, "positions are never changed by a speed rule"


def test_stationary_decisions_are_counted_in_the_adapter_log(calibration):
    rnd = random.Random(7)
    obs = []
    for f in range(60):
        u, v = image(10.0, 8.0)
        obs.append(Observation(f, f / 10.0, "trk-1", u + rnd.gauss(0, 1.5), v + rnd.gauss(0, 1.5), 0.9, "ath-1"))
    res, samples = run(calibration, obs, estimator=EstimatorConfig(stationary_rule="significance"))
    flagged = sum(1 for o in res.outputs if o.stationary_within_noise)
    assert flagged > 0 and res.per_athlete["ath-1"]["stationary_within_noise"] == flagged
    assert sum(1 for s in samples if s["speed_mps"] == 0.0) == flagged


def test_estimator_configuration_is_validated():
    assert EstimatorConfig(stationary_rule="magic").validate()
    assert EstimatorConfig(stationary_k=0).validate()
    assert AdapterConfig(ground_contact_policy="maybe").validate()


# ---- value basis in the engine ----------------------------------------------------------------------------------------

def ts(t, v, basis=None):
    return TrackingSample(athlete_id="a1", timestamp=T0 + timedelta(seconds=t), x_position=0, y_position=0, speed_mps=v, acceleration_mps2=0, tracking_confidence=1.0, value_basis=basis)


def test_predicted_samples_are_never_integrated_and_unlabelled_streams_are_unchanged():
    d = estimate_distance([ts(0, 2.0, "estimated"), ts(1, 2.0, "estimated"), ts(2, 2.0, "predicted"), ts(3, 2.0, "observed"), ts(4, 2.0, "observed")], 4)
    assert d.estimated_distance_m == 4.0 and d.predicted_interval_count == 2 and d.excluded_interval_count == 2
    legacy = estimate_distance([ts(0, 2.0), ts(1, 2.0), ts(2, 2.0)], 2)
    assert legacy.estimated_distance_m == 4.0 and legacy.predicted_interval_count == 0
    assert "value_basis" not in ts(0, 1.0).model_dump(mode="json")
