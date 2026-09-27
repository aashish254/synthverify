# Model manifests (FC-3 gate)

Every **trained** detector in SynthVerify must ship a manifest here before it can
be registered. Heuristic detectors (the 11 built-ins) need none - they contain no
third-party weights.

The gate is `synthverify/compliance/model_manifest.py`, run by:

```bash
./.venv/bin/python -m synthverify.cli model-manifests        # human readable
./.venv/bin/python -m synthverify.cli model-manifests --json  # for CI
./.venv/bin/pytest tests/test_model_manifests.py -q           # the same gate as a test
```

## Why a manifest instead of a code review comment

`docs/goal-spec.md` FC-3 is the constraint that most "open" forensics models fail:
the weights may be MIT while the corpus they were trained on is research-only, and
in that case the weights are not free. The licence question cannot be settled by
reading the code, so it is settled by a machine-checkable claim next to it. An ML
detector that cannot produce a valid manifest is not wired in - see `OUT-4`.

## Schema (`synthverify.model-manifest/v1`)

| Field | Required | Rule enforced |
|---|---|---|
| `schema_version` | - | must equal `synthverify.model-manifest/v1` if present |
| `id` | yes | manifest name, referenced by `Detector.ml_model` |
| `detector` | yes | must name a **registered** detector (both directions are checked) |
| `media_type` | yes | `image` \| `audio` \| `video` \| `text` |
| `license` | yes | weights licence; must pass the FC-1 allowlist. LGPL is **rejected** here - the dynamic-linking caveat does not cover redistributed weights |
| `dataset_license` | yes | same rule (FC-3's usual killer). Corpora are rarely offered under a software licence, so the allowlist carries `CC0-1.0` and `CDLA-Permissive-2.0` for them; `CC-BY-4.0` is **not** on it |
| `dataset_source` | should | citation/URL of that corpus |
| `recipe` | should | training recipe, so the model is rebuildable without buying it |
| `weights_source` | yes | public, non-gated URL or release asset (FC-6); URLs containing `gated`/`consent`/`request-access` are refused |
| `weights_sha256` | yes | 64 lowercase hex |
| `weights_file` | - | path relative to this directory; if the operator has fetched it, its hash **must** match |
| `runtime.device` | - | must be CPU-capable (`cpu`, `cpu+gpu`); a GPU/driver requirement breaks FC-2 |
| `allows_commercial_use` | - | `false` is refused even if the licence id looks fine |
| `eval_report.auc` | yes | 0.5..1.0 |
| `eval_report.eer` | yes | 0.0..0.5 (equal-error rate) |
| `eval_report.ece` | yes | 0.0..0.2 (expected calibration error - GOAL-2 is measured, not asserted) |
| `eval_report.held_out_set` | yes | names the permissively licensed set the numbers came from |
| `eval_report.per_group[]` | yes | at least one labelled group with a measured error rate (demographic parity, REQ-DET-3) |
| `gates` | yes | committed floors/ceilings for each measured metric (`auc` needs a floor above 0.5); a metric that breaches its gate, or a metric with no committed gate, fails the scan - this is how a calibration regression is caught in CI (AC-DET-3) |

`model_card` SHOULD point at a markdown file describing intended and
out-of-scope use. Reports are explainable evidence, never certified findings
(`OUT-3`).

## Adding a model

1. Verify the **dataset** licence before anything else - it is the constraint that
   disqualifies most public deepfake weights, and it cannot be fixed later.
2. Write `synthverify/models/<id>.json` (copy `template.json.example`, drop the
   `.example` suffix).
3. Set `ml_model = "<id>"` on the `Detector` subclass. That attribute is what makes
   the gate apply; heuristics leave it `None`.
4. Run `make model-manifests`. It must print `RESULT: PASS`.

Weight bytes are downloaded by the **operator**, never by the pipeline - keeping a
full ingest→verdict cycle offline is FC-4, and an auto-downloading detector would
silently break it.
