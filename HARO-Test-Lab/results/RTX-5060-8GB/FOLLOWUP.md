# Follow-up results - November 7, 2026

Scheduler sources and supplied tests were not edited. Orbot, watchdog tasks, and voice services were not restarted, reconfigured, or called during this follow-up. The existing LLM server configuration was preserved.

## Matched real Qwen measurements

Three sequential requests per path, with rotated order, identical streaming request settings, the same existing llama.cpp server and model. Single-job host dispatch through the external adapters.

| Path | Passed | Median wall latency (s) | Median first text (s) | Median engine tokens/s | Maximum sampled whole-device VRAM (MiB) |
|---|---:|---:|---:|---:|---:|
| direct | 3/3 | 0.738 | 0.046 | 76.66 | 6443 |
| claude | 3/3 | 0.742 | 0.047 | 75.22 | 6447 |
| grok | 3/3 | 0.738 | 0.047 | 75.09 | 6443 |

All generated texts identical: True. Both scheduler source hashes unchanged: True.

No GPU efficiency improvement is established. These values measure requests to an already-loaded shared service. VRAM includes the entire device and other processes; 250 ms polling can miss peaks. Scheduler reservations account for host dispatch, not the server model weights. The differences are too small and sample size too limited for a performance ranking.

## Claude packaging and tests

Restored only the missing worker.py dependency from C:/Users/bryan/Desktop/runtime/hardware_adaptive_runtime/worker.py (the existing common fixture worker). No scheduler/test edits or worker borrowing from Grok were used. The lab copy is byte-identical to that source. Tests improved from 24/40 to 39/40 passed. The sole remaining failure, test_host_rss_not_used_for_vram_estimate, expects a non-null host RSS. The worker uses the resource module, unavailable on this Windows Python installation, and deliberately reports null RSS. No fabricated memory value was substituted.

## Grok benchmark integrity

Reproduced: two of six scheduled jobs complete and match direct digests; four fail with CPU execution budget exhausted. Consequently identical_outputs=false does not show numerical corruption in completed work: it includes missing outputs from failed jobs. The printed speed ratio is invalid because it compares six completed direct jobs against only two completed scheduled jobs.

Code inspection shows per-backend scheduler slots and CPU execution slots are separate. Simulated-GPU jobs consume shared CPU slots during execution; admissions check simulated GPU availability before its task acquires a CPU slot, while CPU admissions track their own backend slots. This permits conflicting admissions and CPU execution failures. No scheduling repair was made.

The one failing supplied Grok test, test_telemetry_unavailable_conservative, calls live discover() and assumes nvidia-smi is absent. This RTX 5060 machine has working NVIDIA telemetry. The test does not inject telemetry failure, so it fails its environment assumption; it does not demonstrate failure handling when telemetry is actually unavailable.

## Qwen TTS diagnosis, isolated from HARO

Prior direct and adapter calls returned no audio with AcceleratorError: CUDA error: unknown error. This follow-up did not call the running voice service. Read-only inspection found its process started October 29 and its logs include older successful synthesis activity. A separate fresh Python process using installed PyTorch 2.11.0+cu128 successfully initialized CUDA, created a 16-element GPU tensor, summed it, and synchronized (result 16.0). Driver 621.14; RTX 5060; about 1.4 GiB free device memory at observation.

This rules out a universal inability of the installed PyTorch runtime to perform a basic CUDA operation at the time of the probe. It does not rule out TTS model memory pressure, an operation-specific/runtime issue, or a stale CUDA context in the long-running service. The exact TTS cause remains unproven. No model reload, service restart, driver change, or watchdog interaction was attempted under the voice-service exclusion.

## Evidence

- comparison-20261107T153210Z/comparison.json: per-run latency, engine timings, device memory samples and source hashes.
- diagnosis/grok-benchmark.json: all six direct outputs, scheduled results and events.
- diagnosis/claude-tests-with-worker.txt: 40 tests, one failure.
- diagnosis/tts-environment.json: isolated CUDA probe and installed package versions.

Reproduce matched inference with python -B compare_inference.py. Reproduce native tests/CLI with python -B run_lab.py; these commands do not call TTS.