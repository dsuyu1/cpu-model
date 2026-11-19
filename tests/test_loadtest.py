import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


spec = importlib.util.spec_from_file_location("benchmark", Path(__file__).parents[1] / "scripts/bb_loadtest.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def event(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


def choice(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.payloads.append(payload)
        if self.path == "/redirect":
            self.send_response(307)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        if self.path == "/http-error":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"secret prompt should never appear in report")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.path == "/error":
            self.wfile.write(event({"error": {"message": "secret"}}))
            return
        self.wfile.write(b": heartbeat\n\n")
        self.wfile.write(event(choice({"role": "assistant"})))
        self.wfile.write(event(choice({"reasoning": "internal reasoning"})))
        self.wfile.write(event(choice({"content": "many tokens in one chunk"})))
        if self.path == "/incomplete":
            return
        self.wfile.write(event(choice(finish="length" if self.path == "/length" else "stop")))
        if self.path != "/no-usage":
            self.wfile.write(event({"choices": [], "usage": {
                "prompt_tokens": 100, "completion_tokens": 17, "total_tokens": 117},
                "timings": {"predicted_per_second": 11.5, "secret": "do not save"}}))
        self.wfile.write(b"data: [DONE]\n\n")


class StreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.payloads = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def run_request(self, path):
        return bench.request_one(self.url + path, "test-key", "stub", "private prompt",
                                 32, 3, threading.Barrier(1), bench.time.perf_counter())

    def test_usage_not_chunks_and_reasoning_separate(self):
        row = self.run_request("/ok")
        self.assertTrue(row["ok"])
        self.assertEqual(row["output_chunks"], 2)
        self.assertEqual(row["usage"]["completion_tokens"], 17)
        self.assertLessEqual(row["ttft_reasoning_s"], row["ttft_answer_s"])
        self.assertEqual(row["server_timings"], {"predicted_per_second": 11.5})
        self.assertNotIn("private prompt", json.dumps(row))
        self.assertTrue(self.server.payloads[-1]["stream_options"]["include_usage"])
        self.assertEqual(bench.summarize([row])["completion_tokens"], 17)

    def test_missing_usage_is_not_estimated(self):
        row = self.run_request("/no-usage")
        self.assertTrue(row["ok"])
        self.assertIsNone(bench.summarize([row])["aggregate_end_to_end_tok_s"])

    def test_truncation_reported(self):
        row = self.run_request("/length")
        self.assertTrue(row["truncated"])
        self.assertEqual(bench.summarize([row])["truncated"], 1)

    def test_incomplete_stream_fails(self):
        row = self.run_request("/incomplete")
        self.assertFalse(row["ok"])
        self.assertEqual(row["error"], "incomplete_stream")

    def test_failures_suppress_partial_aggregate(self):
        rows = [self.run_request("/ok"), self.run_request("/error")]
        stats = bench.summarize(rows)
        self.assertEqual(stats["failed"], 1)
        self.assertIsNone(stats["aggregate_end_to_end_tok_s"])
        self.assertNotIn("secret", json.dumps(rows))

    def test_http_errors_redacted(self):
        row = self.run_request("/http-error")
        self.assertEqual(row["error"], "http_401")
        self.assertNotIn("secret", json.dumps(row))

    def test_redirect_not_followed(self):
        count = len(self.server.payloads)
        row = self.run_request("/redirect")
        self.assertEqual(row["error"], "http_307")
        self.assertEqual(len(self.server.payloads), count + 1)

    def test_multiline_sse(self):
        result = list(bench.events(io.BytesIO(b': comment\r\ndata: {"a":\r\ndata: 1}\r\n\r\n')))
        self.assertEqual(json.loads(result[0]), {"a": 1})

    def test_unterminated_sse_rejected(self):
        with self.assertRaises(bench.ProtocolError):
            list(bench.events(io.BytesIO(b'data: {"a":1}\n')))

    def test_four_users_complete(self):
        from types import SimpleNamespace
        args = SimpleNamespace(users=4, url=self.url + "/ok", model="stub",
                               max_tokens=32, timeout=3)
        run = bench.run_burst(args, ["one", "two", "three", "four"], "")
        self.assertEqual(run["summary"]["successful"], 4)
        self.assertEqual(run["summary"]["completion_tokens"], 68)

    def test_cli_report_and_non_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            cmd = [sys.executable, str(Path(__file__).parents[1] / "scripts/bb_loadtest.py"),
                   "--url", self.url + "/ok", "--model", "stub", "--users", "2",
                   "--repeats", "2", "--output", str(output)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(len(report["warmups"]), 1)
            self.assertEqual(len(report["runs"]), 2)
            self.assertEqual(report["runs"][0]["summary"]["completion_tokens"], 34)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            before = output.read_bytes()
            again = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(output.read_bytes(), before)

    def test_cli_failure_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "failed.json"
            proc = subprocess.run([
                sys.executable, str(Path(__file__).parents[1] / "scripts/bb_loadtest.py"),
                "--url", self.url + "/incomplete", "--model", "stub",
                "--warmups", "0", "--repeats", "1", "--output", str(output)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(output.read_text())["runs"][0]["summary"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
