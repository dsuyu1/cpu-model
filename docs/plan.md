# Revised implementation and measurement plan

Prepared November 18, 2026. Server configuration is reported by the user's
documents, not verified: Xeon Gold 6140, approximately 92 GB usable RAM,
Proxmox CT 140, previously allocated 12 logical CPUs and 32 GB RAM.

## Decisions

- Start with an advisor, with human verification and no target-facing tools.
- Use direct llama.cpp for controlled measurements. Retain existing Ollama
  configuration, but avoid keeping competing model processes resident during
  isolated trials. Any service interruption is a separately scheduled operation.
- First candidates: existing Qwen2.5-Coder 7B as baseline; Qwen3-Coder
  30B-A3B-Instruct Q4_K_M as main candidate; Gemma 4 26B A4B as a later comparison.
- Start with one slot, 4k context and a bounded output budget. No speculative
  decoding, large-model downloads or 80 GB container allocation initially.
- Pin the llama.cpp commit, model repository revision and file SHA256 before
  collecting comparable numbers. Record exact startup arguments and logs.
- Do not train or fine-tune yet. First establish a capability gap and evaluation
  set. Fine-tuning does not inherently make an architecture faster.

## Corrections to the original documents

The October 0.41 and 0.16 tok/s results are observations; memory contention and
swapping are unconfirmed explanations. The prefill/decode ratio is not proof.
Investigate CPU quota throttling, effective affinity, runtime backend, page
faults and disk reads, thread count, frequency, and competing workloads.

Two NUMA nodes can occur within one socket through Sub-NUMA Clustering. Map
DIMM slots to memory channels using the exact server manual. Inventory alone
does not measure sustainable bandwidth. Do not purchase RAM from a slot count.

Active parameters are a rough compute indicator, not a precise RAM traffic
measurement. Attention, quantization kernels, shared weights and expert routing
also matter. Batch sequences can select different MoE experts. Remove all
predicted 'likely real' token rates until measured on this host.

Zero swap does not rule out rereading evicted memory-mapped weights. Host swap
activity alone does not attribute pressure to inference. Disabling swap does
not turn every insufficient-memory condition into an immediate load failure.

Total RAM includes weights, KV cache, buffers, runtime and host headroom. A
container maximum is not a physical reservation. Leave measured host headroom
and account for other guests. Do not keep Ollama and llama.cpp loaded together
when trying to establish an isolated ceiling.

Ollama exposes concurrency settings. Direct llama.cpp helps control experiments,
but changing the frontend alone does not guarantee a speedup. Context allocation
depends on the pinned runtime and KV mode; inspect startup logs, not assumptions.
Prompt plus output must fit the effective per-request context.

Speculation is optional. Same-family branding is not a compatibility check, and
acceptance percentage alone cannot determine speedup. Evaluate total latency.
Temperature zero does not guarantee bit-for-bit reproducibility across builds,
batching conditions or hardware.

## Milestone 0: explain the baseline

Collect host and container inventories with scripts/diagnose.py. During a short
7B trial, sample worker cgroup counters and process counters before and after;
observe `vmstat 1`, and, if already installed, `pidstat -rdu -p PID 1` and
`iostat -xz 1`. The first vmstat row is since boot; subsequent rows are intervals.
The diagnostics tool does not install tools, stop guests or change limits.

Check the effective cgroup hierarchy on the host as well as inside CT 140;
containers may hide ancestor quotas. Check worker threads, not just manager PID.
If frequency/thermal evidence is missing, collect it separately with appropriate
host tools. Compare quiet and ordinary workload conditions without assuming
the existing contention hypothesis is correct.

Exit: raw model/token/timing records, known effective resources, and either a
repeatable explanation/fix or explicitly unresolved diagnostic evidence.

## Milestone 1: pin a runtime and establish single-user performance

Build a selected, recorded llama.cpp commit inside CT 140 using native CPU
optimization, bounded build jobs and CPU-only execution. Verify detected SIMD
support. Run as an unprivileged service user; repair the documented /etc access
issue before relying on the old root-running service workaround.

Record executable version, compiler/build configuration, model hash, quantization,
thread settings, context/cache types, workload condition and startup memory use.
Start with 7B, then a bounded 30B trial. Test 4, 8, 12 and 18 threads only where
the effective physical cores permit; include SMT as a measured variant. Tune
prompt-processing and generation thread settings separately where supported.

Representative current commands, to verify against the pinned binary's --help:

```bash
llama-bench -m MODEL.gguf -ngl 0 -p 512,4096 -n 128 -t 4,8,12,18 -r 3 -o json
llama-bench -m MODEL.gguf -ngl 0 -p 0 -n 128 -d 4096 -t 8 -r 3 -o json
llama-bench -m MODEL.gguf -ngl 0 -p 0 -n 0 -pg 4096,128 -t 8 -r 3 -o json
```

Depth tests measure decode with an existing context; combined tests measure
prompt-plus-generation, not isolated decode. Repeat at 1k, 4k, 8k, then 16k
when memory and latency justify it. Do not interpret separate pp/tg tests as
generation speed following that prompt length.

## Milestone 2: server behavior and capacity

Bind initially to loopback and access by tunnel. Before LAN exposure, configure
authentication, transport protection and firewall rules. API keys alone do not
encrypt traffic. Do not infer access restrictions from a private bind address.

Use the harness for concurrency 1, 2, 4; compare unique prompts and shared-prefix
prompts. Run at least three measured repetitions after explicit warmup. Warmups
also warm prompt caches: label these runs accordingly. For cold/cache-disabled
trials use fresh controlled server state or verified cache controls; changing
the label does not reset a cache. Never silently mix these regimes.

Record first visible answer, first reasoning output, total completion time,
server token counts, end-to-end aggregate throughput, failure/truncation rates
and optional raw server timing metadata. Server predicted_per_second is a
server-defined metric; do not equate it with client inter-token latency.
With no usage metadata, token throughput is unavailable, not estimated.

The harness is a synchronized burst test. Before declaring shared capacity,
add sustained staggered arrivals and a long-prompt arrival during another
generation. Report latency distributions with sample counts; three trials do
not establish reliable p95 latency. Compare queuing with more active slots.

Stretch goals: 10 tok/s single user; 5 tok/s each for four active users; a 16k
input processed within three minutes (about 91 input tok/s). These are targets,
not promises. Also score completion latency and human time saved. No automatic
pass based only on aggregate throughput.

## Milestone 3: usefulness and evidence preparation

Build an evaluation corpus before choosing a winner: public Juice Shop smoke
cases plus newly authored clean/vulnerable/patched pairs, incomplete-context
cases, report fidelity, scope checks, and embedded prompt-injection examples.
Separate development examples from held-out cases. Record precision, recall,
unsupported claims, abstentions, evidence fidelity and review minutes. Zero
errors on a small corpus does not prove reliability.

Extract relevant code and HTTP slices using deterministic parsing/search before
inference. Preserve file names, line references and enough adjacent context to
avoid misleading the reviewer. Do not treat regex extraction as proof that no
other endpoints exist. Request evidence, uncertainty and concise next checks.

Implement a bounded queue with explicit overload responses and per-user limits
only after measuring service capacity. Program-specific scope enforcement lives
outside the model. Target tools remain a separate sandboxed milestone.

## Milestone 4: optional optimizations

Only after quality and stable latency: compare weight/KV quantization, supported
flash attention and batching settings one variable at a time. Recheck quality.
Try compatible speculative decoding and measure end-to-end benefit. Consider a
larger single-user review model only for a measured quality gap and sufficient
memory headroom. Avoid preloading a second model merely because RAM permits it.

## Sources to recheck when pinning versions

- https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- https://github.com/ggml-org/llama.cpp/blob/master/tools/llama-bench/README.md
- https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md
- https://docs.ollama.com/faq
- https://docs.kernel.org/admin-guide/cgroup-v2.html
- https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct
- https://ai.google.dev/gemma/docs/core
