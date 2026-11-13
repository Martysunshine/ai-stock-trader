"""Approved inference bundles and calibration maps for candidate filters (milestone M3).

``bundle.py`` holds the checksummed ``ModelBundle`` and its numpy-only inference,
``calibration.py`` the Platt/isotonic maps and ``codec.py`` the exact decimal-string encoding
of model numbers. Training lives in ``trader.research.training``; this package never imports a
training library or research code (ADR-0027).

Models only *filter* predefined candidates. They can never size orders, change limits or
permissions, or reach the broker (enforced by tests/test_boundaries.py).
"""
