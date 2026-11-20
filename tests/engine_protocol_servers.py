"""Local protocol servers for adapter conformance tests. SIMULATED ENGINES.

These speak the same HTTP protocols as the engines HARO adapts (Ollama native,
OpenAI-compatible chat, OpenAI-compatible audio, llama-server health) so the
adapters' wire handling, streaming, cancellation and error paths can be tested
without the real engines. They perform no inference. Results produced against
them prove protocol and contract behaviour only, never engine behaviour,
latency or memory use. Live verification is done by
``engine_adapters/live_validation.py`` on a machine running the real engines.
"""
from __future__ import annotations

import io
import json
import math
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def sine_wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    """Generate a real, valid WAV tone for audio-protocol tests."""
    frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                      for i in range(int(seconds * rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return buf.getvalue()


class SimulatedEngine:
    """Shared state + server. ``slow_tokens_s`` makes streams slow for cancellation tests."""

    def __init__(self, *, require_bearer: str | None = None, device: "SimulatedDevice | None" = None):
        self.require_bearer = require_bearer
        self.device = device
        self.installed = {"model-a:latest": 400 * 1024 * 1024, "model-b:latest": 900 * 1024 * 1024}
        self.loaded: dict[str, int] = {}
        self.slow_tokens_s = 0.0
        self.tokens = ["Resource ", "accounting ", "keeps ", "voice ", "turns ", "responsive."]
        self.requests: list[tuple[str, str, dict]] = []
        self.disconnects = 0
        self.fail_next: int | None = None
        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _send(self, code: int, body: bytes, ctype: str = "application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj):
                self._send(code, json.dumps(obj).encode())

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

            def _authorized(self) -> bool:
                if engine.require_bearer is None:
                    return True
                if self.headers.get("Authorization") == f"Bearer {engine.require_bearer}":
                    return True
                self._json(401, {"error": "unauthorized"})
                return False

            def _stream(self, chunks, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for chunk in chunks:
                        data = chunk.encode()
                        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                        if engine.slow_tokens_s:
                            time.sleep(engine.slow_tokens_s)
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    engine.disconnects += 1

            def do_GET(self):
                engine.requests.append(("GET", self.path, {}))
                if not self._authorized():
                    return
                if self.path == "/api/version":
                    return self._json(200, {"version": "simulated"})
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": n, "size": s, "digest": "sim",
                                                        "details": {"parameter_size": "sim"}}
                                                       for n, s in engine.installed.items()]})
                if self.path == "/api/ps":
                    return self._json(200, {"models": [{"name": n, "size": s, "size_vram": s}
                                                       for n, s in engine.loaded.items()]})
                if self.path in ("/v1/models", "/models"):
                    return self._json(200, {"data": [{"id": "remote-model", "owned_by": "sim"}]})
                if self.path == "/health":
                    return self._json(200, {"status": "ok"})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                raw = self._body()
                ctype = self.headers.get("Content-Type", "")
                payload = json.loads(raw) if ctype.startswith("application/json") else {"_multipart": raw}
                engine.requests.append(("POST", self.path, payload))
                if not self._authorized():
                    return
                if engine.fail_next is not None:
                    code, engine.fail_next = engine.fail_next, None
                    return self._json(code, {"error": "simulated failure"})
                if self.path == "/api/generate" and "prompt" not in payload:
                    name = payload["model"] if ":" in payload["model"] else payload["model"] + ":latest"
                    if name not in engine.installed:
                        return self._json(404, {"error": f"model '{name}' not found"})
                    if payload.get("keep_alive") == 0:
                        if engine.loaded.pop(name, None) is not None:
                            engine._device_delta(-engine.installed[name])
                    elif name not in engine.loaded:
                        engine.loaded[name] = engine.installed[name]
                        engine._device_delta(engine.installed[name])
                    return self._json(200, {"model": name, "done": True, "load_duration": 1234})
                if self.path in ("/api/chat", "/api/generate"):
                    name = payload["model"] if ":" in payload["model"] else payload["model"] + ":latest"
                    implicit = 0 if name in engine.loaded else 5_000_000
                    if implicit:
                        engine.loaded[name] = engine.installed[name]
                        engine._device_delta(engine.installed[name])
                    key = "message" if self.path == "/api/chat" else "response"
                    records = []
                    for tok in engine.tokens:
                        piece = {"role": "assistant", "content": tok} if key == "message" else tok
                        records.append(json.dumps({"model": name, key: piece, "done": False}) + "\n")
                    records.append(json.dumps({"model": name, key: {"role": "assistant", "content": ""} if key == "message" else "",
                                               "done": True, "done_reason": "stop", "eval_count": len(engine.tokens),
                                               "eval_duration": 1000, "load_duration": implicit}) + "\n")
                    return self._stream(records, "application/x-ndjson")
                if self.path in ("/v1/chat/completions", "/chat/completions"):
                    if not payload.get("stream"):
                        return self._json(200, {"choices": [{"message": {"content": "".join(engine.tokens)},
                                                             "finish_reason": "stop"}],
                                                "usage": {"completion_tokens": len(engine.tokens)}})
                    events = [f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in engine.tokens]
                    events.append(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': {'completion_tokens': len(engine.tokens)}})}\n\n")
                    events.append("data: [DONE]\n\n")
                    return self._stream(events, "text/event-stream")
                if self.path in ("/v1/audio/speech", "/audio/speech"):
                    if not payload.get("voice"):
                        return self._json(400, {"error": "voice required"})
                    return self._send(200, sine_wav(), "audio/wav")
                if self.path in ("/v1/audio/transcriptions", "/audio/transcriptions"):
                    body = payload["_multipart"]
                    if b'name="file"' not in body or b"RIFF" not in body:
                        return self._json(400, {"error": "file required"})
                    return self._json(200, {"text": "simulated transcript", "language": "en"})
                self._json(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _device_delta(self, size_bytes: int) -> None:
        if self.device is not None:
            with self.device.lock:
                self.device.used_mib += size_bytes // (1024 * 1024)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "SimulatedEngine":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


class SimulatedDevice:
    """Programmable accelerator telemetry for admission tests (SIMULATED DEVICE)."""

    vendor = "simulated"

    def __init__(self, device_id: str = "SIM-GPU-0", total_mib: int = 8151, used_mib: int = 1000):
        self.device_id = device_id
        self.total_mib = total_mib
        self.used_mib = used_mib
        self.fail = False
        self.lock = threading.Lock()

    def devices(self):
        from engine_adapters.device_telemetry import DeviceSnapshot
        if self.fail:
            raise OSError("simulated telemetry outage")
        with self.lock:
            return [DeviceSnapshot(self.device_id, "0", "simulated", self.vendor, self.total_mib,
                                   self.used_mib, self.total_mib - self.used_mib)]

    def process_memory_mib(self):
        return None
