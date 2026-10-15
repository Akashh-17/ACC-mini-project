"""Configurable business-service stub.

Run with SERVICE_NAME, SERVICE_REGION and SERVICE_PORT set in the environment.
Each service exposes /health, /simulate/fail, /simulate/recover and its own
business endpoint from tasks.md section 1.2. Logic is deliberately simple and
deterministic; there is no real payment gateway, fraud model or email delivery.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .catalog import FRAUD_THRESHOLD, PAYMENT_LIMIT, PRODUCTS, starting_stock

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


def create_app(service_name: str, region_id: str) -> FastAPI:
    state = {"healthy": True}
    stock = starting_stock()  # used by inventory-check and admin-api; per process

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service_name = service_name
        app.state.region_id = region_id
        yield

    app = FastAPI(title=service_name, lifespan=lifespan)

    def available():
        # A simulated failure fails the business endpoint too, so callers
        # really have to fail over instead of only the health check changing.
        if not state["healthy"]:
            raise HTTPException(status_code=503, detail="service is simulated as failed")

    @app.get("/health")
    async def health():
        available()
        return {"service_name": service_name, "region_id": region_id, "status": "UP"}

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
            others = [p["product_id"] for p in PRODUCTS if p["product_id"] != product_id]
            return {"recommended_ids": others[:3]}

    elif service_name == "admin-api":
        @app.get("/inventory", dependencies=[Depends(available)])
        async def inventory():
            return {"items": [{"product_id": pid, "stock": qty} for pid, qty in stock.items()]}

        @app.post("/inventory/update", dependencies=[Depends(available)])
        async def update_inventory(req: StockUpdate):
            if req.product_id not in stock:
                raise HTTPException(status_code=404, detail="unknown product_id")
            stock[req.product_id] = req.stock
            return {"status": "updated"}

    elif service_name == "notify-api":
        @app.post("/notify", dependencies=[Depends(available)])
        async def notify(req: NotifyRequest):
            log.warning("notify: %s -> %s for order %s (region %s)",
                        req.event, req.email, req.order_id, region_id)
            return {"status": "queued"}

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
