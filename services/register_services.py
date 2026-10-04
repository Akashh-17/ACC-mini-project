"""Register the four services owned by one region with its local agent."""
from __future__ import annotations

import os

import httpx


SERVICES = (
    ("payments-api", 8081, "critical"),
    ("fraud-check", 8082, "critical"),
    ("inventory-check", 8083, "critical"),
)
STANDARD_BY_REGION = {
    "aws-ap-south-1": ("recommend-api", 8084),
    "azure-indiasouthcentral": ("admin-api", 8084),
    "gcp-asia-south1": ("notify-api", 8084),
}


def main() -> None:
    region = os.environ.get("REGION_ID", "").strip()
    secret = os.environ.get("REGISTRY_SECRET", "")
    host = os.environ.get("SERVICE_HOST", "127.0.0.1").strip()
    agent_url = os.environ.get("REGISTRY_URL", "http://127.0.0.1:9000").rstrip("/")
    if not region or not secret:
        raise SystemExit("REGION_ID and REGISTRY_SECRET must be set")
    if region not in STANDARD_BY_REGION:
        raise SystemExit(f"unsupported REGION_ID: {region}")

    services = [*SERVICES, (*STANDARD_BY_REGION[region], "standard")]
    headers = {"Authorization": f"Bearer {secret}"}
    with httpx.Client(base_url=agent_url, headers=headers, timeout=3.0) as client:
        for service_name, port, tier in services:
            response = client.post("/register", json={
                "service_name": service_name,
                "region_id": region,
                "endpoint": f"{host}:{port}",
                "health_check_path": "/health",
                "tier": tier,
            })
            response.raise_for_status()
            print(f"registered {service_name}: {response.json()}")


if __name__ == "__main__":
    main()