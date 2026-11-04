# Phase 5 Runbook: Experiments & Demo

Status at start: Phase 4 complete (2026-10-14). All 3 agents replicate over mTLS, every
critical service shows 3 regions UP, and Ryan reaches every proxy over Tailscale.

| Region | Tailscale IP | Public IP (replication only) | Standard service |
|---|---|---|---|
| GCP `gcp-asia-south1` | 100.102.19.73 | 8.234.102.90 | notify-api |
| AWS `aws-ap-south-1` | 100.94.161.87 | 65.1.246.1 | recommend-api |
| Azure `azure-indiasouthcentral` | 100.75.78.118 | 172.198.75.240 | admin-api |

**Roles**
- **Ryan** runs the harness on his laptop: triggers failures, polls every region, records timings.
- **Owners** run `netfault.sh` (needs root) and stop/start units **on cue**, and nothing else.
- Run every experiment **on a call**, so cues are instant.

**Timing model (what results should look like)**
- Health check every 5 s with a 2 s timeout, so **failure → own region DOWN ≤ ~7 s**.
- Replication every 2 s, so **own region change → peers ≤ ~2 s** plus network time.
- TTL 15 s: a silent remote entry turns **STALE ~15–20 s** after updates stop.

---

## Step 0: Prerequisites (once, before any experiment)

### 0.1 Read-only `/lookup` through each proxy (needs team approval)
The agent's `/lookup` is localhost-only, so Ryan can't see each region's view from his
laptop. `proxy/nginx.conf.template` already contains a commented-out, read-only
`/lookup` block. Proxies are now reachable only over Tailscale, so enabling it is safe.
- **Template change (one person, then commit):** uncomment the `location = /lookup { ... }` block.
- **Each owner, after pulling:**
  ```bash
  sudo PUBLIC_IP=<your public IP> bash proxy/render.sh proxy/<cloud>.env | sudo tee /etc/nginx/conf.d/registry.conf
  sudo nginx -t && sudo systemctl reload nginx
  curl -sk "https://127.0.0.1/lookup?service_name=payments-api"     # must list 3 regions
  ```
- `POST /register` and `/deregister` stay blocked. Only `GET /lookup` is added.

Why this matters: Ryan measures every timestamp on **one clock** (his laptop), so clock
differences between clouds don't distort the results.

### 0.2 Owners: check that the fault tools work (2 min, on each VM)
```bash
cd /opt/registry && git pull          # or however the code is updated on your VM
sudo iptables -L OUTPUT -n | head -3  # must not error
tc -V                                 # must print a version
timedatectl | grep synchronized       # must say "yes" (lag_ms in logs depends on it)
```
Rule of thumb: **`netfault.sh` rules are not persistent. A reboot clears them.** If anything
gets stuck, `sudo bash scripts/netfault.sh clear <PEER_PUBLIC_IP>` for each peer, or reboot.

### 0.3 Baseline check (Ryan)
```powershell
foreach ($ip in "100.102.19.73","100.94.161.87","100.75.78.118") {
  curl.exe -sk "https://$ip/lookup?service_name=payments-api"
}
```
All three must show `gcp`, `aws` and `azure` = `UP` before every experiment.

---

## Harness requirements (Ryan builds; extends `harness/`)
One script, run from Ryan's laptop, that can:
1. **Trigger:** `POST https://<ip>/<service>/simulate/fail` and `/simulate/recover`.
2. **Poll:** `GET https://<ip>/lookup?service_name=<svc>` on **all 3** proxies every **200 ms**.
   That's 5 requests/s per proxy, well under the 20 req/s rate limit.
3. **Record** one CSV row per observed change:
   `trial, experiment, t_trigger, observer_region, target_region, service, new_status, t_observed`
   All `t_*` come from `time.perf_counter()` / `time.time()` on the laptop.
4. **Wait for steady state** (all UP everywhere) between trials, with a 60 s timeout.
5. Ignore the self-signed cert (`verify=False`), since the proxy certs are self-signed.

Output: one CSV per experiment under `results/`. Summarize with median, p95 and max.

---

## Experiment 1: Convergence time
**Question:** after a failure, how long until every region's lookup reflects it?

**Procedure (no owner action needed):**
1. Pick target region X and `payments-api`.
2. Ryan: `POST https://<X>/payments-api/simulate/fail`, recording `t0`.
3. Poll all 3 lookups until each shows X's `payments-api` = `DOWN`. Record:
   - `t_detect`: X's own lookup shows DOWN (health check noticed it)
   - `t_converge`: the **last** peer shows DOWN
4. `POST .../simulate/recover`, then measure the same way until UP everywhere.
5. **10 trials per region** (30 total). Rotate X: GCP, AWS, Azure.

**Report:** detection time (`t_detect - t0`), replication time (`t_converge - t_detect`),
total (`t_converge - t0`). Expected roughly ≤7 s, ≤2.5 s and ≤9 s.

**Variant 1b, with injected delay (owners on cue):** the owner of X runs, for each peer:
```bash
sudo bash scripts/netfault.sh delay <PEER_PUBLIC_IP> 500     # then repeat with 1000
```
Repeat 5 trials, then `netfault.sh clear <PEER_PUBLIC_IP>` for each peer.
Expect the replication component to grow by about the injected delay.

---

## Experiment 2: Concurrent / rapid updates (last-write-wins correctness)
Per `interfaces.md`, only the owning region writes its own entries, so "concurrent updates"
means **many versions of one entry in flight at once**. The question: do peers always end
on the **latest** state, never an older one?

**2a. Rapid flapping (no owner action):**
1. Ryan sends quickly to region X's payments-api: fail, recover, fail, recover, fail,
   **ending on `fail`**, about 0.5 s apart.
2. Wait 20 s. **Pass:** every region shows X = `DOWN` (the final state).
3. Repeat ending on `recover` (final = `UP`). 5 trials each, rotating regions.

**2b. Changes during a partition (owner of X on cue):**
1. Owner of X: `netfault.sh partition` for **both** peer public IPs.
2. Ryan flips X's `payments-api` 3 times, ending on `fail`.
3. Owner of X: `netfault.sh clear` for both peers.
4. **Pass:** peers converge to X = `DOWN` (the latest), via a backlog or snapshot. Record the time.

Note for the report: version ordering, duplicate and reorder handling are also covered by
the unit tests (`tests/test_replication.py`, `tests/test_storage.py`). Cite them.

---

## Experiment 3: Network partition (availability + recovery)
**Question:** when a region is cut off, does everyone stay available, and how do entries age?

1. Baseline: all UP everywhere.
2. Owner of X (start with **GCP**), at `t0`:
   ```bash
   sudo bash scripts/netfault.sh partition <PEER1_PUBLIC_IP>
   sudo bash scripts/netfault.sh partition <PEER2_PUBLIC_IP>
   ```
   Only port 9443 is blocked, so Tailscale and the proxies keep working and Ryan can still observe.
3. Ryan records:
   - `t_stale`: when the **peers** show X's entries as `STALE` (expect ~15–20 s)
   - that X **still serves**: its own lookup shows its own services UP; it sees peers STALE
   - that lookups on **all** regions keep answering throughout (partition tolerance)
4. After 60 s, owner of X runs `netfault.sh clear` for both peers, at `t_heal`.
5. `t_recover`: all regions show everything UP again (expect ≤ ~4 s after heal).
6. 3 trials per region.

---

## Experiment 4: Lookup latency
1. Ryan: 200 sequential `GET /lookup?service_name=payments-api` per proxy.
2. Also 200 × `GET /health` per proxy, which gives the network + proxy baseline.
3. **Report:** median and p95 of each. `lookup − health` ≈ the registry's own cost.
4. Optional, owners on the VM (agent only, no network):
   ```bash
   for i in $(seq 200); do curl -s -o /dev/null -w "%{time_total}\n" "http://127.0.0.1:9000/lookup?service_name=payments-api"; done | sort -n | awk '{a[NR]=$1} END {print "median", a[int(NR/2)], "p95", a[int(NR*0.95)]}'
   ```

---

## Demo script (≈10 min, rehearse once)
**Setup on screen:** a terminal polling all 3 lookups (the harness in watch mode), plus a
"client" that calls `payments-api`: lookup → first `UP` region → call that region's proxy.

**D1: Payment failover (GCP outage)**
1. Show `payments-api` UP on gcp, aws and azure; the client happily uses GCP.
2. Simulate the GCP outage:
   - simple: `POST https://100.102.19.73/payments-api/simulate/fail`
   - stronger: GCP owner partitions GCP from both peers (Experiment 3 commands)
3. Within ~7 s, the lookups show GCP `DOWN`/`STALE`, and the client **automatically uses AWS/Azure**.
   Payments keep succeeding.
4. Recover. GCP returns UP within ~7 s.

**D2: recommend-api outage (AWS down)**
1. Show `recommend-api` UP (AWS only).
2. AWS owner simulates the outage:
   ```bash
   sudo systemctl stop registry-service@recommend-api      # or: partition AWS from both peers
   ```
3. Lookups show `recommend-api` with **no UP endpoint**: correctly unreachable, since it's
   single-cloud by design. Meanwhile `payments-api` still works via GCP/Azure.
4. Restore: `sudo systemctl start registry-service@recommend-api`.

**Talking points:** short TTLs, retry on failure, partition tolerance, and that the registry holds
no transactional data (the four boxes at the bottom of the architecture diagram).

---

## After every session (owners)
```bash
for ip in <PEER1_PUBLIC_IP> <PEER2_PUBLIC_IP>; do sudo bash scripts/netfault.sh clear $ip; done
sudo systemctl start registry-service@payments-api registry-service@recommend-api   # whichever you stopped
curl -sk "https://127.0.0.1/lookup?service_name=payments-api"                       # 3 regions UP
```
Ryan: `simulate/recover` anything he failed, then run the baseline check (0.3).

## For the report (Ryan compiles)
- A results table per experiment (median, p95, max, number of trials), plus the CSVs.
- Setup: 3 clouds, VM sizes, regions, Tailscale for test access, mTLS replication over public IPs.
- Known limits: in-memory catalog (restart = re-register), one-directional `tc` delay,
  self-signed proxy certs, timing bounded by the 5 s health interval.
- Owners send Ryan: billing screenshots, their module design notes, and issues found
  (e.g. the health-check keep-alive bug: 16% false DOWN, fixed in `abe4408`).
