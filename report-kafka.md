# Cross-cloud replication of payments and stock with Kafka

## Requirement

A payment taken on one cloud, or a change to stock, must be reflected on the
other two clouds. For example, an order paid on Azure must appear in the AWS and
GCP ledgers, and every cloud must agree on how much stock is left.

The service registry already replicates *where services are* between the
clouds (mTLS, version-based last-write-wins). Payments and stock are different:
they are business records that must never be lost or double-counted, and stock
must never be oversold.

## Design

```
payments-api / inventory-check ──write──▶ ledger.db (SQLite: record + outbox row, one transaction)
                                              │
                        registry-replicator ──┤ publish outbox ──▶ Confluent Cloud Kafka
                                              ◀── apply every cloud's events ──┘
```

Each VM runs a small SQLite ledger shared by `payments-api`,
`inventory-check` and a new `registry-replicator` process.

1. **Local first (transactional outbox).** An approved payment or a stock
   reservation is written to the local ledger together with an outbox row, in
   one transaction, and the API returns immediately. Checkout never waits for
   Kafka, so a Kafka outage cannot stop sales.
2. **Publish.** The replicator sends unsent outbox rows to Kafka with an
   idempotent producer (`acks=all`). A row is marked sent only after Kafka
   confirms it, so a crash or outage causes a retry, never a loss.
3. **Consume and apply.** Each cloud's replicator reads both topics from the
   beginning in its own consumer group and applies every event to its ledger.
   The read position is saved only after an event is applied.
4. **Idempotent, order-independent events.** Every event has an `event_id`
   (primary key), so a duplicate delivery is ignored. Events are facts that
   commute ("payment X happened", "N units left region R's share"), so the
   result does not depend on delivery order. A malformed message is logged and
   skipped instead of blocking the topic.

| Topic | Events |
|---|---|
| `payments` | `payment_authorized` |
| `inventory-events` | `stock_reserved`, `stock_transferred` |

Both topics have 1 partition and infinite retention, so a VM that loses its
database rebuilds it by replaying the topics.

### Stock: per-cloud allocation

If every cloud sold from one shared stock count, two clouds could sell the last
unit at the same moment, before either heard about the other's sale. We avoid
this with **per-cloud allocation** (escrow). Each product's stock is split
between the three clouds (e.g. 8 cameras → 3/3/2), and a cloud sells only from
its own share, checked inside the same local transaction as the sale. Since no
cloud can spend another's share, the clouds together can never sell more than
exists, even while cut off from Kafka. Every sale is still replicated, so all
clouds see the true global stock.

When one cloud's share runs out, the storefront asks the next cloud. Share can
be moved between clouds with a `stock_transferred` event (`POST
/inventory-check/stock/transfer`), which only the giving cloud can issue.

### Why managed Kafka on Confluent Cloud

| Option | Decision |
|---|---|
| **Confluent Cloud, one Basic cluster (Mumbai)** | **Chosen.** Real Kafka with no VM changes; covered by the free trial; ~1.5 days of work |
| Kafka on every cloud + MirrorMaker 2 | The production-grade design, but needs ~4 GB VMs on all clouds (ours have 1 GB) and ~5–6 days |
| One self-hosted Kafka on one VM | Needs a bigger VM, and we would have to run and secure it ourselves |
| Azure Event Hubs / AWS MSK / GCP Managed Kafka | Paid tiers only |
| Shared stock + cancel/refund on conflicts | Can briefly oversell; harder to test and demo |

### Security

- Kafka traffic is TLS-encrypted (SASL_SSL) and authenticated with one API key
  per cloud, each bound to its own service account.
- Each service account has only 7 ACLs: READ/WRITE/DESCRIBE on the two topics
  and READ on consumer groups prefixed `replicator-`. A leaked key can be
  revoked without affecting the other clouds.
- Secrets live only in `/etc/registry/replicator.env` (root-only, mode 600)
  on each VM; never in the repository.

## Results (2026-11-14)

Measured from a laptop on the tailnet through each region's proxy
(`python -m harness.kafka_experiments`).

**Replication lag** — 15 rounds × 3 origin clouds, every event checked on both
other clouds:

| Measure | n | min | median | p95 | max |
|---|---|---|---|---|---|
| Payment, end-to-end | 90 | 346 ms | 466 ms | 556 ms | 698 ms |
| Payment, receiver-reported | 90 | 307 ms | 400 ms | 463 ms | 476 ms |
| Stock reservation, end-to-end | 18 | 290 ms | 362 ms | 544 ms | 568 ms |

Every route had a median of 441–528 ms. **0 of 108 deliveries were lost.**
End-to-end time runs from the write returning on the origin cloud to the event
being visible on the peer (it includes polling and the proxy round trip).
Most of the ~0.4 s is the replicator loop, which waits up to 0.5 s for incoming
messages between publishing rounds, not the network.

**Kafka link outage** — GCP's replicator stopped, 5 orders placed on GCP:

- all 5 orders **approved**; checkout does not depend on Kafka;
- GCP held them as 5 unsent events; AWS and Azure saw none;
- after the replicator restarted, all 5 appeared on AWS in **1.7 s** and on
  Azure in **1.8 s** (including the restart and Kafka reconnect); nothing was
  left unsent.

**Stock failover (live demo)** — GCP sold its 2 cameras; the third order was
refused by GCP ("share sold out") and served by AWS from its own share, and all
clouds showed the same remaining total.

**For context: how fast the registry reroutes around a failed service**
(Phase 5 convergence run, 2026-10-15, measured from the GCP VM; 10 trials per
cloud, 30 in total, none missed; pooled across the three clouds from the run's
raw data):

| From a service failing until… | n | median | p95 | max |
|---|---|---|---|---|
| its own registry marks it DOWN (detection; health checks every 5 s) | 30 | 3.1 s | 4.8 s | 5.0 s |
| the other two registries also show DOWN (registry replication) | 30 | 1.0 s | 2.0 s | 2.0 s |
| **all three registries show DOWN (total)** | 30 | **4.3 s** | 6.3 s | 6.9 s |
| a recovered service is UP everywhere again (total) | 30 | 4.2 s | 6.3 s | 6.3 s |

The two mechanisms are complementary. The registry decides **where** a
payment can be taken: within about 4 s every registry lists a failed
payments-api as DOWN, so callers skip it; before that, a call to it fails and
the caller retries the next cloud. Kafka makes sure the payment, wherever it
was taken, reaches the other clouds' ledgers (about 0.5 s).

## Limitations

- **Single Kafka cluster.** Confluent Cloud Basic is one cluster in one
  region. If it is down, sales continue but replication pauses until it returns
  (shown by the outage test). The production design would be a Kafka cluster per
  cloud linked with MirrorMaker 2.
- **Shares are not rebalanced automatically.** A cloud can run out while others
  still have stock; the storefront then fails over to another cloud, and share
  can be moved manually with a transfer.
- **Trust between clouds.** Any cloud's key can publish events, so a
  compromised cloud could publish false events for another. Per-cloud keys
  limit the damage to revoking one key; signing events would remove it.
- **Lag uses VM clocks.** Receiver-reported lag depends on NTP-synced VM
  clocks; the end-to-end figures use only the laptop's clock.
- **Reservation before payment.** Checkout reserves stock before taking
  payment; if payment then fails everywhere, the reserved units are not returned
  (unchanged from the original checkout flow).
