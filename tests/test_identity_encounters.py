"""Conservative identity handling at close encounters between tracked people (observation format 3, near_other_track).

The investigation behind it (PROJECT_STATUS.md, camera section): a perturbed replay of the drawn-figure scene showed a
real tracker identity switch (blue's track continuing on the unassigned referee figure after they overlapped) that the
ground-contact rule cannot see. Fixture data only; no footage of any person."""

import json

import pytest

from camera_adapter.observations import Assignment, ObservationError, apply_assignments, dump, load, parse


def header(version=3):
    return json.dumps({"format": "athletrix-camera-observations", "format_version": version, "video": {"width": 960, "height": 540, "fps": 10.0, "name": "t.avi"},
                       "time_base": "video_relative", "extractor": "test"})


def line(frame, track="trk-1", near=False, **extra):
    d = {"frame": frame, "video_time_s": frame / 10, "track_id": track, "u_px": 100.0, "v_px": 200.0, "confidence": 0.9, "athlete_id": None, **extra}
    if near:
        d["near_other_track"] = True
    return json.dumps(d)


def test_format_3_reads_and_writes_near_other_track_only_as_true(tmp_path):
    f = parse([header(), line(0), line(1, near=True)])
    assert [o.near_other_track for o in f.observations] == [False, True]
    dump(f, tmp_path / "o.jsonl")
    rows = [json.loads(x) for x in (tmp_path / "o.jsonl").read_text().splitlines()[1:]]
    assert "near_other_track" not in rows[0] and rows[1]["near_other_track"] is True and load(tmp_path / "o.jsonl").observations[1].near_other_track
    with pytest.raises(ObservationError, match="exactly the keys"):
        parse([header(2), line(0, near=True)])
    with pytest.raises(ObservationError, match="only as true"):
        parse([header(), line(0, near_other_track=False)])


def test_an_assignment_lapses_at_an_encounter_until_the_track_is_confirmed_again():
    f = parse([header()] + [line(n, near=n in (4, 5)) for n in range(10)] + [line(n, track="trk-2") for n in range(10)])
    out = apply_assignments(f, [Assignment("trk-1", "ath-1"), Assignment("trk-2", "ath-2")])
    got = [o.athlete_id for o in out.observations if o.track_id == "trk-1"]
    assert got == ["ath-1"] * 4 + [None] * 6, "from the first encounter frame on the identity is unconfirmed"
    assert out.assignment_report["observations_unassigned_after_encounter"] == 6 and out.assignment_report["per_track"] == {"trk-1": 6}
    assert all(o.athlete_id == "ath-2" for o in out.observations if o.track_id == "trk-2"), "a track without an encounter is unaffected"
    again = apply_assignments(f, [Assignment("trk-1", "ath-1", 0, 5), Assignment("trk-1", "ath-1", 6), Assignment("trk-2", "ath-2")])
    assert [o.athlete_id for o in again.observations if o.track_id == "trk-1"] == ["ath-1"] * 4 + [None] * 2 + ["ath-1"] * 4, "re-confirmed after the encounter"
    trust = apply_assignments(f, [Assignment("trk-1", "ath-1")], trust_tracker_through_encounters=True)
    assert all(o.athlete_id == "ath-1" for o in trust.observations if o.track_id == "trk-1") and trust.assignment_report["rule"] == "trust_tracker_through_encounters"


def test_the_tracker_marks_overlapping_boxes_of_two_tracked_people():
    np = pytest.importorskip("numpy")
    pytest.importorskip("supervision")
    pytest.importorskip("trackers")
    from camera_adapter.person_video import FrameTracker, VideoTrackingConfig, box_iou

    assert box_iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(50 / 150) and box_iou((0, 0, 1, 1), (2, 2, 3, 3)) == 0

    def detector(f):   # two people walk towards each other, overlap around frame 20, and part again
        a, b = 100 + 6 * f, 340 - 6 * f
        return np.array([[a, 200, a + 30, 300], [b, 205, b + 30, 305]], np.float32), np.array([0.9, 0.9], np.float32)
    ft = FrameTracker(VideoTrackingConfig(encounter_release_s=0), detector, 25.0, 960, 540)
    for f in range(40):
        ft.process(f, f / 25.0, f)
    near = sorted({o.frame for o in ft.obs if o.near_other_track})
    assert near and near[0] > 10 and near[-1] < 30, near
    pair = ft.summary()["encounters"]["pairs"]
    assert len(pair) == 1 and pair[0]["first_frame"] == near[0] and pair[0]["last_frame"] == near[-1], "without a release time: exactly the overlapping frames"
    held = FrameTracker(VideoTrackingConfig(), detector, 25.0, 960, 540)     # default release 0.5 s = 12.5 frames at 25 fps
    for f in range(40):
        held.process(f, f / 25.0, f)
    near_held = sorted({o.frame for o in held.obs if o.near_other_track})
    assert near_held[0] == near[0] and near_held[-1] == near[-1] + 12, "the encounter lasts until the boxes have been apart for the release time"
    off = FrameTracker(VideoTrackingConfig(encounter_iou=0), detector, 25.0, 960, 540)
    for f in range(40):
        off.process(f, f / 25.0, f)
    assert not any(o.near_other_track for o in off.obs) and VideoTrackingConfig(encounter_iou=1.5).validate() and VideoTrackingConfig(encounter_release_s=-1).validate()


def test_assignment_files_version_2_keep_identity_status_operator_work_and_the_video_hash(tmp_path):
    from camera_adapter.observations import AssignmentFile, dump_assignments, load_assignment_file
    f = parse([header()] + [line(n) for n in range(6)])
    doc = {"format": "athletrix-track-assignments", "format_version": 2, "video": {"name": "v.mp4", "sha256": "a" * 64},
           "operator_work": {"seconds_spent": 75, "confirmations": 2, "corrections": 0, "uncertain_marks": 1, "unassigned_seconds": 0.3, "tool": "test"},
           "assignments": [{"track_id": "trk-1", "athlete_id": "ath-1", "from_frame": 0, "to_frame": 2, "confirmed_by": "person"},
                           {"track_id": "trk-1", "athlete_id": "ath-1", "from_frame": 3, "identity": "uncertain"}]}
    (tmp_path / "a.json").write_text(json.dumps(doc))
    af = load_assignment_file(tmp_path / "a.json")
    out = apply_assignments(f, af.assignments)
    assert [o.athlete_id for o in out.observations] == ["ath-1"] * 3 + [None] * 3, "an uncertain identity never feeds an athlete's samples"
    assert out.assignment_report["observations_with_uncertain_identity"] == 3 and af.operator_work["seconds_spent"] == 75 and af.video["sha256"] == "a" * 64
    dump_assignments(tmp_path / "b.json", AssignmentFile(af.assignments, af.video, af.operator_work))
    assert load_assignment_file(tmp_path / "b.json").assignments == af.assignments
    bad = {**doc, "format_version": 1}
    (tmp_path / "c.json").write_text(json.dumps(bad))
    with pytest.raises(ObservationError, match="unexpected top-level keys"):
        load_assignment_file(tmp_path / "c.json")
    doc["assignments"][0]["confirmed_by"] = "robot"
    (tmp_path / "d.json").write_text(json.dumps(doc))
    with pytest.raises(ObservationError, match="confirmed_by person or simulated_oracle"):
        load_assignment_file(tmp_path / "d.json")
