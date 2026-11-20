# A.I.M.S. — Agnostic Immutable Memory System

**HARO deployment:** This directory is HARO's complete host-owned A.I.M.S. system, sourced from [`Spruked/AIMS`](https://github.com/Spruked/AIMS) (private). The same architecture applies with HARO-specific storage identities and workload experience. The v1.0 deployment doctrine is available locally at [`docs/COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.md`](docs/COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.md); verify its adjacent SHA-256 manifest before modification. Source access may require authenticated GitHub credentials. Local copies are not a substitute for current-source freshness checks when access is available.

A.I.M.S. is a local-first, model-agnostic memory system. It preserves
authoritative evidence in append-only ledgers, separates A Priori knowledge
from A Posteriori learning, and derives indexes and current relevance without
rewriting historical truth.

## Ratified and hash-sealed deployment doctrine (v1.0)

**REUSE THE SYSTEM, NOT THE COMPONENTS.** A.I.M.S. and the Vault Logic System are each deployed as a **complete, host-owned system**, not one shared cross-product service and not a selection of reusable files. Their architecture remains intact. The host may change its deployment title, data names, directories, collections, vault labels, and stored content, subject to verified preservation of imports, historical identifiers, schema contracts, and integrity references. When both systems are used, each remains a complete sibling subsystem.

**Canonical authority:** [Complete-System Deployment Doctrine v1.0](docs/COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.md) (PPSAI-CSD-001; frozen 2026-11-19). **SHA-256 (UTF-8 file bytes):** `83a48f537c0084cb919e439989aae4473f6a0c0e8b953815d85018da88759f82`. Verify against [the adjacent checksum manifest](docs/COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.sha256) **before integration**. Its frozen clauses supersede any ambiguous wording in descriptive README examples. Changes to the doctrine require an explicitly approved, newly versioned amendment; do not silently rewrite v1.0. A checksum detects changes but is not itself cryptographic signing or access-control enforcement.

---

## Canonical design intent and complete-system reuse

**A.I.M.S. (Agnostic Immutable Memory System) is a complete, reusable memory architecture, not a collection of files or features to be selected per product.** It provides continuity across sessions and restarts by preserving evidence, distinguishing prior knowledge from observed experience, reconstructing current interpretations, and returning relevant information with provenance. Its host can be ORBOT, HARO, VIV, Communications, or a future product; its design intent does not change with the host.

**Canonical reuse rule:** Preserve and deploy A.I.M.S. as one intact system. Adapt the names and locations of *host-owned data*—storage directories, database namespaces, matrix identifiers, collections, saved files, record categories, and display titles—to the receiving application. Do not rebuild A.I.M.S., cherry-pick its modules, remove architectural responsibilities, or alter the semantics of its ledgers and retrievals to accommodate that application. Renaming must preserve all references, historical record identifiers, cryptographic verification, and migration compatibility.

### What A.I.M.S. is responsible for

| Architectural responsibility | Design intent |
| --- | --- |
| Short-Term Memory (STM) | Fast, capacity-bounded working context. Unretained material may decay or be evicted; durable retention requires a deliberate commitment or review path. |
| Append-only Long-Term Memory | Record observations, interactions, decisions, experiences, and other evidence with provenance and integrity protection. The history is not edited to make later interpretations convenient. |
| Immutable evidence Vault | Preserve the event history from which A Priori and A Posteriori atoms, changes of status, and relationships can be reconstructed. The evidence is authoritative; derived interpretations are not. |
| A Priori knowledge | Represent prior premises, configurations, constraints, and other information introduced before the particular experience being evaluated. Being A Priori is not, by itself, proof of permanent or unchallengeable authority. |
| A Posteriori knowledge | Represent knowledge grounded in subsequent observation, testing, experience, and attributed evidence. New evidence may support, challenge, or retire an interpretation without rewriting its origin. |
| A Priori, A Posteriori, and Collective indexes | Find and reconcile evidence across knowledge domains without replacing the underlying Vault. |
| Structured Knowledge Graphs (SKGs) | Organize relationships, context, support, contradiction, relevance, and current interpretation as rebuildable derived structures. |
| Retrieval Ledger | Preserve what was returned, when, by which retrieval mode, with result snapshots and available feedback. A retrieval is evidence of a return, not proof of the returned claim. |
| Integrity and Glyph lineage | Detect alteration through canonical hashes and, when configured, authenticated Glyph chaining. This is distinct from the Vault Logic System's *visual evaluation-trace glyphs*. |

The full architecture includes these responsibilities even when particular planned enhancements have not yet reached their release gates. **See the release-status section below for implemented versus in-progress capabilities.**

### Operating flow

1. **Receive:** A host supplies an observation, claim, configuration, interaction, or execution result together with its source and context.
2. **Hold or commit:** Transient material enters STM; evidence chosen for durable retention is committed through A.I.M.S.' long-term and Vault mechanisms.
3. **Classify and relate:** A Priori or A Posteriori claims and relationship evidence are recorded through the established A.I.M.S. interfaces.
4. **Derive:** Indexes and SKGs reconstruct searchable and relevant views from retained evidence. Derived state can change or be rebuilt; historical evidence is not overwritten.
5. **Retrieve:** The host requests prior knowledge or experience. A.I.M.S. returns eligible results with provenance and retrieval records.
6. **Evaluate later evidence:** Additional observations, outcome feedback, and maintenance may change *derived* relevance or claim status through recorded events, never by silently erasing the prior record.
7. **Recover:** On restart, authoritative evidence is verified and used to reconstruct derived state according to the implementation's supported recovery behavior.

**Authority boundary:** A.I.M.S. remembers and supplies evidence. It does not inherit the host's right to authorize actions, allocate hardware, release private information, or override current policy. Past success informs the next decision; it does not authorize that decision.

### Host identity is data identity, not architectural identity

A deployment can be named and organized for its host—for example, *HARO Operational Memory* or *ORBOT Memory*—without changing the underlying A.I.M.S. architecture or implementation. Host-specific identifiers should be supplied through existing configuration and data conventions where available. Physical renames affecting imports, manifests, ledger paths, hashes, or stored references require a verified compatibility/migration procedure rather than a blind search-and-replace.

Instances must preserve host and tenant boundaries. A HARO configuration history must not become another host's memory merely because both use A.I.M.S.

### Relationship to the Vault Logic System

The **Vault Logic System** and **A.I.M.S.** are separate, complete systems that may operate as peers under one host:

- The Vault Logic System organizes and evaluates host-defined logic, including its own configuration, rules, evaluation engine, memory matrix, glyph traces, and telemetry.
- A.I.M.S. retains and retrieves evidence, knowledge, experience, and provenance through its own immutable ledger, Vault, indexes, SKGs, STM, and Retrieval Ledger.
- They communicate through explicit host-controlled contracts. Neither system is dismantled to fit inside the other. The Vault Logic System's memory matrix does **not** disappear when A.I.M.S. is present.
- The A.I.M.S. evidence Vault is **not** the Vault Logic System's collection of logic vaults; their similar names do not give them interchangeable responsibilities.

### Example: HARO without a HARO-specific rewrite of A.I.M.S.

HARO can retain hardware and model fingerprints, runtime settings, residency choices, measured resource use, queue and inference latency, admission decisions, failures, recoveries, and validated configuration outcomes as host-domain experience. On a later equivalent workload, it retrieves relevant prior experience to avoid repeating unnecessary exploration. **The active HARO policy, live hardware telemetry, and current reservations must still be checked before admission.** A stored strategy is historical evidence, not a standing permission or guarantee of current capacity.

**Cross-product rule:** Change host-facing names and stored content to fit the product; preserve the complete A.I.M.S. design, its internal relationships, integrity controls, and supported operating behavior.

---

## Release status

**v0.2.0 — Functional Prototype.** The integrity ledger, Vault replay,
indexes, retrieval receipts, and basic adaptive graph behavior are implemented.
The three-SKG model, hybrid retrieval, authority classification, formal outcome
signals, recovery hardening, scale evidence, and Display Hub are not yet
complete. See [the v1 hardening contract](HARDENING_CONTRACT.md) for the
measurable release gates.

The **v0.3 work in progress** includes logical A Priori, A Posteriori, and
Collective SKG domains with a zero-dependency Python reference backend.
GraphQLite is the selected optional embedded accelerator; it remains derived
state and is never Vault authority. Install it only when needed with
`pip install .[graphqlite]`, then provide its backend factory to the host
integration.

> The Vault preserves truth.
>
> The indexes locate truth.
>
> The SKG organizes relevance.
>
> Short-Term Memory carries active cognition.
>
> The Retrieval Ledger remembers every return forever.

## Storage hierarchy

```text
Immutable Vault event ledger     authoritative evidence and provenance
  A Priori / A Posteriori atoms  replayed from append-only Vault events
  evaluation / retirement events evidence for changing interpretation
  relationship evidence          rebuilds the SKG

Derived persistent indexes       A Priori, A Posteriori, Collective
Structured Knowledge Graph       mutable and rebuildable interpretation
Short-Term Memory                temporary, decaying cache
SQLite Retrieval Ledger          permanent retrieval sets and feedback
```

The Vault is never self-pruning. The SKG evaluates relevance and records
resulting evaluation or retirement evidence in the Vault ledger.

## Integrity modes

Every ledger begins with an immutable genesis security policy.

- `visual`: zero-secret SHA-256 hash chain with deterministic visual Glyphs.
- `authenticated`: SHA-256 canonical entry hashes plus an HMAC-SHA-256 Glyph
  chain. `glyph_mode` and `glyph_key_id` are part of the canonical hash.

Authenticated ledgers fail closed without the corresponding runtime keyring.
Secrets are never serialized into ledgers, Vault records, indexes, retrieval
receipts, API responses, or logs. Key rotations create immutable events; old
records continue to validate with their recorded key IDs.

## Local service

```powershell
pip install -r requirements.txt
$env:MEMORY_STORE_PATH = '.\data'
python service.py
```

The default binding is `127.0.0.1:8000`; CORS is disabled by default. Remote
binding requires `AIMS_BIND_HOST` and `AIMS_API_TOKEN` explicitly.

For authenticated mode, provide the active key ID and a local keyring:

```powershell
$env:AIMS_SECURITY_MODE = 'authenticated'
$env:AIMS_GLYPH_KEY_ID = 'local-glyph-2026-01'
$env:AIMS_GLYPH_KEYS_FILE = 'C:\secure\aims-glyph-keys.json'
```

The keyring is a local JSON object mapping key IDs to secrets, for example
`{"local-glyph-2026-01":"runtime-secret"}`. Keep it outside this repository.

## Headless API

The versioned API is presentation-free:

- `GET /api/v1/status`
- `GET /api/v1/memory/summary`
- `GET /api/v1/indexes/status`
- `GET, POST /api/v1/retrievals`
- `GET /api/v1/retrievals/{id}`
- `POST /api/v1/retrievals/{id}/feedback`
- `GET /api/v1/skg/status`
- `GET /api/v1/skg/activity`
- `GET /api/v1/vault/integrity`

The portable display hub is intentionally deferred. The current API is a
prototype surface and does not yet provide production authorization, tenancy,
access auditing, or resource controls.

## Validation

```powershell
python -m unittest discover -s tests -v
```

## License

This repository is proprietary and confidential. See [LICENSE](LICENSE).