"""Configurable business-service stub.

Run with SERVICE_NAME, SERVICE_REGION and SERVICE_PORT set in the environment.
Each service exposes /health, /simulate/fail, /simulate/recover and its own
business endpoint from tasks.md section 1.2. Logic is deliberately simple and
deterministic; there is no real payment gateway or fraud model.

The single-cloud standard services can be backed by their cloud's managed service
(services/backends.py, chosen by env var); /health then also reflects whether that
managed service is reachable. Critical services never depend on one.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .backends import BackendError, HealthCache, backend_from_env
from .catalog import FRAUD_THRESHOLD, PAYMENT_LIMIT, starting_stock

log = logging.getLogger("services")


class AuthorizeRequest(BaseModel):
    order_id: str
    amount: float = Field(ge=0)
    currency: str
    card_token: str


class ScoreRequest(BaseModel):
    order_id: str
    user_id: str
    amount: float = Field(ge=0)


class ReserveItem(BaseModel):
    product_id: str
    qty: int = Field(gt=0)


class ReserveRequest(BaseModel):
    order_id: str
    items: list[ReserveItem] = Field(min_length=1)


class StockUpdate(BaseModel):
    product_id: str
    stock: int = Field(ge=0)


class NotifyRequest(BaseModel):
    order_id: str
    email: str
    event: str


def required_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be set")
    return value


_DEFAULT = object()


def create_app(service_name: str, region_id: str, backend=_DEFAULT) -> FastAPI:
    """`backend` defaults to the in-memory one; main() passes the env-selected one."""
    state = {"healthy": True}
    stock = starting_stock()  # inventory-check's own stock; per process
    if backend is _DEFAULT:
        backend = backend_from_env(service_name, {})
    health = HealthCache(backend.check if backend else (lambda: None))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service_name = service_name
        app.state.region_id = region_id
        yield

    app = FastAPI(title=service_name, lifespan=lifespan)
    app.state.health = health

    async def call_backend(fn, *args):
        # Cloud SDKs block; keep them off the event loop. A failing managed
        # service makes this API unavailable (503) so callers fail over/degrade.
        try:
            return await run_in_threadpool(fn, *args)
        except BackendError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def available():
        # A simulated failure fails the business endpoint too, so callers
        # really have to fail over instead of only the health check changing.
        if not state["healthy"]:
            raise HTTPException(status_code=503, detail="service is simulated as failed")

    @app.get("/health")
    async def health_check():
        available()
        if not await run_in_threadpool(health.healthy):
            raise HTTPException(status_code=503, detail=f"{backend.name} backend unavailable")
        body = {"service_name": service_name, "region_id": region_id, "status": "UP"}
        if backend is not None:
            body["backend"] = backend.name
        return body

    @app.post("/simulate/fail")
    async def simulate_failure():
        state["healthy"] = False
        return {"service_name": service_name, "status": "DOWN"}

    @app.post("/simulate/recover")
    async def simulate_recovery():
        state["healthy"] = True
        return {"service_name": service_name, "status": "UP"}

    if service_name == "payments-api":
        @app.post("/authorize", dependencies=[Depends(available)])
        async def authorize(req: AuthorizeRequest):
            if req.amount > PAYMENT_LIMIT:
                return {"status": "declined", "reason": f"amount exceeds {PAYMENT_LIMIT} limit"}
            return {"status": "approved", "transaction_id": f"txn_{uuid.uuid4().hex[:12]}",
                    "region": region_id}

    elif service_name == "fraud-check":
        @app.post("/score", dependencies=[Depends(available)])
        async def score(req: ScoreRequest):
            if req.amount > FRAUD_THRESHOLD:
                return {"risk_score": 85, "flagged": True}
            # Low score derived from the order id: repeatable for the same order.
            digest = hashlib.sha256(req.order_id.encode()).digest()
            return {"risk_score": 5 + digest[0] % 30, "flagged": False}

    elif service_name == "inventory-check":
        @app.post("/reserve", dependencies=[Depends(available)])
        async def reserve(req: ReserveRequest):
            wanted: dict[str, int] = {}
            for item in req.items:
                wanted[item.product_id] = wanted.get(item.product_id, 0) + item.qty
            items_status = [{"product_id": item.product_id,
                             "ok": stock.get(item.product_id, 0) >= wanted[item.product_id]}
                            for item in req.items]
            if not all(s["ok"] for s in items_status):
                return {"status": "insufficient", "items_status": items_status}
            for pid, qty in wanted.items():  # all-or-nothing
                stock[pid] -= qty
            return {"status": "reserved", "items_status": items_status}

    elif service_name == "recommend-api":
        @app.get("/recommendations", dependencies=[Depends(available)])
        async def recommendations(product_id: str):
            ids = await call_backend(backend.get, product_id)
            return {"recommended_ids": ids, "source": backend.name}

    elif service_name == "admin-api":
        @app.get("/inventory", dependencies=[Depends(available)])
        async def inventory():
            return {"items": await call_backend(backend.items), "source": backend.name}

        @app.post("/inventory/update", dependencies=[Depends(available)])
        async def update_inventory(req: StockUpdate):
            try:
                await call_backend(backend.update, req.product_id, req.stock)
            except KeyError:
                raise HTTPException(status_code=404, detail="unknown product_id")
            return {"status": "updated"}

    elif service_name == "notify-api":
        @app.post("/notify", dependencies=[Depends(available)])
        async def notify(req: NotifyRequest):
            message = {"order_id": req.order_id, "email": req.email, "event": req.event,
                       "region_id": region_id, "ts": time.time()}
            message_id = await call_backend(backend.publish, message)
            return {"status": "queued", "message_id": message_id, "backend": backend.name}

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
    backend = backend_from_env(service_name, os.environ)
    if backend is not None:
        log.warning("%s using %s backend", service_name, backend.name)
    uvicorn.run(create_app(service_name, region_id, backend=backend), host=host, port=port)


if __name__ == "__main__":
    main()
