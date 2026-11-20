# HARO — Hardware-Adaptive Runtime Orchestrator

HARO is an **application-agnostic and engine-agnostic** runtime. Named consumer applications, provider platforms, model families, personal endpoints and developer identities are not architectural dependencies.

## Responsibilities

- **HARO host:** discovery, capabilities, resource accounting, admission, scheduling, process ownership, execution, cancellation and recovery. Execution authority stays with the host.
- **A.I.M.S.** (`AIMS/`): complete independent operational memory and evidence system; immutable events, derived knowledge, retrieval receipts and restart recovery.
- **Vault Logic** (`HARO_Vault_System/`): complete independent logic system; host rules, glyph traces, separate memory matrix and telemetry. Its evaluation results do not grant execution permission.

See [DOCTRINE_SOURCE_MAP.md](DOCTRINE_SOURCE_MAP.md) for the two authoritative upstreams and frozen doctrine.

## Agnostic engine adapter objective

A replaceable adapter should report capabilities, modalities, health, resource demands and measurements; expose supported load/unload, execute, cancel and recovery actions; and distinguish HARO-owned processes from externally managed services. Unsupported operations must be explicit. The scheduler must not know a vendor-specific route, named model or speech profile.

Remote execution is permitted only by explicit per-request governance, never as a silent fallback. Logical reservations are not observed device memory. Memory and logic evaluations cannot bypass current host authorization.

**Status:** `haro_integration.py` connects the two independent subsystems. An engine-neutral adapter registry and real multi-engine execution have not yet been proven or deployed.

## Verification and experiments

`tests/test_haro_integration.py` and `AIMS/tests/` are the active subsystem regressions. `HARO-Test-Lab/` preserves historical scheduler candidates, provider-specific experimental probes and unmodified original benchmark evidence. A fixture's specific protocol does not constrain HARO's architecture.
