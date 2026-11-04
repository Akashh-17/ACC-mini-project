# Decision: replicate payments and stock across clouds with Kafka

**Requirement:** a payment (or stock change) made on one cloud must be reflected on the
other two.

**Decision:** use **managed Kafka on Confluent Cloud** (one Basic cluster in a Mumbai
region). Each cloud publishes its own events and runs a small consumer that applies
everyone's events to its local copy.

**Why this option**
- Real Kafka at ~₹0 for our traffic (first cluster unit free, cents per GB).
- **No VM changes:** the Kafka client adds ~50–80 MB; our 1 GB VMs can't host Kafka itself.
- ~1.5 days of work vs ~5–6 days for self-hosted Kafka on every cloud.
- Lowest risk to the system that already works.

**Rule that keeps us highly available:** payments and stock updates are **saved locally
first and published to Kafka afterwards** (local outbox, retried until sent). If Kafka is
down, checkout still works; other clouds just see the change later. Consumers ignore
duplicates, so retries are safe.

**Stock (proposed):** **per-cloud allocation**. Total stock is split between the clouds,
each cloud sells only from its own share (so it can never oversell), and every sale is
replicated so all clouds see the true global stock. Stock can be moved between clouds
with an event.

**Alternatives considered (for the report)**
| Option | Why not now |
|---|---|
| Kafka on every cloud + MirrorMaker 2 | The production-grade design, but needs ~4 GB VMs on all clouds and ~5–6 days |
| One self-hosted Kafka on one VM | Needs a bigger VM, and we'd have to run and secure it ourselves |
| Azure Event Hubs / AWS MSK / GCP Managed Kafka | Paid tiers only (~₹2,000–20,000+/month each) |
| Shared stock + cancel/refund on conflicts | Can briefly oversell; harder to test and demo |

**What each person does**
- **Ishan:** create the Confluent account, cluster, topics `payments` and
  `inventory-events`, one API key per cloud; write the code, tests and setup guide.
- **Akash, Abhishek:** ~30 min each to deploy on your VM (pull, install `confluent-kafka`,
  add your API key to the env file, start one new service). Steps will be in the repo.
- **Ryan:** new demo step: pay on Azure, see it appear in the AWS and GCP ledgers;
  measure replication time.

**Timeline:** day 1, account + code; day 2, everyone deploys, then verification and dry run.

**Please confirm by replying:** OK with Confluent Cloud, OK with per-cloud stock
allocation, and when you're free for the ~30 min deploy.
