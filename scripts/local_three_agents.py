"""Step 2.7 rehearsal for the replication module: 3 dev agents on one machine,
real mTLS between them.

    bash certs/gen-certs.sh certs/local aws-ap-south-1=127.0.0.1 azure-indiasouthcentral=127.0.0.1 gcp-asia-south1=127.0.0.1
    python scripts/local_three_agents.py

Uses the real agent.storage.CatalogStorage. Checks: registration replicates,
DOWN propagates, deregistration (tombstone) propagates, a restarted peer gets a
full snapshot, a peer can't publish another region's entries, and connections
without a CA-signed client cert are refused.
"""
from __future__ import annotations

import os
import ssl
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CERTS = ROOT / "certs" / "local"
REGIONS = {  # region_id -> (replication port, debug port)
    "aws-ap-south-1": (9441, 9001),
    "azure-indiasouthcentral": (9442, 9002),
    "gcp-asia-south1": (9443, 9003),
}
procs: dict[str, subprocess.Popen] = {}


def start(region: str) -> None:
    rport, dport = REGIONS[region]
    peers = ",".join(f"{r}=https://127.0.0.1:{p}" for r, (p, _) in REGIONS.items() if r != region)
    env = dict(
        os.environ,
        REGION_ID=region,
        REPLICATION_PEERS=peers,
        REPLICATION_HOST="127.0.0.1",
        REPLICATION_PORT=str(rport),
        DEV_API_PORT=str(dport),
        TLS_CERT=str(CERTS / f"{region}.crt"),
        TLS_KEY=str(CERTS / f"{region}.key"),
        TLS_CA=str(CERTS / "ca.crt"),
    )
    log = open(ROOT / "scripts" / f"{region}.log", "w")
    procs[region] = subprocess.Popen(
        [sys.executable, "-m", "agent.replication.dev.devagent"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    wait_until(lambda: debug(region, "GET", "/debug/lookup?service_name=x") is not None, f"{region} start")


def stop(region: str) -> None:
    procs[region].terminate()
    procs[region].wait()


def debug(region: str, method: str, path: str, body: dict | None = None):
    try:
        r = httpx.request(method, f"http://127.0.0.1:{REGIONS[region][1]}{path}", json=body, timeout=1)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError:
        return None


def status_seen(viewer: str, service: str, owner: str) -> str | None:
    found = debug(viewer, "GET", f"/debug/lookup?service_name={service}") or {"endpoints": []}
    for e in found["endpoints"]:
        if e["region_id"] == owner:
            return e["status"]
    return None


def peer_client(region: str) -> httpx.Client:
    ctx = ssl.create_default_context(cafile=str(CERTS / "ca.crt"))
    ctx.load_cert_chain(str(CERTS / f"{region}.crt"), str(CERTS / f"{region}.key"))
    return httpx.Client(verify=ctx, timeout=2)


def wait_until(cond, what: str, timeout: float = 10.0) -> float:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return time.time() - t0
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for: {what}")


def check(name: str, fn) -> None:
    fn()
    print(f"PASS  {name}")


def mtls_rejects(cert: tuple[str, str] | None) -> bool:
    ctx = ssl.create_default_context(cafile=str(CERTS / "ca.crt"))
    if cert:
        ctx.load_cert_chain(*cert)
    try:
        httpx.get("https://127.0.0.1:9443/replicate/ping", verify=ctx, timeout=2)
        return False
    except (httpx.HTTPError, ssl.SSLError):
        return True


def main() -> None:
    for region in REGIONS:
        start(region)
    gcp, aws, az = "gcp-asia-south1", "aws-ap-south-1", "azure-indiasouthcentral"

    def registration_replicates():
        debug(gcp, "POST", "/debug/register", {"service_name": "payments-api", "endpoint": "10.2.0.5:8081"})
        t = wait_until(lambda: status_seen(aws, "payments-api", gcp) == "UP"
                       and status_seen(az, "payments-api", gcp) == "UP", "UP on both peers")
        print(f"      converged in {t * 1000:.0f} ms")

    def down_propagates():
        debug(gcp, "POST", "/debug/status", {"service_name": "payments-api", "status": "DOWN"})
        t = wait_until(lambda: status_seen(aws, "payments-api", gcp) == "DOWN"
                       and status_seen(az, "payments-api", gcp) == "DOWN", "DOWN on both peers")
        print(f"      converged in {t * 1000:.0f} ms")

    def deregistration_propagates():
        debug(aws, "POST", "/debug/register", {"service_name": "recommend-api", "endpoint": "10.0.1.5:8084",
                                               "tier": "standard"})
        wait_until(lambda: status_seen(gcp, "recommend-api", aws) == "UP", "recommend-api on GCP")
        debug(aws, "POST", "/debug/deregister", {"service_name": "recommend-api"})
        t = wait_until(lambda: status_seen(gcp, "recommend-api", aws) is None
                       and status_seen(az, "recommend-api", aws) is None, "tombstone on both peers")
        print(f"      converged in {t * 1000:.0f} ms")

    def snapshot_after_restart():
        stop(az)
        debug(gcp, "POST", "/debug/register", {"service_name": "notify-api", "endpoint": "10.2.0.5:8084",
                                               "tier": "standard"})
        wait_until(lambda: status_seen(aws, "notify-api", gcp) == "UP", "notify-api on AWS")
        start(az)  # fresh, empty catalog, new boot_id
        t = wait_until(lambda: status_seen(az, "notify-api", gcp) == "UP"
                       and status_seen(az, "payments-api", gcp) == "DOWN", "snapshot on restarted Azure")
        print(f"      snapshot delivered {t * 1000:.0f} ms after restart")

    def impersonation_rejected():
        # AWS's valid cert tries to publish an entry claiming to be GCP's, straight to Azure.
        forged = {"service_name": "payments-api", "region_id": gcp, "endpoint": "10.9.9.9:8081",
                  "health_check_path": "/health", "tier": "critical", "status": "UP",
                  "timestamp": time.time(), "version": time.time() + 3600, "deleted": False}
        with peer_client(aws) as client:
            r = client.post(f"https://127.0.0.1:{REGIONS[az][0]}/replicate/receive", json=forged)
        assert r.status_code == 403, r.status_code
        assert status_seen(az, "payments-api", gcp) == "DOWN"  # unchanged

    def mtls_enforced():
        assert mtls_rejects(None), "connection without client cert was accepted"
        rogue = ROOT / "certs" / "rogue"
        assert mtls_rejects((str(rogue / "rogue.crt"), str(rogue / "rogue.key"))), "self-signed cert accepted"
        assert not mtls_rejects((str(CERTS / f"{aws}.crt"), str(CERTS / f"{aws}.key"))), "valid peer rejected"

    try:
        check("registration replicates to both peers", registration_replicates)
        check("DOWN status propagates", down_propagates)
        check("deregistration (tombstone) propagates", deregistration_propagates)
        check("restarted peer receives full snapshot", snapshot_after_restart)
        check("peer cert cannot publish another region's entries", impersonation_rejected)
        check("mTLS: no cert / rogue cert refused, peer cert accepted", mtls_enforced)
    finally:
        for region in procs:
            stop(region)


if __name__ == "__main__":
    main()
