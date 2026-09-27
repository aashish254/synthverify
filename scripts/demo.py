#!/usr/bin/env python3
"""End-to-end demo: generate sample media, run them through the live pipeline
locally (no server needed), and print the human-readable XAI verdicts.

    python scripts/demo.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from fixtures_gen import (  # noqa: E402
    AI_TEXT,
    HUMAN_TEXT,
    ai_generated_photo,
    deepfake_video,
    doctored_photo,
    natural_photo,
    natural_speech,
    natural_video,
    synthetic_voice,
)

from synthverify.orchestrator import run_pipeline  # noqa: E402

SAMPLES = [
    ("Camera photo (authentic)", "vacation.jpg", natural_photo),
    ("Spliced + Photoshop-tagged photo", "invoice_scan.jpg", doctored_photo),
    ("AI-generated render", "render.png", ai_generated_photo),
    ("Human voice recording", "interview.wav", natural_speech),
    ("Voice-clone / TTS audio", "ceo_call.wav", synthetic_voice),
    ("Handheld video clip", "street.avi", natural_video),
    ("Deepfake-style video", "celebrity_endorsement.avi", deepfake_video),
    ("Human-written post", "eyewitness.txt", lambda: HUMAN_TEXT.encode()),
    ("LLM-written post", "astroturf.txt", lambda: AI_TEXT.encode()),
]

SECTIONS = {
    "image": "IMAGE",
    "audio": "AUDIO",
    "video": "VIDEO",
    "text": "TEXT",
}

ACTION_COLORS = {
    "PROCEED": "\033[92m",
    "MANUAL_REVIEW": "\033[93m",
    "ESCALATE": "\033[91m",
    "BLOCK": "\033[91m",
    "NEEDS_HUMAN_REVIEW": "\033[95m",
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "media").mkdir()
        print(f"{BOLD}SynthVerify - Enterprise Synthetic Media Verification Pipeline{RESET}")
        print(f"{DIM}Multi-detector forensics + Explainable AI risk flags (SDG 16){RESET}\n")

        last_section = None
        for label, filename, producer in SAMPLES:
            media_type = {
                "jpg": "image", "png": "image", "wav": "audio",
                "avi": "video", "txt": "text",
            }[filename.rsplit(".", 1)[1]]
            if SECTIONS[media_type] != last_section:
                last_section = SECTIONS[media_type]
                print(f"\n{BOLD}━━━ {last_section} ━━━{RESET}")

            data = producer()
            outcome = run_pipeline(data, filename=filename)
            v = outcome.report.to_dict()["verdict"]
            color = ACTION_COLORS.get(v["recommended_action"], "")

            print(f"\n  {BOLD}{filename}{RESET}  {DIM}({label}, {len(data):,} bytes, "
                  f"{outcome.duration_ms:.0f} ms){RESET}")
            print(f"  risk {v['risk_score']:.3f}/1.00  [{v['risk_tier']}]  "
                  f"confidence {v['confidence']:.0%}  →  "
                  f"{color}{BOLD}{v['recommended_action']}{RESET}")
            if outcome.report.flags:
                print(f"  flags: {', '.join(outcome.report.flags)}")
            print(f"  {DIM}{v['summary']}{RESET}")
            for evidence in outcome.report.top_evidence[:2]:
                print(f"    • {evidence}")

    print(f"\n{DIM}Next steps:"
          f"\n  make run         → start the API + dashboard on :8080"
          f"\n  make test        → run the {''}full test suite"
          f"\n  open http://localhost:8080/dashboard  → operator console{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
