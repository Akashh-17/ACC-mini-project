from fastapi.testclient import TestClient

from services.app import create_app
from services.catalog import PRODUCTS

GCP = "gcp-asia-south1"


def client_for(service_name, region_id=GCP):
    return TestClient(create_app(service_name, region_id))


def test_health_failure_and_recovery():
    client = TestClient(create_app("payments-api", "aws-ap-south-1"))

    healthy = client.get("/health")
    assert healthy.status_code == 200
    assert healthy.json() == {
        "service_name": "payments-api",
        "region_id": "aws-ap-south-1",
        "status": "UP",
    }

    assert client.post("/simulate/fail").json()["status"] == "DOWN"
    assert client.get("/health").status_code == 503

    assert client.post("/simulate/recover").json()["status"] == "UP"
    assert client.get("/health").status_code == 200


def test_each_service_name_is_reported():
    for service_name in ("payments-api", "fraud-check", "inventory-check", "notify-api"):
        response = TestClient(create_app(service_name, "gcp-asia-south1")).get("/health")
        assert response.json()["service_name"] == service_name


# ---------- payments-api: POST /authorize ----------

def authorize(client, amount):
    return client.post("/authorize", json={"order_id": "o1", "amount": amount,
                                           "currency": "INR", "card_token": "tok_demo"})


def test_payment_approved_with_transaction_and_region():
    body = authorize(client_for("payments-api", "aws-ap-south-1"), 1999).json()
    assert body["status"] == "approved"
    assert body["region"] == "aws-ap-south-1"
    assert body["transaction_id"]


def test_payment_declined_above_threshold():
    body = authorize(client_for("payments-api"), 50001).json()
    assert body == {"status": "declined", "reason": "amount exceeds 50000 limit"}
    assert authorize(client_for("payments-api"), 50000).json()["status"] == "approved"


def test_transaction_ids_are_unique():
    client = client_for("payments-api")
    assert authorize(client, 10).json()["transaction_id"] != authorize(client, 10).json()["transaction_id"]


# ---------- fraud-check: POST /score ----------

def score(client, amount, order_id="o1"):
    return client.post("/score", json={"order_id": order_id, "user_id": "u1", "amount": amount}).json()


def test_large_amount_is_flagged():
    body = score(client_for("fraud-check"), 25000)
    assert body["flagged"] is True and body["risk_score"] >= 80


def test_small_amount_low_and_repeatable():
    client = client_for("fraud-check")
    first, second = score(client, 500, "order-42"), score(client, 500, "order-42")
    assert first == second
    assert first["flagged"] is False and 0 <= first["risk_score"] < 50


# ---------- inventory-check: POST /reserve ----------

def reserve(client, items):
    return client.post("/reserve", json={"order_id": "o1", "items": items}).json()


def test_reserve_decrements_stock():
    client = client_for("inventory-check")
    pid = PRODUCTS[0]["product_id"]
    stock = PRODUCTS[0]["stock"]
    assert reserve(client, [{"product_id": pid, "qty": stock}]) == {
        "status": "reserved", "items_status": [{"product_id": pid, "ok": True}]}
    body = reserve(client, [{"product_id": pid, "qty": 1}])
    assert body["status"] == "insufficient"
    assert body["items_status"] == [{"product_id": pid, "ok": False}]


def test_reserve_is_all_or_nothing():
    client = client_for("inventory-check")
    a, b = PRODUCTS[0]["product_id"], PRODUCTS[1]["product_id"]
    body = reserve(client, [{"product_id": a, "qty": 1}, {"product_id": b, "qty": 10_000}])
    assert body["status"] == "insufficient"
    assert body["items_status"] == [{"product_id": a, "ok": True}, {"product_id": b, "ok": False}]
    # a was not decremented by the failed order
    stock_a = PRODUCTS[0]["stock"]
    assert reserve(client, [{"product_id": a, "qty": stock_a}])["status"] == "reserved"


def test_reserve_unknown_product():
    body = reserve(client_for("inventory-check"), [{"product_id": "nope", "qty": 1}])
    assert body["status"] == "insufficient"


def test_each_app_has_its_own_stock():
    pid, stock = PRODUCTS[0]["product_id"], PRODUCTS[0]["stock"]
    reserve(client_for("inventory-check"), [{"product_id": pid, "qty": stock}])
    assert reserve(client_for("inventory-check"), [{"product_id": pid, "qty": 1}])["status"] == "reserved"


# ---------- recommend-api: GET /recommendations ----------

def test_recommendations_exclude_the_product():
    pid = PRODUCTS[0]["product_id"]
    body = client_for("recommend-api", "aws-ap-south-1").get("/recommendations", params={"product_id": pid}).json()
    ids = body["recommended_ids"]
    assert len(ids) == 3 and pid not in ids
    assert set(ids) <= {p["product_id"] for p in PRODUCTS}


# ---------- admin-api: GET /inventory, POST /inventory/update ----------

def test_admin_inventory_read_and_update():
    client = client_for("admin-api", "azure-indiasouthcentral")
    items = client.get("/inventory").json()["items"]
    assert len(items) == len(PRODUCTS)
    pid = items[0]["product_id"]
    assert client.post("/inventory/update", json={"product_id": pid, "stock": 99}).json() == {"status": "updated"}
    assert {i["product_id"]: i["stock"] for i in client.get("/inventory").json()["items"]}[pid] == 99


def test_admin_update_unknown_product_404():
    client = client_for("admin-api", "azure-indiasouthcentral")
    assert client.post("/inventory/update", json={"product_id": "nope", "stock": 1}).status_code == 404


# ---------- notify-api: POST /notify ----------

def test_notify_queues():
    body = client_for("notify-api").post("/notify", json={"order_id": "o1", "email": "a@b.c",
                                                          "event": "order_confirmed"}).json()
    assert body["status"] == "queued"          # tasks.md response
    assert body["backend"] == "log" and body["message_id"]  # default in-memory backend


# ---------- routing and simulated failure ----------

def test_service_only_exposes_its_own_endpoint():
    assert authorize(client_for("fraud-check"), 10).status_code == 404


def test_simulated_failure_also_fails_business_endpoint():
    client = client_for("payments-api")
    client.post("/simulate/fail")
    assert authorize(client, 10).status_code == 503
    client.post("/simulate/recover")
    assert authorize(client, 10).status_code == 200


def test_invalid_request_rejected():
    assert client_for("payments-api").post("/authorize", json={"amount": "lots"}).status_code == 422
