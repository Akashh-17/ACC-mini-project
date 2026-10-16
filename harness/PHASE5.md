# Ryan's Phase 5 laptop workflow

Run commands from the repository root with Python 3.10+ and `requirements.txt`
installed. The harness uses `httpx`, already a project dependency.

## Prerequisites

Copy `harness/clouds.example.json` to `results/clouds.local.json` and replace
the placeholders. `results/` is ignored by Git: addresses, raw observations
and reports stay on the laptop. The current deployment config has been
prepared locally. Do not commit private deployment addresses.

The team-approved template exposes GET `/lookup` through the proxy. Each VM
owner must deploy the updated template with their own public IP:

```bash
sudo PUBLIC_IP=<PUBLIC_IP> bash proxy/render.sh proxy/<cloud>.env | sudo tee /etc/nginx/conf.d/registry.conf
sudo nginx -t && sudo systemctl reload nginx
curl -sk 'https://127.0.0.1/lookup?service_name=payments-api'
```

Owners check `iptables`, `tc`, synchronized VM clocks, and the three regional
entries before starting. Keep Tailscale connected on the laptop. Ryan runs:

```powershell
.\.venv\Scripts\python.exe -m harness.phase5 healthcheck
.\.venv\Scripts\python.exe -m harness.phase5 baseline
```

The baseline requires all three regions UP in **each** observer's lookup.
An HTTP 404, missing entry, malformed reply, or timeout fails the baseline.

## Experiments

Failure experiments require `--on-call`; use it only during the team call.
All commands write to a new timestamped directory under `results/`.

```powershell
# 1: ten trials per target region, round-robin (30 total), failure and recovery
.\.venv\Scripts\python.exe -m harness.phase5 convergence --on-call

# 1b: five trials per target; owner applies delay and clears on interactive cues
.\.venv\Scripts\python.exe -m harness.phase5 delay --delay-ms 500 --on-call
.\.venv\Scripts\python.exe -m harness.phase5 delay --delay-ms 1000 --on-call

# 2a: five trials ending DOWN and five ending UP, rotating targets
.\.venv\Scripts\python.exe -m harness.phase5 flap --on-call

# 2b: owner partitions both peers, Ryan flips, owner heals, latest DOWN must converge
.\.venv\Scripts\python.exe -m harness.phase5 partition-updates --on-call

# 3: three trials per target, 60 s partition hold, stale/availability/recovery
.\.venv\Scripts\python.exe -m harness.phase5 partition --on-call

# 4: 200 sequential samples per endpoint per cloud, six sets / 1200 requests
.\.venv\Scripts\python.exe -m harness.phase5 latency

# Compile existing results; does not contact clouds
.\.venv\Scripts\python.exe -m harness.phase5 report
```

`--targets gcp` limits an experiment to one target. `--trials` controls trials;
the rapid-flapping count is five per final state across rotating regions.
Smoke runs with fewer samples/trials must not be reported as full experiments.

Owner cues print the two public-peer commands and wait for `done`. The harness
never logs in to VMs or modifies VM firewall rules. Partition timestamps are
the laptop's receipt of owner confirmation, so include human cue uncertainty
in the report. Confirm immediately after executing the commands.

Before delay trials, note that `scripts/netfault.sh delay` originally replaced
the root qdisc for every peer: the second command could erase the first peer's
delay. Use the corrected script shipped with this harness and deploy it on
the VMs. Delay is outbound-only, not a round-trip delay guarantee.

## CSVs and measurements

Each run contains `<experiment>.csv` with one row per observed status change:
`trial, experiment, t_trigger, observer_region, target_region, service,
new_status, t_observed` plus wall-clock time, HTTP status and diagnostic detail.
Timestamps use `time.perf_counter()` on the laptop; wall time uses `time.time()`.
Keep the session metadata with the CSV: perf-counter values are not portable
timestamps and should not be compared between laptop boots.

The metrics CSV records detection, replication and total convergence separately
for failure and recovery. `total` waits for **all** observers. Peers can be polled
before the owner, so signed replication sampling skew is retained and documented.
Observation resolution is 200 ms plus request latency. Polling loops are
independent; a slow/offline cloud cannot freeze the other two.

Latency sampling is paced at five requests/sec to avoid the nginx rate limiter.
It includes HTTP failures as failed samples. Summary JSON gives sample counts,
median, nearest-rank p95 and max of successful timings, plus failed counts and
the overall run outcome. A timeout never becomes an artificial success.

The current lookup API exposes region/endpoint/status, **not version numbers**.
Rapid-flapping tests establish eventual final-state convergence; unit tests in
`tests/test_storage.py` and `tests/test_replication.py` cover duplicate/reordered
versions. They do not establish that every intermediate half-second flip was
observed by a five-second health scheduler.

## Imported result sets

Copy run folders beside the existing folders in `results/`; do not copy an
archive's older `phase5-report.md` over the current report. Keep original CSVs
and `session.json` unchanged. Preserve the source's vantage note separately.

For measurements taken somewhere other than Ryan's laptop, add a
`provenance.json` alongside that run's `session.json`, for example:

```json
{
  "measurement_vantage": "GCP VM (asia-south1)",
  "vantage_note": "gcp-vm-run-2026-10-15-VANTAGE-NOTE.md"
}
```

The note path is relative to the `results/` root. `report` identifies the
measurement origin in its session, metrics and latency-comparison tables.
GCP-VM latency is measured with the client in the GCP region; keep it separate
from laptop latency. Perf-counter timestamps from different hosts cannot be
compared directly, even when older imported metadata says "laptop".

## Demo

In two terminals:

```powershell
.\.venv\Scripts\python.exe -m harness.phase5 watch
.\.venv\Scripts\python.exe -m harness.phase5 client --preferred gcp
```

The client looks up UP regions, then calls the region's Tailscale proxy, never
the VM-private address returned by the catalog. It retries other UP regions
when the preferred service actually fails, even before the registry catches up.
The payment service is a stub: the client exercises `/payments-api/health`.

D1: fail/recover GCP on the call; watch the client switch to AWS/Azure. Use a
short convergence run for a repeatable rehearsal:

```powershell
.\.venv\Scripts\python.exe -m harness.phase5 convergence --targets gcp --trials 1 --on-call
```

D2: AWS owner stops `registry-service@recommend-api` on cue. In the laptop
terminal, run `--service recommend-api client --preferred aws`; it should show
UNAVAILABLE after the registry detects the outage. A payments client remains
available. Owner starts the unit again. This demonstration still requires the
owner and is not performed by a read-only baseline or unit test.

## Cleanup and report

Every failure-triggering command recovers services it failed, including when a
POST response is lost. It rechecks the all-UP baseline before exiting. Recovery
errors are surfaced; read terminal output if interrupted. Owners must confirm
both fault rules cleared and restart units they stopped.

```powershell
.\.venv\Scripts\python.exe -m harness.phase5 recover
.\.venv\Scripts\python.exe -m harness.phase5 baseline
.\.venv\Scripts\python.exe -m harness.phase5 report
```

Ryan's report includes measurements and CSVs, cloud regions/VM sizes, Tailscale
test access and mTLS replication on public IPs. Request VM sizes, billing
screenshots, module notes and deployment issues from the owners on the call.
Do not infer missing inputs or claim coordinated experiments ran when only
local tests or health checks ran. Remaining limits: in-memory state and
re-registration after restarts, one-direction delay, self-signed proxy certs,
five-second health sampling, TTL/VM clock synchronization, and operator cues.
