# Demo web app: Store + Control room — design

Replaces the Streamlit storefront with a two-page web app that looks like a
real product. Visual design: the approved mockup
(https://claude.ai/artifact/FEMLvsD9CuY1QhEmZEFRu4).

## Goal

- `/store`: a realistic shop (light). Products with live stock, recommendations,
  cart, checkout, and an order timeline showing which cloud served each step
  and why it failed over.
- `/control`: a control room (dark). Key numbers, one card per cloud with
  Fail/Recover per service, the registry-views matrix, the three ledger copies,
  stock share bars with a transfer control, and a live activity feed.
- Two URLs, opened as two tabs or two windows; both update live from the same
  server, so an order placed in the store appears in the control room within
  about a second.
- Everything the Streamlit demo shows today is still shown.

## Architecture

```
browser /store, /control ──HTTP──▶ webapp (FastAPI, laptop, 127.0.0.1:8500)
                                       │ reuses storefront/backend.py
                                       ▼
                         each cloud's nginx proxy over Tailscale
```

The browser cannot call the proxies itself (self-signed TLS, tailnet only), so
a local FastAPI server does it, reusing `storefront/backend.py` (Registry,
failover `call`, `checkout`, `views`, `ledgers`, `stocks`, `simulate`).
No build step: plain HTML, CSS and JavaScript served as static files.

### Server (`webapp/`)

- `webapp/server.py`: `create_app(registry)` (testable with the existing fake
  three-region world) and `main()` (`python -m webapp`, config from
  `results/clouds.local.json` or `STOREFRONT_CONFIG`, binds 127.0.0.1 only,
  prints both URLs).
- `webapp/monitor.py`: a background thread that every 2 s takes a snapshot
  (registry views, ledgers, stock) and turns differences from the previous
  snapshot into feed events (service status changes per observer, newly
  replicated payments with their lag, stock changes). Keeps the latest
  snapshot and the last 200 events, each with an increasing `seq`.
- API (JSON):
  | Method | Path | Does |
  |---|---|---|
  | GET | `/api/state` | latest snapshot + key numbers |
  | GET | `/api/events?after=<seq>` | feed events newer than `seq` |
  | GET | `/api/products` | catalog + live global stock |
  | GET | `/api/recommendations?product_id=` | via registry, single-cloud |
  | POST | `/api/order` | runs `checkout`, returns the step trace, adds feed events |
  | POST | `/api/simulate` | `{cloud, service, action}` fail/recover via that proxy |
  | POST | `/api/transfer` | `{product_id, qty, from, to}` via the giving cloud's proxy |
- Pages poll `/api/state` every 2 s and `/api/events` every 1 s. Polling, not
  SSE: fewer moving parts on demo day, and the monitor already rate-limits
  calls to the clouds (one snapshot per 2 s however many tabs are open).

### Pages (`webapp/static/`)

`store.html`, `control.html`, `app.css` (shared tokens: cloud colours GCP
blue, AWS amber, Azure violet; status colours always paired with a text label),
`common.js` (fetch helpers, formatting), `store.js`, `control.js`. Layout and
look follow the mockup; both pages work at narrow widths.

### Changes outside `webapp/`

- `storefront/backend.py`: `Step` gains `ms` (time taken by the step), so the
  timeline can show per-step timing. Streamlit app unaffected.
- `services/app.py`: `GET /ledger` and `GET /stock` stop failing under a
  simulated failure; only business actions (`/authorize`, `/reserve`, …) do.
  A "failed" payments-api still refuses payments, but its replicated ledger
  stays visible (the "Unreachable" problem from the rehearsal). Needs a file
  copy + restart of payments-api and inventory-check on each VM.
- The Streamlit storefront stays in the repo as a fallback until the new app
  has been rehearsed; removal is a separate decision.

## Key numbers (control room)

All live, nothing invented: replication median of the replicated payments
currently listed (lag < 60 s), healthy endpoints (UP entries in the home
registry's view), orders placed this session and their total, and changes
waiting for Kafka across all clouds.

## Error handling

- A cloud whose proxy is unreachable: its card and ledger say "Unreachable";
  the rest keeps working.
- Order step unavailable everywhere: the timeline shows that step failed with
  the attempts; email failures stay non-blocking (as today).
- Simulate / transfer failures are shown as a toast with the HTTP status.
- The monitor never crashes the server: a failed snapshot keeps the previous
  one and marks it stale with its age.

## Testing

- `tests/test_webapp.py`: the API against the existing fake three-region
  world (`tests/test_storefront.py`'s `World`): order trace incl. failover,
  simulate, transfer, events produced by a status change and by a replicated
  payment, products' stock.
- Updated `tests/test_services.py`: ledger/stock readable while simulated failed.
- Manual: run against the live clouds, rehearse the demo script.

## Out of scope

Stopping a replicator from the UI (needs SSH), authentication (the app binds to
localhost only), product photos (line-art icons as in the mockup).
