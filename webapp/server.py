"""JSON API and pages for the demo web app.

The browser cannot reach the clouds' proxies itself (self-signed TLS, tailnet
only), so this server does it with storefront.backend and serves:
    /store    the shop             /control  the control room
    /api/...  JSON for both pages (see webapp/README.md)
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from services.catalog import PRODUCTS
from storefront.backend import SHORT, Registry, Unavailable, checkout, recommendations

from .monitor import Monitor

STATIC = Path(__file__).resolve().parent / "static"
PRODUCTS_BY_ID = {p["product_id"]: p for p in PRODUCTS}


class OrderRequest(BaseModel):
    cart: dict[str, int]
    email: str = "customer@example.com"


class SimulateRequest(BaseModel):
    cloud: str
    service: str
    action: Literal["fail", "recover"]


class TransferRequest(BaseModel):
    product_id: str
    qty: int = Field(gt=0)
    from_cloud: str
    to_cloud: str


def create_app(reg: Registry, monitor: Optional[Monitor] = None, start_monitor: bool = False) -> FastAPI:
    monitor = monitor or Monitor(reg)
    if start_monitor:
        monitor.start()
    app = FastAPI(title="CrossCloud demo")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def need_cloud(name: str) -> str:
        if name not in reg.clouds:
            raise HTTPException(status_code=422, detail=f"unknown cloud {name!r}")
        return name

    # ------------------------------------------------------------ pages

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/store")

    @app.get("/store", include_in_schema=False)
    async def store_page():
        return FileResponse(STATIC / "store.html")

    @app.get("/control", include_in_schema=False)
    async def control_page():
        return FileResponse(STATIC / "control.html")

    # ------------------------------------------------------------ reads

    @app.get("/api/state")
    async def state():
        return monitor.state()

    @app.get("/api/events")
    async def events(after: int = 0):
        return monitor.events_after(after)

    @app.get("/api/products")
    async def products():
        st = monitor.state()
        home = reg.clouds[reg.home]["region_id"]
        views = [st["stocks"].get(home)] + [v for r, v in st["stocks"].items() if r != home]
        view = next((v for v in views if v), None)
        stock = {i["product_id"]: i["global_stock"] for i in view["items"]} if view else {}
        return [{"product_id": p["product_id"], "name": p["name"], "price": p["price"],
                 "stock": stock.get(p["product_id"]), "initial": p["stock"]} for p in PRODUCTS]

    @app.get("/api/recommendations")
    async def recs(product_id: str):
        try:
            res = await run_in_threadpool(recommendations, reg, product_id)
        except Unavailable as exc:
            raise HTTPException(status_code=503, detail="recommend-api unavailable") from exc
        return {**res.data, "region": res.region}

    # ------------------------------------------------------------ actions

    @app.post("/api/order")
    async def order(req: OrderRequest):
        if not req.cart or any(pid not in PRODUCTS_BY_ID or qty <= 0 for pid, qty in req.cart.items()):
            raise HTTPException(status_code=422, detail="cart must list known products with qty > 0")
        order_id = f"ord_{uuid.uuid4().hex[:8]}"
        total = sum(PRODUCTS_BY_ID[pid]["price"] * qty for pid, qty in req.cart.items())
        ok, steps = await run_in_threadpool(checkout, reg, dict(req.cart), PRODUCTS_BY_ID, req.email, order_id)
        for s in steps:
            if s.attempts and s.region:
                monitor.add_event("failover", f"{s.name} failed over to {s.region}", "; ".join(s.attempts))
        served = " · ".join(f"{s.name.split()[0].lower()} {s.region}" for s in steps if s.region)
        monitor.add_event("order", f"Order {order_id} {'placed' if ok else 'not placed'} · ₹{total:,.0f}",
                          served or steps[-1].detail)
        if ok:
            monitor.record_order(total)
        monitor.poke()
        return {"ok": ok, "order_id": order_id, "total": total,
                "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail, "region": s.region,
                           "attempts": s.attempts, "ms": s.ms} for s in steps]}

    @app.post("/api/simulate")
    async def simulate(req: SimulateRequest):
        cloud = need_cloud(req.cloud)
        code = await run_in_threadpool(reg.simulate, cloud, req.service, req.action)
        label = SHORT[reg.clouds[cloud]["region_id"]]
        monitor.add_event("demo", f"Demo: {req.action} {req.service} on {label}", f"HTTP {code}")
        monitor.poke()
        return {"status_code": code}

    @app.post("/api/transfer")
    async def transfer(req: TransferRequest):
        src, dst = need_cloud(req.from_cloud), need_cloud(req.to_cloud)
        r = await run_in_threadpool(reg.transfer, src, req.product_id, req.qty, reg.clouds[dst]["region_id"])
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text[:200])
        status = r.json()["status"]
        name = PRODUCTS_BY_ID.get(req.product_id, {}).get("name", req.product_id)
        monitor.add_event("demo", f"Stock transfer {status}",
                          f"{req.qty} × {name}: {SHORT[reg.clouds[src]['region_id']]} → "
                          f"{SHORT[reg.clouds[dst]['region_id']]}")
        monitor.poke()
        return {"status": status}

    return app
