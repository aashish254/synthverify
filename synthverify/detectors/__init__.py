"""Detector package: plugin registry + built-in forensic detectors.

Importing this package eagerly loads every built-in detector module so the
registry is always fully populated for the API, CLI and tests.
"""

from synthverify.detectors.registry import (
    all_detectors,
    detectors_for,
    get,
    load_builtin_detectors,
    register,
)

load_builtin_detectors()

__all__ = [
    "all_detectors",
    "detectors_for",
    "get",
    "load_builtin_detectors",
    "register",
]
