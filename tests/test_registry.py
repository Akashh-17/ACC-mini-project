"""AWS API and actual health-check behaviour against Azure's real storage."""
import asyncio
from contextlib import suppress
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from agent.main import create_agent
from agent.registry import create_registry_app, HealthScheduler
from agent.storage import CatalogStorage


AWS = "aws-ap-south-1"
AZURE = "azure-indiasouthcentral"
TOKEN = "test-secret"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def registration(**changes):
    return {"service_name": "payments-api", "region_id": AWS,
            "endpoint": "10.0.1.10:8081", "health_check_path": "/health",
            "tier": "critical", **changes}


def stored(**changes):
    return {"endpoint": "10.0.1.10:8081", "health_check_path": "/health",
            "tier": "critical", "status": "UP", "timestamp": 1000.0, **changes}


@pytest.fixture
def api():
    catalog = CatalogStorage(AWS)
    # No lifespan here: test routes separately from background probes.
    return catalog, TestClient(create_registry_app(catalog, TOKEN))


@pytest.mark.parametrize("path", ["/register", "/deregister"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic test-secret", "Bearer"])
def test_writes_require_bearer_auth_even_with_invalid_body(api, path, authorization):
    catalog, client = api
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.post(path, content="not-json", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert catalog.list("payments-api") == []


def test_register_waits_for_health_check_and_keeps_metadata(api):
    catalog, client = api
    response = client.post("/register", json=registration(), headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"status": "registered"}
    record = catalog.get("payments-api", AWS)
    assert record["status"] == "STALE" and record["timestamp"] == 0
    assert record["health_check_path"] == "/health" and record["tier"] == "critical"


@pytest.mark.parametrize("endpoint", [
    "8.8.8.8:8081", "169.254.169.254:80", "127.0.0.1:8081", "localhost:8081",
    "http://10.0.1.10:8081", "10.0.1.10", "10.0.1.10:0", "10.0.1.10:65536",
    "user@10.0.1.10:8081", "10.0.1.10:8081/health", "10.0.1.10:8081?x=1",
    "10.0.1.10:8081#fragment", "0.0.0.0:8081", "[::1]:8081",
])
def test_invalid_or_nonprivate_endpoint_is_rejected(api, endpoint):
    catalog, client = api
    assert client.post("/register", json=registration(endpoint=endpoint),
                       headers=HEADERS).status_code == 400
    assert catalog.get("payments-api", AWS) is None


@pytest.mark.parametrize("endpoint", [
    "10.0.1.10:8081", "172.16.1.2:8081", "192.168.1.2:8081", "[fd00::1]:8081",
])
def test_private_addresses_are_accepted(api, endpoint):
    _, client = api
    assert client.post("/register", json=registration(endpoint=endpoint),
                       headers=HEADERS).status_code == 200


@pytest.mark.parametrize("change", [
    {"region_id": AZURE}, {"tier": "unknown"}, {"service_name": " "},
    {"health_check_path": "health"}, {"health_check_path": "//other-host/health"},
    {"health_check_path": "http://other-host/health"}, {"health_check_path": "/health#x"},
    {"endpoint": 8081},
])
def test_invalid_registration_does_not_write(api, change):
    catalog, client = api
    assert client.post("/register", json=registration(**change), headers=HEADERS).status_code == 400
    assert catalog.snapshot()["records"] == []


def test_missing_field_and_malformed_json_return_400(api):
    _, client = api
    body = registration()
    del body["health_check_path"]
    assert client.post("/register", json=body, headers=HEADERS).status_code == 400
    assert client.post("/register", content="{", headers={
        **HEADERS, "Content-Type": "application/json",
    }).status_code == 400


def test_lookup_matches_task_response_and_keeps_local_first(api):
    catalog, client = api
    remote = CatalogStorage(AZURE)
    catalog.apply_replica(remote.set("payments-api", AZURE, stored()), AZURE)
    catalog.set("payments-api", AWS, stored(status="DOWN"))
    response = client.get("/lookup", params={"service_name": "payments-api"})
    assert response.status_code == 200
    assert response.json() == {"service_name": "payments-api", "endpoints": [
        {"region_id": AWS, "endpoint": "10.0.1.10:8081", "status": "DOWN"},
        {"region_id": AZURE, "endpoint": "10.0.1.10:8081", "status": "UP"},
    ]}
    assert client.get("/lookup?service_name=missing").json()["endpoints"] == []
    assert client.get("/lookup").status_code == 400
    assert client.get("/lookup?service_name=%20").status_code == 400


def test_deregister_removes_immediately_and_retains_replication_delete(api):
    catalog, client = api
    client.post("/register", json=registration(), headers=HEADERS)
    body = {key: registration()[key] for key in ("service_name", "region_id", "endpoint")}
    response = client.post("/deregister", json=body, headers=HEADERS)
    assert response.status_code == 200 and response.json() == {"status": "deregistered"}
    assert client.get("/lookup?service_name=payments-api").json()["endpoints"] == []
    assert catalog.changed_since(0)[0]["deleted"] is True
    assert client.post("/deregister", json=body, headers=HEADERS).status_code == 200


def test_old_deregister_cannot_remove_replacement(api):
    catalog, client = api
    client.post("/register", json=registration(), headers=HEADERS)
    client.post("/register", json=registration(endpoint="10.0.1.11:8081"), headers=HEADERS)
    client.post("/deregister", json={key: registration()[key] for key in
                                   ("service_name", "region_id", "endpoint")}, headers=HEADERS)
    assert catalog.get("payments-api", AWS)["endpoint"] == "10.0.1.11:8081"


def test_health_success_failure_and_recovery_use_storage_versions():
    async def exercise():
        catalog = CatalogStorage(AWS)
        catalog.set("payments-api", AWS, stored(status="STALE", timestamp=0))
        replies = iter([200, 503, 200])
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(next(replies)))) as client:
            monitor = HealthScheduler(catalog, client, clock=lambda: 1010)
            await monitor.run_once()
            up = catalog.get("payments-api", AWS)
            assert up["status"] == "UP" and up["timestamp"] == 1010
            await monitor.run_once()
            down = catalog.get("payments-api", AWS)
            assert down["status"] == "DOWN" and down["timestamp"] == 1010
            assert down["version"] > up["version"]
            await monitor.run_once()
            assert catalog.get("payments-api", AWS)["status"] == "UP"
    asyncio.run(exercise())


def test_health_only_probes_owned_entries_and_never_follows_redirects():
    async def exercise():
        catalog = CatalogStorage(AWS)
        remote = CatalogStorage(AZURE)
        catalog.apply_replica(remote.set("fraud-check", AZURE, stored()), AZURE)
        catalog.set("payments-api", AWS, stored())
        seen = []

        def reply(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            await HealthScheduler(catalog, client).run_once()
        assert seen == ["http://10.0.1.10:8081/health"]
        assert catalog.get("payments-api", AWS)["status"] == "DOWN"
        assert catalog.get("fraud-check", AZURE)["status"] == "UP"
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["connection", "timeout"])
def test_failed_or_hanging_health_request_becomes_down(failure):
    async def exercise():
        catalog = CatalogStorage(AWS)
        catalog.set("payments-api", AWS, stored())

        async def reply(request):
            if failure == "connection":
                raise httpx.ConnectError("unreachable", request=request)
            await asyncio.sleep(5)
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            await HealthScheduler(catalog, client, timeout=0.02).run_once()
        result = catalog.get("payments-api", AWS)
        assert result["status"] == "DOWN" and result["timestamp"] == 1000
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["delete", "replace"])
def test_completed_probe_cannot_restore_deleted_or_replaced_service(change):
    async def exercise():
        catalog = CatalogStorage(AWS)
        catalog.set("payments-api", AWS, stored())

        async def reply(request):
            if change == "delete":
                catalog.delete("payments-api", AWS)
            else:
                catalog.set("payments-api", AWS, stored(endpoint="10.0.1.11:8081", status="STALE"))
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            await HealthScheduler(catalog, client).run_once()
        result = catalog.get("payments-api", AWS)
        if change == "delete":
            assert result is None
        else:
            assert result["endpoint"] == "10.0.1.11:8081" and result["status"] == "STALE"
    asyncio.run(exercise())


def test_lifespan_runs_real_probe_and_stops_background_tasks():
    catalog = CatalogStorage(AWS)
    health_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = create_registry_app(catalog, TOKEN, health_client=health_client, health_interval=0.01)
    with TestClient(app) as client:
        client.post("/register", json=registration(), headers=HEADERS)
        deadline = time.monotonic() + 2
        while catalog.get("payments-api", AWS)["status"] != "UP" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert catalog.get("payments-api", AWS)["status"] == "UP"
    version = catalog.get("payments-api", AWS)["version"]
    time.sleep(0.03)
    assert catalog.get("payments-api", AWS)["version"] == version
    asyncio.run(health_client.aclose())


def test_ttl_sweep_runs_independently_of_probes():
    async def exercise():
        catalog = CatalogStorage(AWS, clock=lambda: 1016)
        remote = CatalogStorage(AZURE, clock=lambda: 1000)
        catalog.apply_replica(remote.set("payments-api", AZURE, stored()), AZURE)
        async with httpx.AsyncClient() as client:
            monitor = HealthScheduler(catalog, client)
            task = asyncio.create_task(monitor.sweep())
            await asyncio.sleep(0)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        assert catalog.get("payments-api", AZURE)["status"] == "STALE"
    asyncio.run(exercise())


@pytest.fixture
def local_env(monkeypatch):
    for name in ("REGISTRY_PORT", "REGISTRY_ALLOW_LOOPBACK", "REPLICATION_PEERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REGION_ID", AWS)
    monkeypatch.setenv("REGISTRY_SECRET", TOKEN)


def test_agent_uses_one_catalog_and_local_default_port(local_env):
    catalog, app, port, replication = create_agent()
    assert app.state.catalog is catalog and catalog.region_id == AWS
    assert port == 9000 and replication is None


@pytest.mark.parametrize("name,value", [("REGISTRY_SECRET", ""), ("REGION_ID", ""),
                                      ("REGISTRY_PORT", "0"), ("REGISTRY_ALLOW_LOOPBACK", "yes")])
def test_invalid_agent_configuration_fails_before_start(local_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        create_agent()


def test_loopback_registration_is_opt_in_for_local_integration(local_env, monkeypatch):
    monkeypatch.setenv("REGISTRY_ALLOW_LOOPBACK", "true")
    _, app, _, _ = create_agent()
    client = TestClient(app)
    assert client.post("/register", json=registration(endpoint="127.0.0.1:8081"),
                       headers=HEADERS).status_code == 200
