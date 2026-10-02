"""FC-3 / AC-FC-3: the model-manifest gate.

The spec's harshest constraint lives here. A model may have MIT weights and
still be disqualified, because the corpus it was trained on is research-only -
so this gate grades the *claim*, not the code:

* every required field must be present and mean something,
* both licences must clear the FC-1 allowlist (LGPL explicitly cannot for weights),
* measured per-demographic-group error rates and a named held-out set are required,
* committed calibration gates must not be breached,
* and every registered ML detector must be covered by a valid manifest.

Each case below asserts the *named reason* too, because an operator who is told
"FAIL" with no explanation cannot fix it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from synthverify.compliance.model_manifest import (
    MANIFEST_SCHEMA_VERSION,
    REQUIRED_FIELDS,
    ManifestIssue,
    ManifestReport,
    ModelManifest,
    license_blockers,
    load_manifest,
    load_manifests,
    models_dir,
    scan,
    validate_manifest,
    validate_registry,
)

PY = sys.executable
PROJECT = Path(__file__).resolve().parent.parent
MODELS = PROJECT / "synthverify" / "models"

_SHA_A = "a" * 64


def manifest(**overrides: Any) -> dict[str, Any]:
    """A complete, shippable manifest; ``overrides`` replaces or adds keys."""
    base: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "id": "unit-test-image-model",
        "detector": "image_deepfake",
        "media_type": "image",
        "license": "Apache-2.0",
        "dataset_license": "CC0-1.0",
        "weights_source": "https://example.org/releases/unit-test-image-model-1.0.onnx",
        "weights_sha256": _SHA_A,
        "runtime": {"engine": "onnxruntime", "device": "cpu"},
        "eval_report": {
            "held_out_set": "example-forgery-corpus/split-2026-06",
            "auc": 0.941,
            "eer": 0.072,
            "ece": 0.028,
            "per_group": [
                {"group": "skin-tone-group-A", "auc": 0.938, "fnr": 0.081},
                {"group": "skin-tone-group-B", "auc": 0.935, "fnr": 0.089},
            ],
        },
        "gates": {"auc": 0.9, "eer": 0.1, "ece": 0.05},
    }
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def reasons(issues: list[ManifestIssue], field: str | None = None) -> str:
    """Concatenated reasons, so a test can assert the *explanation* not just the fail."""
    picked = [i for i in issues if field is None or i.field.endswith(field) or i.field == field]
    return " | ".join(str(i) for i in picked)


# --------------------------------------------------------------------- the happy path


class TestValidManifest:
    def test_a_complete_manifest_has_no_issues(self):
        assert validate_manifest(manifest()) == []

    def test_validated_manifest_exposes_what_the_report_needs(self):
        data = manifest()
        ModelManifest.from_dict(data)  # must not raise on KeyError
        built = ModelManifest(id=data["id"], detector=data["detector"], media_type="image",
                             license="Apache-2.0", dataset_license="CC0-1.0",
                             weights_source=data["weights_source"], weights_sha256=_SHA_A,
                             eval_report=data["eval_report"])
        assert built.name == built.id  # no 'name' key -> falls back to id
        assert built.metrics == {"auc": 0.941, "eer": 0.072, "ece": 0.028}
        assert built.to_dict()["detector"] == "image_deepfake"

    def test_only_the_documented_fields_are_load_bearing(self):
        """Everything else (recipe, model card, dataset source) is advisory.

        ``gates`` is not: AC-DET-3 wants the tolerance committed, so a manifest
        that measures a metric must also promise a bound for it.
        """
        minimal = {key: manifest()[key] for key in (*REQUIRED_FIELDS, "schema_version")}
        issues = validate_manifest(minimal)
        assert issues and all("needs a committed gate" in str(i) for i in issues), reasons(issues)
        assert validate_manifest({**minimal, "gates": manifest()["gates"]}) == []
        for advisory in ("recipe", "model_card", "dataset_source", "weights_file", "runtime"):
            assert advisory not in REQUIRED_FIELDS


class TestShippedTemplate:
    """``template.json.example`` is the thing an operator copies - it must be valid."""

    def _template(self) -> dict[str, Any]:
        path = MODELS / "template.json.example"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_template_is_a_clean_manifest(self):
        data = self._template()
        assert validate_manifest(data, source="template.json.example") == []

    def test_template_names_every_schema_field(self):
        data = self._template()
        assert set(REQUIRED_FIELDS) <= set(data)

    def test_example_suffix_keeps_it_out_of_the_scan(self):
        report = load_manifests(MODELS)
        assert report.manifests == []
        assert report.ok, reasons(report.issues)


# ---------------------------------------------------------------- licence rules


class TestRequiredFields:
    """AC-FC-3 names the fields; an empty one must not count as a claim."""

    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_each_required_field_is_enforced_when_absent_or_blank(self, field):
        for blank in (None, "", "   "):
            issues = validate_manifest(manifest(**{field: blank}))
            assert issues, f"{field}={blank!r} was accepted"
            assert field in reasons(issues), f"{field}={blank!r} -> {reasons(issues)}"

    def test_a_manifest_with_nothing_wrong_reports_nothing(self):
        assert reasons(validate_manifest(manifest())) == ""


class TestLicenceRules:
    @pytest.mark.parametrize(
        "weights_license",
        ["GPL-3.0-or-later", "AGPL-3.0-only", "SSPL-1.0", "BUSL-1.1", "Elastic-2.0", "CC-BY-NC-4.0"],
    )
    def test_disqualified_weights_licences_are_rejected(self, weights_license):
        issues = validate_manifest(manifest(license=weights_license))
        assert issues, weights_license
        assert "disqualified by FC-1/FC-3" in reasons(issues, "license")

    @pytest.mark.parametrize("dataset_license", ["CC-BY-4.0", "CC-BY-NC-4.0", "GPL-3.0-only", ""])
    def test_the_training_corpus_is_graded_too(self, dataset_license):
        """FC-3's usual killer: permissive weights trained on a restricted corpus."""
        issues = validate_manifest(manifest(dataset_license=dataset_license))
        assert issues and "license" in reasons(issues)

    @pytest.mark.parametrize("permissive", ["MIT", "Apache-2.0", "BSD-3-Clause", "CC0-1.0", "CDLA-Permissive-2.0"])
    def test_allowlisted_licences_pass_for_both_layers(self, permissive):
        assert validate_manifest(manifest(license=permissive, dataset_license=permissive)) == []

    def test_lgpl_cannot_lean_on_the_dynamic_linking_caveat(self):
        """Conditional for a dependency; a redistributed weight gets no such pass."""
        issues = validate_manifest(manifest(license="LGPL-2.1-or-later"))
        assert "dynamic-linking caveat" in reasons(issues, "license")
        assert license_blockers("m", "license", "MIT") == []

    def test_an_unidentifiable_licence_fails_closed(self):
        issues = validate_manifest(manifest(license="Our Research Licence v0.9"))
        assert "could not be identified as a licence" in reasons(issues, "license")

    def test_dual_license_with_one_forbidden_branch_still_fails(self):
        issues = validate_manifest(manifest(dataset_license="CC0-1.0 AND CC-BY-NC-4.0"))
        assert issues and "license" in reasons(issues)

    def test_commercial_use_flag_beats_a_nice_looking_id(self):
        issues = validate_manifest(manifest(allows_commercial_use=False))
        assert "FC-3 requires commercial use to be permitted" in reasons(issues, "allows_commercial_use")


class TestWeightsSourceIsPublic:
    @pytest.mark.parametrize(
        "url",
        [
            "https://huggingface.co/models?pipeline_tag=gated",
            "https://example.org/weights?request-access=1",
            "https://example.org/weights (consent required)",
        ],
    )
    def test_access_gated_sources_are_refused(self, url):
        issues = validate_manifest(manifest(weights_source=url))
        assert "looks access-gated" in reasons(issues, "weights_source")

    def test_a_private_scheme_is_refused(self):
        issues = validate_manifest(manifest(weights_source="ftp://internal/weights.onnx"))
        assert "public, non-gated URL" in reasons(issues, "weights_source")

    @pytest.mark.parametrize("url", ["https://example.org/w.onnx", "s3://public-bucket/w.onnx"])
    def test_public_and_operator_mirror_urls_are_accepted(self, url):
        assert validate_manifest(manifest(weights_source=url)) == []


class TestSHA256:
    @pytest.mark.parametrize(
        "bad", ["", "abc123", "A" * 64, "z" * 64, _SHA_A + "f0", None]
    )
    def test_only_a_full_lowercase_digest_is_trusted(self, bad):
        issues = validate_manifest(manifest(weights_sha256=bad))
        assert issues, f"{bad!r} accepted as a sha256"
        assert "sha256" in reasons(issues) or "required by FC-3" in reasons(issues)


# ------------------------------------------------------------------- eval report


class TestEvalReport:
    def test_every_headline_metric_must_be_measured(self):
        for key in ("auc", "eer", "ece"):
            data = manifest()
            del data["eval_report"][key]
            issues = validate_manifest(data)
            assert "REQ-DET-3 requires a measured numeric value" in reasons(issues, key), key

    @pytest.mark.parametrize(
        "key, value",
        [("auc", 1.4), ("auc", -0.2), ("eer", 0.6), ("ece", 0.35), ("ece", -0.1)],
    )
    def test_implausible_numbers_are_rejected(self, key, value):
        data = manifest(gates=None)
        data["eval_report"][key] = value
        issues = validate_manifest(data)
        assert "outside the plausible range" in reasons(issues, key), f"{key}={value}"

    def test_a_chance_level_auc_cannot_ship(self):
        """0.5 AUC is in range for a *number* and useless as a detector."""
        data = manifest()
        data["eval_report"]["auc"] = 0.5
        assert "breaches the committed gate" in reasons(validate_manifest(data), "auc")

    def test_a_gate_at_chance_proves_nothing(self):
        data = manifest(gates={"auc": 0.5, "eer": 0.1, "ece": 0.05})
        assert "at or below chance" in reasons(validate_manifest(data), "auc")

    def test_a_metric_without_a_committed_gate_cannot_regress_silently(self):
        """AC-DET-3: the gate has to be in the manifest, not in someone's memory."""
        data = manifest()
        del data["gates"]["ece"]
        assert "needs a committed gate" in reasons(validate_manifest(data), "ece")

    @pytest.mark.parametrize("groups", [[], None, "light-skinned faces only", [{"auc": 0.9}]])
    def test_group_metrics_are_mandatory_not_advisory(self, groups):
        """A model that only works on light-skinned faces is not a public-interest tool."""
        data = manifest()
        if groups is None:
            del data["eval_report"]["per_group"]
        else:
            data["eval_report"]["per_group"] = groups
        issues = validate_manifest(data)
        assert issues, f"per_group={groups!r} passed"
        assert "per_group" in reasons(issues) or "demographic" in reasons(issues)

    def test_a_named_group_needs_a_measured_error_rate(self):
        data = manifest()
        data["eval_report"]["per_group"] = [{"group": "age-band-60plus"}]
        assert "at least one measured error rate" in reasons(validate_manifest(data))

    def test_metrics_must_name_the_held_out_set_they_came_from(self):
        data = manifest()
        del data["eval_report"]["held_out_set"]
        assert "permissively licensed hold-out set" in reasons(validate_manifest(data))

    def test_a_regressed_metric_breaches_its_committed_gate(self):
        """REQ-DET-3 / AC-DET-3: this is how a calibration regression fails CI."""
        data = manifest()
        data["eval_report"]["auc"] = 0.87  # still plausible, below the committed floor
        assert "breaches the committed gate 0.9" in reasons(validate_manifest(data), "auc")

    @pytest.mark.parametrize("key, worse", [("eer", 0.2), ("ece", 0.09)])
    def test_ece_and_eer_are_ceilings_not_floors(self, key, worse):
        data = manifest()
        data["eval_report"][key] = worse
        assert "breaches the committed gate" in reasons(validate_manifest(data), key)

    def test_gates_may_live_inside_the_eval_report(self):
        data = manifest(gates=None)
        data["eval_report"]["gates"] = {"auc": 0.9, "eer": 0.1, "ece": 0.05}
        assert validate_manifest(data) == []

    def test_a_non_numeric_report_object_is_rejected_wholesale(self):
        assert "must be an object" in reasons(validate_manifest(manifest(eval_report="auc 0.94")))


class TestRuntime:
    def test_a_gpu_only_runtime_breaks_fc2(self):
        issues = validate_manifest(manifest(runtime={"device": "cuda:0"}))
        assert "not CPU-capable" in reasons(issues)

    @pytest.mark.parametrize("device", ["cpu", "cpu+gpu", "CPU,GPU"])
    def test_cpu_capable_devices_are_fine(self, device):
        assert validate_manifest(manifest(runtime={"device": device})) == []

    def test_absent_runtime_means_cpu_only(self):
        assert validate_manifest(manifest(runtime=None)) == []


# ---------------------------------------------------------------- the on-disk gate


class TestLoader:
    def _write(self, dir_path: Path, name: str, payload: Any) -> Path:
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / name
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )
        return path

    def test_a_valid_manifest_file_loads(self, tmp_path):
        self._write(tmp_path, "good.json", manifest())
        report = load_manifests(tmp_path)
        assert report.ok, reasons(report.issues)
        assert [m.id for m in report.manifests] == ["unit-test-image-model"]
        assert report.by_detector("image_deepfake") is report.manifests[0]

    def test_only_the_bad_manifest_is_rejected(self, tmp_path):
        self._write(tmp_path, "good.json", manifest())
        self._write(tmp_path, "bad.json", manifest(license="GPL-3.0-or-later"))
        report = load_manifests(tmp_path)
        assert [m.id for m in report.manifests] == ["unit-test-image-model"]
        assert len(report.issues) == 1
        assert report.issues[0].manifest == "bad.json"
        assert not report.ok

    def test_malformed_json_is_a_named_issue_not_a_crash(self, tmp_path):
        self._write(tmp_path, "broken.json", '{"id": "x", ')
        report = load_manifests(tmp_path)
        assert "unreadable" in reasons(report.issues)
        assert not report.ok

    def test_a_yaml_or_list_file_is_refused(self, tmp_path):
        self._write(tmp_path, "list.json", [{"id": "unit-test"}])
        assert "top level must be a JSON object" in reasons(load_manifests(tmp_path).issues)

    def test_a_missing_models_directory_is_idle_not_broken(self, tmp_path):
        report = load_manifests(tmp_path / "no-here")
        assert report.ok and report.manifests == []

    def test_load_manifest_returns_none_when_it_has_issues(self, tmp_path):
        path = self._write(tmp_path, "bad.json", manifest(media_type="spreadsheet"))
        collected: list[ManifestIssue] = []
        assert load_manifest(path, models_root=tmp_path, collect=collected) is None
        assert "image|audio|video|text" in reasons(collected)

    def test_unknown_schema_version_is_flagged(self, tmp_path):
        self._write(tmp_path, "v2.json", manifest(schema_version="synthverify.model-manifest/v99"))
        assert "unknown manifest schema" in reasons(load_manifests(tmp_path).issues)


class TestWeightsFileIntegrity:
    def _manifest_on_disk(self, dir_path: Path, data: dict[str, Any]) -> ManifestReport:
        (dir_path / "m.json").write_text(json.dumps(data), encoding="utf-8")
        return load_manifests(dir_path)

    def test_fetched_weights_may_not_be_the_wrong_bytes(self, tmp_path):
        (tmp_path / "weights").mkdir()
        (tmp_path / "weights" / "m.onnx").write_bytes(b"some other model")
        report = self._manifest_on_disk(tmp_path, manifest(weights_file="weights/m.onnx"))
        assert "hashes to" in reasons(report.issues)
        assert "manifest promises" in reasons(report.issues)

    def test_matching_bytes_pass(self, tmp_path):
        (tmp_path / "weights").mkdir()
        blob = b"graded weights"
        (tmp_path / "weights" / "m.onnx").write_bytes(blob)
        report = self._manifest_on_disk(
            tmp_path,
            manifest(weights_file="weights/m.onnx", weights_sha256=hashlib.sha256(blob).hexdigest()),
        )
        assert report.ok, reasons(report.issues)

    def test_absent_weights_are_not_a_failure(self, tmp_path):
        """FC-4: the operator fetches weight bytes; CI only validates the claims."""
        report = self._manifest_on_disk(tmp_path, manifest(weights_file="weights/never-downloaded.onnx"))
        assert report.ok, reasons(report.issues)


class TestRegistryCrossCheck:
    """AC-FC-3: a registered model without a valid manifest fails the build."""

    @staticmethod
    def _manifest(detector: str = "image_deepfake") -> ModelManifest:
        return ModelManifest(
            id="m", detector=detector, media_type="image", license="MIT",
            dataset_license="CC0-1.0", weights_source="https://example.org/m",
            weights_sha256=_SHA_A, eval_report={},
        )

    def test_an_ml_detector_with_no_manifest_is_blocked(self):
        issues = validate_registry(["ela", "image_deepfake"], ["image_deepfake"], ManifestReport())
        assert any("no validated manifest" in str(i) for i in issues)

    def test_a_manifest_whose_detector_was_renamed_is_an_issue(self):
        """A stale manifest is a silent loophole: its licence claim describes nothing."""
        issues = validate_registry(["ela"], [], ManifestReport(manifests=[self._manifest("deleted")]))
        assert any("not a registered detector" in str(i) for i in issues)

    def test_a_manifest_for_a_heuristic_detector_is_an_issue(self):
        """Registering a manifest does not make a heuristic an ML model."""
        issues = validate_registry(["ela"], [], ManifestReport(manifests=[self._manifest("ela")]))
        assert any("does not declare ml_model" in str(i) for i in issues)

    def test_a_matching_manifest_and_detector_is_clean(self):
        issues = validate_registry(
            ["image_deepfake"], ["image_deepfake"], ManifestReport(manifests=[self._manifest()])
        )
        assert issues == []

    def test_manifest_issues_propagate_into_the_registry_check(self):
        pre_existing = ManifestReport(issues=[ManifestIssue("m.json", "license", "GPL")])
        issues = validate_registry(["image_deepfake"], ["image_deepfake"], pre_existing)
        assert ManifestIssue("m.json", "license", "GPL") in issues


# ----------------------------------------------------------- this repository


class TestRepositoryConformance:
    def test_models_dir_is_the_shipped_package_directory(self):
        assert models_dir().name == "models"
        assert models_dir().is_dir()
        assert (models_dir() / "README.md").is_file()

    def test_no_builtin_detector_is_ml_so_the_gate_is_idle_not_skipped(self):
        from synthverify.detectors import all_detectors, load_builtin_detectors

        load_builtin_detectors()
        detectors = all_detectors()
        assert detectors, "registry came up empty - the gate would pass for the wrong reason"
        assert [d.name for d in detectors.values() if d.is_ml] == []

    def test_full_gate_is_green_on_this_checkout(self):
        report = scan()
        assert report.ok, reasons(report.issues)
        assert "RESULT: PASS" in report.format_text()

    def test_cli_exits_zero(self):
        result = subprocess.run(
            [PY, "-m", "synthverify.cli", "model-manifests"],
            capture_output=True, text=True, cwd=str(PROJECT), timeout=120, encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        assert "RESULT: PASS" in result.stdout

    def test_cli_json_is_machine_readable(self):
        result = subprocess.run(
            [PY, "-m", "synthverify.cli", "model-manifests", "--json"],
            capture_output=True, text=True, cwd=str(PROJECT), timeout=120, encoding="utf-8",
        )
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["issues"] == []
        assert "validated" in payload
