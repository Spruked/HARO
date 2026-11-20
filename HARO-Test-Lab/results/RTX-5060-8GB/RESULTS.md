# HARO lab results — November 7, 2026

Historical initial results. See [FOLLOWUP.md](FOLLOWUP.md) for restored Claude packaging, matched measurements, and failure diagnosis.

Hardware: NVIDIA GeForce RTX 5060, 8151 MiB reported VRAM. RTX 3050 was not available for testing.

Both versions remain separate. Claude files were renamed only; candidate source and supplied test contents were not modified. The external service adapter is outside both candidate directories.

| Check | Claude | Grok |
|---|---|---|
| Supplied tests after filename fix | 24/40 passed; 13 failures, 3 errors | 26/27 passed; 1 failures, 0 errors |
| Stock demo/benchmark | Missing worker.py prevents execution | Benchmark runs but identical_outputs=false |
| Real llama.cpp/Qwen via external adapter | PASS | PASS |
| Real Qwen TTS via external adapter | FAIL: service CUDA error, no audio | FAIL: service CUDA error, no audio |

## Real inference

Direct Qwen2.5-7B-Instruct Q4_K_M baseline: PASS. First text 0.250 s; complete request 0.922 s; engine generation 79.78 tokens/s. Device-wide peak VRAM 6451 MiB. This is an already-loaded shared service; VRAM is not attributed to HARO or this request.

The service reports an 8192-token context. The common workload prompt, token limit, temperature and seed were used. Context and offload remain controlled by the already-running server.

| Service trial | LLM request seconds | TTS |
|---|---:|---|
| claude | 0.805 | No audio: CUDA unknown error |
| grok | 0.709 | No audio: CUDA unknown error |

Direct TTS, with no HARO scheduler, returned the same CUDA unknown error and no audio. This isolates the observed TTS failure to the shared service path. No service restart or candidate repair was performed.

These are sequential smoke tests, not a speed ranking. Warm caches and shared GPU activity differ between requests. Both schedulers can dispatch a real language-model request using the same external host-service adapter. Their native GPU fixture backends were not converted to inference adapters.

## Stock failures

- Claude: missing worker.py causes fixture errors; renaming the module fixes the import failure but does not supply that missing dependency.
- Grok: 1 supplied test fails (test_telemetry_unavailable_conservative). Its simulated telemetry-failure path discovers the live GPU. Its benchmark exits zero with identical_outputs=false, so its printed speed ratio is not a valid correctness-preserving speedup.

Raw stock logs: report-20261107T152304587486Z.json and timestamped candidate subfolders. Real service logs: each candidate/services-voice-final/service-trial.json and tts-response.json. Direct TTS: direct-baseline/services-tts/.
