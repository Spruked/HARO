# A.I.M.S. Development Log

## 2026-11-21 — HARO-related services left stopped

- Shutdown requested at 2026-11-21T18:47:59.8587857-06:00 America/Chicago.
- `ORB Qwen 7B Server` was stopped at 18:48:11.0472812 and is now `Ready`; it remains enabled for its normal future trigger.
- `Shared Qwen3 TTS` was stopped at 18:48:11.0667102 and is now `Ready`; it remains enabled for its normal future trigger.
- No listeners were present on HARO-related ports 8031, 11434, 16520 or 3900 at the shutdown checks. No Ollama process was present.
- Final shutdown evidence is recorded in `HARO-Test-Lab/results/RTX-5060/SHUTDOWN-STATE-20261121.md`.

## 2026-11-21 — HARO Checkpoint 0 live evidence reconciliation

- Corrected the Windows Hugging Face cache interpretation: the Q4_K_M snapshot entries are symbolic links whose targets contain 3,993,201,344 and 689,872,288 bytes.
- The matching Q4_K_M pair was merged locally for the Ollama comparison; no model download was used. The merged test artifact and its digest are preserved in Test-Lab evidence, not in the HARO source tree.
- Targeted Ollama Q4 lifecycle passed: health, inventory, load, chat, recovery and unload. Device memory sampling was continuous at 200 ms; engine-reported residency was 5,219 MiB and peak device usage was 7,659 MiB.
- Restoring `ORB Qwen 7B Server` returned port 16520 to HTTP 200. The full Checkpoint 0 remains incomplete because the earlier orchestration stopped after inventory before speech, cancellation, remote Nemotron and A.I.M.S. restart evidence.
- Ollama is optional. An absent local Ollama installation is recorded as an unavailable/not-run binding with its connection reason, not as a validation failure.
- Owner-declared fingerprint fact: NVIDIA CUDA Sysmem Fallback Policy = Prefer No Sysmem Fallback (Global), applied 2026-11-20T14:24:45.1616126-06:00 America/Chicago.

## 2026-09-13 — Initial repository

- Created and pushed root commit `1204914` to `Spruked/AIMS` on `main`.
- Established canonical product name: **A.I.M.S. — Agnostic Immutable Memory
  System**.

## 2026-09-13 — Trust and persistence repair

- Replaced transient Vault state with an append-only Vault event ledger and
  replay-based reconstruction.
- Added immutable ledger genesis policy, canonical hash material containing
  Glyph mode/key ID, authenticated HMAC Glyph chains, and runtime keyrings.
- Added historical key validation, key-rotation events, fail-closed startup,
  and cross-process Windows/POSIX writer locking.
- Added persistent A Priori, A Posteriori, and Collective indexes; SQLite
  retrieval receipts; loopback-only service defaults; no-default CORS; and
  remote token enforcement.
- Flattened the repository, added `pyproject.toml`, CI, and regression tests.
- Validation at this checkpoint: 11 regression tests passing, API smoke test,
  compile check, and `git diff --check`.

## 2026-09-13 — Active cognitive-adaptation completion

- Verified the remaining v1 gaps: public retrieval aliases, exact retrieval
  snapshots, SKG edge lifecycle/merge, causal outcome evaluation, and adaptive
  retrieval ranking.
- Implemented forensic retrieval snapshots, canonical public retrieval modes,
  replayable SKG edge lifecycle, node merge evidence, causal outcome events,
  and persistent utility-informed ranking.
- Added and passed `test_complete_cognitive_memory_cycle`, proving retrieval →
  outcome → immutable evaluation → edge transition → changed persisted ranking
  after restart, with Vault and Glyph chains intact.
- Current validation: 12 regression/integration tests passing.

## 2026-09-13 — Licensing

- Added the Spruked proprietary and confidential software license.

## 2026-09-13 — Prototype freeze and v1 hardening contract

- Reclassified the current implementation as **v0.2.0 — Functional Prototype**.
- Corrected documentation that had implied completed cognitive architecture.
- Recorded measurable v0.3 through v1.0 requirements covering three-SKG
  domain isolation, epistemic authority, outcome evidence, hybrid retrieval,
  security, recovery, scale, inspection, anchoring, replication, and
  independent review.

## 2026-09-13 — v0.3 three-SKG and backend foundation

- Replaced the mixed-domain SKG with A Priori, A Posteriori, and Collective
  logical graph domains, routed only by Vault atom epistemic origin.
- Made graph mutation event-first: immutable Vault evidence is appended before
  derived graph state changes; a failed Vault event leaves graph state intact.
- Preserved `memory_core.skg` imports through a compatibility facade and moved
  long-term entry provenance out of SKG topology.
- Added the zero-dependency Python reference backend and optional GraphQLite
  derived-state mirror. GraphQLite was verified on Windows with Python 3.14
  using its current 0.8.0 wheel and a Cypher traversal probe.
