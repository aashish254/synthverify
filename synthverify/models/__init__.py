"""FC-3 manifest directory: one ``<model-id>.json`` per trained detector.

Only metadata lives here (plus optionally operator-fetched weight files). A
JSON file in this directory is a *claim* that a model is free to use and
measurably calibrated; :mod:`synthverify.compliance.model_manifest` decides
whether the claim holds, and ``tests/test_model_manifests.py`` makes it a CI
gate. An empty directory is a valid state - it means no ML detector is wired in
yet, which is exactly today's conformance with FC-3.
"""
