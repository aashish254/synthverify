"""Text stylometry / AI-authorship heuristics.

No single lexical test proves machine authorship, but large-language-model
prose leaves measurable style residue: unusually uniform sentence rhythm
(low burstiness), template connectives and hedge phrases, near-zero typos with
highly regular punctuation, and distinctive "AI stock phrases". This detector
makes those measurements explicit and reports them with the numbers, so a human
reviewer can weigh them rather than trust a black box.
"""

from __future__ import annotations

import re

from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, clamp01
from synthverify.detectors.registry import register

AI_STOCK_PHRASES = [
    "as an ai",
    "i cannot",
    "it is important to note",
    "it's important to note",
    "it is worth noting",
    "it's worth noting",
    "delve into",
    "in today's fast-paced",
    "in the realm of",
    "in conclusion,",
    "furthermore,",
    "moreover,",
    "additionally,",
    "a testament to",
    "navigate the complexities",
    "unlock the potential",
    "tapestry",
    "multifaceted",
    "holistic approach",
    "landscape of",
    "robust framework",
    "seamless integration",
    "leverage the power",
    "crucial to remember",
    "keep in mind that",
    "here is a breakdown",
    "certainly! here",
]

HEDGES = ["may", "might", "could", "typically", "generally", "often", "usually", "various", "several"]

SENT_SPLIT = re.compile(r"[^.!?]+[.!?]+")
WORD = re.compile(r"[A-Za-z']+")


@register
class TextStylometryDetector(Detector):
    name = "text_stylometry"
    media_types = ("text",)
    weight = 1.0
    description = (
        "Stylometric analysis: sentence-rhythm burstiness, connective/hedge density, "
        "AI stock-phrase rate and lexical repetition patterns."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        text = ctx.text.strip()
        words = WORD.findall(text)
        if len(words) < 40:
            return self.skipped(self, ctx, f"Text too short for stylometry ({len(words)} words; need >= 40).")

        sentences = [s.strip() for s in SENT_SPLIT.findall(text) if len(s.split()) >= 2]
        if len(sentences) < 3:
            sentences = _chunk(text, words)

        sent_lens = [len(s.split()) for s in sentences]
        mean_len = sum(sent_lens) / len(sent_lens)
        # burstiness: coefficient of variation of sentence length
        cv = (sum((n - mean_len) ** 2 for n in sent_lens) / len(sent_lens)) ** 0.5 / (mean_len + 1e-9)

        lower = text.lower()
        word_set = words and {w.lower() for w in words}
        ttr = len(word_set) / len(words)  # type-token ratio

        stock_hits = [p for p in AI_STOCK_PHRASES if p in lower]
        stock_rate = len(stock_hits)
        connectives = sum(lower.count(p) for p in ("furthermore,", "moreover,", "additionally,", "in conclusion,", "however,"))
        hedge_count = sum(len(re.findall(rf"\b{h}\b", lower)) for h in HEDGES)
        hedge_density = hedge_count / len(words)
        connective_density = connectives / len(sentences)

        # repeated 6-gram rate (template boilerplate repeats)
        sixgrams = [tuple(lower.split()[i : i + 6]) for i in range(max(0, len(lower.split()) - 6))]
        unique = len(set(sixgrams))
        rep_rate = 1 - (unique / len(sixgrams)) if sixgrams else 0.0

        findings: list[str] = []
        flags: list[str] = []
        score = 0.05

        if cv < 0.35:
            flags.append("LOW_BURSTINESS")
            score += 0.30
            findings.append(
                f"Sentence rhythm is unusually uniform (length CV {cv:.2f}, mean {mean_len:.0f} words) - "
                "human authors burst between short punches and long winded sentences; LLM output flows evenly."
            )
        if stock_rate >= 2:
            flags.append("AI_STOCK_PHRASES")
            score += 0.30
            quoted = ", ".join(f"'{p}'" for p in stock_hits[:4])
            findings.append(
                f"{stock_rate} signature LLM phrases detected ({quoted}) - phrases over-represented in "
                "model-generated prose."
            )
        elif stock_rate == 1:
            score += 0.12
            findings.append(f"One LLM-associated phrase found ('{stock_hits[0]}').")
        if connective_density > 0.5:
            flags.append("CONNECTIVE_OVERUSE")
            score += 0.15
            findings.append(
                f"Template connectives appear {connectives} times across {len(sentences)} sentences "
                "(>0.5 per sentence) - formal scaffolding typical of generated answers."
            )
        if ttr < 0.42 and len(words) >= 120:
            flags.append("LOW_LEXICAL_DIVERSITY")
            score += 0.10
            findings.append(f"Lexical diversity is low (type-token ratio {ttr:.2f}).")
        if rep_rate > 0.15:
            flags.append("REPEATED_NGRAMS")
            score += 0.15
            findings.append(f"Repeated 6-gram rate is {rep_rate * 100:.0f}% - boilerplate repetition.")

        if not flags:
            findings.append(
                f"Style metrics are within human-author ranges: burstiness CV {cv:.2f}, "
                f"type-token ratio {ttr:.2f}, {stock_rate} LLM-associated phrases, "
                f"{connective_density:.2f} connectives/sentence."
            )

        confidence = 0.6
        if len(words) < 150:
            confidence *= 0.7  # short texts -> noisy stylometry

        return DetectorResult(
            detector=self.name,
            media_type="text",
            score=clamp01(score),
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence={
                "words": len(words),
                "sentences": len(sentences),
                "mean_sentence_len": round(mean_len, 1),
                "burstiness_cv": round(cv, 3),
                "type_token_ratio": round(ttr, 3),
                "stock_phrase_hits": stock_hits,
                "connective_density": round(connective_density, 3),
                "hedge_density": round(hedge_density, 4),
                "repeated_6gram_rate": round(rep_rate, 4),
            },
        )


def _chunk(text: str, words: list[str]) -> list[str]:
    """Fallback 'sentence' segmentation for unpunctuated text: 25-word chunks."""
    tokens = text.split()
    return [" ".join(tokens[i : i + 25]) for i in range(0, len(tokens) - 25, 25)] or [text]
