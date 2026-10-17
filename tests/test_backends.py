"""Managed-service backends (Pub/Sub, S3, Azure Table Storage) with fake clients."""
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from services.app import create_app
from services.backends import (AzureTableInventory, BackendError, HealthCache, LogNotifier,
                               MemoryInventory, MemoryRecommendations, PubSubNotifier,
                               S3Recommendations, backend_from_env)
from services.catalog import PRODUCTS, RECOMMENDATIONS


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------- memory backends (default, today's behaviour) ----------

def test_memory_recommendations_from_catalog():
    rec = MemoryRecommendations()
    assert rec.get("p1") == RECOMMENDATIONS["p1"]
    assert "p1" not in rec.get("p1")


def test_memory_inventory_update_and_unknown():
    inv = MemoryInventory()
    assert len(inv.items()) == len(PRODUCTS)
    inv.update("p1", 5)
    assert dict((i["product_id"], i["stock"]) for i in inv.items())["p1"] == 5
    with pytest.raises(KeyError):
        inv.update("nope", 1)


def test_log_notifier_returns_message_id():
    assert LogNotifier().publish({"order_id": "o1"}).startswith("log-")


# ---------- Pub/Sub (REST + GCE metadata token) ----------

class FakeGoogle:
    def __init__(self):
        self.token_calls = 0
        self.published = []
        self.topic_ok = True

    def handler(self, request: httpx.Request):
        if request.url.host == "metadata.google.internal":
            assert request.headers["Metadata-Flavor"] == "Google"
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_calls}", "expires_in": 3600})
        assert request.headers["Authorization"].startswith("Bearer tok")
        if request.method == "GET":
            return httpx.Response(200 if self.topic_ok else 403, json={"name": "t"})
        body = json.loads(request.content)
        self.published.append(json.loads(base64.b64decode(body["messages"][0]["data"])))
        return httpx.Response(200, json={"messageIds": [str(1000 + len(self.published))]})


def pubsub(fake, clock=None):
    return PubSubNotifier("proj", "order-events", client=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                          clock=clock or Clock())


def test_pubsub_publishes_json_and_caches_token():
    fake = FakeGoogle()
    n = pubsub(fake)
    assert n.publish({"order_id": "o1", "event": "order_confirmed"}) == "1001"
    assert n.publish({"order_id": "o2"}) == "1002"
    assert fake.published[0] == {"order_id": "o1", "event": "order_confirmed"}
    assert fake.token_calls == 1


def test_pubsub_refreshes_expired_token():
    fake, clock = FakeGoogle(), Clock()
    n = pubsub(fake, clock)
    n.publish({"a": 1})
    clock.t += 3600
    n.publish({"a": 2})
    assert fake.token_calls == 2


def test_pubsub_check_and_errors():
    fake = FakeGoogle()
    n = pubsub(fake)
    n.check()
    fake.topic_ok = False
    with pytest.raises(BackendError):
        n.check()


# ---------- S3 ----------

class FakeBody:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class FakeS3:
    def __init__(self, data):
        self.data = json.dumps(data).encode()
        self.gets = 0
        self.fail = False

    def get_object(self, Bucket, Key):
        if self.fail:
            raise RuntimeError("s3 down")
        self.gets += 1
        return {"Body": FakeBody(self.data)}

    def head_object(self, Bucket, Key):
        if self.fail:
            raise RuntimeError("s3 down")
        return {}


def test_s3_reads_and_caches():
    fake, clock = FakeS3({"p1": ["p2", "p3", "p4"]}), Clock()
    rec = S3Recommendations("bucket", client=fake, clock=clock)
    assert rec.get("p1") == ["p2", "p3", "p4"]
    assert rec.get("p9") == []
    assert fake.gets == 1
    clock.t += 31
    rec.get("p1")
    assert fake.gets == 2


def test_s3_errors_become_backend_errors():
    fake = FakeS3({})
    rec = S3Recommendations("bucket", client=fake)
    fake.fail = True
    with pytest.raises(BackendError):
        rec.get("p1")
    with pytest.raises(BackendError):
        rec.check()


# ---------- Azure Table Storage ----------

class NotFound(Exception):
    pass


class FakeTable:
    def __init__(self):
        self.rows = {p["product_id"]: {"PartitionKey": "inventory", "RowKey": p["product_id"], "stock": p["stock"]}
                     for p in PRODUCTS}
        self.fail = False

    def list_entities(self, **kw):
        if self.fail:
            raise RuntimeError("azure down")
        return iter(list(self.rows.values()))

    def get_entity(self, partition_key, row_key):
        if row_key not in self.rows:
            raise NotFound(row_key)
        return self.rows[row_key]

    def update_entity(self, entity, **kw):
        self.rows[entity["RowKey"]]["stock"] = entity["stock"]


def test_azure_table_inventory():
    table = FakeTable()
    inv = AzureTableInventory(table_client=table, not_found=NotFound)
    assert {i["product_id"]: i["stock"] for i in inv.items()}["p1"] == 25
    inv.update("p1", 7)
    assert table.rows["p1"]["stock"] == 7
    with pytest.raises(KeyError):
        inv.update("nope", 1)
    inv.check()
    table.fail = True
    with pytest.raises(BackendError):
        inv.items()
    with pytest.raises(BackendError):
        inv.check()


# ---------- health cache ----------

def test_health_cache_rechecks_after_interval():
    calls, clock = [], Clock()

    def check():
        calls.append(1)
        if len(calls) == 2:
            raise BackendError("down")

    h = HealthCache(check, interval=10, clock=clock)
    assert h.healthy() is True
    assert h.healthy() is True and len(calls) == 1  # cached
    clock.t += 11
    assert h.healthy() is False
    clock.t += 11
    assert h.healthy() is True


# ---------- env factory ----------

def test_backend_from_env_defaults_to_memory():
    assert backend_from_env("payments-api", {}) is None
    assert isinstance(backend_from_env("recommend-api", {}), MemoryRecommendations)
    assert isinstance(backend_from_env("admin-api", {}), MemoryInventory)
    assert isinstance(backend_from_env("notify-api", {}), LogNotifier)


def test_backend_from_env_validates():
    with pytest.raises(ValueError):
        backend_from_env("notify-api", {"NOTIFY_BACKEND": "kafka"})
    with pytest.raises(ValueError):
        backend_from_env("notify-api", {"NOTIFY_BACKEND": "pubsub"})  # missing project/topic
    with pytest.raises(ValueError):
        backend_from_env("recommend-api", {"RECOMMEND_BACKEND": "s3"})  # missing bucket


# ---------- app wiring ----------

def test_app_uses_backend_and_reports_source():
    fake = FakeGoogle()
    app = TestClient(create_app("notify-api", "gcp-asia-south1", backend=pubsub(fake)))
    body = app.post("/notify", json={"order_id": "o1", "email": "a@b.c", "event": "order_confirmed"}).json()
    assert body == {"status": "queued", "message_id": "1001", "backend": "pubsub"}
    assert fake.published[0]["region_id"] == "gcp-asia-south1"


def test_app_recommendations_from_s3():
    app = TestClient(create_app("recommend-api", "aws-ap-south-1",
                                backend=S3Recommendations("b", client=FakeS3({"p1": ["p5", "p2", "p3"]}))))
    assert app.get("/recommendations", params={"product_id": "p1"}).json() == {
        "recommended_ids": ["p5", "p2", "p3"], "source": "s3"}


def test_app_admin_on_azure_table():
    table = FakeTable()
    app = TestClient(create_app("admin-api", "azure-indiasouthcentral",
                                backend=AzureTableInventory(table_client=table, not_found=NotFound)))
    assert app.post("/inventory/update", json={"product_id": "p2", "stock": 3}).json() == {"status": "updated"}
    assert table.rows["p2"]["stock"] == 3
    assert app.get("/inventory").json()["source"] == "azuretable"
    assert app.post("/inventory/update", json={"product_id": "nope", "stock": 1}).status_code == 404


def test_failing_managed_service_makes_api_unhealthy():
    fake = FakeS3({"p1": ["p2"]})
    app = TestClient(create_app("recommend-api", "aws-ap-south-1", backend=S3Recommendations("b", client=fake)))
    assert app.get("/health").status_code == 200
    fake.fail = True
    app.app.state.health.force_recheck()
    assert app.get("/health").status_code == 503
    assert app.get("/recommendations", params={"product_id": "p1"}).status_code == 503


# ---------- seed ----------

def test_seed_s3_uploads_catalog_recommendations():
    from services.seed import seed_s3

    class Put:
        def put_object(self, **kw):
            self.kw = kw

    client = Put()
    seed_s3("bucket", "recommendations.json", "ap-south-1", client=client)
    assert client.kw["Bucket"] == "bucket" and json.loads(client.kw["Body"]) == RECOMMENDATIONS


def test_seed_azure_table_upserts_one_row_per_product():
    from services.seed import seed_azure_table

    class Table:
        rows = []

        def upsert_entity(self, row):
            self.rows.append(row)

    class Service:
        def create_table_if_not_exists(self, name):
            self.name = name
            return Table()

    svc = Service()
    assert seed_azure_table("https://x", "inventory", service_client=svc) == len(PRODUCTS)
    assert svc.name == "inventory"
