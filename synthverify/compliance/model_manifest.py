"""FC-3 / REQ-DET-3: model manifests - "open weights or nothing".

Every ML detector in this product must be able to prove four things that
research-grade forensic models usually cannot:

1. the **weights** are under an FC-1-permissive licence that allows commercial use,
2. the **training data** is permissively licensed too (weights inherit the
   licence of the corpus they were trained on - this is where "free" deepfake
   models usually turn out not to be free),
3. the **recipe** is published or citable, so the model can be rebuilt by someone
   who cannot buy it,
4. measured **error rates per demographic group** exist (a model that only works
   on light-skinned faces is not a public-interest tool).

A manifest is a plain JSON file in ``synthverify/models/*.json``. This module
validates them and cross-checks them against the detector registry, so a model
that has not proven all four cannot be wired in: the gate is a test, not a
review comment.

Weight files themselves are **operator-supplied** (FC-4 keeps the pipeline
offline): a manifest may name a local file, and if that file is present its
sha256 must match - but its absence is not a failure.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from synthverify.compliance.licenses import LicenseStatus, classify_expression

MANIFEST_SCHEMA_VERSION = "synthverify.model-manifest/v1"
MODELS_DIRNAME = "models"

REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "detector",
    "media_type",
    "license",
    "dataset_license",
    "weights_source",
    "weights_sha256",
    "eval_report",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ManifestIssue:
    """One reason a model may not be wired into the product."""

    manifest: str
    field: str
    reason: str

    def __str__(self) -> str:
        target = f"{self.manifest}:{self.field}" if self.field else self.manifest
        return f"{target}: {self.reason}"


@dataclass
class ModelManifest:
    """A validated FC-3 claim about one model."""

    id: str
    detector: str
    media_type: str
    license: str
    dataset_license: str
    weights_source: str
    weights_sha256: str
    eval_report: dict[str, Any]
    path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # --- informational accessors used by the report/CLI
    @property
    def name(self) -> str:
        return str(self.raw.get("name") or self.id)

    @property
    def metrics(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for key in ("auc", "eer", "ece"):
            value = self.eval_report.get(key)
            if isinstance(value, (int, float)):
                out[key] = float(value)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> ModelManifest:
        return cls(
            id=str(data["id"]),
            detector=str(data["detector"]),
            media_type=str(data["media_type"]),
            license=str(data["license"]),
            dataset_license=str(data["dataset_license"]),
            weights_source=str(data["weights_source"]),
            weights_sha256=str(data["weights_sha256"]),
            eval_report=dict(data["eval_report"]),
            path=path,
            raw=data,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ManifestReport:
    """All manifests found on disk, plus everything wrong with them."""

    manifests: list[ModelManifest] = field(default_factory=list)
    issues: list[ManifestIssue] = field(default_factory=list)
    scanned_dir: str = ""

    @property
    def ok(self) -> bool:
        return not self.issues

    def by_detector(self, detector: str) -> ModelManifest | None:
        return next((m for m in self.manifests if m.detector == detector), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scanned_dir": self.scanned_dir,
            "validated": [m.id for m in self.manifests],
            "issues": [str(i) for i in self.issues],
        }

    def format_text(self) -> str:
        lines = [f"FC-3 model-manifest gate: {len(self.manifests)} manifest(s) in {self.scanned_dir}"]
        for manifest in self.manifests:
            metrics = ", ".join(f"{k}={v:.3f}" for k, v in sorted(manifest.metrics.items()))
            lines.append(
                f"  ok  {manifest.id:<28} detector={manifest.detector:<20} "
                f"weights={manifest.license} data={manifest.dataset_license} {metrics}"
            )
        for issue in self.issues:
            lines.append(f"  XX  {issue}")
        if not self.manifests and not self.issues:
            lines.append("  --  no ML detectors registered yet; the gate is idle, not skipped")
        lines.append("  RESULT: " + ("PASS" if self.ok else f"FAIL ({len(self.issues)} issue(s))"))
        return "\n".join(lines)


def models_dir(package_dir: Path | str | None = None) -> Path:
    """The manifest directory that ships with the installed package."""
    if package_dir:
        return Path(package_dir)
    return Path(__file__).resolve().parent.parent / MODELS_DIRNAME


def license_blockers(source: str, field_name: str, expression: str) -> list[ManifestIssue]:
    """Reject a licence expression that FC-1/FC-3 cannot carry."""
    status, canonical = classify_expression(expression)
    if status is LicenseStatus.CONDITIONAL:
        return [
            ManifestIssue(
                source,
                field_name,
                f"{canonical} is LGPL-class: model weights cannot lean on the dynamic-linking caveat",
            )
        ]
    if status is LicenseStatus.ALLOWED:
        return []
    reason = (
        f"{expression!r} could not be identified as a licence"
        if status is LicenseStatus.UNKNOWN
        else f"{canonical} is disqualified by FC-1/FC-3"
    )
    return [
        ManifestIssue(
            source,
            field_name,
            reason + "; research-only and non-commercial weights are prohibited (spec OUT-4)",
        )
    ]


def validate_manifest(
    data: dict[str, Any],
    *,
    source: str = "manifest",
    models_root: Path | str | None = None,
) -> list[ManifestIssue]:
    """Return every FC-3/REQ-DET-3 problem with one manifest (empty = shippable)."""
    issues: list[ManifestIssue] = []
    version = data.get("schema_version")
    if version and version != MANIFEST_SCHEMA_VERSION:
        issues.append(ManifestIssue(source, "schema_version", f"unknown manifest schema {version!r}"))

    missing = [
        ManifestIssue(source, name, "required by FC-3 but missing or empty")
        for name in REQUIRED_FIELDS
        if data.get(name) is None or (isinstance(data.get(name), str) and not str(data[name]).strip())
    ]
    if missing:
        return missing

    if data["media_type"] not in {"image", "audio", "video", "text"}:
        issues.append(
            ManifestIssue(source, "media_type", f"must be image|audio|video|text, got {data['media_type']!r}")
        )

    if not _SHA256_RE.match(str(data["weights_sha256"])):
        issues.append(
            ManifestIssue(source, "weights_sha256", "must be 64 lowercase hex chars (sha256 of the weights)")
        )

    issues += license_blockers(source, "license", str(data["license"]))
    issues += license_blockers(source, "dataset_license", str(data["dataset_license"]))

    if data.get("allows_commercial_use") is False:
        issues.append(
            ManifestIssue(source, "allows_commercial_use", "FC-3 requires commercial use to be permitted")
        )

    source_url = str(data["weights_source"])
    if not source_url.startswith(("https://", "http://", "s3://", "file://", "ipfs://")):
        issues.append(
            ManifestIssue(
                source,
                "weights_source",
                "must be a public, non-gated URL or release asset (FC-6)",
            )
        )
    gated = ("gated", "request-access", "consent", "login required")
    if any(token in source_url.lower() for token in gated):
        issues.append(
            ManifestIssue(source, "weights_source", f"URL looks access-gated ({source_url}); FC-6 forbids it")
        )

    issues += _validate_eval(source, data["eval_report"], data.get("gates"))
    issues += _validate_runtime(source, data.get("runtime"))
    issues += _validate_weights_file(
        source,
        data.get("weights_file"),
        str(data["weights_sha256"]),
        models_root,
    )
    return issues


def _validate_eval(source: str, report: Any, gates: Any = None) -> list[ManifestIssue]:
    prefix = f"{source}.eval_report"
    if not isinstance(report, dict):
        return [ManifestIssue(prefix, "", "must be an object with auc, eer, ece and per_group")]
    issues: list[ManifestIssue] = []
    numbers: dict[str, float] = {}
    for key, low, high in (("auc", 0.5, 1.0), ("eer", 0.0, 0.5), ("ece", 0.0, 0.2)):
        value = report.get(key)
        if not isinstance(value, (int, float)):
            issues.append(ManifestIssue(prefix, key, "REQ-DET-3 requires a measured numeric value"))
            continue
        if not low <= float(value) <= high:
            issues.append(ManifestIssue(prefix, key, f"{value} outside the plausible range [{low}, {high}]"))
        else:
            numbers[key] = float(value)
    groups = report.get("per_group")
    if not isinstance(groups, list) or not groups:
        issues.append(
            ManifestIssue(prefix, "per_group", "FC-3 requires measured error rates per demographic group")
        )
    else:
        for idx, group in enumerate(groups):
            if not isinstance(group, dict) or not group.get("group"):
                issues.append(ManifestIssue(prefix, f"per_group[{idx}]", "each entry needs a 'group' label"))
                continue
            if not any(k in group for k in ("auc", "eer", "fpr", "fnr")):
                issues.append(
                    ManifestIssue(prefix, f"per_group[{idx}]", "group needs at least one measured error rate")
                )
    if not report.get("held_out_set"):
        issues.append(
            ManifestIssue(prefix, "held_out_set", "metrics must name the permissively licensed hold-out set")
        )
    committed: dict[str, Any] = {}
    if isinstance(gates, dict):
        committed.update(gates)
    if isinstance(report.get("gates"), dict):
        committed.update(report["gates"])
    for key, value in numbers.items():
        bound = committed.get(key)
        if not isinstance(bound, (int, float)):
            issues.append(
                ManifestIssue(prefix, key, "REQ-DET-3 needs a committed gate for every measured metric")
            )
            continue
        floor_above_chance = key == "auc" and float(bound) <= 0.5
        if floor_above_chance:
            issues.append(
                ManifestIssue(prefix, key, f"gate {bound} is at or below chance: it proves nothing")
            )
            continue
        # AC-DET-3: a metric that regressed past its committed gate fails CI.
        worse = value > float(bound) if key in ("eer", "ece") else value < float(bound)
        if worse:
            issues.append(
                ManifestIssue(prefix, key, f"measured {value} breaches the committed gate {bound}")
            )
    return issues


def _validate_runtime(source: str, runtime: Any) -> list[ManifestIssue]:
    if runtime is None:
        return []  # default is CPU-only, which is what REQ-DET-2 demands
    if not isinstance(runtime, dict):
        return [ManifestIssue(f"{source}.runtime", "", "must be an object")]
    device = str(runtime.get("device", "cpu")).lower()
    if device not in {"cpu", "cpu+gpu", "cpu,gpu"}:
        return [
            ManifestIssue(
                source,
                "runtime.device",
                f"{device!r} is not CPU-capable: FC-2 forbids a GPU/driver requirement",
            )
        ]
    return []


def _validate_weights_file(
    source: str, weights_file: Any, expected_sha: str, models_root: Any
) -> list[ManifestIssue]:
    """If the operator has fetched the weights, they must be the graded bytes."""
    if not weights_file or not isinstance(weights_file, str):
        return []
    base = Path(models_root) if models_root else Path(".")
    path = (base / weights_file).resolve()
    if not path.is_file():
        return []
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected_sha:
        return [
            ManifestIssue(
                source,
                "weights_file",
                f"{weights_file} hashes to {digest[:12]}…, manifest promises {expected_sha[:12]}…",
            )
        ]
    return []


def load_manifest(
    path: Path, *, models_root: Path | None = None, collect: list[ManifestIssue] | None = None
) -> ModelManifest | None:
    """Parse and validate one manifest file; ``None`` when it has issues."""
    source = path.name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if collect is not None:
            collect.append(ManifestIssue(source, "", f"unreadable: {exc}"))
        return None
    if not isinstance(data, dict):
        if collect is not None:
            collect.append(ManifestIssue(source, "", "top level must be a JSON object"))
        return None
    root = Path(models_root) if models_root else path.parent
    issues = validate_manifest(data, source=source, models_root=root)
    if collect is not None:
        collect.extend(issues)
    if issues:
        return None
    return ModelManifest.from_dict(data, path=path)


def load_manifests(directory: Path | str | None = None) -> ManifestReport:
    """Validate every ``*.json`` manifest in the models directory."""
    target = Path(directory) if directory else models_dir()
    report = ManifestReport(scanned_dir=str(target))
    if not target.is_dir():
        return report
    for path in sorted(target.glob("*.json")):
        manifest = load_manifest(path, models_root=target, collect=report.issues)
        if manifest is not None:
            report.manifests.append(manifest)
    return report


def validate_registry(
    detector_names: Iterable[str],
    ml_detector_names: Iterable[str],
    report: ManifestReport,
) -> list[ManifestIssue]:
    """Cross-check the registry against the manifests, both directions.

    * every ML detector (``Detector.ml_model`` set) needs a valid manifest whose
      ``detector`` field names it - otherwise FC-3 is unproven, so it cannot run;
    * every manifest must point at a detector that actually exists, otherwise the
      licence claim describes nothing (a stale manifest is a silent loophole).
    """
    detectors = set(detector_names)
    issues: list[ManifestIssue] = list(report.issues)
    covered: set[str] = set()
    by_detector = {m.detector: m for m in report.manifests}

    for name in ml_detector_names:
        manifest = by_detector.get(name)
        if manifest is None:
            issues.append(
                ManifestIssue(
                    name,
                    "detector",
                    "is an ML detector with no validated manifest: FC-3 blocks it from running",
                )
            )
            continue
        covered.add(manifest.id)

    for manifest in report.manifests:
        if manifest.detector not in detectors:
            issues.append(
                ManifestIssue(
                    manifest.id,
                    "detector",
                    f"names '{manifest.detector}', which is not a registered detector",
                )
            )
        elif manifest.detector not in set(ml_detector_names):
            issues.append(
                ManifestIssue(
                    manifest.id,
                    "detector",
                    f"'{manifest.detector}' is registered but does not declare ml_model",
                )
            )
    return issues


def scan(directory: Path | str | None = None) -> ManifestReport:
    """Full FC-3 gate: manifests on disk cross-checked against the registry."""
    from synthverify.detectors import all_detectors  # local import: avoids a cycle

    report = load_manifests(Path(directory) if directory else None)
    detectors = all_detectors()
    ml = [d.name for d in detectors.values() if d.is_ml]
    report.issues = validate_registry(list(detectors), ml, report)
    return report
