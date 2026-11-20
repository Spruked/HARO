"""Contract, protocol-adapter and fabric tests.

Labels used in test names and docstrings:
  [recorded]  input is a real measurement recorded on the RTX 5060 test machine
  [simulated] runs against local protocol servers / a simulated device; proves
              wire handling and HARO logic only, not engine behaviour or memory
No test here contacts a real inference engine. See engine_adapters/live_validation.py.
"""
import os
import socket
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from engine_adapters import (
    CAP_CHAT, CAP_SPEECH_RECOGNITION, CAP_SPEECH_SYNTHESIS, CAP_TEXT_GENERATION,
    LIFECYCLE_ENGINE_MANAGED, LIFECYCLE_NONE, LIFECYCLE_OWNED_PROCESS, MEASURE_ENGINE_REPORTED,
    PRIORITY_BACKGROUND, PRIORITY_INTERACTIVE, RESIDENCY_COLD, RESIDENCY_RESIDENT, STATUS_CANCELLED,
    STATUS_ERROR, STATUS_OK, STATUS_REFUSED, STATUS_TIMEOUT, Binding, CancelToken, CapabilityRequest,
    EngineAdapter, Fabric, InferenceRequest, UnsupportedOperation,
)
from engine_adapters import transport
from engine_adapters.config import build_fabric, load_config
from engine_adapters.device_telemetry import NvidiaSmiTelemetry
from engine_adapters.llama_cpp import LlamaServerAdapter
from engine_adapters.ollama import OllamaAdapter
from engine_adapters.openai_audio import OpenAIAudioAdapter, describe_wav
from engine_adapters.openai_compatible import OpenAICompatibleAdapter
from haro_integration import HaroSubsystems
from tests.engine_protocol_servers import SimulatedDevice, SimulatedEngine, sine_wav

# [recorded] nvidia-smi values from HARO-Test-Lab/results/RTX-5060-8GB (Nov 7, 2026).
RTX5060_ROW = "0, GPU-a96299dc-a636-f16a-1a96-46b39f5fe64c, NVIDIA GeForce RTX 5060, 8151, 6443, 1453, 621.14\n"


def chat_request(model="model-a", **kw):
    return InferenceRequest(CAP_CHAT, model, {"messages": [{"role": "user", "content": "hi"}]}, **kw)


class TelemetryTests(unittest.TestCase):
    def test_recorded_rtx5060_row_parses_exactly(self):
        """[recorded]"""
        telemetry = NvidiaSmiTelemetry(runner=lambda args, t: RTX5060_ROW)
        (snap,) = telemetry.devices()
        self.assertEqual(snap.device_id, "GPU-a96299dc-a636-f16a-1a96-46b39f5fe64c")
        self.assertEqual((snap.total_mib, snap.used_mib, snap.free_mib), (8151, 6443, 1453))
        self.assertEqual(snap.driver, "621.14")

    def test_unreported_memory_is_none_not_zero(self):
        """WDDM drivers can print [N/A]; HARO must not turn that into a number."""
        row = "0, GPU-x, Card, [N/A], [N/A], [N/A], 1\n"
        (snap,) = NvidiaSmiTelemetry(runner=lambda a, t: row).devices()
        self.assertIsNone(snap.free_mib)
        per_process = NvidiaSmiTelemetry(runner=lambda a, t: "1234, [N/A]\n").process_memory_mib()
        self.assertIsNone(per_process)


class TransportTests(unittest.TestCase):
    def test_cancel_interrupts_a_stream_promptly(self):
        """[simulated]"""
        with SimulatedEngine() as engine:
            engine.slow_tokens_s = 0.5
            cancel = CancelToken()
            threading.Timer(0.3, cancel.cancel, args=("barge-in",)).start()
            adapter = OllamaAdapter("o", engine.url)
            started = time.monotonic()
            result = adapter.execute(chat_request(timeout_s=30), cancel)
            self.assertEqual(result.status, STATUS_CANCELLED)
            self.assertIn("barge-in", result.cancel_reason)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_whole_exchange_timeout_is_reported_as_timeout(self):
        """[simulated]"""
        with SimulatedEngine() as engine:
            engine.slow_tokens_s = 0.5
            result = OllamaAdapter("o", engine.url).execute(chat_request(timeout_s=0.8), CancelToken())
            self.assertEqual(result.status, STATUS_TIMEOUT)

    def test_unreachable_engine_is_an_error_not_a_hang(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        result = OllamaAdapter("o", f"http://127.0.0.1:{port}").execute(chat_request(), CancelToken())
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertIn("EngineUnavailable", result.error)

    def test_credentials_in_base_url_are_rejected(self):
        with self.assertRaises(ValueError):
            transport.split_base_url("https://user:secret@example.invalid/v1")


class OllamaProtocolTests(unittest.TestCase):
    """[simulated] Ollama-native protocol handling."""

    def test_lifecycle_reports_engine_memory_and_releases(self):
        with SimulatedEngine() as engine:
            adapter = OllamaAdapter("o", engine.url)
            self.assertEqual(adapter.describe().lifecycle, LIFECYCLE_ENGINE_MANAGED)
            self.assertTrue(adapter.describe().implicit_load_on_execute)
            states = {s.model: s for s in adapter.list_models()}
            self.assertEqual(states["model-a:latest"].residency, RESIDENCY_COLD)
            loaded = adapter.load("model-a", keep_alive_s=-1)
            self.assertEqual(loaded.status, STATUS_OK)
            self.assertEqual(loaded.device_memory_mib, 400)
            self.assertEqual(loaded.memory_measurement, MEASURE_ENGINE_REPORTED)
            self.assertEqual(engine.requests[-2][2]["keep_alive"], -1)
            unloaded = adapter.unload("model-a")
            self.assertEqual((unloaded.status, unloaded.residency_after), (STATUS_OK, RESIDENCY_COLD))
            self.assertEqual(unloaded.device_memory_mib, 400)

    def test_chat_stream_text_timings_and_implicit_load_flag(self):
        with SimulatedEngine() as engine:
            adapter = OllamaAdapter("o", engine.url)
            result = adapter.execute(chat_request(max_output_tokens=16, temperature=0, seed=7), CancelToken())
            self.assertEqual(result.status, STATUS_OK)
            self.assertEqual(result.output["text"], "".join(engine.tokens))
            self.assertIn("first_output_s", result.timings)
            self.assertGreater(result.engine_metrics["implicit_load_ns"], 0)
            sent = engine.requests[-1][2]
            self.assertEqual(sent["options"], {"num_predict": 16, "temperature": 0, "seed": 7})

    def test_unknown_model_load_fails_explicitly(self):
        with SimulatedEngine() as engine:
            result = OllamaAdapter("o", engine.url).load("missing-model")
            self.assertEqual(result.status, STATUS_ERROR)
            self.assertIn("404", result.error)

    def test_unserved_capability_is_refused(self):
        with SimulatedEngine() as engine:
            req = InferenceRequest(CAP_SPEECH_SYNTHESIS, "model-a", {"text": "x"})
            self.assertEqual(OllamaAdapter("o", engine.url).execute(req, CancelToken()).status, STATUS_REFUSED)


class OpenAICompatibleTests(unittest.TestCase):
    """[simulated] OpenAI-compatible chat protocol, including credential handling."""

    def test_remote_streaming_with_env_credential(self):
        with SimulatedEngine(require_bearer="test-credential") as engine:
            os.environ["HARO_TEST_KEY"] = "test-credential"
            try:
                adapter = OpenAICompatibleAdapter("r", engine.url + "/v1", api_key_env="HARO_TEST_KEY")
                self.assertTrue(adapter.health().healthy)
                result = adapter.execute(chat_request("remote-model"), CancelToken())
                self.assertEqual(result.status, STATUS_OK)
                self.assertEqual(result.output["text"], "".join(engine.tokens))
                self.assertEqual(result.engine_metrics["usage"], {"completion_tokens": len(engine.tokens)})
                self.assertNotIn("test-credential", repr(result.to_dict()) + repr(adapter.describe()))
            finally:
                del os.environ["HARO_TEST_KEY"]

    def test_missing_credential_fails_without_leaking(self):
        with SimulatedEngine(require_bearer="x") as engine:
            adapter = OpenAICompatibleAdapter("r", engine.url + "/v1", api_key_env="HARO_TEST_KEY_ABSENT")
            result = adapter.execute(chat_request("remote-model"), CancelToken())
            self.assertEqual(result.status, STATUS_ERROR)
            self.assertIn("HARO_TEST_KEY_ABSENT", result.error)
            self.assertFalse(adapter.health().healthy)

    def test_lifecycle_is_explicitly_unsupported(self):
        adapter = OpenAICompatibleAdapter("r", "https://example.invalid/v1")
        self.assertEqual(adapter.describe().lifecycle, LIFECYCLE_NONE)
        with self.assertRaises(UnsupportedOperation):
            adapter.load("m")

    def test_engine_http_error_is_surfaced(self):
        with SimulatedEngine() as engine:
            engine.fail_next = 503
            result = OpenAICompatibleAdapter("r", engine.url + "/v1").execute(chat_request("m"), CancelToken())
            self.assertEqual(result.status, STATUS_ERROR)
            self.assertIn("503", result.error)


class AudioProtocolTests(unittest.TestCase):
    """[simulated] OpenAI-compatible audio protocol."""

    def test_synthesis_writes_validated_wav_then_transcribes_it(self):
        with SimulatedEngine() as engine, tempfile.TemporaryDirectory() as tmp:
            adapter = OpenAIAudioAdapter("voice", engine.url + "/v1")
            out = Path(tmp) / "speech.wav"
            synth = adapter.execute(InferenceRequest(CAP_SPEECH_SYNTHESIS, "tts-model", {
                "text": "Test of the speech adapter.", "voice": "profile-1", "output_path": str(out)}), CancelToken())
            self.assertEqual(synth.status, STATUS_OK)
            self.assertTrue(out.is_file())
            self.assertAlmostEqual(synth.output["duration_s"], 0.5, places=2)
            self.assertIsNotNone(synth.engine_metrics["real_time_factor"])
            stt = adapter.execute(InferenceRequest(CAP_SPEECH_RECOGNITION, "stt-model",
                                                   {"audio_path": str(out)}), CancelToken())
            self.assertEqual(stt.status, STATUS_OK)
            self.assertEqual(stt.output["text"], "simulated transcript")

    def test_voice_is_never_defaulted(self):
        with SimulatedEngine() as engine, tempfile.TemporaryDirectory() as tmp:
            result = OpenAIAudioAdapter("voice", engine.url + "/v1").execute(InferenceRequest(
                CAP_SPEECH_SYNTHESIS, "tts", {"text": "x", "output_path": str(Path(tmp) / "a.wav")}), CancelToken())
            self.assertEqual(result.status, STATUS_REFUSED)

    def test_invalid_wav_is_rejected(self):
        with self.assertRaises(Exception):
            describe_wav(b"RIFF....not audio")
        self.assertGreater(describe_wav(sine_wav())["frames"], 0)


FAKE_LLAMA_SERVER = textwrap.dedent('''\
    #!{python}
    # SIMULATED llama-server: real process, real HTTP, no inference.
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1]); alias = args[args.index("--alias") + 1]
    if "--crash" in args: sys.exit(3)
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _j(self, obj):
            b = json.dumps(obj).encode(); self.send_response(200)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            self._j({{"status": "ok"}} if self.path == "/health" else {{"data": [{{"id": alias}}]}})
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._j({{"choices": [{{"message": {{"content": "owned reply"}}, "finish_reason": "stop"}}]}})
    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.name == "posix", "simulated server binary is a POSIX script")
class OwnedProcessTests(unittest.TestCase):
    """[simulated binary] Real process ownership: start, health, execute, stop, refusals."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = Path(self.tmp.name) / "llama-server"
        self.server.write_text(FAKE_LLAMA_SERVER.format(python=sys.executable))
        self.server.chmod(self.server.stat().st_mode | stat.S_IEXEC)
        self.gguf = Path(self.tmp.name) / "model.gguf"
        self.gguf.write_bytes(b"GGUF")

    def tearDown(self):
        self.tmp.cleanup()

    def adapter(self, port, extra=()):
        return LlamaServerAdapter("llama", server_path=str(self.server), models={"m1": str(self.gguf)},
                                  port=port, extra_args=extra, log_dir=self.tmp.name, startup_timeout_s=20)

    def test_load_execute_unload_and_process_is_gone(self):
        adapter = self.adapter(free_port())
        self.assertEqual(adapter.describe().lifecycle, LIFECYCLE_OWNED_PROCESS)
        refused = adapter.execute(InferenceRequest(CAP_CHAT, "m1", {"messages": [{"role": "user", "content": "x"}]},
                                                   stream=False), CancelToken())
        self.assertEqual(refused.status, STATUS_REFUSED)    # no implicit load
        loaded = adapter.load("m1")
        self.assertEqual(loaded.status, STATUS_OK, loaded.error)
        pid = loaded.details["pid"]
        self.assertEqual([s.residency for s in adapter.list_models()], [RESIDENCY_RESIDENT])
        result = adapter.execute(InferenceRequest(CAP_CHAT, "m1", {"messages": [{"role": "user", "content": "x"}]},
                                                  stream=False), CancelToken())
        self.assertEqual((result.status, result.output["text"]), (STATUS_OK, "owned reply"))
        unloaded = adapter.unload("m1")
        self.assertEqual(unloaded.status, STATUS_OK)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(adapter.health().healthy)

    def test_refuses_to_start_on_an_occupied_port(self):
        port = free_port()
        first = self.adapter(port)
        self.assertEqual(first.load("m1").status, STATUS_OK)
        try:
            second = self.adapter(port)
            with self.assertRaises(Exception) as ctx:
                second.load("m1")
            self.assertIn("does not own", str(ctx.exception))
        finally:
            first.close()

    def test_crash_during_startup_is_reported_with_log(self):
        result = self.adapter(free_port(), extra=("--crash",)).load("m1")
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertIn("exited with code 3", result.error)


class _CountingAdapter:
    """Wraps an adapter to observe the order in which the fabric dispatches work."""

    def __init__(self, inner: EngineAdapter, log: list):
        self.inner, self.log = inner, log

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def execute(self, request, cancel):
        self.log.append(request.inputs.get("tag"))
        return self.inner.execute(request, cancel)


class FabricTests(unittest.TestCase):
    """[simulated] Routing, invariants, admission, residency, eviction and records."""

    def setUp(self):
        self.device = SimulatedDevice(used_mib=1000)     # 7151 MiB free of 8151
        self.engine = SimulatedEngine(device=self.device).__enter__()
        self.remote = SimulatedEngine().__enter__()
        self.local = OllamaAdapter("local", self.engine.url, device_id=self.device.device_id)
        self.cloud = OpenAICompatibleAdapter("cloud", self.remote.url + "/v1")

    def tearDown(self):
        self.engine.__exit__()
        self.remote.__exit__()

    def fabric(self, bindings, **kw):
        return Fabric({"local": self.local, "cloud": self.cloud}, bindings, telemetry=self.device,
                      safety_margin_mib=0, **kw)

    def ask(self, **kw):
        defaults = dict(capability=CAP_CHAT, inputs={"messages": [{"role": "user", "content": "hi"}]},
                        client_id="test-client", queue_timeout_s=2, timeout_s=10)
        defaults.update(kw)
        return CapabilityRequest(**defaults)

    def test_remote_requires_explicit_permission_and_never_is_a_silent_fallback(self):
        fabric = self.fabric([Binding(CAP_CHAT, "cloud", "remote-model")])
        refused = fabric.run(self.ask())
        self.assertEqual(refused.status, STATUS_REFUSED)
        self.assertIn("remote execution not allowed", repr(refused.decisions))
        allowed = fabric.run(self.ask(allow_remote=True))
        self.assertEqual(allowed.status, STATUS_OK)
        fabric.close()

    def test_no_fallback_unless_requested(self):
        bindings = [Binding(CAP_CHAT, "local", "model-a", resident_mib=None, transient_mib=100),
                    Binding(CAP_CHAT, "cloud", "remote-model")]
        fabric = self.fabric(bindings)
        outcome = fabric.run(self.ask(allow_remote=True))
        self.assertEqual(outcome.status, STATUS_REFUSED)            # first binding lacks a memory figure
        self.assertIn("no resident memory figure", outcome.error)
        fallback = fabric.run(self.ask(allow_remote=True, allow_fallback=True))
        self.assertEqual(fallback.status, STATUS_OK)
        self.assertEqual(fallback.binding["adapter_id"], "cloud")
        fabric.close()

    def test_loads_through_adapter_then_reuses_residency(self):
        fabric = self.fabric([Binding(CAP_CHAT, "local", "model-a", resident_mib=400, transient_mib=200)])
        first = fabric.run(self.ask())
        self.assertEqual(first.status, STATUS_OK, first.error)
        self.assertTrue(any("load" in d for d in first.decisions))
        self.assertEqual(self.device.used_mib, 1400)
        second = fabric.run(self.ask())
        self.assertFalse(any("load" in d for d in second.decisions))
        self.assertEqual(second.result["engine_metrics"]["implicit_load_ns"], 0)
        fabric.close()

    def test_insufficient_memory_is_refused_not_overcommitted(self):
        self.device.used_mib = 8000      # 151 MiB free
        fabric = self.fabric([Binding(CAP_CHAT, "local", "model-a", resident_mib=400, transient_mib=200)])
        outcome = fabric.run(self.ask(queue_timeout_s=0.5))
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertIn("not admitted", outcome.error)
        self.assertEqual(self.engine.loaded, {})
        fabric.close()

    def test_telemetry_outage_fails_closed(self):
        fabric = self.fabric([Binding(CAP_CHAT, "local", "model-a", resident_mib=400, transient_mib=200)])
        self.device.fail = True
        outcome = fabric.run(self.ask())
        self.assertNotEqual(outcome.status, STATUS_OK)
        self.assertEqual(self.engine.loaded, {})
        fabric.close()

    def test_eviction_only_of_idle_models_haro_loaded(self):
        self.device.used_mib = 6800      # 1351 MiB free
        bindings = [Binding(CAP_CHAT, "local", "model-a", resident_mib=400, transient_mib=100),
                    Binding(CAP_TEXT_GENERATION, "local", "model-b", resident_mib=900, transient_mib=100)]
        no_evict = self.fabric(bindings)
        self.assertEqual(no_evict.run(self.ask()).status, STATUS_OK)               # model-a resident: 951 free
        blocked = no_evict.run(self.ask(capability=CAP_TEXT_GENERATION, inputs={"prompt": "x"}, queue_timeout_s=0.5))
        self.assertEqual(blocked.status, STATUS_REFUSED)
        self.assertEqual(no_evict.unload("local", "model-a")["status"], STATUS_OK)
        no_evict.close()
        evicting = self.fabric(bindings, allow_eviction=True)
        self.assertEqual(evicting.run(self.ask()).status, STATUS_OK)               # this fabric loads model-a
        outcome = evicting.run(self.ask(capability=CAP_TEXT_GENERATION, inputs={"prompt": "x"}))
        self.assertEqual(outcome.status, STATUS_OK, outcome.error)
        self.assertTrue(any(d.get("evicted") == "local/model-a" for d in outcome.decisions))
        self.assertNotIn("model-a:latest", self.engine.loaded)
        evicting.close()

    def test_interactive_work_is_admitted_before_background(self):
        order = []
        counting = _CountingAdapter(self.local, order)
        fabric = Fabric({"local": counting}, [Binding(CAP_CHAT, "local", "model-a", resident_mib=400, transient_mib=100)],
                        telemetry=self.device, safety_margin_mib=0)
        self.engine.slow_tokens_s = 0.05
        blocker = fabric.submit(self.ask(inputs={"messages": [{"role": "user", "content": "x"}], "tag": "first"}))
        time.sleep(0.2)
        background = fabric.submit(self.ask(priority=PRIORITY_BACKGROUND,
                                            inputs={"messages": [{"role": "user", "content": "x"}], "tag": "background"}))
        time.sleep(0.1)
        interactive = fabric.submit(self.ask(priority=PRIORITY_INTERACTIVE, queue_timeout_s=10,
                                             inputs={"messages": [{"role": "user", "content": "x"}], "tag": "interactive"}))
        for ticket in (blocker, background, interactive):
            self.assertEqual(ticket.outcome(15).status, STATUS_OK)
        self.assertEqual(order, ["first", "interactive", "background"])
        fabric.close()

    def test_cancel_while_queued_and_while_running(self):
        fabric = self.fabric([Binding(CAP_CHAT, "local", "model-a", resident_mib=400, transient_mib=100)])
        self.engine.slow_tokens_s = 0.3
        running = fabric.submit(self.ask(timeout_s=30))
        time.sleep(0.4)
        queued = fabric.submit(self.ask(queue_timeout_s=30))
        time.sleep(0.2)
        queued.cancel("user moved on")
        running.cancel("barge-in")
        self.assertEqual(queued.outcome(10).status, STATUS_CANCELLED)
        self.assertEqual(running.outcome(10).status, STATUS_CANCELLED)
        fabric.close()

    def test_experience_is_recorded_without_content_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            subsystems = HaroSubsystems(Path(tmp))
            fabric = Fabric({"local": self.local}, [Binding(CAP_CHAT, "local", "model-a", resident_mib=400, transient_mib=100)],
                            telemetry=self.device, subsystems=subsystems, safety_margin_mib=0)
            secret_prompt = "my private question"
            outcome = fabric.run(self.ask(inputs={"messages": [{"role": "user", "content": secret_prompt}]}))
            self.assertEqual(outcome.status, STATUS_OK, outcome.error)
            self.assertEqual(outcome.decisions[0]["vault_logic"]["decision"], "allow")
            self.assertTrue(outcome.evidence_entry_ids)
            fabric.close()
            count = len(subsystems.memory.long_term.entries)
            subsystems.close()
            reopened = HaroSubsystems(Path(tmp))
            entries = reopened.memory.long_term.entries
            self.assertEqual(len(entries), count)
            events = [e.content.get("event") for e in entries]
            kinds = [ev.get("kind") for ev in events if isinstance(ev, dict)]
            self.assertIn("model_load", kinds)
            self.assertIn("capability_request", kinds)
            stored = repr([e.content for e in entries])
            self.assertNotIn(secret_prompt, stored)
            self.assertNotIn("".join(self.engine.tokens), stored)
            reopened.close()

    def test_vault_logic_denial_blocks_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            subsystems = HaroSubsystems(Path(tmp))
            fabric = Fabric({"local": self.local}, [Binding(CAP_CHAT, "local", "model-a", resident_mib=400, transient_mib=100)],
                            telemetry=self.device, subsystems=subsystems, safety_margin_mib=0)
            self.device.fail = True    # live free memory unknown -> governance receives -1 and denies
            outcome = fabric.run(self.ask())
            self.assertEqual(outcome.status, STATUS_REFUSED)
            self.assertEqual(self.engine.loaded, {})
            fabric.close()
            subsystems.close()


class ConfigTests(unittest.TestCase):
    def test_inline_secret_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text('{"adapters": [{"id": "r", "type": "openai_compatible", "base_url": "https://x.invalid/v1", "api_key": "abc"}]}')
            with self.assertRaises(ValueError):
                load_config(path)

    def test_unknown_adapter_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            build_fabric({"adapters": [{"id": "o", "type": "ollama", "base_url": "http://127.0.0.1:1", "bogus": 1}]})

    def test_example_config_builds_without_contacting_engines(self):
        config = load_config(Path(__file__).parents[1] / "deployment" / "test-machine.json")
        config = {**config, "telemetry": None, "fabric": {k: v for k, v in config["fabric"].items()
                                                           if k != "device_concurrency"}}
        for spec in config["adapters"]:
            spec.pop("device", None)
        fabric = build_fabric(config)
        self.assertIn(CAP_SPEECH_SYNTHESIS, {b.capability for b in fabric.bindings})
        fabric.close()


if __name__ == "__main__":
    unittest.main()
