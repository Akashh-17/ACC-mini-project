"""AWS-owned register/deregister/lookup APIs; shared by all regional agents."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import hmac
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictStr, field_validator

from .health import HealthScheduler
from .validation import validate_endpoint, validate_health_path


class DeregisterBody(BaseModel):
    service_name: StrictStr = Field(min_length=1)
    region_id: StrictStr = Field(min_length=1)
    endpoint: StrictStr = Field(min_length=1)

    @field_validator("service_name", "region_id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class RegisterBody(DeregisterBody):
    health_check_path: StrictStr
    tier: Literal["critical", "standard"]

    @field_validator("health_check_path")
    @classmethod
    def local_health_path(cls, value):
        return validate_health_path(value)


def create_registry_app(catalog, secret: str, *, allow_loopback=False,
                        health_client=None, health_interval=5.0) -> FastAPI:
    """Use the SAME catalog instance as GCP replication, with one API worker."""
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("REGISTRY_SECRET must be set and non-empty")

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            client = health_client
            if client is None:
                # No keep-alive: probes run on a 5 s beat, exactly uvicorn's idle
                # timeout, so a reused connection is often closed mid-request and
                # a healthy service is reported DOWN (measured 16% false failures).
                client = await stack.enter_async_context(httpx.AsyncClient(
                    timeout=2.0, follow_redirects=False, trust_env=False,
                    limits=httpx.Limits(max_keepalive_connections=0),
                ))
            scheduler = HealthScheduler(catalog, client, interval=health_interval,
                                        allow_loopback=allow_loopback)
            app.state.health_scheduler = scheduler
            tasks = [asyncio.create_task(scheduler.run()),
                     asyncio.create_task(scheduler.sweep())]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Registry & API", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.catalog = catalog

    @app.middleware("http")
    async def authenticate_writes(request: Request, call_next):
        # Authenticate before parsing the body, including malformed requests.
        if request.method == "POST" and request.url.path in ("/register", "/deregister"):
            parts = request.headers.get("Authorization", "").split()
            if (len(parts) != 2 or parts[0].lower() != "bearer"
                    or not hmac.compare_digest(parts[1].encode(), secret.encode())):
                return JSONResponse({"detail": "Bad or missing bearer token"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # tasks.md specifies 400, rather than FastAPI's default 422.
        return JSONResponse({"detail": "Invalid request", "errors": [
            {"field": ".".join(map(str, error["loc"])), "message": error["msg"]}
            for error in exc.errors()
        ]}, status_code=400)

    def local_target(body: DeregisterBody) -> str:
        if body.region_id != catalog.region_id:
            raise HTTPException(400, "region_id must match this agent's region")
        try:
            return validate_endpoint(body.endpoint, allow_loopback=allow_loopback)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/register")
    async def register(body: RegisterBody):
        endpoint = local_target(body)
        catalog.set(body.service_name, body.region_id, {
            "endpoint": endpoint, "health_check_path": body.health_check_path,
            "tier": body.tier, "status": "STALE", "timestamp": 0.0,
        })
        return {"status": "registered"}

    @app.post("/deregister")
    async def deregister(body: DeregisterBody):
        endpoint = local_target(body)
        # Idempotent acknowledgement. A missing entry or old endpoint cannot
        # remove a newer registration; storage's endpoint guard handles this.
        catalog.delete(body.service_name, body.region_id, endpoint=endpoint)
        return {"status": "deregistered"}

    @app.get("/lookup")
    async def lookup(service_name: str = Query(..., min_length=1)):
        if not service_name.strip():
            raise HTTPException(400, "service_name must not be blank")
        records = catalog.list(service_name.strip())
        # Preserve tasks.md's diagnostic response: callers select UP entries.
        return {"service_name": service_name.strip(), "endpoints": [
            {key: record[key] for key in ("region_id", "endpoint", "status")}
            for record in records
        ]}

    return app
