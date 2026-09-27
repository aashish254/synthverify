# Business Analysis & SDG 16 Alignment

## 1. Problem statement

AI-generated media has crossed the credibility threshold:

* **Financial fraud** — voice-cloned "family member in trouble" calls and CEO
  impersonations drained billions globally; synthetic KYC selfies open bank
  accounts and mule networks.
* **Identity theft targeting vulnerable populations** — the elderly are the
  primary target group for clone-call scams; coercion scams weaponize fake
  "proof-of-life" media.
* **Misinformation** — fabricated photos/videos of public figures spread faster
  than corrections, eroding trust in electoral processes and public media.

Enterprises (banks, insurers, newsrooms, marketplaces, public agencies) have a
detection gap: model scores exist in labs, but **no orchestration layer turns
forensics into a routed, explainable, auditable business decision**. That gap
is the product.

## 2. Solution framing (Systems & BA view)

SynthVerify is deliberately an **orchestration framework**, not a single model:

| Business capability | Delivered by | Why it matters to the business |
|---|---|---|
| Accept media from any channel | ingest/analyze/batch APIs, SDK, CLI | one verification choke-point across web, mobile, support lines |
| Run multiple independent detectors | plugin registry (11 built-in, extensible) | detection resilience: no single point of evasion; heuristics + (future) ML co-exist |
| Produce a decision, not a score | XAI engine → tiers + **recommended action** | workflows need routing (block / review / escalate) with rationale for compliance |
| Explain every verdict | narratives, ranked evidence, flag glossary | analyst trust, customer-facing justifications, regulator readiness |
| Fit enterprise controls | API keys + roles, rate limits, audit chain, metrics | procurement/security review passes; SOC-2-style traceability |
| Integrate with existing queues | HMAC-signed webhooks w/ retries + delivery ledger | case management & SIEM ingest without polling |

## 3. Stakeholders & jobs-to-be-done

| Stakeholder | Job to be done | Feature serving it |
|---|---|---|
| Fraud-ops analyst | triage suspicious media in seconds | dashboard queue, risk tier, top-evidence quotes |
| Compliance officer | prove why a decision was made | per-detector evidence, audit hash chain, policy thresholds |
| Integration engineer | wire verification into workflows | webhooks (signed, retrying), SDK, OpenAPI docs |
| Platform/security admin | operate safely at scale | role-scoped keys, rate limits, metrics, containerized deploy |
| Citizen / end user | know media can be challenged | consistent verdicts + human-readable reasons |

## 4. Requirements traceability (selected)

| # | Requirement | Type | Verification |
|---|---|---|---|
| R1 | Accept image/audio/video/text uploads, sniff type by content | FR | `test_media_utils.py`, `test_api.py::TestMediaEndpoints` |
| R2 | Run multi-detector forensics per media type | FR | `test_detectors.py` (11 detectors) |
| R3 | Fuse scores with confidence + coverage honesty | FR | `test_xai.py::TestFusion` (incl. inconclusive routing) |
| R4 | Emit human-readable risk flags & narrative | FR | `test_xai.py::TestExplainability`, glossary coverage test |
| R5 | Map verdicts to workflow actions per policy | FR | routing tests, `PUT /admin/policy` ordering validation |
| R6 | Authenticate and authorize (roles) | FR/NFR | `test_api.py::TestAuth` (401/403, tenant isolation) |
| R7 | Rate-limit abusive clients | NFR | `TestRateLimit` (429 + Retry-After) |
| R8 | Deliver signed webhook callbacks with retries | FR | `test_admin_webhooks.py::TestWebhooks` (signature reproduction, ledger) |
| R9 | Tamper-evident audit of every decision | NFR | `TestAuditLedger` (chain verify + tamper detection) |
| R10 | Survive crashes without losing jobs | NFR | recovery logic; durable-then-queued ordering |
| R11 | Observe the system (metrics, health) | NFR | `/metrics`, `/readyz` + container smoke test in CI |
| R12 | Deploy via container | NFR | `docker/Dockerfile`, compose, CI docker job |

## 5. SDG 16 targets mapping

| SDG 16 target | How SynthVerify contributes |
|---|---|
| **16.1** Reduce violence & related death rates | indirect: degrades the effectiveness of fraud-driven coercion and scam pipelines that prey on the vulnerable |
| **16.2** End abuse & exploitation of children / trafficking | synthetic KYC & CSAM detection tooling: flagged uploads route to human review for escalation to authorities |
| **16.3** Rule of law, equal access to justice | evidence-grade, explainable reports + tamper-evident audit chain support due process (defense *and* prosecution can inspect the same evidence) |
| **16.4** Combat illicit financial flows, arms & crime | direct: cloned-voice payment authorization and synthetic-identity onboarding are the exact media types the audio/image pipelines flag before money moves |
| **16.5** Reduce corruption & bribery | fabricated "audio evidence" in procurement disputes can be challenged with detector findings instead of accepted or dismissed wholesale |
| **16.6** Effective, transparent, accountable institutions | the hash-chained audit ledger makes every automated decision inspectable and reproducible — accountability by construction |
| **16.10** Public access to information & fundamental freedoms | restores digital trust in public media: newsrooms can verify UGC before publication, protecting democratic discourse without silencing it |

## 6. Success metrics (KPIs)

**Product / detection**
- Precision of `BLOCK`/`ESCALATE` routing against analyst adjudications (target ≥ 90% after calibration quarter)
- Median pipeline latency (heuristics: ~50 ms/image; SLO < 2 s end-to-end ingest→callback)
- Detector coverage on production traffic (target ≥ 98% `ran`)

**Business**
- Fraud losses averted on voice-clone claims (partner-bank pilot)
- % of KYC synthetic selfies blocked pre-account-opening
- Newsroom verification turnaround vs. manual baseline

**Trust & accountability**
- 100% of verdicts auditable via chain verification (continuously verified)
- Zero unexplained auto-blocks (every action carries `action_rationale`)

## 7. Rollout roadmap

| Phase | Scope |
|---|---|
| **P0 (shipped here)** | Multi-detector heuristic pipeline, XAI routing, keys/rate-limits/audit/webhooks, dashboard, SDK/CLI, container, CI |
| **P1 (next quarter)** | Trained ML detector plugins (image/video-face/audio), C2PA signature *validation*, per-org policy profiles in UI, Redis rate-limit store, PostgreSQL reference deploy |
| **P2** | Provenance capture SDK for origin cameras/phones, media fingerprint sharing consortium (known-deepfake hashes), region-legal policy packs (AI-Act disclosure rules) |
| **P3** | Real-time call-center voice verification, browser extension for citizen reporting, public transparency reports |

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Heuristic false positives harm legitimate users | conservative thresholds, `MANUAL_REVIEW` default for medium risk, every finding states its measured evidence; policy is org-tunable |
| Adversarial evasion (noise matching, metadata spoofing) | multi-detector independence raises evasion cost; ML plugins in P1; provenance (C2PA) layer; continuous calibration KPIs |
| Over-reliance on automation | the engine *refuses* to conclude when coverage/confidence are low (`NEEDS_HUMAN_REVIEW`); narratives explicitly frame findings as statistical indicators, not proof |
| Privacy of uploaded media | content-addressed storage, no third-party calls, webhooks are org-configured; retention is an ops policy knob (data dir) |
| Regulatory divergence across markets | policy profiles are per-organisation and versioned through the audit ledger |
