"""Storefront client logic: registry lookup, failover routing and checkout.

Everything here is plain Python (no Streamlit) so it can be unit-tested. The
storefront reaches each region's nginx proxy over Tailscale; it never calls the
VM-private endpoints returned by /lookup, only uses their region_id.
"""
from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

SERVICES = ["payments-api", "fraud-check", "inventory-check",
            "recommend-api", "admin-api", "notify-api"]
SHORT = {"gcp-asia-south1": "GCP", "aws-ap-south-1": "AWS", "azure-indiasouthcentral": "Azure"}


class Unavailable(Exception):
    """No region could serve the request."""

    def __init__(self, service: str, attempts: list[str]):
        super().__init__(f"{service} unavailable")
        self.service = service
        self.attempts = attempts


@dataclass
class CallResult:
    data: Any
    region_id: str
    attempts: list[str] = field(default_factory=list)  # failed tries before success

    @property
    def region(self) -> str:
        return SHORT.get(self.region_id, self.region_id)


def load_clouds(path: Path) -> dict[str, dict]:
    """Same format as harness/clouds.example.json; keyed by short name."""
    clouds = json.loads(Path(path).read_text(encoding="utf-8"))
    for name, cloud in clouds.items():
        if not cloud.get("base_url", "").startswith("https://") or "_IP" in cloud["base_url"]:
            raise ValueError(f"{path}: fill in base_url for {name}")
    return clouds


class Registry:
    def __init__(self, clouds: dict[str, dict], home: str = "gcp",
                 client: Optional[httpx.Client] = None, timeout: float = 2.0):
        self.clouds = clouds
        self.home = home
        self.base = {c["region_id"]: c["base_url"].rstrip("/") for c in clouds.values()}
        self.client = client or httpx.Client(verify=False, timeout=timeout)

    # ---------- lookups ----------

    def lookup_from(self, region_id: str, service: str) -> list[dict]:
        r = self.client.get(f"{self.base[region_id]}/lookup", params={"service_name": service})
        r.raise_for_status()
        return r.json()["endpoints"]

    def lookup(self, service: str) -> list[dict]:
        """Ask the home region's registry first (it lists itself first), then
        fall back to the others if that registry is unreachable."""
        home_id = self.clouds[self.home]["region_id"]
        order = [home_id] + [r for r in self.base if r != home_id]
        last: Exception = Unavailable(service, [])
        for region_id in order:
            try:
                return self.lookup_from(region_id, service)
            except httpx.HTTPError as exc:
                last = exc
        raise Unavailable(service, [f"no registry reachable ({type(last).__name__})"])

    def views(self) -> dict[str, dict[str, list[dict]]]:
        """Every region's own view of every service, fetched in parallel.
        {observer_region_id: {service: endpoints or None if unreachable}}"""
        jobs = [(obs, svc) for obs in self.base for svc in SERVICES]

        def fetch(job):
            obs, svc = job
            try:
                return job, self.lookup_from(obs, svc)
            except httpx.HTTPError:
                return job, None

        out: dict[str, dict] = {obs: {} for obs in self.base}
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            for (obs, svc), eps in pool.map(fetch, jobs):
                out[obs][svc] = eps
        return out

    # ---------- calls with failover ----------

    def call(self, service: str, method: str, path: str, **kwargs) -> CallResult:
        """Call the first UP region for `service`; on failure try the next UP
        region (the architecture's 'retry on failure' rule)."""
        attempts: list[str] = []
        for ep in self.lookup(service):
            if ep["status"] != "UP" or ep["region_id"] not in self.base:
                continue
            region_id = ep["region_id"]
            try:
                r = self.client.request(method, f"{self.base[region_id]}/{service}{path}", **kwargs)
                if r.status_code >= 500:
                    attempts.append(f"{SHORT.get(region_id, region_id)}: HTTP {r.status_code}")
                    continue
                r.raise_for_status()
                return CallResult(r.json(), region_id, attempts)
            except httpx.HTTPError as exc:
                attempts.append(f"{SHORT.get(region_id, region_id)}: {type(exc).__name__}")
        raise Unavailable(service, attempts)

    def simulate(self, region_short: str, service: str, action: str) -> int:
        """action: 'fail' or 'recover'. Goes through that region's proxy."""
        base = self.clouds[region_short]["base_url"].rstrip("/")
        return self.client.post(f"{base}/{service}/simulate/{action}").status_code


# ---------- checkout ----------

@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    region: Optional[str] = None
    attempts: list[str] = field(default_factory=list)


def checkout(reg: Registry, cart: dict[str, int], products: dict[str, dict],
             email: str = "customer@example.com") -> tuple[bool, list[Step]]:
    """Fraud check -> reserve stock -> authorize payment -> confirmation email.
    Returns (order_placed, steps). Stops at the first blocking failure; the
    email step is best-effort (notify-api is single-cloud by design)."""
    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    amount = sum(products[pid]["price"] * qty for pid, qty in cart.items())
    steps: list[Step] = []

    def run(name, service, method, path, describe, blocking=True, **kw):
        try:
            res = reg.call(service, method, path, **kw)
        except Unavailable as exc:
            steps.append(Step(name, not blocking, f"{service} unavailable in every region"
                              if blocking else "Unavailable; will retry later", None, exc.attempts))
            return None
        ok, detail = describe(res.data)
        steps.append(Step(name, ok, detail, res.region, res.attempts))
        return res if ok else None

    if not run("Fraud check", "fraud-check", "POST", "/score",
               lambda d: (not d["flagged"], f"Risk score {d['risk_score']}" +
                          (" — order held for manual review" if d["flagged"] else " — low risk")),
               json={"order_id": order_id, "user_id": "demo-user", "amount": amount}):
        return False, steps
    if not run("Reserve stock", "inventory-check", "POST", "/reserve",
               lambda d: (d["status"] == "reserved",
                          "Items reserved" if d["status"] == "reserved" else
                          "Out of stock: " + ", ".join(products[s["product_id"]]["name"]
                                                       for s in d["items_status"] if not s["ok"])),
               json={"order_id": order_id,
                     "items": [{"product_id": p, "qty": q} for p, q in cart.items()]}):
        return False, steps
    if not run("Payment", "payments-api", "POST", "/authorize",
               lambda d: (d["status"] == "approved",
                          f"Approved ({d['transaction_id']})" if d["status"] == "approved"
                          else f"Declined: {d['reason']}"),
               json={"order_id": order_id, "amount": amount, "currency": "INR",
                     "card_token": "tok_demo"}):
        return False, steps
    run("Confirmation email", "notify-api", "POST", "/notify",
        lambda d: (True, "Queued"), blocking=False,
        json={"order_id": order_id, "email": email, "event": "order_confirmed"})
    return True, steps


def recommendations(reg: Registry, product_id: str) -> CallResult:
    return reg.call("recommend-api", "GET", "/recommendations", params={"product_id": product_id})


def now() -> str:
    return time.strftime("%H:%M:%S")
