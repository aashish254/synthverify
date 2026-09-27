"""XAI aggregation engine tests: fusion math, routing, narratives, honesty."""

from __future__ import annotations

from synthverify.db import RiskTier
from synthverify.detectors.base import DetectorResult, ResultStatus
from synthverify.xai import (
    FLAGS_GLOSSARY,
    Policy,
    VerificationReport,
    aggregate,
    tier_for,
)


def det(name: str, score: float, conf: float, weight: float = 1.0, flags: list[str] | None = None) -> DetectorResult:
    d = DetectorResult(detector=name, media_type="image", score=score, confidence=conf)
    d.flags = flags or []
    if flags:
        d.findings = [f"Finding for {name}"]
    return d


def agg(results, **kwargs) -> VerificationReport:
    return aggregate(results, media_type="image", filename="f.jpg", **kwargs)


class TestTiering:
    def test_tier_boundaries(self):
        assert tier_for(0.0) is RiskTier.LOW
        assert tier_for(0.34) is RiskTier.LOW
        assert tier_for(0.35) is RiskTier.MEDIUM
        assert tier_for(0.64) is RiskTier.MEDIUM
        assert tier_for(0.65) is RiskTier.HIGH
        assert tier_for(0.849) is RiskTier.HIGH
        assert tier_for(0.85) is RiskTier.CRITICAL


class TestFusion:
    def test_empty_results_inconclusive(self):
        report = agg([])
        assert not report.conclusive
        assert report.recommended_action == "NEEDS_HUMAN_REVIEW"
        assert "inconclusive" in report.summary.lower() or "could not be assessed" in report.summary.lower()

    def test_all_errored_inconclusive(self):
        bad = DetectorResult(detector="ela", media_type="image", score=0, confidence=0, status=ResultStatus.ERROR)
        bad.error = "boom"
        report = agg([bad])
        assert not report.conclusive
        assert report.recommended_action == "NEEDS_HUMAN_REVIEW"

    def test_neutral_image_low_risk(self):
        results = [
            det("ela", 0.10, 0.75),
            det("metadata", 0.05, 0.65),
            det("noise", 0.0, 0.70),
        ]
        report = agg(results)
        assert report.risk_score < 0.25
        assert report.risk_tier is RiskTier.LOW
        assert report.recommended_action == "PROCEED"
        assert report.conclusive

    def test_single_strong_detector_not_diluted(self):
        results = [det("metadata", 0.95, 0.95), det("ela", 0.05, 0.75), det("noise", 0.0, 0.7)]
        report = agg(results)
        assert report.risk_score >= 0.85  # declared-synthetic override
        assert report.recommended_action == "BLOCK"

    def test_moderate_signals_route_to_review(self):
        results = [det("ela", 0.55, 0.7, flags=["ELA_INCONSISTENT"]), det("metadata", 0.3, 0.6)]
        report = agg(results)
        assert report.recommended_action in ("MANUAL_REVIEW", "ESCALATE")

    def test_three_flags_escalate(self):
        results = [
            det("a", 0.45, 0.6, flags=["FLAG_A", "FLAG_B", "FLAG_C"]),
            det("b", 0.2, 0.5),
        ]
        report = agg(results)
        assert report.recommended_action == "ESCALATE"

    def test_low_coverage_inconclusive(self):
        results = [det("ela", 0.9, 0.9)]
        skipped = DetectorResult(
            detector="metadata", media_type="image", score=0, confidence=0,
            status=ResultStatus.SKIPPED, skip_reason="n/a",
        )
        report = agg([results[0]] + [skipped] * 3)
        # coverage 0.25 < min 0.5 -> inconclusive even though ela screamed
        assert report.recommended_action == "NEEDS_HUMAN_REVIEW"
        assert not report.conclusive

    def test_policy_thresholds_respected(self):
        results = [det("ela", 0.55, 0.9, flags=["ELA_INCONSISTENT"])]
        report = agg(results, policy=Policy(review_score=0.3, escalate_score=0.5, block_score=0.6))
        assert report.recommended_action == "ESCALATE"

    def test_score_in_unit_range_always(self):
        import random

        rng = random.Random(7)
        for _ in range(200):
            results = [
                det(rng.choice(["ela", "noise", "x"]), rng.random(), rng.random())
                for _ in range(rng.randint(1, 6))
            ]
            report = agg(results)
            assert 0.0 <= report.risk_score <= 1.0
            assert 0.0 <= report.confidence <= 1.0


class TestExplainability:
    def test_flag_glossary_covers_detector_flags(self):
        from fixtures_gen import (
            ai_generated_photo,
            deepfake_video,
            doctored_photo,
            synthetic_voice,
        )

        from synthverify.detectors import detectors_for
        from synthverify.detectors.base import DetectionContext

        corpus = [
            ("image", doctored_photo(), "d.jpg"),
            ("image", ai_generated_photo(), "a.png"),
            ("audio", synthetic_voice(), "s.wav"),
            ("video", deepfake_video(), "v.avi"),
        ]
        all_flags = set()
        for media_type, data, fn in corpus:
            ctx = DetectionContext(data=data, media_type=media_type, filename=fn)
            for detector in detectors_for(media_type):
                r = detector.run(ctx)
                all_flags.update(r.flags)
        unknown = all_flags - set(FLAGS_GLOSSARY)
        assert not unknown, f"flags missing from glossary: {unknown}"

    def test_flag_explanations_included(self):
        report = agg([det("ela", 0.6, 0.7, flags=["ELA_INCONSISTENT"])])
        assert "ELA_INCONSISTENT" in report.flag_explanations
        assert "uneven" in report.flag_explanations["ELA_INCONSISTENT"].lower()

    def test_narrative_mentions_detectors_and_flags(self):
        results = [det("ela", 0.9, 0.9, flags=["ELA_INCONSISTENT"])]
        report = agg(results)
        text = " ".join(report.narrative)
        assert "ela" in text
        assert "ELA_INCONSISTENT" in text

    def test_top_evidence_ranked_with_contribution(self):
        results = [det("ela", 0.8, 0.8, flags=["ELA_INCONSISTENT"]), det("noise", 0.1, 0.6)]
        report = agg(results)
        assert report.top_evidence
        assert "ela" in report.top_evidence[0]
        assert "contribution" in report.top_evidence[0]

    def test_review_force_flag_drives_manual_review(self):
        for flag in ["AI_GENERATION_TAG", "DIGITAL_SILENCE", "DUPLICATE_FRAMES"]:
            results = [det("meta", 0.2, 0.6, flags=[flag])]
            report = agg(results)
            assert report.recommended_action == "MANUAL_REVIEW", flag

    def test_report_serializable(self):
        import json

        report = agg([det("ela", 0.5, 0.5, flags=["ELA_HOTSPOTS"])])
        blob = json.dumps(report.to_dict())
        assert "verdict" in blob and "detectors" in blob

    def test_inconclusive_narrative_honest(self):
        bad = DetectorResult(detector="ela", media_type="image", score=0, confidence=0, status=ResultStatus.ERROR)
        bad.error = "decode failed"
        report = agg([bad])
        assert any("INCONCLUSIVE" in p for p in report.narrative)
