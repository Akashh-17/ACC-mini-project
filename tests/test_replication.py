"""Replication module tests against the real Azure CatalogStorage."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.replication import ReplicationWorker, create_replication_app
from agent.replication.config import parse_peers
from agent.replication.receiver import PEER_SCOPE_KEY
from agent.replication.server import cert_common_name
from agent.storage import CatalogStorage

GCP = "gcp-asia-south1"
AWS = "aws-ap-south-1"
AZ = "azure-indiasouthcentral"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def record(status="UP", ts=1000.0, endpoint="10.0.1.10:8081", tier="critical"):
    return {"endpoint": endpoint, "health_check_path": "/health", "tier": tier,
            "status": status, "timestamp": ts}


def as_peer(app, cn):
    """Simulate what PeerCertH11Protocol does after a verified mTLS handshake."""
    async def wrapped(scope, receive, send):
        scope[PEER_SCOPE_KEY] = cn
        await app(scope, receive, send)
    return wrapped


# ---------- /replicate/receive ----------

@pytest.fixture
def gcp_receiver():
    clock = Clock()
    catalog = CatalogStorage(GCP, clock=clock)
    app = create_replication_app(catalog, GCP, allowed_peers=[AWS, AZ])
    return catalog, app


def owner_record(clock_t=1000.0, **kw):
    """A record exactly as AWS's storage would emit it."""
    aws = CatalogStorage(AWS, clock=Clock(clock_t))
    return aws.set("payments-api", AWS, record(**kw))


def test_receive_applies_peer_record(gcp_receiver):
    catalog, app = gcp_receiver
    r = TestClient(as_peer(app, AWS)).post("/replicate/receive", json=owner_record())
    assert r.json() == {"status": "applied"}
    assert catalog.get("payments-api", AWS)["status"] == "UP"


def test_older_or_duplicate_version_is_discarded(gcp_receiver):
    catalog, app = gcp_receiver
    client = TestClient(as_peer(app, AWS))
    aws = CatalogStorage(AWS, clock=Clock(1000.0))
    v1 = aws.set("payments-api", AWS, record(status="UP"))
    aws._clock = Clock(1005.0)
    v2 = aws.set("payments-api", AWS, record(status="DOWN"))

    assert client.post("/replicate/receive", json=v2).json() == {"status": "applied"}
    assert client.post("/replicate/receive", json=v1).json() == {"status": "discarded"}  # reordered
    assert client.post("/replicate/receive", json=v2).json() == {"status": "discarded"}  # duplicate
    assert catalog.get("payments-api", AWS)["status"] == "DOWN"


def test_tombstone_removes_entry(gcp_receiver):
    catalog, app = gcp_receiver
    client = TestClient(as_peer(app, AWS))
    aws = CatalogStorage(AWS, clock=Clock(1000.0))
    client.post("/replicate/receive", json=aws.set("payments-api", AWS, record()))
    aws._clock = Clock(1001.0)
    aws.delete("payments-api", AWS)
    (tombstone,) = aws.changed_since(0)
    assert client.post("/replicate/receive", json=tombstone).json() == {"status": "applied"}
    assert catalog.get("payments-api", AWS) is None


def test_peer_cannot_publish_another_region(gcp_receiver):
    catalog, app = gcp_receiver
    # AWS's cert sending Azure's (or GCP's own) region is rejected, whatever the body says.
    az = CatalogStorage(AZ, clock=Clock()).set("payments-api", AZ, record())
    assert TestClient(as_peer(app, AWS)).post("/replicate/receive", json=az).status_code == 403
    gcp_rec = dict(az, region_id=GCP)
    assert TestClient(as_peer(app, GCP)).post("/replicate/receive", json=gcp_rec).status_code == 403
    assert catalog.get("payments-api", AZ) is None


@pytest.mark.parametrize("cn", [None, "rogue", GCP])
def test_unknown_or_missing_cert_identity_rejected(gcp_receiver, cn):
    _, app = gcp_receiver
    client = TestClient(as_peer(app, cn))
    assert client.post("/replicate/receive", json=owner_record()).status_code == 403
    assert client.get("/replicate/ping").status_code == 403


def test_invalid_record_rejected(gcp_receiver):
    _, app = gcp_receiver
    bad = dict(owner_record(), status="MAYBE")
    assert TestClient(as_peer(app, AWS)).post("/replicate/receive", json=bad).status_code == 400


def test_ping_reports_region_and_stable_boot_id(gcp_receiver):
    _, app = gcp_receiver
    client = TestClient(as_peer(app, AWS))
    a, b = client.get("/replicate/ping").json(), client.get("/replicate/ping").json()
    assert a["region_id"] == GCP and a["boot_id"] == b["boot_id"]


def test_applied_change_is_logged_for_harness(gcp_receiver, caplog):
    _, app = gcp_receiver
    with caplog.at_level("INFO", logger="replication"):
        TestClient(as_peer(app, AWS)).post("/replicate/receive", json=owner_record())
    line = json.loads(caplog.records[-1].getMessage())
    assert line["event"] == "replication_applied"
    assert line["receiver"] == GCP and line["region_id"] == AWS and "lag_ms" in line


def test_cert_common_name():
    cert = {"subject": ((("countryName", "IN"),), (("commonName", AWS),))}
    assert cert_common_name(cert) == AWS
    assert cert_common_name(None) is None


# ---------- worker ----------

class FakePeers:
    """In-memory peers behind an httpx MockTransport; can be cut off or restarted."""

    def __init__(self):
        self.catalogs = {AWS: CatalogStorage(AWS), AZ: CatalogStorage(AZ)}
        self.boot = {AWS: "boot-1", AZ: "boot-1"}
        self.received = {AWS: [], AZ: []}
        self.down = set()

    def restart(self, peer):
        self.catalogs[peer] = CatalogStorage(peer)
        self.boot[peer] = self.boot[peer] + "'"

    def handler(self, request: httpx.Request) -> httpx.Response:
        peer = request.url.host
        if peer in self.down:
            raise httpx.ConnectError("unreachable", request=request)
        if request.url.path == "/replicate/ping":
            return httpx.Response(200, json={"region_id": peer, "boot_id": self.boot[peer]})
        rec = json.loads(request.content)
        self.received[peer].append(rec)
        applied = self.catalogs[peer].apply_replica(rec, rec["region_id"])
        return httpx.Response(200, json={"status": "applied" if applied else "discarded"})

    def sees(self, peer, service):
        rec = self.catalogs[peer].get(service, GCP)
        return rec and rec["status"]


def setup(snapshot_interval=1e9):
    clock, mono = Clock(), Clock(0.0)
    catalog = CatalogStorage(GCP, clock=clock)
    peers = FakePeers()
    client = httpx.AsyncClient(transport=httpx.MockTransport(peers.handler))
    worker = ReplicationWorker(catalog, {AWS: f"http://{AWS}", AZ: f"http://{AZ}"}, client,
                               snapshot_interval=snapshot_interval, clock=mono)
    return clock, mono, catalog, peers, worker


def run(worker, cycles=1):
    async def go():
        for _ in range(cycles):
            await worker.run_once()
    asyncio.run(go())


def test_first_contact_sends_snapshot_then_only_deltas():
    clock, _, catalog, peers, worker = setup()
    catalog.set("payments-api", GCP, record())
    run(worker)
    assert peers.sees(AWS, "payments-api") == "UP" and peers.sees(AZ, "payments-api") == "UP"

    run(worker)  # nothing changed -> nothing sent
    assert len(peers.received[AWS]) == 1

    clock.t += 2
    catalog.set("fraud-check", GCP, record())
    run(worker)
    assert [r["service_name"] for r in peers.received[AWS]] == ["payments-api", "fraud-check"]


def test_down_and_deletion_propagate():
    clock, _, catalog, peers, worker = setup()
    catalog.set("payments-api", GCP, record())
    catalog.set("notify-api", GCP, record(tier="standard"))
    run(worker)
    clock.t += 5
    catalog.set("payments-api", GCP, record(status="DOWN"))  # DOWN keeps old timestamp
    catalog.delete("notify-api", GCP)
    run(worker)
    assert peers.sees(AWS, "payments-api") == "DOWN"
    assert peers.catalogs[AZ].get("notify-api", GCP) is None


def test_partitioned_peer_catches_up_with_latest_state():
    clock, _, catalog, peers, worker = setup()
    catalog.set("payments-api", GCP, record())
    run(worker)
    peers.down.add(AZ)
    for status in ("DOWN", "UP", "DOWN"):
        clock.t += 2
        catalog.set("payments-api", GCP, record(status=status))
        run(worker)
    assert peers.sees(AWS, "payments-api") == "DOWN"
    assert peers.sees(AZ, "payments-api") == "UP"  # stale view while partitioned
    peers.down.clear()
    run(worker)
    assert peers.sees(AZ, "payments-api") == "DOWN"


def test_restarted_peer_gets_full_snapshot():
    clock, _, catalog, peers, worker = setup()
    catalog.set("payments-api", GCP, record())
    catalog.set("fraud-check", GCP, record())
    run(worker)
    peers.restart(AZ)  # empty catalog, new boot_id; no local changes since
    run(worker)
    assert peers.sees(AZ, "payments-api") == "UP" and peers.sees(AZ, "fraud-check") == "UP"


def test_periodic_snapshot_backstop():
    _, mono, catalog, peers, worker = setup(snapshot_interval=30)
    catalog.set("payments-api", GCP, record())
    run(worker)
    peers.catalogs[AWS] = CatalogStorage(AWS)  # lost state without a boot_id change
    mono.t += 10
    run(worker)
    assert peers.sees(AWS, "payments-api") is None
    mono.t += 25
    run(worker)
    assert peers.sees(AWS, "payments-api") == "UP"


def test_wrong_peer_identity_on_ping_is_not_synced():
    _, _, catalog, peers, worker = setup()
    catalog.set("payments-api", GCP, record())
    worker.peers[AZ] = f"http://{AWS}"  # misconfigured URL points at the wrong agent
    run(worker)
    assert peers.received[AZ] == [] and len(peers.received[AWS]) == 1


def test_parse_peers():
    assert parse_peers(f"{AWS}=https://1.2.3.4:9443/, {AZ}=https://5.6.7.8:9443") == {
        AWS: "https://1.2.3.4:9443",
        AZ: "https://5.6.7.8:9443",
    }
    assert parse_peers("") == {}
    with pytest.raises(ValueError):
        parse_peers("no-equals-sign")
