# Registry & API module (AWS owner)

Implements step 2.2 using Azure's `CatalogStorage`. The same module runs in every
region. `python -m agent.main` creates one catalog shared by this API, real health
checks, the TTL sweep and (when configured) GCP replication.

## Run locally before Ryan's services are ready

From the inner repository folder, create a Python 3.10+ environment and install
the existing requirements:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:REGION_ID = "aws-ap-south-1"
$env:REGISTRY_SECRET = "choose-a-private-shared-secret"
$env:REPLICATION_PEERS = ""
.\.venv\Scripts\python.exe -m agent.main
```

The registry listens only on `127.0.0.1:9000`. `REGISTRY_PORT` can change the port
for three-agent local testing. Replication is disabled when `REPLICATION_PEERS`
is empty; configuring peers enables the existing GCP mTLS module and requires
`TLS_CERT`, `TLS_KEY`, `TLS_CA` and the peer addresses from its run guide.

For local integration only, `REGISTRY_ALLOW_LOOPBACK=true` permits literal
loopback service addresses such as `127.0.0.1:8081`. The default is false:
registration requires a private IPv4 (RFC1918) or IPv6 (ULA) address and a port
from 1 through 65535. Hostnames, public IPs and link-local addresses are rejected.
An IPv6 endpoint must be written as `[fd00::1]:8081`.

## APIs (tasks.md contract)

`POST /register` requires `Authorization: Bearer <REGISTRY_SECRET>`:

```json
{
  "service_name": "payments-api",
  "region_id": "aws-ap-south-1",
  "endpoint": "10.0.1.10:8081",
  "health_check_path": "/health",
  "tier": "critical"
}
```

Returns `200 {"status":"registered"}`. The region must match this agent.
New records start STALE with timestamp 0 until a successful probe, as allowed
by `interfaces.md`. Re-registering replaces that region's record.

`POST /deregister` requires the same bearer header and
`{"service_name", "region_id", "endpoint"}`. Returns
`200 {"status":"deregistered"}`; repeating a deletion is harmless. If the
submitted endpoint belongs to an old registration, the new endpoint is preserved.
Active removal is immediate; Azure storage retains its replication tombstone.

Bad/missing write authentication returns 401. Missing/invalid fields, an invalid
endpoint, or the wrong region returns 400. Authentication is checked before body
parsing. No registry secret or submitted body is included in validation logs.

`GET /lookup?service_name=payments-api` requires no bearer header and returns:

```json
{
  "service_name": "payments-api",
  "endpoints": [
    {"region_id": "aws-ap-south-1", "endpoint": "10.0.1.10:8081", "status": "UP"}
  ]
}
```

The local region is first. This is the all-status/diagnostic response specified
in `tasks.md`: DOWN and STALE entries remain visible; callers select UP entries
for use. Storage-only fields (version, timestamp, tier, deleted) are not exposed.
Unknown services return an empty endpoints list. Missing/blank names return 400.

## Health and storage integration

- Probe only this region's active entries every 5 seconds, concurrently.
- GET the registered health path with a 2-second total request deadline.
- A 2xx response writes UP and refreshes the successful-health timestamp.
- A failure, timeout, or redirect writes DOWN without refreshing that timestamp.
- Never follow redirects away from the registered target.
- Pass the observed version to storage so a late probe cannot restore a deleted
  entry or overwrite a replacement registration.
- Run the storage TTL sweep independently every 5 seconds (15-second TTL).
- Use one API worker and one shared catalog. No external database is added.

## AWS deployment handoff

Use `deploy/aws-agent.env.example` for the existing systemd unit and
`proxy/aws.env` with GCP's existing proxy renderer. Fill actual secrets, IPs and
certificate paths privately; do not commit them. The supplied systemd command
`python -m agent.main` now has an entry point.

AWS runs payments-api (8081), fraud-check (8082), inventory-check (8083) and
recommend-api (8084) once Ryan provides the stubs. This module does not implement
their business logic or provision EC2 resources.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The registry tests use the real Azure storage and mocked health HTTP responses.
They cover authentication, validation, response shape, regional ownership,
failure/recovery, total timeout, deletion/replacement during a probe, TTL and
application startup/shutdown. The final step 2.7 still needs Ryan's services.

Framework lifecycle and test transport references:
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/) and
[HTTPX transports](https://www.python-httpx.org/advanced/transports/).
