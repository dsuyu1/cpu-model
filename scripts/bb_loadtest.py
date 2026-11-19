#!/usr/bin/env python3
"""Synchronized streaming burst benchmark; Python standard library only."""
import argparse
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class ProtocolError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward credentials or prompts to another endpoint.


def events(response):
    """Decode SSE events, including multiline data and comment heartbeats."""
    data = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        raise ProtocolError("unterminated_sse_event")


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def request_one(url, key, model, prompt, max_tokens, timeout, barrier, origin):
    result = {"ok": False, "usage": None, "server_timings": None,
              "output_chunks": 0, "ttft_output_s": None,
              "ttft_answer_s": None, "ttft_reasoning_s": None,
              "finish_reason": None, "truncated": False}
    try:
        body = json.dumps({"model": model,
                           "messages": [{"role": "user", "content": prompt}],
                           "max_tokens": max_tokens, "temperature": 0,
                           "stream": True,
                           "stream_options": {"include_usage": True}}).encode()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(url, data=body, headers=headers)
        barrier.wait(timeout=30)
        start = time.perf_counter()
        result["start_offset_s"] = start - origin
        # Explicitly bypass environment proxies: benchmark inputs stay on the
        # configured route. socket timeout is inactivity, NOT a total deadline.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        done = False
        with opener.open(req, timeout=timeout) as response:
            if response.headers.get_content_type() != "text/event-stream":
                raise ProtocolError("expected_event_stream")
            for event in events(response):
                if event.strip() == "[DONE]":
                    done = True
                    break
                chunk = json.loads(event)
                if not isinstance(chunk, dict):
                    raise ProtocolError("invalid_event_object")
                if "error" in chunk:
                    raise ProtocolError("server_error_event")
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    # Keep only counts; never arbitrary provider fields.
                    result["usage"] = {
                        field: value for field in ("prompt_tokens", "completion_tokens", "total_tokens")
                        if isinstance((value := usage.get(field)), int)
                        and not isinstance(value, bool) and value >= 0}
                timings = chunk.get("timings")
                if isinstance(timings, dict):
                    result["server_timings"] = {
                        k: v for k, v in timings.items()
                        if k in {"prompt_n", "prompt_ms", "prompt_per_second",
                                 "predicted_n", "predicted_ms", "predicted_per_second", "cache_n"}
                        and isinstance(v, (int, float)) and not isinstance(v, bool)
                        and math.isfinite(v) and v >= 0}
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                if len(choices) != 1 or choices[0].get("index", 0) != 0:
                    raise ProtocolError("unexpected_choices")
                choice = choices[0]
                if choice.get("finish_reason") is not None:
                    reason = choice["finish_reason"]
                    result["finish_reason"] = reason if reason in {
                        "stop", "length", "tool_calls", "content_filter", "function_call"} else "unknown"
                delta = choice.get("delta") or {}
                answer = bool(delta.get("content"))
                reasoning = bool(delta.get("reasoning_content") or delta.get("reasoning"))
                output = answer or reasoning or bool(delta.get("tool_calls"))
                elapsed = time.perf_counter() - start
                for active, field in ((output, "ttft_output_s"),
                                      (answer, "ttft_answer_s"),
                                      (reasoning, "ttft_reasoning_s")):
                    if active and result[field] is None:
                        result[field] = elapsed
                if output:
                    result["output_chunks"] += 1
        if not done or result["finish_reason"] is None:
            raise ProtocolError("incomplete_stream")
        result["ok"] = True
        result["truncated"] = result["finish_reason"] == "length"
    except ProtocolError as exc:
        result["error"] = str(exc)  # Only our fixed messages, no response body.
    except urllib.error.HTTPError as exc:
        result["error"] = f"http_{exc.code}"
        exc.close()
    except Exception as exc:
        result["error"] = type(exc).__name__  # URLs/credentials may appear in str(exc).
    finally:
        end = time.perf_counter()
        result["end_offset_s"] = end - origin
        if "start_offset_s" in result:
            result["duration_s"] = result["end_offset_s"] - result["start_offset_s"]
    return result


def summarize(rows):
    ok = [r for r in rows if r["ok"]]
    starts = [r["start_offset_s"] for r in rows if "start_offset_s" in r]
    wall = max(r["end_offset_s"] for r in rows) - min(starts) if starts else 0
    counts = [(r.get("usage") or {}).get("completion_tokens") for r in ok]
    # Failed requests have unknown partial usage: suppress whole-run throughput.
    complete_counts = len(ok) == len(rows) and all(n is not None for n in counts)
    stats = {"requests": len(rows), "successful": len(ok), "failed": len(rows) - len(ok),
             "truncated": sum(r["truncated"] for r in ok), "wall_s": wall,
             "completion_tokens": sum(counts) if complete_counts else None,
             "aggregate_end_to_end_tok_s": sum(counts) / wall if complete_counts and wall > 0 else None}
    for field in ("ttft_answer_s", "ttft_reasoning_s", "duration_s"):
        values = [r[field] for r in ok if r.get(field) is not None]
        stats[field] = {"n": len(values), "median": statistics.median(values),
                        "worst": max(values)} if values else None
    return stats


def run_burst(args, prompts, key):
    origin = time.perf_counter()
    barrier = threading.Barrier(args.users)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.users) as pool:
        futures = [pool.submit(request_one, args.url, key, args.model,
                               prompts[i % len(prompts)], args.max_tokens,
                               args.timeout, barrier, origin) for i in range(args.users)]
        rows = [f.result() for f in futures]
    return {"summary": summarize(rows), "requests": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8081/v1/chat/completions")
    parser.add_argument("--model", required=True)
    parser.add_argument("--users", type=positive_int, default=1)
    parser.add_argument("--repeats", type=positive_int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--max-tokens", type=positive_int, default=256)
    parser.add_argument("--timeout", type=positive_float, default=120,
                        help="socket inactivity timeout in seconds, not total deadline")
    parser.add_argument("--prompt", default="List five ways to verify an authorization check in a local test application.")
    parser.add_argument("--prompt-file", action="append", default=[])
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-http", action="store_true",
                        help="explicitly allow cleartext HTTP beyond loopback")
    args = parser.parse_args()
    if args.warmups < 0:
        parser.error("--warmups cannot be negative")
    url = urllib.parse.urlsplit(args.url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error("use an HTTP(S) endpoint without credentials, query or fragment")
    if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"} and not args.allow_http:
        parser.error("use HTTPS/a local tunnel, or explicitly --allow-http")
    prompts = [Path(p).read_text(encoding="utf-8") for p in args.prompt_file] or [args.prompt]
    if any(not p.strip() for p in prompts):
        parser.error("prompts cannot be empty")
    metadata = json.loads(args.metadata.read_text()) if args.metadata else {}
    if not isinstance(metadata, dict):
        parser.error("metadata must be a JSON object; do not include secrets")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve output before sending any requests; never overwrite a past run.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    report = {"schema_version": 1, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "model": args.model, "metadata": metadata,
              "settings": {k: getattr(args, k) for k in ("users", "repeats", "warmups", "max_tokens", "timeout")},
              "prompt_sha256": [hashlib.sha256(p.encode()).hexdigest() for p in prompts],
              "prompt_bytes": [len(p.encode()) for p in prompts], "warmups": [], "runs": []}
    key = os.environ.get("LLAMA_API_KEY", "")
    failed = False
    with os.fdopen(fd, "w") as output:
        try:
            for phase, count in (("warmups", args.warmups), ("runs", args.repeats)):
                for _ in range(count):
                    result = run_burst(args, prompts, key)
                    report[phase].append(result)
                    failed |= result["summary"]["failed"] > 0
                    print(phase, json.dumps(result["summary"]), flush=True)
                    output.seek(0)
                    json.dump(report, output, indent=2)
                    output.truncate()
                    output.flush()
        finally:
            output.seek(0)
            json.dump(report, output, indent=2)
            output.truncate()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
