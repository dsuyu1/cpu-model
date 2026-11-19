# CPU-only local security assistant

Status: preparation and local harness tests only. No server measurements yet.

We are selecting and serving an existing quantized model on the Xeon Gold 6140
server, not training a foundation model. Optimize correct, evidence-backed
answers per unit of human time. Token throughput is a diagnostic, not the goal.

## Architecture

Member → authenticated local gateway with a bounded queue → llama-server.
An evidence preparation step selects relevant code, request/response excerpts,
and scope rules before inference. Initially the assistant has no target tools.
Retrieved content is untrusted evidence, never authority to change instructions.

Start with one active request and 4k total context, including answer space.
Benchmark two and four active requests before increasing concurrency. Keep
client requests and logs local. No automatic hosted-model fallback.
The gateway, authentication, evidence extraction and UI are later milestones;
they are not implemented in this repository yet.

## Start here

1. Read [the revised plan](docs/plan.md).
2. On the Proxmox host, collect inventory (no configuration changes):

   ```bash
   python3 scripts/diagnose.py --ct 140 --output results/host.json
   ```

3. Inside CT 140, collect a separate snapshot. During a benchmark include the
   actual inference worker PID, not just the supervising Ollama PID:

   ```bash
   python3 scripts/diagnose.py --pid WORKER_PID --output results/container.json
   ```

   Repeat during load to compare cumulative counters. Missing commands and
   permissions are recorded. Nothing is installed and no service is stopped.

4. After an endpoint is running, run the harness from CT 150 or over an SSH tunnel:

   ```bash
   python3 scripts/bb_loadtest.py --model LOCAL_MODEL --users 1 --repeats 3 \
     --metadata config/run.example.json --output results/single.json
   ```

   Set `LLAMA_API_KEY` in the environment if required. Do not put keys on the
   command line. The default endpoint is loopback; remote cleartext HTTP requires
   `--allow-http`. Prefer an SSH tunnel or HTTPS for member traffic.

   Use repeated `--prompt-file PATH` options for different user inputs. Files
   are sent verbatim as user messages: include the task instruction yourself.
   Use at least as many distinct files as users for independent-prompt trials.
   The default synthetic shared prompt is a smoke test, not a capacity estimate.

## Validation

```bash
python3 -m unittest discover -s tests -v
```

The harness uses Python's standard library. It requests server usage counts,
never treats SSE chunks as tokens, and fails on incomplete streams. JSON reports
exclude prompt/response bodies, credentials and raw server error messages. Keep
reports private anyway: model identifiers and environment metadata may be sensitive.

Original documents remain in Downloads. [Review corrections](docs/plan.md)
supersede their recommendations; the October measurements remain historical data.
