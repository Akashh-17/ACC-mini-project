# Replication & Security module (GCP owner)

Pushes this agent's own catalog changes to the two peer agents and applies
theirs, over mutual TLS. Storage semantics (versions, tombstones, snapshots)
are defined in the repo-root `interfaces.md`. This file covers the wire side.

## Using it from the assembled agent

```python
import asyncio, os
from agent.storage import CatalogStorage
from agent.replication import ReplicationConfig, run_replication

catalog = CatalogStorage(region_id=os.environ["REGION_ID"], ttl_seconds=15)  # one shared instance
cfg = ReplicationConfig.from_env()
await asyncio.gather(registry_server.serve(), run_replication(catalog, cfg))
```

Environment (see `deploy/agent.env.example`):

| Variable | Example | Default |
|---|---|---|
| `REGION_ID` | `gcp-asia-south1` | required |
| `REPLICATION_PEERS` | `aws-ap-south-1=https://13.x.x.x:9443,azure-indiasouthcentral=https://20.x.x.x:9443` | none |
| `TLS_CERT`, `TLS_KEY`, `TLS_CA` | this agent's cert/key and the shared CA cert | required |
| `REPLICATION_HOST` / `REPLICATION_PORT` | | `0.0.0.0` / `9443` |
| `REPLICATION_INTERVAL` | delta push period, seconds | `2.0` |
| `REPLICATION_TIMEOUT` | per-request timeout, seconds | `2.0` |
| `REPLICATION_SNAPSHOT_INTERVAL` | full-snapshot backstop, seconds | `30.0` |

## Wire protocol (port 9443, mTLS only)

Both endpoints require a client cert signed by the team CA. The cert's CN must be
one of the configured peer region_ids; anything else gets 403.

**`POST /replicate/receive`**: body is one record exactly as `changed_since()` /
`snapshot()` return it (full record, or a tombstone with `"deleted": true`).
- The sender's region comes from its **certificate CN**, never the body. A body
  `region_id` that differs from the CN gets 403, so AWS's cert can't publish
  Azure's or GCP's entries.
- The receiver calls `catalog.apply_replica(record, peer_region_id)`.
  Returns `{"status": "applied"}` or `{"status": "discarded"}` (old or duplicate
  version, which is a normal no-op). An invalid record gets 400.

**`GET /replicate/ping`**: returns `{"region_id", "boot_id", "time"}`. `boot_id` changes
whenever the agent restarts. The sender uses it to detect an empty, restarted
peer, and checks `region_id` to catch a misconfigured peer URL.

## Sender behaviour (every 2 s, per peer, independently)

1. Ping the peer. If it's unreachable, skip it and keep its cursor.
2. If this is the first contact, the peer's `boot_id` changed, or 30 s have passed
   since the last snapshot, send `snapshot()["records"]` and take the snapshot's cursor.
3. Otherwise send `changed_since(cursor)`.
4. Advance the peer's cursor **only if every record was acknowledged**. On any
   failure the whole batch is retried next cycle, so a partitioned peer catches
   up with the latest state when it reconnects.

## Logs for the Test Harness

Each applied change logs one JSON line on logger `replication`:
```json
{"event": "replication_applied", "receiver": "gcp-asia-south1", "service_name": "payments-api",
 "region_id": "aws-ap-south-1", "status": "DOWN", "version": 1791054002.0,
 "applied_at": 1791054003.9, "lag_ms": 1900.0}
```
`status` is `"DELETED"` for tombstones. `lag_ms` compares the owner's clock with
the receiver's clock, so keep the VMs NTP-synced (the cloud default).

## Security pieces

- `certs/gen-certs.sh`: team CA and one cert per agent (CN = region_id, SAN = public IP).
  Only the GCP owner keeps `ca.key`.
- `proxy/nginx.conf.template`: shared public reverse proxy (TLS and rate limiting) for
  the services. Replication does not go through it: 9443 is served directly with
  mTLS and firewalled to the two peer IPs.

## Testing

```bash
python -m pytest -q                                   # unit tests (storage + replication)
bash certs/gen-certs.sh certs/local aws-ap-south-1=127.0.0.1 azure-indiasouthcentral=127.0.0.1 gcp-asia-south1=127.0.0.1
python scripts/local_three_agents.py                  # 3 agents on localhost, real mTLS
```
The local script also needs `certs/rogue/rogue.{crt,key}` (any self-signed cert
for 127.0.0.1) for its "rogue cert refused" check.

`agent/replication/dev/devagent.py` is a stand-in agent with a debug API, used
until the Registry & API module exists. Delete it after assembly (step 2.6).
