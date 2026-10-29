"""Athletrix camera adapter prototype: observations -> field calibration -> movement estimates -> TrackingSample.

An adapter outside the engine. The core (observations, calibration, estimator, pipeline) needs no third-party packages;
synthetic video generation and marker extraction (camera_adapter/synthetic.py) need OpenCV and NumPy
(requirements-vision.txt). Nothing here detects people or identifies players.
"""
