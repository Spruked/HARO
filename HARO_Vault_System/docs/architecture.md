# Architecture

## Overview
HARO deploys Vault Logic and A.I.M.S. as complete, independent sibling systems. Vault Logic evaluates host-defined rules and emits traces; A.I.M.S. retains evidence and retrieval history. HARO remains the authority for authorization, resource admission, scheduling, execution, and hardware telemetry.

## Components
- **Vaults**: Logic containers
- **Engine**: Execution core
- **Glyphs**: Symbolic tracing
- **Memory**: Persistent storage
- **Telemetry**: Observability

## Flow

```text
HARO operational context
        |
        +--> Vault Logic: active HARO vaults -> decision + trace + telemetry
        |
        +--> A.I.M.S.: committed event/evidence -> provenance + retrieval receipt
        |
        `--> HARO runtime: current policy + live resources -> schedule/execute
```

The two subsystem results are recorded distinctly. A stored memory result or a
Vault Logic decision never bypasses HARO's current live checks.
