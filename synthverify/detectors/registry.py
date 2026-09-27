"""Detector registry - the plugin surface of the pipeline.

Lives in its own module (not the package ``__init__``) so detectors can
``from synthverify.detectors.registry import register`` without an import cycle.

To add a new forensic model (e.g. a trained CNN deepfake classifier), subclass
``Detector`` and decorate it with ``@register``. The orchestrator picks it up
automatically for its declared media types; no other code changes are needed.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from synthverify.detectors.base import Detector

_REGISTRY: dict[str, Detector] = {}

_BUILTIN_MODULES = (
    "synthverify.detectors.image_ela",
    "synthverify.detectors.image_metadata",
    "synthverify.detectors.image_frequency",
    "synthverify.detectors.image_noise",
    "synthverify.detectors.audio_spectral",
    "synthverify.detectors.audio_dynamics",
    "synthverify.detectors.audio_metadata",
    "synthverify.detectors.video_temporal",
    "synthverify.detectors.video_metadata",
    "synthverify.detectors.text_stylometry",
)


def register(detector_cls: type[Detector]) -> type[Detector]:
    """Class decorator that instantiates and registers a detector."""
    instance = detector_cls()
    if instance.name in _REGISTRY:
        raise ValueError(f"Duplicate detector name: {instance.name}")
    if not instance.media_types:
        raise ValueError(f"Detector {instance.name} declares no media types")
    _REGISTRY[instance.name] = instance
    return detector_cls


def get(name: str) -> Detector:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown detector '{name}'. Available: {sorted(_REGISTRY)}"
        ) from None


def all_detectors() -> dict[str, Detector]:
    return dict(_REGISTRY)


def detectors_for(media_type: str, requested: list[str] | None = None) -> list[Detector]:
    """Detectors applicable to ``media_type``, optionally filtered by name.

    Raises ``KeyError`` if a requested detector does not exist or cannot handle
    the media type - the API surfaces that as a 422.
    """
    applicable = [d for d in _REGISTRY.values() if media_type in d.media_types]
    if not requested:
        return sorted(applicable, key=lambda d: d.name)
    unknown = [r for r in requested if r not in _REGISTRY]
    if unknown:
        raise KeyError(f"Unknown detector(s): {unknown}. Available: {sorted(_REGISTRY)}")
    selected = [_REGISTRY[r] for r in requested]
    incompatible = [d.name for d in selected if media_type not in d.media_types]
    if incompatible:
        raise KeyError(f"Detector(s) {incompatible} do not support media type '{media_type}'")
    return selected


def load_builtin_detectors() -> None:
    """Import all built-in detector modules so their @register decorators run."""
    for module in _BUILTIN_MODULES:
        importlib.import_module(module)
