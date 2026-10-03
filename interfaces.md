# Storage integration contract

Status: implemented Azure storage proposal, ready for AWS/GCP owners to review.
Python 3.10+; no framework or external package is required by storage.
The repository was empty when this module was built. Language, framework,
ports and the complete Phase 0 agreement still require team confirmation.

## Shared instance

Create exactly one catalog per agent process and inject that same instance into
the registry/API, health scheduler and replication worker:

```python
import os
from agent.storage import CatalogStorage

catalog = CatalogStorage(region_id=os.environ["REGION_ID"], ttl_seconds=15)
```

Region identifiers:
- AWS: aws-ap-south-1
- Azure: azure-indiasouthcentral
- GCP: gcp-asia-south1

## Records and keys

Key: (service_name, region_id). One endpoint per service per region.
Multiple instances of the same service in a region require a future instance_id
key extension; registering again replaces that region's existing endpoint.

Active record returned by storage:
```json
{
  "service_name": "payments-api",
  "region_id": "azure-indiasouthcentral",
  "endpoint": "10.1.0.4:8081",
  "health_check_path": "/health",
  "tier": "critical",
  "status": "UP",
  "timestamp": 1791054000.0,
  "version": 1791054001.0,
  "updated_at": 1791054001.0,
  "deleted": false
}
```

Times are Unix seconds:
- timestamp: last successful owner health check; used for TTL, not LWW.
- version: owner write time, strictly increasing per key within a process even
  for identical wall-clock readings or clock rollback. Used for LWW.
- updated_at: strictly increasing local cursor. Used only by changed_since.
  Receiver generates its own updated_at; never compares it to peer cursors.

This deliberately corrects execution-order.md 0.6: last-success time cannot
order a later DOWN update. Every owner write, including DOWN and deletion, gets
a new version. All JSON participants must preserve numeric precision; do not
round versions or cursors to whole seconds.

## API

| Method | Returns | Behavior |
| --- | --- | --- |
| set(service_name: str, region_id: str, data: dict, *, expected_version: float or None = None) | dict or None | Local-owner insert/update; optional atomic version guard |
| get(service_name: str, region_id: str) | dict or None | Active entry only |
| list(service_name: str) | list[dict] | All statuses, local region first, others sorted by region |
| delete(service_name: str, region_id: str, endpoint: str or None = None) | bool | Local deletion; optional endpoint guard; retains tombstone |
| changed_since(ts: float) | list[dict] | Latest local-owner changes with updated_at > ts, including deletions |
| sweepTTL() | list[dict] | Mark age > 15 seconds STALE; return only newly changed entries |
| snapshot() | dict | Atomic owner records/tombstones plus cursor |
| apply_replica(data: dict, peer_region_id: str) | bool | Atomic newer-version comparison and apply; False for old/duplicate |

set data must include endpoint, health_check_path, tier, status, timestamp.
Additional keys are ignored. Invalid data raises ValueError without applying.
Private-IP/port validation and HTTP error mapping belong to the Registry/API.
Storage validates record fields, status, tier, numeric times and ownership.

All records returned are copies. Changing them cannot change the catalog.
Calls are thread-safe within one process. Do not use separate catalog instances
or multi-process API workers for a single logical agent.

## AWS owner: register, lookup, health checks, deregister

- Authenticate writes and enforce that region_id equals this agent's region.
- Validate endpoint is an allowed private address and service port.
- New registrations can use status=STALE, timestamp=0 until first successful check.
- Health-check only local entries every 5 seconds; use 2-second request timeout.
- Successful check: set status=UP, timestamp=time.time().
- Failed check: set status=DOWN, keep the previous successful timestamp.
- For a completed health probe, pass expected_version=observed_record["version"]
  to set(). None means the service changed or was deregistered while the probe
  was in flight: discard that result and check the current record next cycle.
  This prevents late probes from resurrecting deleted/replaced services.
- Schedule catalog.sweepTTL() every ~5 seconds in the assembled agent.
- list returns diagnostic records including DOWN/STALE. Your lookup route must
  filter to UP for usable endpoints or document a separate diagnostic response.
- For deregistration pass the submitted endpoint to delete. A mismatch returns
  False so a delayed deregistration cannot remove a replacement instance.
- list(service_name) gives local-first ordering but does not perform HTTP calls.

## GCP owner: replication and reconnect

- Every 2 seconds call changed_since(peer_cursor) independently for each peer.
- Send complete records, including version, tier and health_check_path.
- Maintain a separate acknowledged cursor for each peer.
- Advance a peer cursor to max(sent updated_at) ONLY after the whole batch has
  been acknowledged successfully. On failure keep its cursor and retry.
- changed_since returns latest state per key, not every historical transition.
  This is state replication, not an audit/event log.
- Receiver calls apply_replica(record, peer_region_id). Derive peer_region_id
  from the authenticated certificate mapping, never the HTTP body.
- False from apply_replica is an idempotent no-op, not a transport failure.
- Replicate only owner records. Storage already excludes remote records.
- On connection/reconnection and receiver restart, send snapshot()["records"].
  After successful acknowledgement, use that snapshot's cursor for the peer.
  Send later deltas; writes occurring during transmission are not lost.
- Send snapshot periodically as an additional recovery measure if receiver
  restarts are not explicitly signalled by your transport protocol.

Deletion payload:
```json
{
  "service_name": "payments-api",
  "region_id": "azure-indiasouthcentral",
  "version": 1791054002.0,
  "updated_at": 1791054002.0,
  "deleted": true
}
```

Tombstones stay in memory to prevent a delayed old registration from resurrecting
a deleted service. A newer owner registration can replace a tombstone.

## TTL and partitions

DOWN is never changed to STALE by TTL. UP becomes STALE only when age exceeds
TTL; exactly 15 seconds remains UP until the next sweep.
Owner-local expiry publishes a newer STALE version. Remote expiry changes only
the observer's local status; it is not republished and does not increment the
owner version. A subsequent owner health update restores UP.

Peer records use owner's last-success time. Clock skew can therefore affect
expiry; synchronize VM clocks and document this limitation. A future-dated owner
clock can delay expiry. This prototype does not guarantee a hard freshness bound
under arbitrary clock skew.

## Known limits and remaining integration work

- In-memory only: owner restart loses records, tombstones and version history.
  Services must re-register; peers reconcile from snapshots. If clocks regress
  across owner restarts, peers can reject versions until time catches up.
  Durable version epochs are a future enhancement, not implemented here.
- Latest change/tombstone per key is retained with no pruning. Memory grows with
  distinct registered keys; appropriate for the bounded prototype.
- Owner-only writes intentionally replace the older documents' multi-writer
  health model. Same-endpoint concurrency tests should replay/reorder one
  owner's versions, not let unrelated regions claim authority over its health.
- No HTTP server, authentication, mTLS, background scheduler, cloud deployment
  or networking is implemented by this module.
- The replication payload and apply_replica interface are contract additions.
  Your friend must adopt them when integrating; the original timestamp-only
  payload is not compatible.

