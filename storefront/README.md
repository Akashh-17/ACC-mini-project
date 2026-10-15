# CrossCloud Store (demo frontend)

A small shop on top of the cross-cloud registry, for the live demo. It runs on the
presenter's laptop and reaches the three regions' proxies over Tailscale.

- **Left (the shop):** add products, "customers also bought" (recommend-api, AWS only),
  and checkout: fraud check → reserve stock → payment → confirmation email. Each step
  asks the registry for UP regions, calls the first one through its proxy, retries the
  next region on failure, and shows which cloud served it.
- **Right (behind the scenes):** each region's own registry view of every service,
  refreshed every 2 s, an event log of every status change, and demo buttons that
  fail/recover services through each region's proxy.

## Run

```powershell
.\.venv\Scripts\python.exe -m pip install -r storefront\requirements.txt
.\.venv\Scripts\streamlit.exe run storefront\app.py
```

Needs: laptop connected to Tailscale; `results/clouds.local.json` (same format as
`harness/clouds.example.json`, gitignored), or set `STOREFRONT_CONFIG` to another path.
The home region is GCP: normal purchases go through GCP until it fails.

## Demo script

1. **Normal:** add items, place order. Every step says *via GCP*.
2. **Payment failover (D1):** *Fail* GCP payments. Place an order immediately: the
   payment step retries and says *via AWS/Azure* ("failed over after: GCP: HTTP 503").
   Within ~5 s GCP's view turns red, then AWS's and Azure's views follow (replication).
   *Recover* GCP: it turns green again.
3. **Single-cloud outage (D2):** *Fail* AWS recommend-api. "Customers also bought"
   becomes unavailable, but checkout still works. *Recover* it afterwards.

## Notes

- Stock is per region (each inventory stub keeps its own count): the registry carries
  no transactional data, by design.
- Orders above ₹20,000 are held by fraud-check; payments above ₹50,000 are declined.
- Proxy certificates are self-signed; the app skips verification for these
  Tailscale-only addresses.
