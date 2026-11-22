# HARO Checkpoint 0 — Live status

Date: 2026-11-21 (America/Chicago)

## Completed evidence

- VoiceStudio speech round trip passed twice using voice `40d388b3`; the earlier `/v1/models` timeout was startup latency. A later probe returned HTTP 200.
- Matching local Qwen2.5-7B Q4_K_M HF snapshot symlinks were verified. Their target blob sizes are 3,993,201,344 and 689,872,288 bytes.
- Targeted Ollama Q4 lifecycle passed: health, inventory, load, chat, recovery and unload.
- Ollama Q4 engine-reported residency: 5,219 MiB. Device peak during the 200 ms sampled load/chat operations: 7,659 MiB.
- The llama-server task was restored and port 16520 returned HTTP 200 at 2026-11-20T14:46:26.8866175-06:00.
- NVIDIA CUDA Sysmem Fallback Policy was owner-declared as `Prefer No Sysmem Fallback (Global)`, applied at 2026-11-20T14:24:45.1616126-06:00.

## Remaining Checkpoint 0 work

The full orchestration still needs the speech round trip with peaks in the same run, the permitted Nemotron call, cancellation/recovery measurements for each applicable engine, and A.I.M.S. commit latency plus restart-integrity evidence.

## Optional Ollama behavior

When Ollama is absent, the live validator records `inventory:local-ollama` as `not_run` with `optional engine unavailable: EngineUnavailable: ...`; it does not classify the optional binding as a failed test.
