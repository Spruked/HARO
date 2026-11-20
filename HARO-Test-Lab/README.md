# HARO Test Lab

Latest measurements and diagnosis: [FOLLOWUP.md](results/RTX-5060-8GB/FOLLOWUP.md). Run `python -B compare_inference.py` for matched direct/Claude/Grok streaming measurements without calling TTS. Claude's missing fixture worker was restored from the existing `runtime/hardware_adaptive_runtime/worker.py`; scheduler and test contents remain unchanged.

- `inference-engine/`: existing real llama.cpp inference probe and its instructions.
- `candidates/haro-claude/`: copy of the existing Claude scheduler and tests.
- `candidates/haro-grok/`: reserved for the Grok candidate.
- `test-config/common-workload.json`: shared comparison settings, initialized from the probe defaults. Set `model` to the chosen GGUF identity before comparing runs; use the same model file contents on both machines.
- `results/<GPU>/<candidate>/`: output folders for measured runs. No hardware results have been generated.

The original source files remain in their original locations. These copies are independent. Grok was copied from `C:\Users\bryan\Desktop\runtime\final cut` as confirmed by the user. Claude's files were renamed to `haro.py`, `test_haro.py`, and `README.md` to match their imports; their contents are unchanged. No missing worker was borrowed from the competing version.

Run `python -B run_lab.py` from this directory for stock unit tests, CLI trials, and the live inference baseline. The harness reads the shared JSON and defaults to the existing localhost server on port 16520. Existing-server mode preserves its model, context and offload settings; the JSON's context/offload values are not applied to an already-running service. Logs and candidate file hashes are saved under the detected GPU folder. Inspect job statuses and output equality as well as exit codes: candidates can exit zero despite job failure or mismatched outputs.

Run `python -B service_trial.py --candidate claude --tts --output results/RTX-5060-8GB/claude/my-service-run` for real Qwen text and speech service calls through the external adapter. Substitute `grok` in both places for the other version. This adapter subclasses the candidate's host backend outside its source tree. It tests host dispatch to existing services, not HARO control of GPU model residency or GPU admission. The stock fixture failures remain visible separately.

From this lab directory, an owned-server baseline command is:

```powershell
python .\inference-engine\inference_probe.py --server-path 'C:\path\to\llama-server.exe' --model 'C:\path\to\model.gguf' --output .\results\RTX-3050-6GB\direct-baseline\run-001.json
```

Choose the matching GPU result folder on each machine. Keep workload settings identical across baseline and candidate runs. The current Claude candidate uses host fixtures and does not yet execute real GPU inference; it requires an inference adapter before its measurements can be compared with the direct baseline.
