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

**Status:** `haro_integration.py` connects the two independent subsystems. `engine_adapters/` implements the engine-neutral contract, four protocol adapters and the capability fabric. Live RTX 5060 evidence is recorded under `HARO-Test-Lab/results/RTX-5060/`; the full multi-engine Checkpoint 0 remains incomplete. Ollama is optional: when it is not installed, live validation records the binding as unavailable/not-run with the connection reason rather than as a test failure.

## Engine adapters and capability fabric

- `engine_adapters/contract.py` — the only interface the fabric depends on: capabilities, execution location, lifecycle control (HARO-owned process, engine-managed residency, or none), residency, memory-measurement provenance, cancellation, explicit `UnsupportedOperation`.
- Protocol adapters, named for the wire protocol they speak: `ollama.py` (Ollama native), `llama_cpp.py` (llama-server, owned or external), `openai_compatible.py` (OpenAI-compatible chat; remote providers use environment-variable credentials), `openai_audio.py` (OpenAI-compatible speech synthesis and transcription).
- `fabric.py` — capability routing from deployment bindings; remote execution only with explicit per-request permission; no silent fallback; priority admission (interactive before user tasks before background) against live device memory minus reservations; refusal when a memory figure or live telemetry is missing; load/unload only through adapter lifecycle; eviction limited to idle models HARO itself loaded and off by default; Vault Logic evaluation and A.I.M.S. experience records through `haro_integration.py`. Prompts, outputs and transcripts are not stored in HARO's memory, only their shape.
- `config.py` + `deployment/*.json` — the only place engines, endpoints, models and devices are named. Inline credentials are rejected.
- `device_telemetry.py` — vendor-neutral device interface; NVIDIA provider via `nvidia-smi`.

Run the tests from the repository root: `python -m unittest tests.test_engine_adapters` (simulated engines; one recorded RTX 5060 reading).

Run live validation on the engine machine (PowerShell, repository root):

```powershell
python -m engine_adapters.live_validation --config deployment\test-machine.json --voice <voice-id> --ollama-model <installed-model> --allow-remote
# optional HARO-owned process test:
#   --llama-server C:\path\to\llama-server.exe --llama-model C:\path\to\model.gguf --llama-port 17667
```

It writes `HARO-Test-Lab/results/<device>/adapters-<timestamp>/report.json`, labelling every figure by source. Device memory is sampled continuously at 200 ms during measured operations. It loads and unloads only the model named by `--ollama-model` (restoring prior residency), starts a llama-server only on `--llama-port`, and only calls the speech service, any separately managed llama-server and (with `--allow-remote`) the remote provider.

## Verification and experiments

`tests/test_haro_integration.py` and `AIMS/tests/` are the active subsystem regressions. `HARO-Test-Lab/` preserves historical scheduler candidates, provider-specific experimental probes and unmodified original benchmark evidence. A fixture's specific protocol does not constrain HARO's architecture.
