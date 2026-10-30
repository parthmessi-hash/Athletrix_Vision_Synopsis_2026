"""Athletrix camera adapter: observations -> field calibration -> movement estimates -> TrackingSample.

An adapter outside the engine. The core (observations, calibration, estimator, pipeline, evaluation's estimator mode)
needs no third-party packages. Synthetic video generation and marker extraction (synthetic_video.py) and the drawn-figure
scene (synthetic_people.py) need OpenCV and NumPy (requirements-vision.txt); real-video person tracking
(person_video.py) also needs requirements-video.txt and a locally fetched, pinned model file. Nothing here identifies
players: temporary track ids are assigned to athletes only by an explicit operator step.
"""
