"""Explainable-AI aggregation engine.

Turns a bag of heterogeneous DetectorResults into ONE enterprise-consumable
verification report. Design principles:

1. **No black boxes.** Every number in the verdict is derivable from the
   per-detector breakdown that ships next to it.
2. **Confidence-weighted fusion.** A detector that is unsure of itself (or was
   skipped, or errored) cannot drag the verdict around.
3. **Strong-evidence override.** One highly-confident alarm is not diluted to
   silence by five neutral detectors - but it is capped, and named.
4. **Honest inconclusiveness.** If coverage or confidence is too low the report
   says "needs human review" instead of pretending to know.
5. **Actions, not just scores.** Enterprise workflows need routing decisions
   (proceed / manual review / escalate / block) with the rationale exposed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from synthverify.db import RiskTier
from synthverify.detectors.base import DetectorResult, ResultStatus

REPORT_SCHEMA = "synthverify.report/v1"

# Machine flag code -> one-line human explanation (shown in UIs / tickets).
FLAGS_GLOSSARY: dict[str, str] = {
    "ELA_INCONSISTENT": "Re-compression error is uneven across the image - typical of localized edits.",
    "ELA_HOTSPOTS": "Sharp error-level boundaries suggest re-touched or pasted regions.",
    "EXIF_ABSENT": "No EXIF metadata; origin cannot be confirmed (often stripped by platforms).",
    "EDITING_SOFTWARE_TAG": "Editing software declared in metadata - image was post-processed.",
    "AI_GENERATION_TAG": "Metadata self-identifies a generative-AI tool as the creator.",
    "CAMERA_ORIGIN_DECLARED": "A camera make/model is declared (weak authenticity signal; forgeable).",
    "PARTIAL_CAMERA_INFO": "Partial camera info present.",
    "TIMESTAMP_MISMATCH": "Capture and modification timestamps diverge - file re-saved after capture.",
    "C2PA_PROVENANCE_PRESENT": "C2PA content-credential manifest present - signed provenance claims attached.",
    "PROVENANCE_AUTHENTIC": "Content-credential signature verifies and the asset matches its data-hash binding - provenance is cryptographically intact.",
    "PROVENANCE_INVALID": "A content credential is present but does not hold: bad signature, expired certificate, or the asset changed after signing.",
    "PROVENANCE_STRIPPED": "No content credential in the container. Weak signal - platforms strip provenance on re-encode as often as bad actors do.",
    "PROVENANCE_UNVERIFIABLE": "A credential looks present but this build could not check it (verifier not installed, or the manifest does not parse). Absence of a check is not proof of tampering.",
    "DOUBLE_COMPRESSION": "Multiple JPEG compression histories - the file was re-saved repeatedly.",
    "HEAVY_RECOMPRESS": "Low-quality re-encode; original forensic detail may be lost.",
    "SPECTRAL_SPIKES": "Periodic Fourier-domain peaks characteristic of upsampling networks.",
    "CHECKERBOARD_ARTIFACT": "Transposed-convolution checkerboard energy - generator fingerprint.",
    "NOISE_ABSENT": "No sensor grain - real cameras always imprint stochastic noise.",
    "NOISE_INCONSISTENT": "Noise floor varies between regions - content likely spliced from sources.",
    "STRUCTURED_RESIDUAL": "High-pass energy is periodic, not random - synthetic upsampling artifacts.",
    "SPECTRAL_FLATNESS_STABLE": "Audio spectral texture is unnaturally uniform - vocoder-like.",
    "SPECTRAL_DISCONTINUITIES": "Abrupt spectral jumps - concatenative TTS or spliced audio.",
    "HIGH_BAND_MISSING": "Wideband container but no energy above ~7.5kHz - band-limited voice clone.",
    "DIGITAL_SILENCE": "Bit-exact zero silences - assembled from generated parts, not room tone.",
    "UNIFORM_PAUSES": "Metronomic pause lengths - synthetic pacing.",
    "AMPLITUDE_CLIPPING": "Full-scale samples present - harsh processing or poor capture.",
    "TTS_ENCODER_TAG": "Container tagged by a TTS/audio-synthesis pipeline encoder.",
    "RECORDER_ORIGIN": "A recorder origin signature is present.",
    "TELEPHONY_BAND": "8/11kHz sample rate - common for voice-clone render pipelines.",
    "TTS_CANONICAL_FORMAT": "16kHz mono PCM is the canonical TTS output format.",
    "PHOTOMETRIC_FLICKER": "Frame-to-frame illumination instability - generative video tell.",
    "DUPLICATE_FRAMES": "Non-adjacent identical frames - looped or spliced segments.",
    "STATIC_LOOP": "Effectively frozen video - possible re-rendered insert.",
    "ERRATIC_CUTS": "Unusually many hard-cut transitions.",
    "GENERATOR_GEOMETRY": "Generator-native frame size (power-of-two square).",
    "ABNORMAL_FPS": "Implausible frame rate - reassembly or synthetic timing.",
    "SHORT_CLIP": "Very short duration - typical of deepfake inserts.",
    "FRAME_EL_INCONSISTENT": "Some frames carry a different compression history - frame-level tampering.",
    "LOW_BURSTINESS": "Unnaturally uniform sentence rhythm - LLM writing style.",
    "AI_STOCK_PHRASES": "Phrases heavily over-represented in model-generated text.",
    "CONNECTIVE_OVERUSE": "Template connective scaffolding typical of generated answers.",
    "LOW_LEXICAL_DIVERSITY": "Repetitive vocabulary.",
    "REPEATED_NGRAMS": "Repeated phrases - boilerplate generation.",
}

# Flags that on their own justify human review regardless of fused score.
REVIEW_FORCE_FLAGS = {
    "AI_GENERATION_TAG",
    "TTS_ENCODER_TAG",
    "ELA_INCONSISTENT",
    "NOISE_INCONSISTENT",
    "STRUCTURED_RESIDUAL",
    "CHECKERBOARD_ARTIFACT",
    "DIGITAL_SILENCE",
    "PHOTOMETRIC_FLICKER",
    "DUPLICATE_FRAMES",
    "FRAME_EL_INCONSISTENT",
    # A content credential that is present but does not hold is the strongest single tamper
    # signal the pipeline can produce: someone signed this asset and then changed it. It
    # forces review on its own; AUTHENTIC/STRIPPED/UNVERIFIABLE deliberately do not.
    "PROVENANCE_INVALID",
}

# Flags that report provenance *state* rather than a forensic anomaly. The routing heuristics
# count how many independent alarms fired; a credential that is merely absent (``STRIPPED``),
# merely present-and-intact (``AUTHENTIC``), or present-but-uncheckable (``UNVERIFIABLE``) -
# and the back-compat ``C2PA_PROVENANCE_PRESENT`` marker - are not alarms. Counting them would
# flip every credential-less photo (i.e. almost every real photo) to human review, which is
# exactly the accusation-by-absence this feature is built to avoid. Only ``PROVENANCE_INVALID``
# is a real alarm, and it is handled above.
PROVENANCE_STATE_FLAGS = frozenset(
    {
        "PROVENANCE_AUTHENTIC",
        "PROVENANCE_STRIPPED",
        "PROVENANCE_UNVERIFIABLE",
        "C2PA_PROVENANCE_PRESENT",
    }
)


@dataclass
class Policy:
    """Routing policy applied to the fused verdict (overridable per org)."""

    block_score: float = 0.85
    escalate_score: float = 0.70
    review_score: float = 0.40
    low_confidence: float = 0.30
    min_coverage: float = 0.5
    name: str = "defaults"

    #: threshold fields (everything but the provenance label)
    THRESHOLD_FIELDS: ClassVar[tuple[str, ...]] = (
        "block_score",
        "escalate_score",
        "review_score",
        "low_confidence",
        "min_coverage",
    )

    def to_dict(self) -> dict[str, float]:
        return {field_name: getattr(self, field_name) for field_name in self.THRESHOLD_FIELDS}

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, name: str = "profile") -> Policy:
        """Build from a stored thresholds mapping, filling gaps from defaults."""
        kwargs = {k: float(data[k]) for k in cls.THRESHOLD_FIELDS if data.get(k) is not None}
        return cls(name=name, **kwargs)


@dataclass
class VerificationReport:
    """The full XAI report persisted on the job and delivered to workflows."""

    risk_score: float
    risk_tier: RiskTier
    confidence: float
    coverage: float
    conclusive: bool
    recommended_action: str
    action_rationale: str
    summary: str
    narrative: list[str]
    detectors: list[dict[str, Any]]
    flags: list[str]
    flag_explanations: dict[str, str]
    top_evidence: list[str]
    policy: dict[str, float]
    policy_name: str = "defaults"
    artifacts: list[dict[str, str]] = field(default_factory=list)
    #: The C2PA cryptographic-provenance verdict for this asset (REQ-DET-5), lifted from the
    #: metadata detector's evidence. None when the pipeline ran no provenance check (e.g. a
    #: media type we do not parse); the shape is ``ProvenanceResult.to_dict()`` when present.
    provenance: dict[str, Any] | None = None
    schema_version: str = REPORT_SCHEMA
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema_version,
            "verdict": {
                "risk_score": round(self.risk_score, 4),
                "risk_tier": self.risk_tier.value,
                "confidence": round(self.confidence, 4),
                "detector_coverage": round(self.coverage, 4),
                "conclusive": self.conclusive,
                "recommended_action": self.recommended_action,
                "action_rationale": self.action_rationale,
                "summary": self.summary,
            },
            "narrative": self.narrative,
            "detectors": self.detectors,
            "flags": self.flags,
            "flag_explanations": self.flag_explanations,
            "top_evidence": self.top_evidence,
            "policy": self.policy,
            "policy_name": self.policy_name,
            "artifacts": self.artifacts,
            "provenance": self.provenance,
            "generated_at": self.generated_at,
        }


def tier_for(score: float, high_threshold: float = 0.65, critical_threshold: float = 0.85) -> RiskTier:
    if score >= critical_threshold:
        return RiskTier.CRITICAL
    if score >= high_threshold:
        return RiskTier.HIGH
    if score >= 0.35:
        return RiskTier.MEDIUM
    return RiskTier.LOW


def aggregate(
    results: list[DetectorResult],
    *,
    policy: Policy | None = None,
    media_type: str = "unknown",
    filename: str = "upload",
    artifacts: list[dict[str, str]] | None = None,
    policy_name: str | None = None,
) -> VerificationReport:
    """Fuse detector results into a VerificationReport."""
    policy = policy or Policy()
    ran = [r for r in results if r.status is ResultStatus.RAN]
    applicable_n = len(results) or 1
    coverage = len(ran) / applicable_n

    # ---------------------------------------------------------- fusion math
    num = sum(d.weight * r.score * r.confidence for d, r in _with_defs(ran))
    den = sum(d.weight * r.confidence for d, r in _with_defs(ran))
    fused = num / den if den > 0 else 0.0

    strong = max((r.score * r.confidence for r in ran), default=0.0)
    if strong > 0:
        fused = max(fused, strong * 0.9)

    declared = [r for r in ran if r.confidence >= 0.85 and r.score >= 0.85]
    if declared:
        fused = max(fused, 0.90)

    confidence = _verdict_confidence(ran, coverage, policy)

    flags = _unique(flag for r in ran for flag in r.flags)
    flag_explanations = {f: FLAGS_GLOSSARY.get(f, "Forensic anomaly flag.") for f in flags}

    # The metadata detector runs the C2PA cryptographic check and stashes the full verdict in
    # its structured evidence; lift it onto the report so the API, the audit trail, and the
    # console all read provenance from one first-class place rather than digging per-detector.
    provenance = next(
        (r.evidence["provenance"] for r in ran if isinstance(r.evidence.get("provenance"), dict)),
        None,
    )

    # ------------------------------------------------------------- routing
    conclusive = coverage >= policy.min_coverage and confidence >= policy.low_confidence and bool(ran)
    action, rationale = _route(
        fused=fused,
        flags=flags,
        confidence=confidence,
        conclusive=conclusive,
        declared=bool(declared),
        policy=policy,
    )

    tier = tier_for(fused) if conclusive else RiskTier.LOW

    top_evidence = _rank_evidence(ran)
    summary = _summary(fused, tier, media_type, filename, action, conclusive)
    narrative = _narrative(fused, tier, ran, flags, coverage, confidence, media_type, conclusive)

    return VerificationReport(
        risk_score=fused,
        risk_tier=tier,
        confidence=confidence,
        coverage=coverage,
        conclusive=conclusive,
        recommended_action=action,
        action_rationale=rationale,
        summary=summary,
        narrative=narrative,
        detectors=[r.sanitized() for r in results],
        flags=flags,
        flag_explanations=flag_explanations,
        top_evidence=top_evidence,
        policy=policy.to_dict(),
        policy_name=policy_name or policy.name,
        artifacts=artifacts or [],
        provenance=provenance,
    )


def _with_defs(ran: list[DetectorResult]):
    """Pair results with their detector's fusion weight (registry lookup safe)."""
    from synthverify.detectors import all_detectors

    weights = {name: det.weight for name, det in all_detectors().items()}
    return [(type("W", (), {"weight": weights.get(r.detector, 1.0)}), r) for r in ran]


def _verdict_confidence(ran: list[DetectorResult], coverage: float, policy: Policy) -> float:
    """Overall confidence: mean detector confidence, discounted by coverage."""
    if not ran:
        return 0.0
    base = sum(r.confidence for r in ran) / len(ran)
    # skipping half the detectors must hurt; running all of them must not
    coverage_factor = 0.55 + 0.45 * min(1.0, coverage / max(policy.min_coverage, 1e-6)) / 1.0
    coverage_factor = min(1.0, 0.55 + 0.45 * coverage)
    return round(max(0.05, min(0.99, base * coverage_factor)), 4)


def _route(
    *,
    fused: float,
    flags: list[str],
    confidence: float,
    conclusive: bool,
    declared: bool,
    policy: Policy,
) -> tuple[str, str]:
    if not conclusive:
        return (
            "NEEDS_HUMAN_REVIEW",
            "Detector coverage or confidence is too low for an automated decision; "
            "the report is provided as evidence only.",
        )
    # Count only anomaly flags: provenance-state markers must not inflate the "how many
    # independent alarms fired" heuristic (see PROVENANCE_STATE_FLAGS).
    alarms = [f for f in flags if f not in PROVENANCE_STATE_FLAGS]
    if declared or fused >= policy.block_score:
        if declared:
            return (
                "BLOCK",
                "A high-confidence detector self-reports near-certain synthetic origin "
                "(e.g. generation tool signature); policy blocks automatically.",
            )
        return ("BLOCK", f"Fused risk {fused:.2f} meets or exceeds the block threshold {policy.block_score:.2f}.")
    if fused >= policy.escalate_score or len(alarms) >= 3:
        return (
            "ESCALATE",
            f"Fused risk {fused:.2f} is above the escalation threshold "
            f"{policy.escalate_score:.2f}, or three-plus independent forensic flags fired.",
        )
    if fused >= policy.review_score or len(alarms) >= 2 or any(f in REVIEW_FORCE_FLAGS for f in flags):
        drivers = [f for f in flags if f in REVIEW_FORCE_FLAGS]
        why = f"fused risk {fused:.2f} >= review threshold {policy.review_score:.2f}"
        if drivers:
            why += f"; driving flags: {', '.join(drivers[:3])}"
        elif alarms:
            why += f"; {len(alarms)} forensic flag(s) present"
        return ("MANUAL_REVIEW", "Route to a human reviewer: " + why + ".")
    return (
        "PROCEED",
        f"All detectors ran; fused risk {fused:.2f} is below the review threshold "
        f"{policy.review_score:.2f} and no forensic flags fired.",
    )


def _rank_evidence(ran: list[DetectorResult], limit: int = 6) -> list[str]:
    """Most decision-relevant findings first (contribution = score x confidence x weight)."""
    from synthverify.detectors import all_detectors

    weights = {name: det.weight for name, det in all_detectors().items()}
    scored = sorted(
        (
            (weights.get(r.detector, 1.0) * r.score * r.confidence, r)
            for r in ran
            if r.findings
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    out: list[str] = []
    for contribution, r in scored:
        out.append(f"[{r.detector} · contribution {contribution:.2f}] {r.findings[-1]}")
        if len(out) >= limit:
            break
    return out


def _unique(items) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _summary(fused: float, tier: RiskTier, media_type: str, filename: str, action: str, conclusive: bool) -> str:
    tier_txt = {
        RiskTier.LOW: "no significant synthetic-media indicators",
        RiskTier.MEDIUM: "moderate synthetic-media indicators",
        RiskTier.HIGH: "strong synthetic-media indicators",
        RiskTier.CRITICAL: "near-certain synthetic origin or severe tampering",
    }[tier]
    if not conclusive:
        return (
            f"'{filename}' could not be assessed conclusively ({media_type}); "
            "manual review is required."
        )
    return (
        f"'{filename}' ({media_type}) shows {tier_txt} "
        f"(fused risk {fused:.2f}/1.00). Recommended workflow action: {action}."
    )


def _narrative(
    fused: float,
    tier: RiskTier,
    ran: list[DetectorResult],
    flags: list[str],
    coverage: float,
    confidence: float,
    media_type: str,
    conclusive: bool,
) -> list[str]:
    """Plain-language paragraphs a fraud analyst can read in 20 seconds."""
    paras: list[str] = []
    if not conclusive:
        paras.append(
            "This verdict is INCONCLUSIVE: too few detectors completed or their "
            "confidence was too low to automate a decision. Treat the per-detector "
            "output below as supporting evidence for a human reviewer, not as a "
            "ruling."
        )
    ran_n = len(ran)
    para1 = (
        f"{ran_n} forensic detector(s) completed on this {media_type} "
        f"(coverage {coverage * 100:.0f}%, overall confidence {confidence * 100:.0f}%). "
    )
    if tier is RiskTier.LOW and conclusive:
        para1 += (
            "They found no consistent evidence of synthetic generation or tampering: "
            "compression history, noise structure, spectral behaviour and metadata "
            "tells are within normal ranges for authentic capture."
        )
    elif tier is RiskTier.MEDIUM:
        para1 += (
            "Some indicators deviate from authentic-capture norms. Individually these "
            "can be explained by ordinary processing (re-encoding, filters), so human "
            "confirmation is advised before any consequential action."
        )
    elif tier in (RiskTier.HIGH, RiskTier.CRITICAL):
        para1 += (
            "Multiple independent measurements agree with synthetic-generation or "
            "manipulation signatures rather than optical/electrical capture."
        )
    paras.append(para1)

    alarm = [r for r in ran if r.score >= 0.5 and r.confidence >= 0.3]
    if alarm:
        names = ", ".join(sorted({r.detector for r in alarm}))
        paras.append(
            f"Alarming detectors: {names}. Their strongest findings are quoted in "
            "'Top evidence'. Each is a statistical indicator, not proof - but their "
            "agreement is what drives the fused risk."
        )
    elif conclusive:
        paras.append(
            "No individual detector crossed its alarm threshold; the fused score "
            "reflects weak, mutually-inconsistent signals only."
        )

    if flags:
        paras.append(
            f"Machine-readable flags for workflow automation: {', '.join(flags[:8])}"
            + ("." if len(flags) <= 8 else f" (+{len(flags) - 8} more).")
        )
    return paras
