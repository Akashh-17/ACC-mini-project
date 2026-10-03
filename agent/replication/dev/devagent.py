"""Dev-only agent: real CatalogStorage + replication + a tiny local debug API.

Stands in for the assembled agent until the AWS owner's Registry & API module
exists. The debug API plays the role of /register, /deregister and the health
checker, following the rules in interfaces.md (AWS owner section).

    python -m agent.replication.dev.devagent      (config from env, see config.py)
    DEV_API_PORT   local debug API port, bound to 127.0.0.1 (default 9000)

Debug API:
    POST /debug/register    {"service_name", "endpoint", "tier"}  -> own-region entry, UP
    POST /debug/status      {"service_name", "status"}            -> simulate a health-check result
    POST /debug/deregister  {"service_name"}                      -> delete (tombstone replicates)
    GET  /debug/lookup?service_name=X                             -> every region's entry this agent knows
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent.storage import CatalogStorage

from ..config import ReplicationConfig
from ..server import run_replication

HEALTH_INTERVAL = 5.0


class RegisterBody(BaseModel):
    service_name: str
    endpoint: str
    tier: str = "critical"


class StatusBody(BaseModel):
    service_name: str
    status: str


class DeregisterBody(BaseModel):
    service_name: str


def create_debug_app(catalog: CatalogStorage, region_id: str) -> FastAPI:
    app = FastAPI(title=f"dev agent {region_id}")

    def local(service_name: str) -> dict:
        record = catalog.get(service_name, region_id)
        if record is None:
            raise HTTPException(404, "not registered in this region")
        return record

    @app.post("/debug/register")
    async def register(body: RegisterBody):
        catalog.set(body.service_name, region_id, {
            "endpoint": body.endpoint, "health_check_path": "/health", "tier": body.tier,
            "status": "UP", "timestamp": time.time(),
        })
        return {"status": "registered"}

    @app.post("/debug/status")
    async def status(body: StatusBody):
        record = local(body.service_name)
        record["status"] = body.status
        if body.status == "UP":
            record["timestamp"] = time.time()  # DOWN keeps the last-success time
        catalog.set(body.service_name, region_id, record)
        return {"status": body.status}

    @app.post("/debug/deregister")
    async def deregister(body: DeregisterBody):
        local(body.service_name)
        catalog.delete(body.service_name, region_id)
        return {"status": "deregistered"}

    @app.get("/debug/lookup")
    async def lookup(service_name: str):
        return {"service_name": service_name, "endpoints": catalog.list(service_name)}

    return app


async def fake_health_checks(catalog: CatalogStorage, region_id: str) -> None:
    """Every 5s re-confirm own UP entries (as a passing health check would) and sweep TTL."""
    while True:
        await asyncio.sleep(HEALTH_INTERVAL)
        for record in catalog.snapshot()["records"]:
            if not record["deleted"] and record["status"] == "UP":
                record["timestamp"] = time.time()
                catalog.set(record["service_name"], region_id, record,
                            expected_version=record["version"])
        catalog.sweepTTL()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = ReplicationConfig.from_env()
    catalog = CatalogStorage(region_id=cfg.region_id, ttl_seconds=15)
    debug = uvicorn.Server(uvicorn.Config(
        create_debug_app(catalog, cfg.region_id),
        host="127.0.0.1",
        port=int(os.environ.get("DEV_API_PORT", "9000")),
        log_level="warning",
    ))
    await asyncio.gather(
        debug.serve(),
        run_replication(catalog, cfg),
        fake_health_checks(catalog, cfg.region_id),
    )


if __name__ == "__main__":
    asyncio.run(main())
