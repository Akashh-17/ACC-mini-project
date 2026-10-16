"""Laptop observation, experiments and demo for the three-cloud registry.

Run `python -m harness.phase5 --help`. Network addresses live in a private
JSON config, not in this module. All experiment timestamps use this laptop.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import statistics
import time

import httpx


CRITICAL = ("payments-api", "fraud-check", "inventory-check")
EVENT_FIELDS = (
    "trial", "experiment", "t_trigger", "observer_region", "target_region",
    "service", "new_status", "t_observed", "wall_time", "http_status", "detail",
)
METRIC_FIELDS = ("trial", "experiment", "target_region", "phase", "metric", "value", "unit", "passed")


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class Cloud:
    name: str
    region_id: str
    base_url: str
    public_ip: str = ""
    standard_service: str = ""


def load_clouds(path: Path) -> dict[str, Cloud]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if set(data) != {"gcp", "aws", "azure"}:
        raise HarnessError("Config must contain exactly gcp, aws and azure")
    clouds = {name: Cloud(name=name, **values) for name, values in data.items()}
    if len({c.region_id for c in clouds.values()}) != 3:
        raise HarnessError("Config region IDs must be distinct")
    for cloud in clouds.values():
        url = httpx.URL(cloud.base_url)
        if url.scheme not in ("http", "https") or not url.host or url.path != "/" or url.query:
            raise HarnessError(f"{cloud.name}: base_url must be an HTTP(S) origin")
    return clouds


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; includes the slowest sample for small p95 sets."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


class Results:
    def __init__(self, directory: Path, experiment: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.events_path = directory / f"{experiment}.csv"
        self.metrics_path = directory / f"{experiment}-metrics.csv"
        self.summary_path = directory / f"{experiment}-summary.json"
        self.event_file = self.events_path.open("w", newline="", encoding="utf-8")
        self.metric_file = self.metrics_path.open("w", newline="", encoding="utf-8")
        self.events = csv.DictWriter(self.event_file, fieldnames=EVENT_FIELDS)
        self.metrics = csv.DictWriter(self.metric_file, fieldnames=METRIC_FIELDS)
        self.events.writeheader()
        self.metrics.writeheader()
        self.rows: list[dict] = []
        self.outcome = "INCOMPLETE"

    def event(self, **row):
        self.events.writerow(row)
        self.event_file.flush()

    def metric(self, trial, experiment, target, phase, metric, value, unit="seconds", passed=True):
        row = dict(trial=trial, experiment=experiment, target_region=target,
                   phase=phase, metric=metric, value=value, unit=unit, passed=passed)
        self.rows.append(row)
        self.metrics.writerow(row)
        self.metric_file.flush()

    def close(self):
        groups: dict[tuple, list[dict]] = {}
        for row in self.rows:
            key = tuple(row[k] for k in ("experiment", "target_region", "phase", "metric", "unit"))
            groups.setdefault(key, []).append(row)
        summaries = []
        for key, rows in groups.items():
            values = [float(row["value"]) for row in rows if row["passed"]]
            summaries.append(dict(zip(("experiment", "target_region", "phase", "metric", "unit"), key),
                                  samples=len(rows), passed=len(values), failed=len(rows)-len(values),
                                  median=statistics.median(values) if values else None,
                                  p95=percentile(values, .95) if values else None,
                                  max=max(values) if values else None))
        self.summary_path.write_text(json.dumps({"outcome": self.outcome, "metrics": summaries}, indent=2),
                                     encoding="utf-8")
        self.event_file.close()
        self.metric_file.close()


class Monitor:
    """Independent 200 ms polling loops; an unresponsive cloud cannot stall peers."""
    def __init__(self, harness, trial, experiment, service, trigger, result, targets=None):
        self.harness = harness
        self.trial = trial
        self.experiment = experiment
        self.service = service
        self.trigger = trigger
        self.result = result
        self.targets = targets or list(harness.clouds)
        self.states: dict[str, dict[str, str]] = {}
        self.replies: dict[str, int] = {}
        self.first: dict[tuple[str, str, str], float] = {}
        self.errors: dict[str, int] = {name: 0 for name in harness.clouds}
        self.last_errors: dict[str, str] = {}
        self.last_observed: dict[str, float] = {}
        self.tasks: list[asyncio.Task] = []

    async def __aenter__(self):
        self.tasks = [asyncio.create_task(self.poll(name)) for name in self.harness.clouds]
        return self

    async def __aexit__(self, *args):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def poll(self, observer):
        cloud = self.harness.clouds[observer]
        previous = {}
        while True:
            started = time.perf_counter()
            code, data, detail = await self.harness.fetch(observer, "/lookup", service=self.service)
            observed = time.perf_counter()
            statuses = {}
            valid = code == 200 and isinstance(data, dict) and isinstance(data.get("endpoints"), list)
            if valid:
                for entry in data["endpoints"]:
                    if isinstance(entry, dict):
                        region, status = entry.get("region_id"), entry.get("status")
                        if not isinstance(region, str) or region in statuses or status not in ("UP", "DOWN", "STALE"):
                            valid = False
                            detail = "Duplicate region or invalid lookup status"
                            break
                        statuses[region] = status
            if not valid:
                self.errors[observer] += 1
                self.last_errors[observer] = detail or f"HTTP {code}: invalid lookup response"
            self.replies[observer] = code if valid else 0
            current = {}
            for target in self.targets:
                region = self.harness.clouds[target].region_id
                status = statuses.get(region, "MISSING") if valid else "LOOKUP_ERROR"
                current[target] = status
                # This is a receipt timestamp on the laptop, never a VM's clock.
                self.first.setdefault((observer, target, status), observed)
                if previous.get(target) != status:
                    self.result.event(trial=self.trial, experiment=self.experiment,
                                      t_trigger=self.trigger, observer_region=cloud.region_id,
                                      target_region=region, service=self.service, new_status=status,
                                      t_observed=observed, wall_time=time.time(), http_status=code,
                                      detail=detail)
            self.states[observer] = current
            self.last_observed[observer] = observed
            previous = current
            await asyncio.sleep(max(0, self.harness.interval - (time.perf_counter() - started)))

    def matches(self, expected: dict[str, dict[str, str]]) -> bool:
        return all(self.replies.get(observer) == 200 and
                   all(self.states.get(observer, {}).get(target) == status and
                       (observer, target, status) in self.first for target, status in targets.items())
                   for observer, targets in expected.items())

    async def wait(self, expected, timeout):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.matches(expected):
                return
            await asyncio.sleep(min(.02, self.harness.interval))
        raise HarnessError(f"Timed out waiting for {expected}; observed={self.states}; errors={self.last_errors}")

    async def refresh(self):
        threshold = time.perf_counter()
        deadline = threshold + self.harness.timeout
        while time.perf_counter() < deadline:
            if all(self.last_observed.get(observer, 0) >= threshold for observer in self.harness.clouds):
                return
            await asyncio.sleep(self.harness.interval)
        raise HarnessError("An observer stopped responding during final-state verification")


class Harness:
    def __init__(self, clouds, client, result, *, interval=.2, timeout=60.0, cue=None):
        self.clouds = clouds
        self.client = client
        self.result = result
        self.interval = interval
        self.timeout = timeout
        self.failed: set[tuple[str, str]] = set()
        self.cue = cue or operator_cue
        self.trial = 0

    async def fetch(self, cloud, path, *, service=None, method="GET"):
        try:
            response = await self.client.request(method, self.clouds[cloud].base_url.rstrip("/") + path,
                                                 params={"service_name": service} if service else None)
            try:
                body = response.json()
            except ValueError:
                body = None
            return response.status_code, body, "" if response.is_success else response.text[:180]
        except httpx.HTTPError as exc:
            return 0, None, str(exc)

    async def trigger(self, target, service, action):
        if action == "fail":
            # Keep the recovery obligation even when a POST's response is lost.
            self.failed.add((target, service))
        started = time.perf_counter()
        code, body, detail = await self.fetch(target, f"/{service}/simulate/{action}", method="POST")
        if code != 200 or not isinstance(body, dict) or body.get("status") != {"fail": "DOWN", "recover": "UP"}[action]:
            raise HarnessError(f"{target} {action} failed (HTTP {code}): {detail or body}")
        if action == "recover":
            self.failed.discard((target, service))
        return started

    def expected(self, status="UP", target=None):
        targets = [target] if target else list(self.clouds)
        return {observer: {name: status for name in targets} for observer in self.clouds}

    async def baseline(self, service="payments-api", timeout=None):
        async with Monitor(self, self.trial, "baseline", service, 0, self.result) as monitor:
            await monitor.wait(self.expected(), self.timeout if timeout is None else timeout)
            return monitor.states

    async def cleanup(self):
        errors = []
        for target, service in list(self.failed):
            try:
                await self.trigger(target, service, "recover")
                print(f"Recovered {target}/{service}", flush=True)
            except HarnessError as exc:
                errors.append(str(exc))
        if errors:
            raise HarnessError("Recovery incomplete: " + "; ".join(errors))

    async def healthcheck(self):
        rows = []
        for name, cloud in self.clouds.items():
            for path in ["/health", *(f"/{s}/health" for s in (*CRITICAL, cloud.standard_service))]:
                started = time.perf_counter()
                code, data, detail = await self.fetch(name, path)
                passed = code == 200 and isinstance(data, dict) and data.get("status") in ("ok", "UP")
                rows.append({"cloud": name, "path": path, "http_status": code, "body": data,
                             "detail": detail, "passed": passed, "elapsed": time.perf_counter()-started})
                print(f"{name:5} {path:28} HTTP {code} {'PASS' if passed else 'FAIL'}", flush=True)
                await asyncio.sleep(self.interval)
        (self.result.events_path.parent / "healthcheck.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        if not all(row["passed"] for row in rows):
            raise HarnessError("At least one health endpoint failed; see healthcheck.json")

    async def convergence(self, targets, trials, service, label="convergence"):
        # Round-robin targets, 10 trials EACH by default.
        for iteration in range(trials):
            for target in targets:
                self.trial += 1
                print(f"{label} trial {self.trial}: {target} ({iteration+1}/{trials})", flush=True)
                await self.baseline(service)
                for action, status in (("fail", "DOWN"), ("recover", "UP")):
                    t0 = time.perf_counter()
                    async with Monitor(self, self.trial, label, service, t0, self.result) as monitor:
                        await self.trigger(target, service, action)
                        await monitor.wait(self.expected(status, target), self.timeout)
                        own = monitor.first[(target, target, status)]
                        last_peer = max(monitor.first[(peer, target, status)] for peer in self.clouds if peer != target)
                        # Peers may be sampled before the owner's next poll; preserve
                        # signed sampling skew rather than manufacturing zero latency.
                        for metric, value in (("detection", own-t0), ("replication", last_peer-own),
                                              ("total", max(own, last_peer)-t0)):
                            self.result.metric(self.trial, label, target, action, metric, value)
                await self.baseline(service)

    def owner_commands(self, target, action, delay=0):
        commands = []
        for peer, cloud in self.clouds.items():
            if peer == target:
                continue
            if not cloud.public_ip:
                raise HarnessError(f"Config needs public_ip for owner cue: {peer}")
            commands.append(f"sudo bash scripts/netfault.sh {action} {cloud.public_ip}" +
                            (f" {delay}" if action == "delay" else ""))
        return "\n".join(commands)

    async def delay(self, targets, trials, service, delay_ms):
        for target in targets:
            await self.baseline(service)
            cleared = False
            try:
                await self.cue(f"{target} owner: confirm UPDATED netfault.sh is deployed; apply {delay_ms} ms delay to BOTH peers:\n" +
                               self.owner_commands(target, "delay", delay_ms))
                await self.convergence([target], trials, service, label=f"delay-{delay_ms}ms")
            finally:
                await self.cue(f"{target} owner: CLEAR both peer rules:\n" + self.owner_commands(target, "clear"))
                cleared = True
            if cleared:
                await self.baseline(service)

    async def flap(self, targets, trials, service, hold=20, spacing=.5):
        for iteration in range(trials):
            for final in ("fail", "recover"):
                target = targets[iteration % len(targets)]
                self.trial += 1
                await self.baseline(service)
                t0 = time.perf_counter()
                status = "DOWN" if final == "fail" else "UP"
                actions = ["fail", "recover", "fail", "recover", "fail"]
                if final == "recover":
                    actions.append("recover")
                async with Monitor(self, self.trial, "flap", service, t0, self.result) as monitor:
                    for index, action in enumerate(actions):
                        due = t0 + index * spacing
                        await asyncio.sleep(max(0, due-time.perf_counter()))
                        await self.trigger(target, service, action)
                    await asyncio.sleep(hold)
                    # Require a fresh complete polling round after the hold.
                    await monitor.refresh()
                    passed = monitor.matches(self.expected(status, target))
                    self.result.metric(self.trial, "flap", target, final, "final_state_correct", int(passed), "boolean", passed)
                    if not passed:
                        raise HarnessError(f"Rapid update final state mismatch: {monitor.states}")
                await self.trigger(target, service, "recover")
                await self.baseline(service)

    async def partition_updates(self, targets, trials, service):
        for iteration in range(trials):
            for target in targets:
                self.trial += 1
                await self.baseline(service)
                cleared = False
                try:
                    await self.cue(f"{target} owner: partition BOTH peers:\n" + self.owner_commands(target, "partition"))
                    t0 = time.perf_counter()
                    async with Monitor(self, self.trial, "partition-updates", service, t0, self.result) as monitor:
                        for action in ("fail", "recover", "fail"):
                            await self.trigger(target, service, action)
                            await asyncio.sleep(.5)
                        await monitor.wait({target: {target: "DOWN"}}, self.timeout)
                        peers = [name for name in self.clouds if name != target]
                        await monitor.wait({peer: {target: "STALE"} for peer in peers}, self.timeout)
                        await self.cue(f"{target} owner: clear BOTH peers:\n" + self.owner_commands(target, "clear"))
                        cleared = True
                        healed = time.perf_counter()
                        monitor.first.clear()
                        await monitor.wait(self.expected("DOWN", target), self.timeout)
                        last = max(monitor.first[(peer, target, "DOWN")] for peer in peers)
                        self.result.metric(self.trial, "partition-updates", target, "heal", "latest_state_convergence", last-healed)
                finally:
                    if not cleared:
                        await self.cue(f"{target} owner: confirm both rules are cleared:\n" + self.owner_commands(target, "clear"))
                    await self.trigger(target, service, "recover")
                await self.baseline(service)

    async def partition(self, targets, trials, service, hold=60):
        for iteration in range(trials):
            for target in targets:
                self.trial += 1
                await self.baseline(service)
                cleared = False
                t0 = time.perf_counter()
                async with Monitor(self, self.trial, "partition", service, t0, self.result) as monitor:
                    try:
                        # Enter IMMEDIATELY after both owner commands complete.
                        await self.cue(f"{target} owner: partition BOTH peers NOW; confirm immediately:\n" +
                                       self.owner_commands(target, "partition"))
                        t0 = time.perf_counter()
                        monitor.trigger = t0
                        monitor.first.clear()
                        peers = [name for name in self.clouds if name != target]
                        expected = {target: {target: "UP", **{p: "STALE" for p in peers}},
                                    **{p: {target: "STALE"} for p in peers}}
                        await monitor.wait(expected, self.timeout)
                        for peer in peers:
                            self.result.metric(self.trial, "partition", target, "partition",
                                               f"stale_at_{peer}", monitor.first[(peer, target, "STALE")]-t0)
                        await asyncio.sleep(max(0, t0+hold-time.perf_counter()))
                        await monitor.refresh()
                        # Verify partition state persists throughout the hold.
                        if not monitor.matches(expected):
                            raise HarnessError(f"Partition steady state lost: {monitor.states}")
                        for observer, errors in monitor.errors.items():
                            self.result.metric(self.trial, "partition", target, "partition",
                                               f"lookup_errors_{observer}", errors, "count", errors == 0)
                        had_errors = any(monitor.errors.values())
                        await self.cue(f"{target} owner: HEAL both peers; confirm immediately:\n" +
                                       self.owner_commands(target, "clear"))
                        cleared = True
                        healed = time.perf_counter()
                        monitor.trigger = healed
                        monitor.first.clear()
                        await monitor.wait(self.expected(), self.timeout)
                        changed = [(p, target) for p in peers] + [(target, p) for p in peers]
                        restored = max(monitor.first[(o, t, "UP")] for o, t in changed)
                        self.result.metric(self.trial, "partition", target, "heal", "all_up", restored-healed)
                        if had_errors:
                            raise HarnessError("A lookup failed during the partition; availability criterion failed")
                    finally:
                        if not cleared:
                            await self.cue(f"{target} owner: confirm both rules are cleared:\n" + self.owner_commands(target, "clear"))
                await self.baseline(service)

    async def latency(self, service, samples=200):
        await self.baseline(service)
        async def sample_cloud(name):
            # Avoid cache/connection warm-up distorting only one measured path.
            for path in ("/lookup", "/health"):
                await self.fetch(name, path, service=service if path == "/lookup" else None)
                await asyncio.sleep(self.interval)
                for sample in range(1, samples+1):
                    started = time.perf_counter()
                    code, body, detail = await self.fetch(name, path, service=service if path == "/lookup" else None)
                    elapsed = time.perf_counter()-started
                    passed = code == 200 and isinstance(body, dict)
                    self.result.metric(sample, "latency", name, path, "request_latency", elapsed, passed=passed)
                    if not passed:
                        print(f"{name} {path}: HTTP {code} {detail}", flush=True)
                    # 5 requests/sec rather than blasting nginx's 20/sec limiter.
                    await asyncio.sleep(max(0, self.interval-elapsed))
                print(f"{name}: {samples} {path} latency samples recorded", flush=True)
        # Each cloud gets sequential samples; distinct proxies are independent.
        await asyncio.gather(*(sample_cloud(name) for name in self.clouds))
        if any(not row["passed"] for row in self.result.rows):
            raise HarnessError("Latency sampling had unsuccessful requests; see CSV")

    async def watch(self, service, duration):
        async with Monitor(self, 0, "watch", service, 0, self.result) as monitor:
            until = time.perf_counter()+duration if duration else float("inf")
            previous = None
            while time.perf_counter() < until:
                view = json.dumps(monitor.states, sort_keys=True)
                if view != previous:
                    print(datetime.now().astimezone().isoformat(timespec="seconds"), view, flush=True)
                    previous = view
                await asyncio.sleep(self.interval)

    async def client_once(self, service, preferred="gcp"):
        order = [preferred, *[name for name in self.clouds if name != preferred]]
        # Prefer cached/local region ordering explicitly rather than depending on
        # the remote registry's local-first list. Try every reachable observer.
        for observer in order:
            code, data, _ = await self.fetch(observer, "/lookup", service=service)
            if code != 200 or not isinstance(data, dict):
                continue
            entries = data.get("endpoints", [])
            if not isinstance(entries, list):
                continue
            usable = {entry.get("region_id") for entry in entries
                      if isinstance(entry, dict) and isinstance(entry.get("region_id"), str)
                      and entry.get("status") == "UP"}
            for target in order:
                if self.clouds[target].region_id not in usable:
                    continue
                # Catalog endpoints are VM-private addresses; route via the
                # region's configured Tailscale proxy instead of dialing them.
                code, body, _ = await self.fetch(target, f"/{service}/health")
                if code == 200 and isinstance(body, dict) and body.get("status") == "UP":
                    return {"status": "UP", "cloud": target, "body": body}
        return {"status": "UNAVAILABLE", "service": service}

    async def client_loop(self, service, preferred, duration):
        until = time.perf_counter()+duration if duration else float("inf")
        while time.perf_counter() < until:
            print(json.dumps(await self.client_once(service, preferred)), flush=True)
            # Demo uses health stubs, not payment transactions.
            await asyncio.sleep(1)


async def operator_cue(message):
    print("\nOWNER CUE\n" + message, flush=True)
    answer = await asyncio.to_thread(input, "Type done when completed (or abort): ")
    if answer.strip().lower() != "done":
        raise HarnessError("Owner cue not confirmed; aborting")


def compile_report(output: Path):
    """Summarize recorded data without fabricating missing experiments or owner inputs."""
    output.mkdir(parents=True, exist_ok=True)
    sessions = []
    for metadata in sorted(output.glob('*/session.json')):
        folder = metadata.parent
        session = json.loads(metadata.read_text(encoding='utf-8'))
        provenance_file = folder / 'provenance.json'
        provenance = json.loads(provenance_file.read_text(encoding='utf-8')) if provenance_file.exists() else {}
        # Imported runs can carry the old harness's incorrect hardcoded laptop
        # clock label. Keep their original session/CSV bytes and report the
        # independently supplied measurement origin alongside every result.
        session['_report_vantage'] = provenance.get('measurement_vantage', session.get('measurement_vantage', "Ryan's laptop"))
        session['_report_vantage_note'] = provenance.get('vantage_note')
        summary_file = folder / (session['command'] + '-summary.json')
        summary = json.loads(summary_file.read_text(encoding='utf-8')) if summary_file.exists() else {'outcome': 'INCOMPLETE', 'metrics': []}
        sessions.append((folder, session, summary))
    lines = ['# Phase 5 experiment results', '',
             'This report summarizes recorded runs. Local unit tests do not count as cloud experiments.', '',
             '## Measurement origins', '',
             'Every results row identifies the client measurement vantage. Keep laptop and GCP-VM latency results separate; do not pool them or describe GCP-VM timings as laptop timings.',
             'Each run uses one clock on its measurement host. Raw perf-counter timestamps are not comparable across hosts.', '']
    origins = sorted({(session['_report_vantage'], session['_report_vantage_note'] or '') for _, session, _ in sessions})
    for vantage, note in origins:
        reference = f' See the [original vantage note]({note}).' if note else ''
        lines.append(f'- {vantage}.{reference}')
    if any(note for _, note in origins):
        lines += ['', 'Imported GCP-VM runs have client located in the GCP region: the GCP proxy is effectively local. Original session files may retain the older hardcoded laptop label; the supplied provenance corrects that interpretation without modifying raw data.']
    lines += ['', '| Run | Outcome | Measurement vantage | Parameters | Raw data |', '|---|---|---|---|---|']
    for folder, session, summary in sessions:
        params = session.get('parameters', {})
        spec = ', '.join(f'{key}={params[key]}' for key in ('targets', 'trials', 'samples', 'delay_ms') if key in params)
        lines.append(f"| {session['command']} | {summary['outcome']} | {session['_report_vantage']} | {spec or 'read-only / diagnostic'} | [{folder.name}]({folder.name}/) |")
    lines += ['', '## Measured results', '',
              'Durations are seconds unless another unit is shown. Failed samples are counted separately.', '',
              '| Run | Measurement vantage | Target | Phase | Metric | N | Failed | Median | p95 | Max | Unit |',
              '|---|---|---|---|---|---:|---:|---:|---:|---:|---|']
    for folder, session, summary in sessions:
        for row in summary.get('metrics', []):
            numbers = [f'{row[key]:.6f}' if row[key] is not None else '—' for key in ('median', 'p95', 'max')]
            lines.append(f"| {folder.name} | {session['_report_vantage']} | {row['target_region']} | {row['phase']} | {row['metric']} | {row['samples']} | {row['failed']} | " +
                         ' | '.join(numbers) + f" | {row['unit']} |")
    lines += ['', '## Lookup latency minus health baseline', '',
              'Difference of medians/p95s is a rough estimate, not paired subtraction or pure agent execution time.', '',
              '| Run | Measurement vantage | Cloud | Median difference (s) | p95 difference (s) |', '|---|---|---|---:|---:|']
    for folder, session, summary in sessions:
        if session['command'] != 'latency':
            continue
        for target in ('gcp', 'aws', 'azure'):
            rows = {row['phase']: row for row in summary.get('metrics', []) if row['target_region'] == target}
            if all(path in rows and rows[path]['median'] is not None for path in ('/lookup', '/health')):
                lookup, health = rows['/lookup'], rows['/health']
                lines.append(f"| {folder.name} | {session['_report_vantage']} | {target} | {lookup['median']-health['median']:.6f} | {lookup['p95']-health['p95']:.6f} |")
    completed = {session['command'] for _, session, summary in sessions if summary['outcome'] == 'PASS'}
    lines += ['', '## Remaining work', '']
    for command in ('baseline', 'convergence', 'flap', 'partition-updates', 'partition', 'latency'):
        if command not in completed:
            lines.append(f'- {command}: no passing cloud run recorded.')
    for delay_ms in (500, 1000):
        if not any(session['command'] == 'delay' and session.get('parameters', {}).get('delay_ms') == delay_ms
                   and summary['outcome'] == 'PASS' for _, session, summary in sessions):
            lines.append(f'- Delay variant {delay_ms} ms: no passing cloud run recorded.')
    lines += ['- Confirm full required trial/sample counts; a short passing run is a smoke rehearsal.',
              '- Rehearse and record D1 payment failover and owner-coordinated D2 recommend-api outage.',
              '',
              '## Demo rehearsal evidence', '']
    demo_log = output / 'demo-payments-client.log'
    if demo_log.exists():
        observations = []
        raw_log = demo_log.read_bytes()
        decoded_log = raw_log.decode('utf-16' if raw_log.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
        for line in decoded_log.splitlines():
            if line.startswith('{'):
                try:
                    observations.append(json.loads(line))
                except ValueError:
                    pass
        successes = [sample for sample in observations if sample.get('status') == 'UP']
        selected = sorted({sample.get('cloud', '') for sample in successes})
        lines += [f'- Payments client: {len(successes)}/{len(observations)} successful recorded responses.',
                  '- Successful selected regions: ' + ', '.join(selected) + '.',
                  '- Evidence: [client log](demo-payments-client.log). Recorded during convergence failure/recovery trials.',
                  '- This client calls the stub health endpoint; it does not perform financial transactions.']
    else:
        lines.append('- No payments client rehearsal log recorded.')
    storefront_summary = output / 'storefront-rehearsal-summary.json'
    if storefront_summary.exists():
        storefront = json.loads(storefront_summary.read_text(encoding='utf-8'))
        stages = storefront.get('stages', {})
        lines += ['', '## Storefront rehearsal', '',
                  f"Recorded outcome: **{storefront.get('outcome', 'INCOMPLETE')}**.",
                  f"Method: {storefront.get('method', 'See raw evidence')}.",
                  'Orders use simulated business services; there are no real payments or email deliveries.', '',
                  '| Scenario | Order placed | Payment region |', '|---|---|---|']
        for name, label in (('normal_order', 'Normal checkout'),
                            ('gcp_payments_failed_order', 'GCP payments failed'),
                            ('checkout_during_recommendation_outage', 'AWS recommendations failed')):
            stage = stages.get(name)
            if stage:
                payment = next((step.get('region') for step in stage.get('steps', []) if step.get('name') == 'Payment'), None)
                lines.append(f"| {label} | {stage.get('order_placed', False)} | {payment or 'unavailable'} |")
        lines += ['', f"All three UI panels rendered GCP payments red: {stages.get('gcp_failure_registry_views', {}).get('all_three_render_red', False)}.",
                  f"AWS recommendation-unavailable warning observed: {bool(stages.get('aws_recommendations_failed', {}).get('ui_unavailable_warning'))}.",
                  f"AWS recommendations restored in UI: {stages.get('recommendations_recovered', {}).get('ui_aws_recommendations_available', False)}.",
                  '[Rehearsal evidence](storefront-rehearsal-summary.json) · [Registry transitions](storefront-rehearsal-events.jsonl).']
        cleanup = storefront.get('post_rehearsal_verification', {})
        if cleanup:
            lines += ['', 'Post-rehearsal verification: ' + cleanup.get('status', 'see evidence') + '.',
                      'An initial health sweep found recommendations in simulated failure again; explicit recovery was followed by successful business-endpoint, full health and baseline checks. The initial failed sweep is retained in the results.']
    lines += ['', '## Owner inputs (left blank as requested)', '',
              '| Cloud | VM size | Billing evidence | Module / issue notes |',
              '|---|---|---|---|', '| GCP | | | |', '| AWS | | | |', '| Azure | | | |', '',
              '## Setup and limitations', '',
              '- Regions: gcp-asia-south1, aws-ap-south-1, azure-indiasouthcentral.',
              '- Test access: Tailscale HTTPS proxies with self-signed certificates; measurement origin is identified per run.',
              '- Replication: mTLS on VM public IPs, TCP 9443; capture owner logs to corroborate.',
              '- Catalog is in memory; restart requires service re-registration.',
              '- Health detection is sampled every five seconds; laptop lookup polling is every 200 ms.',
              '- Owner confirmation is timed on the laptop and includes human/network cue uncertainty.',
              '- Delay injection affects outbound traffic. It does not simulate symmetric latency.',
              '- Negative replication components reflect observer sampling order; retain them as sampling skew.',
              '- Final-state rapid flips test eventual convergence; the lookup does not expose version numbers.',
              '- Duplicate, out-of-order and old-version behavior: tests/test_storage.py and tests/test_replication.py.', '']
    destination = output / 'phase5-report.md'
    destination.write_text('\n'.join(lines), encoding='utf-8')
    print(f'Report: {destination.resolve()}')
    return destination


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("results/clouds.local.json"))
    parser.add_argument("--output", type=Path, default=Path("results"))
    parser.add_argument("--interval", type=float, default=.2)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--request-timeout", type=float, default=2)
    parser.add_argument("--service", default="payments-api")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report")
    for command in ("baseline", "healthcheck", "recover"):
        sub.add_parser(command)
    for command in ("convergence", "flap", "partition-updates", "partition", "delay"):
        item = sub.add_parser(command)
        item.add_argument("--targets", nargs="+", choices=("gcp", "aws", "azure"), default=["gcp", "aws", "azure"])
        item.add_argument("--trials", type=int, default={"convergence": 10, "flap": 5, "partition": 3,
                                                     "partition-updates": 1, "delay": 5}[command])
        item.add_argument("--on-call", action="store_true", help="Confirm the team call is active")
        if command in ("partition", "flap"):
            item.add_argument("--hold", type=float, default=60 if command == "partition" else 20)
        if command == "delay":
            item.add_argument("--delay-ms", type=int, choices=(500, 1000), required=True)
    item = sub.add_parser("latency")
    item.add_argument("--samples", type=int, default=200)
    for command in ("watch", "client"):
        item = sub.add_parser(command)
        item.add_argument("--duration", type=float, default=0, help="Seconds; zero runs until Ctrl+C")
        if command == "client":
            item.add_argument("--preferred", choices=("gcp", "aws", "azure"), default="gcp")
    return parser


async def run(args):
    if args.command == 'report':
        compile_report(args.output)
        return
    if args.interval < .2 or args.timeout <= 0 or args.request_timeout <= 0:
        raise HarnessError("Use interval >= 0.2 s and positive timeouts")
    if hasattr(args, "trials") and (args.trials <= 0 or not args.on_call):
        raise HarnessError("Failure experiments require positive --trials and --on-call")
    if getattr(args, "samples", 1) <= 0 or getattr(args, "hold", 1) < 0 or getattr(args, "duration", 0) < 0:
        raise HarnessError("Samples must be positive; hold/duration must be nonnegative")
    clouds = load_clouds(args.config)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    folder = args.output / f"{stamp}-{args.command}"
    result = Results(folder, args.command)
    print(f"Results: {folder.resolve()}", flush=True)
    (folder / "session.json").write_text(json.dumps({
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "started_local": datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat(),
        "command": args.command, "parameters": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "clouds": {name: vars(cloud) for name, cloud in clouds.items()},
        "clock": "time.perf_counter on one laptop; wall_time is time.time",
        "tls_verify": False,
        "owner_cue_timing": "Operator receipt after owner confirmation, not precise remote command execution",
    }, indent=2), encoding="utf-8")
    async with httpx.AsyncClient(verify=False, trust_env=False, timeout=args.request_timeout,
                                 follow_redirects=False,
                                 limits=httpx.Limits(max_connections=12, max_keepalive_connections=6)) as client:
        harness = Harness(clouds, client, result, interval=args.interval, timeout=args.timeout)
        try:
            if args.command == "baseline":
                print(json.dumps(await harness.baseline(args.service), indent=2), flush=True)
            elif args.command == "healthcheck":
                await harness.healthcheck()
            elif args.command == "recover":
                for name in clouds:
                    await harness.trigger(name, args.service, "recover")
                await harness.baseline(args.service)
            elif args.command == "convergence":
                await harness.convergence(args.targets, args.trials, args.service)
            elif args.command == "flap":
                await harness.flap(args.targets, args.trials, args.service, hold=args.hold)
            elif args.command == "delay":
                await harness.delay(args.targets, args.trials, args.service, args.delay_ms)
            elif args.command == "partition-updates":
                await harness.partition_updates(args.targets, args.trials, args.service)
            elif args.command == "partition":
                await harness.partition(args.targets, args.trials, args.service, hold=args.hold)
            elif args.command == "latency":
                await harness.latency(args.service, args.samples)
            elif args.command == "watch":
                await harness.watch(args.service, args.duration)
            elif args.command == "client":
                await harness.client_loop(args.service, args.preferred, args.duration)
            result.outcome = "PASS"
        except BaseException:
            result.outcome = "FAILED_OR_INTERRUPTED"
            raise
        finally:
            try:
                # Stop observing before recovery so cleanup is outside trial timing.
                await asyncio.shield(harness.cleanup())
                if hasattr(args, "on_call"):
                    await harness.baseline(args.service)
            except BaseException:
                result.outcome = "CLEANUP_OR_BASELINE_FAILED"
                raise
            finally:
                result.close()
                print(f"Summary: {result.summary_path.resolve()} ({result.outcome})", flush=True)


def main():
    try:
        asyncio.run(run(make_parser().parse_args()))
        return 0
    except (HarnessError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1
    except KeyboardInterrupt:
        print("Stopped. Review recovery output and ask owners to clear fault rules.", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
