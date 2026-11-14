"""Kafka replication experiments for payments and stock, run from a laptop on
the tailnet through each region's proxy.

    python -m harness.kafka_experiments lag --rounds 15
    python -m harness.kafka_experiments outage --cloud gcp \
        --stop-cmd "ssh ... sudo systemctl stop registry-replicator" \
        --start-cmd "ssh ... sudo systemctl start registry-replicator"

lag:    each cloud in turn makes a Rs 1 payment (and every few rounds a 1-unit
        stock reservation); the other two clouds' ledgers are polled until the
        event appears. Records the laptop-measured end-to-end time (POST
        returned -> visible on the peer, includes polling and proxy RTT) and
        the receiver-reported lag_ms (origin timestamp -> applied; VM clocks).
outage: stops one cloud's replicator, places orders there (they must still
        succeed and wait in the outbox), restarts it and times the catch-up.

Every order is real: reset the ledgers afterwards for a clean demo.
Output: results/kafka-<date>/<experiment>.json plus a printed summary.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
warnings.filterwarnings("ignore", message="Unverified HTTPS request")


class Clouds:
    def __init__(self, config: Path, timeout: float = 5.0):
        self.cfg = json.loads(config.read_text(encoding="utf-8"))
        self.client = httpx.Client(verify=False, timeout=timeout)

    def url(self, cloud: str, path: str) -> str:
        return self.cfg[cloud]["base_url"].rstrip("/") + path

    def pay(self, cloud: str, order_id: str, amount: float = 1) -> dict:
        r = self.client.post(self.url(cloud, "/payments-api/authorize"),
                             json={"order_id": order_id, "amount": amount, "currency": "INR",
                                   "card_token": "tok_experiment"})
        r.raise_for_status()
        return r.json()

    def reserve(self, cloud: str, order_id: str, product_id: str = "p1") -> dict:
        r = self.client.post(self.url(cloud, "/inventory-check/reserve"),
                             json={"order_id": order_id, "items": [{"product_id": product_id, "qty": 1}]})
        r.raise_for_status()
        return r.json()

    def ledger(self, cloud: str) -> dict:
        r = self.client.get(self.url(cloud, "/payments-api/ledger"), params={"limit": 100})
        r.raise_for_status()
        return r.json()

    def global_stock(self, cloud: str, product_id: str = "p1") -> int:
        r = self.client.get(self.url(cloud, "/inventory-check/stock"))
        r.raise_for_status()
        return next(i["global_stock"] for i in r.json()["items"] if i["product_id"] == product_id)


def wait_for(check, timeout: float, interval: float = 0.05):
    """Poll check() until it returns a non-None value; (value, seconds) or (None, timeout)."""
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        try:
            value = check()
        except httpx.HTTPError:
            value = None
        if value is not None:
            return value, time.perf_counter() - start
        time.sleep(interval)
    return None, timeout


def watch_peers(peers: list[str], check, timeout: float) -> dict:
    """Poll every peer in parallel, all timed from the same start (the write
    just returned); {peer: (value or None, seconds)}."""
    with ThreadPoolExecutor(max_workers=len(peers)) as pool:
        futures = {p: pool.submit(wait_for, lambda p=p: check(p), timeout) for p in peers}
        return {p: f.result() for p, f in futures.items()}


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    s = sorted(values)
    pick = lambda q: s[min(len(s) - 1, round(q * (len(s) - 1)))]
    return {"n": len(s), "min": round(s[0]), "median": round(statistics.median(s)),
            "p95": round(pick(0.95)), "max": round(s[-1])}


# ---------------------------------------------------------------- lag

def run_lag(clouds: Clouds, rounds: int, reserve_every: int, timeout: float) -> dict:
    names = list(clouds.cfg)
    samples = []
    for rnd in range(rounds):
        for origin in names:
            peers = [n for n in names if n != origin]
            # payment
            order = f"exp-lag-{origin}-{rnd}-{uuid.uuid4().hex[:6]}"
            clouds.pay(origin, order)

            def payment_seen(peer):
                hit = [p for p in clouds.ledger(peer)["recent"] if p["order_id"] == order]
                return hit[0] if hit else None
            for peer, (hit, secs) in watch_peers(peers, payment_seen, timeout).items():
                samples.append({"kind": "payment", "origin": origin, "peer": peer, "order_id": order,
                                "e2e_ms": round(secs * 1000) if hit else None,
                                "reported_lag_ms": hit["lag_ms"] if hit else None})
            # stock reservation, checked through the peers' global stock
            if reserve_every and rnd % reserve_every == 0:
                before = {p: clouds.global_stock(p) for p in peers}
                if clouds.reserve(origin, f"exp-stock-{origin}-{rnd}")["status"] == "reserved":
                    def stock_seen(peer):
                        return True if clouds.global_stock(peer) <= before[peer] - 1 else None
                    for peer, (hit, secs) in watch_peers(peers, stock_seen, timeout).items():
                        samples.append({"kind": "stock", "origin": origin, "peer": peer,
                                        "e2e_ms": round(secs * 1000) if hit else None})
        print(f"round {rnd + 1}/{rounds} done", flush=True)

    out = {"experiment": "lag", "rounds": rounds, "samples": samples, "summary": {}}
    for kind in ("payment", "stock"):
        ks = [s for s in samples if s["kind"] == kind]
        out["summary"][kind] = {
            "e2e_ms": summarize([s["e2e_ms"] for s in ks if s["e2e_ms"] is not None]),
            "missing": sum(1 for s in ks if s["e2e_ms"] is None)}
        if kind == "payment":
            out["summary"][kind]["reported_lag_ms"] = summarize(
                [s["reported_lag_ms"] for s in ks if s["reported_lag_ms"] is not None])
            out["summary"][kind]["by_route_e2e_ms"] = {
                f"{o}->{p}": summarize([s["e2e_ms"] for s in ks
                                        if s["origin"] == o and s["peer"] == p and s["e2e_ms"] is not None])
                for o in names for p in names if o != p}
    return out


# ---------------------------------------------------------------- outage

def run_outage(clouds: Clouds, cloud: str, orders: int, stop_cmd: str, start_cmd: str,
               hold: float, timeout: float) -> dict:
    peers = [n for n in clouds.cfg if n != cloud]
    subprocess.run(stop_cmd, shell=True, check=True)
    time.sleep(2)
    placed = []
    for i in range(orders):
        order = f"exp-outage-{cloud}-{i}-{uuid.uuid4().hex[:6]}"
        res = clouds.pay(cloud, order)
        placed.append({"order_id": order, "status": res["status"]})
    time.sleep(hold)  # give a (wrongly) running replicator time to leak them
    unsent_during = clouds.ledger(cloud)["unsent_events"]
    leaked = {p: sum(1 for o in placed for q in clouds.ledger(p)["recent"] if q["order_id"] == o["order_id"])
              for p in peers}

    t0 = time.perf_counter()
    subprocess.run(start_cmd, shell=True, check=True)
    catch_up = {}
    for peer in peers:
        def all_seen(peer=peer):
            ids = {q["order_id"] for q in clouds.ledger(peer)["recent"]}
            return True if all(o["order_id"] in ids for o in placed) else None
        ok, _ = wait_for(all_seen, timeout, interval=0.2)
        catch_up[peer] = round((time.perf_counter() - t0) * 1000) if ok else None
    _, _ = wait_for(lambda: True if clouds.ledger(cloud)["unsent_events"] == 0 else None, timeout, 0.2)
    return {"experiment": "outage", "cloud": cloud, "orders": placed,
            "all_approved_while_kafka_link_down": all(o["status"] == "approved" for o in placed),
            "unsent_events_during_outage": unsent_during, "seen_by_peers_during_outage": leaked,
            "catch_up_ms_after_restart": catch_up,
            "unsent_events_after": clouds.ledger(cloud)["unsent_events"]}


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("experiment", choices=["lag", "outage"])
    ap.add_argument("--config", type=Path, default=ROOT / "results" / "clouds.local.json")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / f"kafka-{time.strftime('%Y-%m-%d')}")
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--reserve-every", type=int, default=5, help="stock reservation every N rounds (0 = never)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--cloud", default="gcp", help="outage: whose replicator is stopped")
    ap.add_argument("--orders", type=int, default=5)
    ap.add_argument("--hold", type=float, default=10.0, help="outage: seconds to wait before restarting")
    ap.add_argument("--stop-cmd")
    ap.add_argument("--start-cmd")
    args = ap.parse_args()

    clouds = Clouds(args.config)
    if args.experiment == "lag":
        result = run_lag(clouds, args.rounds, args.reserve_every, args.timeout)
    else:
        if not (args.stop_cmd and args.start_cmd):
            ap.error("outage needs --stop-cmd and --start-cmd")
        result = run_outage(clouds, args.cloud, args.orders, args.stop_cmd, args.start_cmd,
                            args.hold, args.timeout)
    result["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    result["vantage"] = "laptop on tailnet, via each region's nginx proxy"
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{args.experiment}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=2))
    print(f"saved {path}")


if __name__ == "__main__":
    main()
