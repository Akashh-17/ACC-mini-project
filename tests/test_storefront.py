"""Storefront routing/failover/checkout against a fake 3-region world that runs
the real service stubs (services.app) behind a mock HTTP transport."""
import httpx
import pytest
from fastapi.testclient import TestClient

from services.app import create_app
from services.catalog import PRODUCTS
from storefront.backend import Registry, Unavailable, checkout, recommendations

CLOUDS = {
    "gcp": {"region_id": "gcp-asia-south1", "base_url": "https://gcp.test"},
    "aws": {"region_id": "aws-ap-south-1", "base_url": "https://aws.test"},
    "azure": {"region_id": "azure-indiasouthcentral", "base_url": "https://azure.test"},
}
HOST_TO_REGION = {"gcp.test": "gcp-asia-south1", "aws.test": "aws-ap-south-1",
                  "azure.test": "azure-indiasouthcentral"}
PLACEMENT = {"payments-api": "all", "fraud-check": "all", "inventory-check": "all",
             "recommend-api": ["aws-ap-south-1"], "admin-api": ["azure-indiasouthcentral"],
             "notify-api": ["gcp-asia-south1"]}
PRODUCTS_BY_ID = {p["product_id"]: p for p in PRODUCTS}


class World:
    def __init__(self):
        self.apps = {}
        self.status = {}  # (service, region) -> registry status
        for svc, where in PLACEMENT.items():
            regions = list(HOST_TO_REGION.values()) if where == "all" else where
            for r in regions:
                self.apps[(svc, r)] = TestClient(create_app(svc, r))
                self.status[(svc, r)] = "UP"
        self.unreachable = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        observer = HOST_TO_REGION[request.url.host]
        if observer in self.unreachable:
            raise httpx.ConnectError("down", request=request)
        if request.url.path == "/lookup":
            svc = request.url.params["service_name"]
            eps = [{"region_id": r, "endpoint": "10.0.0.1:8081", "status": st}
                   for (s, r), st in self.status.items() if s == svc]
            eps.sort(key=lambda e: e["region_id"] != observer)  # own region first
            return httpx.Response(200, json={"service_name": svc, "endpoints": eps})
        svc, _, rest = request.url.path.lstrip("/").partition("/")
        app = self.apps.get((svc, observer))
        if app is None:
            return httpx.Response(404)
        resp = app.request(request.method, "/" + rest, params=dict(request.url.params),
                           content=request.content, headers={"content-type": "application/json"})
        return httpx.Response(resp.status_code, content=resp.content)

    def registry(self):
        return Registry(CLOUDS, home="gcp", client=httpx.Client(transport=httpx.MockTransport(self.handler)))


@pytest.fixture
def world():
    return World()


def test_normal_purchase_served_by_home_region(world):
    ok, steps = checkout(world.registry(), {"p1": 1}, PRODUCTS_BY_ID)
    assert ok
    assert [s.name for s in steps] == ["Fraud check", "Reserve stock", "Payment", "Confirmation email"]
    assert all(s.ok and s.region == "GCP" and not s.attempts for s in steps)


def test_failed_service_not_yet_detected_is_retried_elsewhere(world):
    # GCP payments fails but the registry still says UP: the call fails, retry wins.
    world.apps[("payments-api", "gcp-asia-south1")].post("/simulate/fail")
    ok, steps = checkout(world.registry(), {"p1": 1}, PRODUCTS_BY_ID)
    payment = steps[2]
    assert ok and payment.ok and payment.region in ("AWS", "Azure")
    assert payment.attempts == ["GCP: HTTP 503"]


def test_down_region_is_skipped_after_detection(world):
    world.status[("payments-api", "gcp-asia-south1")] = "DOWN"
    ok, steps = checkout(world.registry(), {"p1": 1}, PRODUCTS_BY_ID)
    assert ok and steps[2].region != "GCP" and steps[2].attempts == []


def test_stale_entries_are_not_used(world):
    world.status[("payments-api", "gcp-asia-south1")] = "STALE"
    world.status[("payments-api", "aws-ap-south-1")] = "STALE"
    ok, steps = checkout(world.registry(), {"p1": 1}, PRODUCTS_BY_ID)
    assert ok and steps[2].region == "Azure"


def test_payment_unavailable_everywhere_fails_order(world):
    for r in HOST_TO_REGION.values():
        world.status[("payments-api", r)] = "DOWN"
    ok, steps = checkout(world.registry(), {"p1": 1}, PRODUCTS_BY_ID)
    assert not ok and steps[-1].name == "Payment" and not steps[-1].ok


def test_email_down_does_not_fail_order(world):
    world.status[("notify-api", "gcp-asia-south1")] = "DOWN"
    ok, steps = checkout(world.registry(), {"p1": 1}, PRODUCTS_BY_ID)
    assert ok and steps[-1].name == "Confirmation email" and steps[-1].region is None


def test_large_order_held_by_fraud_check(world):
    ok, steps = checkout(world.registry(), {"p5": 2}, PRODUCTS_BY_ID)  # 2 x 18999 > 20000
    assert not ok and steps[-1].name == "Fraud check" and "review" in steps[-1].detail


def test_out_of_stock_stops_before_payment(world):
    # Drain GCP's backpack stock (30) directly, then try to buy one more.
    world.apps[("inventory-check", "gcp-asia-south1")].post(
        "/reserve", json={"order_id": "x", "items": [{"product_id": "p4", "qty": 30}]})
    ok, steps = checkout(world.registry(), {"p4": 1}, PRODUCTS_BY_ID)
    assert not ok and steps[-1].name == "Reserve stock" and "Laptop Backpack" in steps[-1].detail


def test_recommendations_single_cloud(world):
    res = recommendations(world.registry(), "p1")
    assert res.region == "AWS" and "p1" not in res.data["recommended_ids"]
    world.status[("recommend-api", "aws-ap-south-1")] = "DOWN"
    with pytest.raises(Unavailable):
        recommendations(world.registry(), "p1")


def test_lookup_falls_back_when_home_registry_unreachable(world):
    world.unreachable.add("gcp-asia-south1")
    world.status[("payments-api", "gcp-asia-south1")] = "STALE"  # peers stop trusting GCP
    ok, steps = checkout(world.registry(), {"p1": 1}, PRODUCTS_BY_ID)
    assert steps[2].ok and steps[2].region in ("AWS", "Azure")


def test_views_marks_unreachable_observer(world):
    world.unreachable.add("azure-indiasouthcentral")
    views = world.registry().views()
    assert views["azure-indiasouthcentral"]["payments-api"] is None
    assert len(views["gcp-asia-south1"]["payments-api"]) == 3


def test_simulate_goes_through_region_proxy(world):
    reg = world.registry()
    assert reg.simulate("aws", "recommend-api", "fail") == 200
    assert world.apps[("recommend-api", "aws-ap-south-1")].get("/health").status_code == 503
