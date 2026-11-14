"""Demo web app (webapp/) against the fake three-region world."""
from services.catalog import PRODUCTS
from storefront.backend import checkout
from tests.test_storefront import World
from webapp.monitor import Monitor

PRODUCTS_BY_ID = {p["product_id"]: p for p in PRODUCTS}


def test_first_snapshot_has_no_events_and_metrics():
    m = Monitor(World().registry()); m.refresh()
    st = m.state()
    assert m.events_after(0) == []
    assert st["metrics"]["endpoints"] == 12 and st["metrics"]["healthy"] == 12


def test_status_change_becomes_one_event():
    w = World(); m = Monitor(w.registry()); m.refresh()
    w.status[("payments-api", "gcp-asia-south1")] = "DOWN"; m.refresh()
    [e] = m.events_after(0)
    assert e["kind"] == "status" and "payments-api" in e["title"] and "DOWN" in e["title"]
    assert "GCP, AWS, Azure" in e["detail"]


def test_replicated_payment_event():
    w = World(); reg = w.registry(); m = Monitor(reg); m.refresh()
    checkout(reg, {"p1": 1}, PRODUCTS_BY_ID)
    # fake world has no Kafka: make AWS's ledger show the payment as replicated
    pay = w.apps[("payments-api", "gcp-asia-south1")].get("/ledger").json()["recent"][0]
    w.ledger_extra = {"aws-ap-south-1": [{**pay, "replicated": True, "lag_ms": 412}]}
    m.refresh()
    [e] = [e for e in m.events_after(0) if e["kind"] == "replicated"]
    assert "AWS 412 ms" in e["detail"]
    assert e["ref"] == pay["txn_id"]


def test_replication_metric_and_unsent():
    w = World(); reg = w.registry(); m = Monitor(reg); m.refresh()
    checkout(reg, {"p1": 1}, PRODUCTS_BY_ID)
    pay = w.apps[("payments-api", "gcp-asia-south1")].get("/ledger").json()["recent"][0]
    w.ledger_extra = {"aws-ap-south-1": [{**pay, "replicated": True, "lag_ms": 400}],
                      "azure-indiasouthcentral": [{**pay, "replicated": True, "lag_ms": 500}]}
    m.refresh()
    metrics = m.state()["metrics"]
    assert metrics["replication_ms"] == 450 and metrics["unsent"] == 1  # fake world never sends


def test_unreachable_cloud_and_failed_refresh_keep_working():
    w = World(); reg = w.registry(); m = Monitor(reg); m.refresh()
    w.unreachable.add("azure-indiasouthcentral"); m.refresh()
    assert m.state()["ledgers"]["azure-indiasouthcentral"] is None
    reg.views = lambda: 1 / 0  # snapshot blows up
    m.refresh()
    assert m.state()["views"]  # previous snapshot kept
    assert m.events_after(0)[-1]["kind"] == "error"


# ---------------------------------------------------------------- server

from fastapi.testclient import TestClient  # noqa: E402

from webapp.server import create_app  # noqa: E402


def app_for(world):
    reg = world.registry()
    m = Monitor(reg); m.refresh()
    return TestClient(create_app(reg, monitor=m)), m


def test_order_trace_and_events():
    w = World(); w.apps[("payments-api", "gcp-asia-south1")].post("/simulate/fail")
    client, m = app_for(w)
    body = client.post("/api/order", json={"cart": {"p1": 1}, "email": "a@b.c"}).json()
    assert body["ok"] and body["total"] == 2499 and body["order_id"].startswith("ord_")
    pay = body["steps"][2]
    assert pay["region"] in ("AWS", "Azure") and pay["attempts"] == ["GCP: HTTP 503"]
    assert isinstance(pay["ms"], int)
    kinds = [e["kind"] for e in client.get("/api/events?after=0").json()]
    assert "order" in kinds and "failover" in kinds
    assert client.get("/api/state").json()["metrics"]["orders"] == 1


def test_bad_orders_rejected():
    client, _ = app_for(World())
    assert client.post("/api/order", json={"cart": {}, "email": "a@b.c"}).status_code == 422
    assert client.post("/api/order", json={"cart": {"nope": 1}, "email": "a@b.c"}).status_code == 422
    assert client.post("/api/order", json={"cart": {"p1": 0}, "email": "a@b.c"}).status_code == 422


def test_simulate_and_transfer():
    w = World(); client, _ = app_for(w)
    assert client.post("/api/simulate", json={"cloud": "aws", "service": "recommend-api",
                                              "action": "fail"}).json() == {"status_code": 200}
    assert w.apps[("recommend-api", "aws-ap-south-1")].get("/health").status_code == 503
    r = client.post("/api/transfer", json={"product_id": "p5", "qty": 1, "from_cloud": "aws", "to_cloud": "gcp"})
    assert r.json() == {"status": "transferred"}
    assert client.post("/api/simulate", json={"cloud": "mars", "service": "x", "action": "fail"}).status_code == 422


def test_products_recommendations_and_pages():
    client, _ = app_for(World())
    prods = client.get("/api/products").json()
    assert prods[0]["product_id"] == "p1" and prods[0]["stock"] == 25
    rec = client.get("/api/recommendations", params={"product_id": "p1"}).json()
    assert rec["region"] == "AWS" and "p1" not in rec["recommended_ids"]
    for page in ("/store", "/control"):
        assert client.get(page).status_code == 200
    assert client.get("/", follow_redirects=False).status_code in (302, 307)


def test_recommendations_unavailable_is_503():
    w = World(); w.status[("recommend-api", "aws-ap-south-1")] = "DOWN"
    client, _ = app_for(w)
    assert client.get("/api/recommendations", params={"product_id": "p1"}).status_code == 503
