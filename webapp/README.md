# Demo web app: Store + Control room

Two live pages for the final demo, served from your laptop (on the tailnet):

| Page | What it shows |
|---|---|
| `http://127.0.0.1:8500/store` | The shop: products with live stock, recommendations, cart, checkout, and an order timeline showing which cloud served each step and why it failed over |
| `http://127.0.0.1:8500/control` | The control room: key numbers, one card per cloud with Fail/Recover per service, what each registry sees, each cloud's ledger copy, stock shares with transfers, and a live activity feed |

Open them as two tabs or two windows; both update live.

## Run

From the repository root, with the project venv:

```powershell
.\.venv\Scripts\python.exe -m webapp
```

It reads the same cloud config as the Streamlit storefront:
`results/clouds.local.json` (override with `STOREFRONT_CONFIG`). Port 8500
(override with `WEBAPP_PORT`). It listens on 127.0.0.1 only.

## How it works

The browser cannot reach the clouds' proxies (self-signed TLS, tailnet only),
so `webapp/server.py` does it, reusing `storefront/backend.py` for lookups,
failover and checkout. `webapp/monitor.py` takes one snapshot of the clouds
every 2 s (registry views, ledgers, stock), whatever number of tabs is open,
and turns changes into the activity feed. The pages are plain HTML/CSS/JS in
`webapp/static/` and poll the JSON API below.

| Method | Path | Does |
|---|---|---|
| GET | `/api/state` | latest snapshot + key numbers |
| GET | `/api/events?after=<seq>` | feed events newer than `seq` |
| GET | `/api/products` | catalog + live global stock |
| GET | `/api/recommendations?product_id=` | via the registry (AWS only) |
| POST | `/api/order` | `{cart: {product_id: qty}, email}` → step trace |
| POST | `/api/simulate` | `{cloud: gcp\|aws\|azure, service, action: fail\|recover}` |
| POST | `/api/transfer` | `{product_id, qty, from_cloud, to_cloud}` |

## Notes

- Orders are real: they reserve stock and record payments on the clouds.
  Reset the ledgers before the demo (`infra/kafka/README.md` section 4).
- A failed payments-api keeps showing its ledger copy once every VM runs the
  current `services/app.py` (read-only views no longer fail under simulated
  failure). Until then that cloud's ledger shows as unreachable while failed.
- "Replication" in the key numbers is the median lag of the replicated
  payments currently in the ledgers (under 60 s; older ones are catch-ups).
- Tests: `tests/test_webapp.py` (runs against a fake three-cloud world).
- The Streamlit storefront (`storefront/app.py`) still works as a fallback.
