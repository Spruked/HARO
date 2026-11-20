# HARO — Consolidated Implementation Plan v1.2 (APPROVED)

**Status:** **APPROVED by the owner, Bryan Spruk, on 2026-11-20** as the implementation baseline. Architecture agreed by Claude and GPT-6 (v1.1 and v1.2 incorporate GPT-6's review). Approval authorizes the plan and its checkpoint order; it does not assert that anything is implemented. Each checkpoint must pass its tests and independent review before the next begins. Changes to this plan require owner approval.
**Repository:** `Spruked/HARO`, base `e3269a8`.
**Doctrine:** PPSAI-CSD-001 v1.0. Verify both local copies hash to
`83a48f537c0084cb919e439989aae4473f6a0c0e8b953815d85018da88759f82` before every checkpoint.

## 0. Ground rules for every checkpoint

1. **No mock data.** No seeded quality, latency, memory or reliability figure anywhere. Unknown is `null` with source `unmeasured`. Example numbers in this document illustrate format only.
2. **Labelled tests.** Tests against simulated engines or devices say so in their docstring. Simulated results never become performance claims.
3. **Don't modify A.I.M.S. internals** (`AIMS/memory_core/*`). Use its public interfaces.
4. **Vault Logic changes stay in HARO's deployment** (`HARO_Vault_System/`). Don't touch the canonical template repository until HARO's version is proven.
5. **Preserve** historical evidence, the Test-Lab candidates, the frozen doctrine and all passing suites. Port existing fabric tests when their code moves; never delete them silently.
6. **One checkpoint per commit series.** Each ends with all suites run, actual counts reported, and a list of what is simulated and what is measured. Claude reviews each checkpoint before the next one starts.

## 1. Authority model (fixed)

| Owner | Decides / holds | Never does |
|---|---|---|
| **HARO runtime** | Physical and execution invariants, leases, admission, execution, residency, cancellation | Override a policy verdict; act on remembered success without live checks |
| **Vault Logic** | Owner policy verdicts: facts in, verdict + matched rules + rules hash out | See the queue, rank, select or execute |
| **A.I.M.S.** | Evidence of what happened; derived knowledge with provenance; retrieval receipts | Grant authority; be called on the admission hot path |
| **Decision journal** | What HARO intended and authorized, written before acting | Stand in for observed outcomes |

**Scope of authority (owner-approved amendment, 2026-11-20).** HARO schedules and executes inference. It does not act in the world, and it is not an ethical authority. Ethical evaluation, consent and any ethical exception belong to the host that acts (e.g. Orbot's Core-4), which remains the system's only override route. HARO has no ethical-exception or override mechanism of its own. No owner command, Vault Logic rule or remembered evidence bypasses HARO's invariants. Owner attestation (§3) only closes the record of an abandoned operation.

Hard invariants live in code and no rule file can weaken them: live device memory, fresh telemetry, principal authorization, explicit remote permission, no silent fallback, verified lease release.

## 2. Target structure (responsibility boundaries, not processes)

- **contracts** — types only: requests, bindings, profiles, evidence records, decision records, ticket/lease states, explanations, provenance.
- **knowledge** (read side) — registry, profiles, in-memory cache of current A.I.M.S. knowledge. Rebuildable.
- **resolver** — pure: `(request, state snapshot, knowledge, policy snapshot) -> plan + explanation`.
- **runtime** — **single writer** of all mutable state (tickets, leases, residency, queues), run as one asyncio event loop. Slow work (loads, inference, unloads, telemetry polling, journal/outbox delivery, A.I.M.S. commits) runs in worker threads and reports back as events.
- **feasibility** — one pure function `feasible(snapshot, plan) -> verdict`. The resolver calls it on a snapshot. The runtime calls it again under its own authority before granting a lease. A plan that looks feasible is not permission.
- **adapters** — existing `engine_adapters/` protocol adapters.
- **governance client** — calls Vault Logic at the three defined points (§6).
- **journal + outbox** — local append-only decision journal; asynchronous delivery to Vault matrix/telemetry and A.I.M.S.
- **service** — loopback HTTP daemon, the single authoritative HARO instance.

## 3. State machines

**Ticket:** `received → queued → admitted → (loading) → executing →` one of `completed | failed | refused | cancelled | timed_out | abandoned`. `cancel_requested` is a flag, not a state.

**Lease:** `held → release_pending → released`, plus `suspect` after a restart.
- Every lease carries a **generation number**. Every slow operation carries `(ticket_id, lease_generation)`, and the loop discards events whose pair is stale.
- A lease is released only when the adapter's declared cancellation semantics are verified, or telemetry shows the memory returned. Otherwise it stays `release_pending`. When a time limit expires it is marked `release_unverified`. **That marking is diagnostic only.** The reserved capacity stays counted in admission and the device stays restricted. Time passing never makes capacity available. Only one of these resolves it: an engine idle report, telemetry showing the memory returned, or an explicit recorded owner attestation (`release_attested`). **An attestation resolves only the uncertainty about an abandoned operation.** It closes the lease record. It never grants admission: every subsequent admission still requires fresh telemetry and sufficient live physical resources. No owner command bypasses those invariants.
- **Stale events.** An event whose `(ticket_id, lease_generation)` is stale has no authority over any lease. Its physical facts are still real, though: a stale `load_completed` means a model may now be resident. Those facts go to residency reconciliation as observations. Such a model is adopted as a HARO-owned orphan with no ticket, and becomes subject to governed unload/eviction.

**Cancellation semantics** are declared per adapter and verified by live validation: `requested`, `transport_closed`, `engine_acknowledged`, `computation_stopped`, `resources_released`. Admission depends only on `resources_released`.

**Client disconnect:** `cancel_on_disconnect` per request; defaults to true for interactive work.

**Restart reconciliation:**
1. Read journal ownership hints: PIDs of processes HARO started, models it loaded, leases in flight.
2. Reconcile with reality. Live recorded processes are adopted as HARO-owned. Engine-reported resident models are adopted with their journal owner, or as `external`.
3. Engines that had in-flight work are `suspect`. Admit no new accelerator work on that device until the engine reports idle, or telemetry shows the in-flight memory resolved, or the owner records an attestation. Elapsed time alone never clears it. While restricted, the reason is visible in every refusal.
4. Non-terminal tickets become `abandoned`. Leases are **never** assumed released.
5. Intent with no observed outcome becomes an `outcome_unknown` event. A completion is never fabricated.

## 4. Selection (deterministic; no weighted-score optimizer)

1. **Eligibility.** Capability support, owner-approved work type, privacy clearance against the request's data class, principal authorization, provider status (cooldown), Vault Logic candidate-policy verdict, feasibility.
2. **Service requirements.** Owner-set targets per work type and latency class, e.g. "interactive conversation: first-output p95 within target". Evaluated only against evidence of the required maturity.
3. **Evidence classification.** Each candidate's relevant evidence is `none | experimental | operational` (§5).
4. **Deterministic preference.** Owner preference data, then residency (resident before cold for interactive work), then measured resource cost, then a stable identity tie-break.

When the selected engine for a work type changes because evidence changed, the decision record states it explicitly. The owner can pin a choice. No numeric optimization until real measurements show a problem that needs it.

## 5. Evidence

**Two independent dimensions:**
- **Provenance**, stored on each observation: `owner_declared | engine_reported | device_measured | externally_evaluated | client_reported`.
- **Maturity**, derived at aggregation time, never stored as truth: `none` (no exact-match evidence), `experimental` (probationary A.I.M.S. atom or validation runs only), `operational` (active A.I.M.S. atom meeting the sample threshold).

**Full population.** Record every attempt: success, failure, refusal, timeout, cancellation, recovery. Each statistic declares its population. Reliability counts every attempt. Latency and memory percentiles use completed attempts and report how many were excluded.

**Fingerprint identity** (exact match required for admission use): device ID, driver version, engine/runtime version, model digest and quantization, context size, binding config hash. Evidence from related configurations may be shown to the owner but is never used for admission.

**Three performance dimensions, kept separate:** engine reliability (HARO measures), model quality (external evaluation records only), task success (client-reported via a feedback call). Define the quality-evidence record schema now. No consumer logic yet.

**Memory models**, declared per binding and confirmed by live validation:
- `engine_managed_residency` — Ollama, llama.cpp; KV cache sized at load.
- `standing_service` — PyTorch speech services whose caching allocator may never visibly release memory. Modelled as standing occupancy with a ceiling, not per-request transient memory.
- `transient_per_request`.

## 6. Vault Logic (HARO deployment completion)

- **Three call points.** (1) Candidate policy, inside resolver eligibility, per candidate. (2) Before remote dispatch. (3) Before any autonomous lifecycle action: eviction, preemption, HARO-initiated unload.
- **Facts in, verdict out.** HARO supplies typed facts. Vault Logic returns verdict, matched rules, rules hash, glyph trace. Owner preference order is policy *data* the resolver reads.
- **Evaluation is separated from recording.** Today `evaluate_haro_context` / `HaroSubsystems.evaluate_and_record()` evaluate and then synchronously write to the matrix, telemetry and A.I.M.S. Split them:
  - a **pure** `evaluate(policy_snapshot, facts) -> verdict`: no I/O, against rules loaded and pinned by hash;
  - a **recording** step that delivers the verdict to the Vault matrix, Vault telemetry and A.I.M.S. through the outbox, keyed by decision ID.

  The verdict itself goes into the decision journal. The resolver and runtime call only the pure function. `evaluate_and_record()` and `evaluate_condition()` stay as compatibility entry points for existing callers and tests. Matrix-based diagnostics (flapping, blanket denial) become asynchronous, which is acceptable because they are diagnostic.
- **Typed declarative predicates** in JSON: numeric comparisons, set membership, and an ordered privacy-class lattice (request data class ≤ provider clearance). No executable code in rules. Unsupported predicates fail rule-set validation.
- **Versioned rules.** Loaded once at startup or on an explicit reload, identified by content hash. Every verdict records the hash.
- **Policy content:** work-type approvals, privacy clearances, latency targets per work type/latency class, required evidence maturity per priority class, remote-provider rules, eviction and preemption eligibility.
- **Fixes:**
  - Two agreeing allow rules are not a conflict. Only allow vs. deny at the same priority is unresolved.
  - Move the resource/residency checks that are currently hardcoded out of the evaluator. They are HARO invariants and belong to the runtime.
  - Reconcile `glyphs/trace.py` with the native `glyph_map.json`/`trace_engine.js` without discarding either.
  - Wire `telemetry.yaml` and `alerts.yaml`.
  - Lay out `vaults/haro/` per `vaults/_template` (`vault.json`, `logic.md`, `rules/`, `tests/`).
- **Memory matrix** = Vault Logic's own bounded working memory (recent verdicts per rule, to detect flapping or blanket denial). Bounded by the TTL in `memory/matrix_store.yaml`. Written off the event loop. Evidence does not live here.
- **Governance required in production.** The daemon refuses to start without a working Vault Logic. `not_evaluated` is possible only under an explicit test-mode flag that is recorded in every decision record.

## 7. A.I.M.S. use (full architecture, through public interfaces)

- **Long-term ledger — observations.** Every load, unload, execution, cancellation, status change and restart reconciliation, with provenance and fingerprint. Each carries a unique `event_id`.
- **Owner declarations — versioned ledger records, NOT A Priori atoms.** A.I.M.S. A Priori atoms are foundational axioms: active on assertion and not retirable. Each config/policy version is committed as a ledger record (version, hash, effective time) so A.I.M.S. preserves what was authorized then. Current authority is the live config and Vault Logic policy. A Priori is reserved for genuinely foundational principles; none are required now.
- **A Posteriori atoms — derived operational knowledge.** HARO computes statistics from the ledger (`long_term.search_entries`) and commits each consolidation via `derive_aposteriori`, linked to its source entries. Atoms **start on probation**. A later consolidation that agrees within tolerance reinforces the atom, which A.I.M.S. promotes to active at its own confidence and support thresholds. Contradicting evidence **for the same fingerprint** challenges the atom through `vault.challenge()`; standing changes only through A.I.M.S.'s own mechanisms. A **fingerprint change does not retire** an atom: its evidence remains valid for its original configuration. It is simply inapplicable to the new one, and becomes applicable again if that configuration returns. Probationary means experimental maturity; active plus the sample threshold means operational.
- **Knowledge-graph links:** runs-on-fingerprint, corroborated-by / contradicted-by (engine-reported vs. device-measured), and a driver/runtime change recorded as a relation between fingerprints, not as retirement of either one's knowledge.
- **Operational knowledge projection — two read paths, never conflated.** `retrieve()` / `VaultIndexes.search()` return only ACTIVE atoms. That is A.I.M.S.'s validated knowledge, and the receipts it produces are A.I.M.S.'s record of returning it.
  - HARO's projection additionally reads its own probationary consolidations through the public Vault interfaces (`memory.vault.all_atoms()` / `get()`, filtered by HARO's atom metadata kind and fingerprint; provenance via `provenance_for()` / `memory.trace()`). Off the event loop.
  - Each atom keeps its A.I.M.S. status. A probationary atom is used **only** as experimental evidence and is never presented as promoted.
  - Each refresh produces a **projection snapshot**: atom IDs, status, confidence and support at refresh time, plus the retrieval receipt for the ACTIVE part, hashed. Decisions cite the snapshot hash. The snapshot is stored in the journal, and its hash goes in the A.I.M.S. summary.
- **Prediction vs. outcome.** When an observed outcome falls outside an atom's prediction:
  - ACTIVE atom: `record_retrieval_feedback` against the receipt that returned it, plus `vault.challenge(atom_id)`.
  - Probationary atom: `vault.challenge(atom_id)` only.

  Agreement reinforces through the next consolidation. Promotion and retirement remain A.I.M.S.'s own transitions.
- **No A.I.M.S. call on the hot path.** Measure ledger growth and commit latency during live validation; set consolidation frequency from those measurements.

## 8. Decisions, snapshots and crash consistency

**Frozen snapshot.** A decision binds the Vault Logic rules hash and the knowledge projection snapshot hash in force at admission (which includes the retrieval receipt of its ACTIVE part). Policy or knowledge changes during execution affect the next decision only. Physical invariants are not frozen: the runtime still reacts to live device and engine state. **Emergency revocation** is a separate explicit owner command. It cancels affected in-flight tickets through the normal cancellation path and is recorded as a revocation event.

**Decision record:** request intent and constraints, principal, candidates and eligibility results, evidence IDs and maturity, rules hash and verdicts, resource snapshot and reservations, ordering reasons, selected plan, admission result, lifecycle transitions, observed outcome, predicted vs. observed. The human-readable explanation is generated from the record.

**Journal.** Append-only JSONL. The decision entry is flushed **before** acting; execution waits only on that local write (measure its latency in live validation). The full decision record stays in the journal. A.I.M.S. receives a summary with the record ID and content hash, sufficient on its own to say what was chosen and why (reason codes, evidence IDs, verdict). Journal rotation can reduce detail but never makes a decision unexplainable.

**Outbox: at-least-once delivery, with idempotent consumers.**
- Delivery to the Vault matrix, Vault telemetry and A.I.M.S. may repeat after a crash. Every delivered record carries its `event_id`/`decision_id`.
- All projections and statistics deduplicate by ID, so a duplicate ledger entry cannot distort knowledge. A delivered-up-to watermark plus a check of recent entries keeps duplicates rare.
- The journal records **intent and authorization**; adapter observations record **what happened**. Recovery preserves the distinction.

## 9. Provider status and preemption

- **Provider status.**
  - Availability comes from health checks plus real call outcomes.
  - HTTP 429 and `Retry-After`/`x-ratelimit-*` are kept by the transport; `HttpError` must keep the response headers.
  - Cooldowns use a monotonic clock.
  - Latency percentiles come from evidence.
  - Every status change is an A.I.M.S. event.
- **Cooperative preemption**, as admission policy on top of the state machines.
  - Background requests may be marked preemptible. When interactive work can't be admitted, HARO cancels eligible preemptible work, subject to a governance verdict at call point 3.
  - The waiting request is admitted only after `resources_released`.
  - Work an engine can't verifiably interrupt is limited by admission duration or concurrency, never promised preemption.

## 10. Service

- **Transport:** loopback-only HTTP using the standard library (threaded HTTP server whose handlers pass commands into the event loop).
- **Single instance:** lock so only one daemon runs.
- **Principals:** per-principal tokens read from files protected by OS permissions. Every request carries its principal into the decision record.
- **Cancel authority:** a ticket can be cancelled only by its owning principal or an admin principal.
- **API:** submit capability request; ticket status and explanation; cancel; residency and inventory snapshot; owner commands (policy reload, pin, revocation, release attestation).
- **Development mode until governance is complete.** From checkpoint 3 until checkpoints 5 and 6 pass review, the daemon runs only in an explicit development/test mode: recorded in every decision record, refusing a production start, and never presented as production-capable. Production mode becomes available only once mandatory Vault Logic governance is in place and verified.

## 11. Checkpoints

| # | Checkpoint | Key acceptance tests (simulated unless stated) |
|---|---|---|
| **0** | **Owner: live validation on the RTX 5060**, in parallel with C1. `python -m engine_adapters.live_validation --config deployment\test-machine.json --voice <voice-id> --allow-remote` (add `--ollama-model` after the first run lists the inventory). | **Live.** Answers: engine-reported vs. device memory, Voice Studio memory behaviour (standing vs. transient), cancellation effect on each engine, A.I.M.S. commit latency |
| **1** | **Foundation corrections.** Remove the `("conversation",)` approval default; make `work_type` required everywhere; declare approvals in `deployment/test-machine.json` (Nemotron chat: reasoning, analysis, planning; STT: transcription; TTS: voice_output); enforce strict `MetricSummary` validation (reject measured/engine_reported without value or with 0 samples, unmeasured with any value, p50 > p95); validate resolver latency/privacy values; move `Binding` into contracts; re-save `structure_tree.txt` as UTF-8; replace `not_evaluated` with an explicit test-mode flag | One test per rejected case; no binding eligible without declared approval |
| **2** | **Contracts.** Decision record, explanation, provenance, ticket/lease states with generations, cancellation-semantics declarations on `AdapterDescriptor`, observation schema (event_id, provenance, fingerprint, population fields), quality-evidence record schema, memory model per binding | Contract validation tests; round-trip serialization |
| **3** | **Runtime.** Event-loop single writer, pure `feasible()`, residency manager, leases with generation checks, journal + outbox (at-least-once, idempotent IDs), restart reconciliation. Existing fabric behaviour ported; existing tests ported | Owner attestation closes an abandoned lease but admission still refuses without fresh telemetry and sufficient memory; stale event cannot touch the reassigned lease but its loaded model is adopted as an orphan; `release_unverified` keeps capacity reserved and the device restricted indefinitely until idle report / telemetry / owner attestation; restart with live owned process → adopted; restart with in-flight load → device suspect; duplicate outbox delivery → no duplicate statistic; external models never evicted |
| **4** | **Service.** Loopback daemon, principals, single-instance lock, cancel authority, `cancel_on_disconnect` | Two client processes cannot over-admit one device; cross-principal cancel refused; second daemon refused; production start refused (development mode only at this stage) |
| **5** | **Vault Logic completion** (§6) | Pure evaluation performs no I/O (asserted); verdict journaled, matrix/telemetry/A.I.M.S. writes delivered via outbox; typed predicates; rules hash on every verdict; agreeing allows → allow; matrix bounded by TTL; production start refused without governance |
| **6** | **Resolver as the only routing path** + three governance call points + frozen snapshots + decision records | Resident candidate preferred for interactive work; targets evaluated only with sufficient maturity; preference change recorded; revocation cancels in-flight work |
| **7** | **A.I.M.S. evidence pipeline** (§7): owner declarations as versioned ledger records, consolidations as probationary atoms, reinforcement/retirement, graph links, receipts, feedback | Restart → knowledge rebuilt; probationary atoms in the projection but labelled experimental and absent from `retrieve()`; probation → active only via A.I.M.S. thresholds; contradicted prediction (same fingerprint) → `challenge()`; fingerprint change → old atom inapplicable but not retired, applicable again on return; failures included in reliability |
| **8** | **Provider status** (§9) | 429 + Retry-After → cooldown → resolver skips → recovery clears |
| **9** | **Cooperative preemption** (§9) | Interactive admitted only after verified release; non-interruptible work bounded |
| **10** | **Integrated acceptance — live on the RTX 5060** | Canonical HARO demonstration: execute with measured resources → record in A.I.M.S. → restart → retrieve and reuse the strategy → revalidate live state → execute again → new provenance, reporting resident weights, per-request memory, historical estimates and live measurements separately. Plus: two client processes sharing one daemon |

## 12. Later (not in this plan)

Evaluation harness producing quality records; workload-aware selection; related-configuration evidence for experimentation; capacity prediction; numeric optimization if evidence shows a need; multi-GPU and multi-host placement; promotion of proven Vault Logic improvements to the canonical template.

## 13. Roles

Codex implements. Claude reviews each checkpoint independently. GPT-6 reviews architecture conformance. The owner approves the plan and any change to it, runs the live validations, and decides anything that changes authority boundaries.
