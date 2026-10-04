from fastapi.testclient import TestClient

from services.app import create_app


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