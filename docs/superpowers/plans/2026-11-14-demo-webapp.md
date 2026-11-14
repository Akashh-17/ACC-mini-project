# Demo Web App (Store + Control room) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Streamlit storefront with a two-page web app (`/store`, `/control`) that looks like the approved mockup and shows everything the current demo shows.

**Architecture:** A FastAPI server on the laptop (`python -m webapp`) reuses `storefront/backend.py` to reach the clouds' proxies; a background monitor snapshots the clouds every 2 s and turns changes into feed events; plain HTML/CSS/JS pages poll the server's JSON API.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, httpx (already in `requirements.txt`); vanilla HTML/CSS/JS, Google Fonts (Inter, Space Grotesk, JetBrains Mono); pytest.

**Spec:** `docs/superpowers/specs/2026-11-14-demo-webapp-design.md`. Visual reference: https://claude.ai/artifact/FEMLvsD9CuY1QhEmZEFRu4

## Global Constraints

- No build step and no new dependencies.
- Server binds `127.0.0.1` only; default port `8500`.
- Config: `results/clouds.local.json`, overridable with `STOREFRONT_CONFIG` (same as the Streamlit app).
- Cloud colours: GCP `#60A5FA`, AWS `#FBBF24`, Azure `#A78BFA`; status colours always paired with a text label (UP/DOWN/STALE).
- The monitor calls the clouds at most once per 2 s regardless of open tabs.
- Git: the user commits. Each task ends by stopping for the user to commit; never run git write commands.
- Existing tests must keep passing (`.venv/Scripts/python.exe -m pytest -q`).

---

### Task 1: Read-only ledger/stock views survive a simulated failure

**Files:**
- Modify: `services/app.py` (the `/ledger` and `/stock` routes)
- Test: `tests/test_services.py`

**Interfaces:** Produces: `GET /ledger` and `GET /stock` return 200 while `/simulate/fail` is active; `/authorize`, `/reserve`, `/stock/transfer` still return 503.

- [ ] **Step 1: Failing test** (append to `tests/test_services.py`)

```python
def test_read_only_views_survive_simulated_failure():
    pay, inv = client_for("payments-api"), client_for("inventory-check")
    pay.post("/simulate/fail"); inv.post("/simulate/fail")
    assert pay.get("/ledger").status_code == 200
    assert inv.get("/stock").status_code == 200
    assert authorize(pay, 10).status_code == 503
    assert reserve(inv, [{"product_id": "p1", "qty": 1}]) == {"detail": "service is simulated as failed"}
```

- [ ] **Step 2:** Run `.venv/Scripts/python.exe -m pytest tests/test_services.py -q` → the new test fails (503 on `/ledger`).
- [ ] **Step 3:** In `services/app.py` remove `dependencies=[Depends(available)]` from `@app.get("/ledger")` and `@app.get("/stock")`, and add a comment above `/ledger`: `# Read-only replicated data stays visible while the service is "failed".`
- [ ] **Step 4:** Run the suite → all pass.
- [ ] **Step 5:** Stop for the user to commit. Deployment: copy `services/app.py` to each VM and restart `registry-service@payments-api registry-service@inventory-check`.

### Task 2: Step timing, chosen order id, and transfer in the storefront backend

**Files:**
- Modify: `storefront/backend.py`
- Test: `tests/test_storefront.py`

**Interfaces:** Produces:
- `Step.ms: Optional[int]` — milliseconds the step's call took (None if never answered).
- `checkout(reg, cart, products, email=..., order_id=None)` — uses `order_id` when given.
- `Registry.transfer(from_short: str, product_id: str, qty: int, to_region: str) -> httpx.Response` — POST `{base}/inventory-check/stock/transfer` on the giving cloud.

- [ ] **Step 1: Failing tests** (append)

```python
def test_steps_have_timing_and_given_order_id(world):
    ok, steps = checkout(world.registry(), {"p1": 1}, PRODUCTS_BY_ID, order_id="ord_fixed")
    assert ok and all(isinstance(s.ms, int) and s.ms >= 0 for s in steps)
    body = world.apps[("payments-api", "gcp-asia-south1")].get("/ledger").json()
    assert body["recent"][0]["order_id"] == "ord_fixed"


def test_transfer_goes_through_giving_cloud(world):
    r = world.registry().transfer("aws", "p5", 1, "gcp-asia-south1")
    assert r.status_code == 200 and r.json() == {"status": "transferred"}
    stock = world.apps[("inventory-check", "aws-ap-south-1")].get("/stock").json()["items"]
    assert next(i for i in stock if i["product_id"] == "p5")["local_share"] == allocation()["aws-ap-south-1"]["p5"] - 1
```

- [ ] **Step 2:** Run → fail (`unexpected keyword 'order_id'`, no `transfer`).
- [ ] **Step 3:** Implement: `Step` gets `ms: Optional[int] = None`; in `checkout.run` time `reg.call` with `time.perf_counter()` and pass `ms` to `Step`; `order_id = order_id or f"ord_{uuid.uuid4().hex[:8]}"`; add `Registry.transfer` posting `{"product_id", "qty", "to_region"}`.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Stop for the user to commit.

### Task 3: Monitor — snapshots, feed events, key numbers

**Files:**
- Create: `webapp/__init__.py`, `webapp/monitor.py`
- Test: `tests/test_webapp.py`

**Interfaces:** Produces `class Monitor(reg, interval=2.0, clock=time.time, max_events=200)` with:
- `refresh() -> None` — takes a snapshot (`reg.views()`, `ledgers(reg)`, `stocks(reg)`), appends feed events for differences from the previous one. The first snapshot produces no diff events.
- `add_event(kind: str, title: str, detail: str = "") -> int` — kinds: `order`, `failover`, `status`, `replicated`, `demo`, `error`; returns `seq`.
- `events_after(seq: int) -> list[dict]` — each `{"seq", "time" (HH:MM:SS), "kind", "title", "detail"}`.
- `state() -> dict` — `{"views", "ledgers", "stocks", "taken_at", "age_s", "metrics"}`; `metrics` = `{"replication_ms": int|None, "healthy": int, "endpoints": int, "orders": int, "order_total": float, "unsent": int}`.
- `record_order(total: float) -> None`, `poke() -> None` (wake the loop early), `start() -> None` (daemon thread; exceptions become an `error` event and keep the last snapshot).

Diff rules: a status change of service *s* in region *r* becomes one `status` event per (s, r, new status) listing the observers that saw it ("seen by GCP, AWS, Azure"); a payment newly present as replicated in some cloud's ledger becomes one `replicated` event per txn listing `cloud lag ms` for the clouds that newly have it.

- [ ] **Step 1: Failing tests** (`tests/test_webapp.py`)

```python
from services.catalog import PRODUCTS
from storefront.backend import checkout
from tests.test_storefront import World
from webapp.monitor import Monitor

PRODUCTS_BY_ID = {p["product_id"]: p for p in PRODUCTS}


def test_first_snapshot_has_no_events_and_metrics():
    m = Monitor(World().registry()); m.refresh()
    st = m.state()
    assert m.events_after(0) == []
    assert st["metrics"]["endpoints"] == 12 and st["metrics"]["healthy"] == 12


def test_status_change_becomes_one_event():
    w = World(); m = Monitor(w.registry()); m.refresh()
    w.status[("payments-api", "gcp-asia-south1")] = "DOWN"; m.refresh()
    [e] = m.events_after(0)
    assert e["kind"] == "status" and "payments-api" in e["title"] and "DOWN" in e["title"]
    assert "GCP, AWS, Azure" in e["detail"]


def test_replicated_payment_event():
    w = World(); reg = w.registry(); m = Monitor(reg); m.refresh()
    checkout(reg, {"p1": 1}, PRODUCTS_BY_ID)
    # fake world has no Kafka: make AWS's ledger show the payment as replicated
    pay = w.apps[("payments-api", "gcp-asia-south1")].get("/ledger").json()["recent"][0]
    w.ledger_extra = {"aws-ap-south-1": [{**pay, "replicated": True, "lag_ms": 412}]}
    m.refresh()
    kinds = [e["kind"] for e in m.events_after(0)]
    assert "replicated" in kinds
```

  `World` gains an optional `ledger_extra` dict used by its handler to append rows to `/payments-api/ledger` responses (test-only hook, added in `tests/test_storefront.py`).

- [ ] **Step 2:** Run → fail (no module `webapp`).
- [ ] **Step 3:** Implement `webapp/monitor.py` per the interface above (threading.Lock around snapshot/events; `deque(maxlen=max_events)`; healthy = UP endpoints in the first reachable observer's view, home `gcp` first).
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Stop for the user to commit.

### Task 4: Server — JSON API and static pages

**Files:**
- Create: `webapp/server.py`, `webapp/__main__.py`
- Test: `tests/test_webapp.py`

**Interfaces:** `create_app(registry, monitor=None, start_monitor=False) -> FastAPI`; routes exactly as in the spec table; `/` redirects to `/store`; `/store`, `/control` serve `webapp/static/store.html`, `control.html`; `/static` mounted. `POST /api/order` body `{"cart": {pid: qty}, "email": str}` → `{"ok", "order_id", "total", "steps": [{"name","ok","detail","region","attempts","ms"}]}`; unknown product or empty cart → 422. `POST /api/simulate` `{"cloud": "gcp"|"aws"|"azure", "service", "action": "fail"|"recover"}` → `{"status_code"}`. `POST /api/transfer` `{"product_id","qty","from_cloud","to_cloud"}` → `{"status"}`. `GET /api/products` → `[{product_id, name, price, stock}]` (stock = global stock from the latest snapshot, None if unknown). Order/simulate/transfer add feed events (failover attempts become `failover` events) and `poke()` the monitor.

- [ ] **Step 1: Failing tests** (append)

```python
from fastapi.testclient import TestClient
from webapp.server import create_app


def app_for(world):
    m = Monitor(world.registry()); m.refresh()
    return TestClient(create_app(world.registry(), monitor=m)), m


def test_order_trace_and_events():
    w = World(); w.apps[("payments-api", "gcp-asia-south1")].post("/simulate/fail")
    client, m = app_for(w)
    body = client.post("/api/order", json={"cart": {"p1": 1}, "email": "a@b.c"}).json()
    assert body["ok"] and body["total"] == 2499
    pay = body["steps"][2]
    assert pay["region"] in ("AWS", "Azure") and pay["attempts"] == ["GCP: HTTP 503"]
    kinds = [e["kind"] for e in client.get("/api/events?after=0").json()]
    assert "order" in kinds and "failover" in kinds
    assert client.get("/api/state").json()["metrics"]["orders"] == 1


def test_bad_orders_rejected():
    client, _ = app_for(World())
    assert client.post("/api/order", json={"cart": {}, "email": "a@b.c"}).status_code == 422
    assert client.post("/api/order", json={"cart": {"nope": 1}, "email": "a@b.c"}).status_code == 422


def test_simulate_and_transfer():
    w = World(); client, _ = app_for(w)
    assert client.post("/api/simulate", json={"cloud": "aws", "service": "recommend-api",
                                              "action": "fail"}).json() == {"status_code": 200}
    assert w.apps[("recommend-api", "aws-ap-south-1")].get("/health").status_code == 503
    r = client.post("/api/transfer", json={"product_id": "p5", "qty": 1, "from_cloud": "aws", "to_cloud": "gcp"})
    assert r.json() == {"status": "transferred"}


def test_products_and_pages():
    client, _ = app_for(World())
    prods = client.get("/api/products").json()
    assert prods[0]["product_id"] == "p1" and prods[0]["stock"] == 25
    assert client.get("/store").status_code == 200 and client.get("/control").status_code == 200
```

- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement `webapp/server.py` (blocking calls via `run_in_threadpool`) and `webapp/__main__.py` (`main()`: load clouds via `storefront.backend.load_clouds`, build `Registry(home="gcp")`, `Monitor.start()`, print `http://127.0.0.1:8500/store` and `/control`, `uvicorn.run(host="127.0.0.1", port=int(os.environ.get("WEBAPP_PORT", 8500)))`). Create minimal placeholder `store.html`/`control.html` so the page test passes; Tasks 5–6 replace them.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Stop for the user to commit.

### Task 5: Store page

**Files:** Create `webapp/static/app.css`, `webapp/static/common.js`, `webapp/static/store.html`, `webapp/static/store.js`.

**Interfaces:** Consumes `/api/products`, `/api/recommendations`, `/api/order`, `/api/state`. `common.js` exposes `api(path, opts)`, `inr(n)`, `esc(s)`, `cloudChip(label)`, `CLOUD` colour map.

Content and look: the mockup's Store artboard — header (logo, search filters the grid client-side, Control room link opening `/control` in a new window, cart button with count), dark hero with live health line from `/api/state` metrics, product grid with live stock badges (≤5 shows "Only N left", 0 shows "Sold out" and disables the button), recommendations for the last product added, cart with qty +/−, email field, Place order (disabled while running, shows "Routing your order…"), order timeline with per-step cloud chip, ms and orange failover reason; a replication line appears once `/api/events` reports a `replicated` event for this order's payment.

- [ ] **Step 1:** Write the four files.
- [ ] **Step 2:** Run `python -m webapp` against the fake world is not possible; verify with `tests/test_webapp.py::test_products_and_pages` and manually against the live clouds (Task 7).
- [ ] **Step 3:** Stop for the user to commit.

### Task 6: Control room page

**Files:** Create `webapp/static/control.html`, `webapp/static/control.js` (shares `app.css`, `common.js`).

**Interfaces:** Consumes `/api/state` (2 s), `/api/events?after=` (1 s), `/api/simulate`, `/api/transfer`.

Content and look: the mockup's Control artboard — header with Kafka pill (green when no cloud has unsent changes and all ledgers readable; amber otherwise) and live clock, four key-number tiles from `metrics`, three cloud cards with each service's status as seen by that cloud's own registry and a Fail/Recover button (button shows Recover when DOWN), registry-views matrix, three ledger copies, stock share bars from the first readable stock view with the transfer form, live activity feed (newest first, 60 items).

- [ ] **Step 1:** Write the two files.
- [ ] **Step 2:** Verify page test still passes; manual check in Task 7.
- [ ] **Step 3:** Stop for the user to commit.

### Task 7: Live run, docs, requirements

**Files:** Create `webapp/README.md`; modify `storefront/README.md` (point to the new app).

- [ ] **Step 1:** Run `python -m webapp` against the live clouds; open both pages; place an order; fail/recover a service; check the feed and ledgers; screenshot both pages.
- [ ] **Step 2:** Write `webapp/README.md` (run command, pages, API table, troubleshooting).
- [ ] **Step 3:** Full suite passes. Stop for the user to commit; Task 1's VM deployment noted for the team.
