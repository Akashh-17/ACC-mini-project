# Cross-Cloud Service Discovery Registry

Azure Catalog & Storage module for the team's shared registry agent.
Python 3.10+, standard library only.

Implemented: set, get, list, delete, changed_since, sweepTTL; atomic peer merge,
owner snapshots and deletion tombstones for safe integration.
Registry/API, networking and replication transport are separate team components.

## Run from VS Code

Open this repository folder directly:
C:\Users\Abhishek\OneDrive\Desktop\ACC-mini-project

Files saved in this folder appear in VS Code immediately. Do not export another
copy if you want to see ongoing edits.

Open Terminal > New Terminal, ensure you are at the repository root, then run:

```powershell
python -m unittest discover -s tests -v
python -m examples.storage_demo
```

If python is unavailable, choose an installed Python 3.10+ interpreter in
VS Code. On the machine where this was implemented, the verified runtime is:

```powershell
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest discover -s tests -v
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m examples.storage_demo
```

No pip installation, cloud account, credentials or network connection is needed.

## Files

- agent/storage/catalog.py: storage implementation.
- tests/test_storage.py: behavior and concurrency tests.
- examples/storage_demo.py: three in-memory agents demonstrating integration.
- interfaces.md: the exact contract to share with the AWS/GCP owner.

## Minimal use

```python
from agent.storage import CatalogStorage
import time

catalog = CatalogStorage("azure-indiasouthcentral")
catalog.set("payments-api", catalog.region_id, {
    "endpoint": "10.1.0.4:8081",
    "health_check_path": "/health",
    "tier": "critical",
    "status": "UP",
    "timestamp": time.time(),
})
print(catalog.list("payments-api"))
```

For integration, read interfaces.md first. Particularly: timestamp measures
health freshness; version orders writes; updated_at is only a local cursor.
Incoming peer writes use apply_replica(), not set(). Deletes replicate tombstones.

Run three-agent local integration before cloud deployment. API and replication
owners should share ONE CatalogStorage instance in each agent process. They are
responsible for scheduling health checks, TTL sweeps and network synchronization.
Never commit shared secrets or private keys.

## Validation

26 automated tests cover registration, separate regional records, local-first
ordering, defensive copying, TTL boundary/DOWN handling, monotonic cursors,
deletion propagation, replay rejection, missed updates, receiver restart,
ownership checks, malformed data, concurrent writes/replica delivery,
and health checks completing after deletion or replacement.
The demo uses function calls, not real HTTP or mTLS.

