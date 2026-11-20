# HARO Test Lab — isolated experiments

This directory preserves independent scheduler candidates, an engine-specific baseline probe, original measurements, and configurable experiment harnesses. None defines a required consumer application, model, provider or development agent for HARO.

- `candidates/candidate-alpha/`, `candidates/candidate-beta/`: source-identical candidate snapshots, presented with neutral names.
- `archive/initial-candidate-alpha/`: separately preserved initial source.
- `inference-engine/`: historical provider-specific baseline fixture, not a HARO core adapter.
- `test-config/`: common workload definitions.
- `results/`: original benchmark reports and source-integrity records, unchanged to preserve provenance.

New experiments require an explicitly selected, authorized service endpoint:

```bash
python run_lab.py --url http://127.0.0.1:PORT
python compare_inference.py --url http://127.0.0.1:PORT
python service_trial.py --candidate alpha --text-url http://127.0.0.1:PORT/completion --output results/new-trial
```

The specialized speech fixture additionally requires `--tts --speech-url URL --speaker NAME`. Its request shape is specific to that test fixture, not universal. Refer to `inference-engine/README.md` for the original engine baseline. Never treat historical experiment names or machine details as HARO requirements. Test scripts may send real calls to the supplied endpoints; obtain deployment authorization before running.
