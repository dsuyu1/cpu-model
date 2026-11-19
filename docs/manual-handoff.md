# First server diagnostic pass (manual access)

Run only this inventory pass initially. Do not stop guests, change container
limits, install a model, or start a load test yet. This establishes the actual
hardware and limits before we select runtime build and service settings.

Copy `scripts/diagnose.py` from this repository onto the Proxmox host as
`/tmp/diagnose.py` using your normal transfer method. It has no pip dependencies.
Run these commands in a host shell with permission to inspect CT 140:

```bash
python3 /tmp/diagnose.py --ct 140 --output /tmp/pve-inventory.json
pct push 140 /tmp/diagnose.py /tmp/diagnose.py
pct exec 140 -- python3 /tmp/diagnose.py --output /tmp/ct140-inventory.json
pct pull 140 /tmp/ct140-inventory.json /tmp/ct140-inventory.json
```

These commands copy a script and write two reports; they do not change service
configuration. If an output already exists, choose a fresh name instead of
overwriting it. If Python is absent in the container, report that error; the
host inventory remains useful. Do not install anything just to hide missing data.

Bring back `/tmp/pve-inventory.json` and `/tmp/ct140-inventory.json`. They contain
local host/network/storage information from inventory commands, but no private
keys, process command lines, prompt bodies or model responses. Inspect them
before sharing; redact addresses or storage identifiers if desired. Never put
target findings or credentials in run metadata or diagnostic reports.

The next step will be a bounded baseline test with the actual model worker PID
observed during inference. Idle inventory alone cannot diagnose the old slowdown.

For a later inference snapshot, replace WORKER_PID with a numeric PID:

```bash
python3 /tmp/diagnose.py --pid WORKER_PID --output /tmp/worker-during-run.json
```

Run inside the same PID namespace as the worker. For Ollama, select the inference
runner process, not merely its parent service. Collect on the host as well if
container namespace visibility hides ancestor cgroup limits.
