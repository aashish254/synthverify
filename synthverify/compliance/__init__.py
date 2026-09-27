"""Freedom-compliance gates (spec: ``docs/goal-spec.md`` §2).

Two machine-checkable constraints live here:

* **FC-1** - every dependency the product needs is permissively licensed
  (:mod:`synthverify.compliance.licenses`).
* **FC-3** - every ML model is open-weights *and* open-data, proven by a
  machine-readable manifest (:mod:`synthverify.compliance.model_manifest`).

Both are importable (tests, CI) and callable from the CLI.
"""

from synthverify.compliance.licenses import (
    LicenseStatus,
    ScanReport,
    scan_declared_dependencies,
)
from synthverify.compliance.model_manifest import (
    ManifestIssue,
    ModelManifest,
    load_manifests,
    validate_manifest,
    validate_registry,
)

__all__ = [
    "LicenseStatus",
    "ManifestIssue",
    "ModelManifest",
    "ScanReport",
    "load_manifests",
    "scan_declared_dependencies",
    "validate_manifest",
    "validate_registry",
]
