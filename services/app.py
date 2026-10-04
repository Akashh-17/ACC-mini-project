"""Configurable business-service stub.

Run with SERVICE_NAME, SERVICE_REGION and SERVICE_PORT set in the environment.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException


def required_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be set")
    return value


def create_app(service_name: str, region_id: str) -> FastAPI:
    state = {"healthy": True}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service_name = service_name
        app.state.region_id = region_id
        yield

    app = FastAPI(title=service_name, lifespan=lifespan)

    @app.get("/health")
    async def health():
        if not state["healthy"]:
            raise HTTPException(status_code=503, detail="service is simulated as failed")
        return {"service_name": service_name, "region_id": region_id, "status": "UP"}

    @app.post("/simulate/fail")
    async def simulate_failure():
        state["healthy"] = False
        return {"service_name": service_name, "status": "DOWN"}

    @app.post("/simulate/recover")
    async def simulate_recovery():
        state["healthy"] = True
        return {"service_name": service_name, "status": "UP"}

    return app


def main() -> None:
    service_name = required_setting("SERVICE_NAME")
    region_id = required_setting("SERVICE_REGION")
    try:
        port = int(os.environ.get("SERVICE_PORT", "8081"))
    except ValueError as exc:
        raise ValueError("SERVICE_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SERVICE_PORT must be between 1 and 65535")

    host = os.environ.get("SERVICE_BIND_HOST", "0.0.0.0").strip()
    uvicorn.run(create_app(service_name, region_id), host=host, port=port)


if __name__ == "__main__":
    main()