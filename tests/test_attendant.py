"""Tests for offline-attendant.

These run on any platform (the POSIX lock degrades to an exclusive-create
fallback) and never start a real inference engine. A tiny fake resident server
stands in for llama-server so the HTTP contract is exercised for real.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import attendant as A  # noqa: E402


# --------------------------------------------------------------------- fakes


class _Resident(BaseHTTPRequestHandler):
    """Stand-in for llama-server: /health and /completion."""

    reply = "いらっしゃいませ。ゆっくりどうぞ。"
    delay = 0.0
    last_prompt = ""

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).last_prompt = body.get("prompt", "")
        if self.delay:
            time.sleep(self.delay)
        payload = json.dumps({"content": type(self).reply}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Resident)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _cfg(tmp: str, port: int, **over) -> A.Config:
    base = dict(
        engine=Path("/nonexistent/llama-server"),
        model=Path("/nonexistent/model.gguf"),
        host="127.0.0.1",
        port=port,
        threads=4,
        ctx=512,
        n_predict=48,
        cold_start_s=2.0,
        line_budget_s=5.0,
        runtime_dir=Path(tmp) / "runtime",
        lines={
            "greeting": "挨拶を一文だけ。",
            "closing": "締めの一文を。",
        },
        fallback={
            "greeting": "いらっしゃいませ。",
            "closing": "本日はここまでです。",
        },
    )
    base.update(over)
    return A.Config(**base)


# --------------------------------------------------------------------- tests


class TestSanitize(unittest.TestCase):
    def test_strips_control_and_collapses_whitespace(self):
        self.assertEqual(A.sanitize("  こんにちは\n\n  world\t!"), "こんにちは world !")

    def test_strips_nul_and_escape(self):
        self.assertEqual(A.sanitize("a\x00b\x1bc"), "abc")

    def test_strips_repeated_label(self):
        self.assertEqual(A.sanitize("応答: おはようございます。"), "おはようございます。")
        self.assertEqual(A.sanitize("assistant: hello"), "hello")

    def test_fenced_code_markers_removed(self):
        self.assertEqual(A.sanitize("```hello```"), "hello")

    def test_long_text_cuts_at_sentence_boundary(self):
        text = "あ" * 60 + "。" + "い" * 60 + "。"
        out = A.sanitize(text)
        self.assertLessEqual(len(out), A.MAX_CHARS)
        self.assertTrue(out.endswith("。"))

    def test_short_text_untouched(self):
        self.assertEqual(A.sanitize("短い。"), "短い。")


class TestConfig(unittest.TestCase):
    def test_rejects_non_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(A.ConfigError):
                _cfg(tmp, 8080, host="0.0.0.0")

    def test_rejects_public_bind_constructed_directly(self):
        # The rule must hold for code-built configs too, not only for files.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(A.ConfigError):
                _cfg(tmp, 8080, host="192.168.1.20")

    def test_rejects_line_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(A.ConfigError):
                _cfg(tmp, 8080, fallback={"closing": "おしまい。"})

    def test_accepts_loopback_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            for host in ("127.0.0.1", "localhost", "::1"):
                self.assertEqual(_cfg(tmp, 8080, host=host).host, host)

    def test_load_from_example_file(self):
        path = Path(__file__).resolve().parent.parent / "config.example.toml"
        cfg = A.Config.load(path)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.ctx, 512)
        self.assertEqual(set(cfg.lines), set(cfg.fallback))

    def test_url_normalises_localhost(self):
        with tempfile.TemporaryDirectory() as tmp:
            att = A.Attendant(_cfg(tmp, 9999, host="localhost"))
            self.assertEqual(att._url("/health"), "http://127.0.0.1:9999/health")


class _Harmless(unittest.TestCase):
    """Builds attendents whose temporary directories and locks get cleaned up."""

    def make(self, port: int = 9, **over):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)          # runs last (LIFO)
        att = A.Attendant(_cfg(tmp.name, port, **over))
        self.addCleanup(att.close)            # runs first
        return att, Path(tmp.name)


class TestFailClosed(_Harmless):
    """Rule 1 and 2: line() always returns, and never invents one."""

    def test_missing_engine_returns_fallback(self):
        att, tmp = self.make(port=9)          # port 9: nothing listening
        self.assertEqual(att.line("greeting"), "いらっしゃいませ。")
        self.assertTrue((tmp / "runtime" / "attendant.log").exists())

    def test_unknown_kind_uses_greeting_fallback(self):
        att, _ = self.make(port=9)
        self.assertEqual(att.line("nope"), "いらっしゃいませ。")

    def test_start_is_attempted_once(self):
        att, _ = self.make(port=9)
        self.assertFalse(att.ensure_started())
        self.assertTrue(att._start_failed)
        self.assertFalse(att.ensure_started())   # no second spawn attempt

    def test_empty_model_output_falls_back(self):
        server = _Server()
        self.addCleanup(server.close)
        _Resident.reply = "   \n\x00  "
        self.addCleanup(setattr, _Resident, "reply", "いらっしゃいませ。ゆっくりどうぞ。")
        att, _ = self.make(port=server.port)
        self.assertEqual(att.line("greeting"), "いらっしゃいませ。")


class TestResident(_Harmless):
    def test_uses_resident_server_and_sanitizes(self):
        server = _Server()
        self.addCleanup(server.close)
        _Resident.reply = "応答: いらっしゃいませ。\n\n今日はいい天気ですね。"
        self.addCleanup(setattr, _Resident, "reply", "いらっしゃいませ。ゆっくりどうぞ。")
        att, _ = self.make(port=server.port)
        out = att.line("greeting")
        self.assertEqual(out, "いらっしゃいませ。 今日はいい天気ですね。")
        self.assertNotIn("応答", out)

    def test_prompt_contains_instruction_and_clock_only(self):
        server = _Server()
        self.addCleanup(server.close)
        att, _ = self.make(port=server.port)
        att.line("closing")
        prompt = _Resident.last_prompt
        self.assertIn("締めの一文を。", prompt)
        self.assertIn("現在時刻は", prompt)
        self.assertTrue(prompt.rstrip().endswith("### 応答:"))
        # line() takes only a kind: no caller-supplied text can reach the model.
        self.assertNotIn("greeting", prompt)

    def test_slow_answer_falls_back(self):
        server = _Server()
        self.addCleanup(server.close)
        _Resident.delay = 1.5
        self.addCleanup(setattr, _Resident, "delay", 0.0)
        att, _ = self.make(port=server.port, line_budget_s=0.4)
        started = time.monotonic()
        out = att.line("greeting")
        elapsed = time.monotonic() - started
        self.assertEqual(out, "いらっしゃいませ。")
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
