# Managed cloud services: plan for the demo (+2 days)

**Why:** the demo must show real managed cloud services (storage, databases, messaging),
not only VMs. We add one per cloud, three different service types (messaging, object storage,
NoSQL database), placed so they match our availability design.

## The rule we follow (put this in the report)
- **Critical services** (payments, fraud-check, inventory-check) run on all 3 clouds.
  They must **not** depend on any single cloud's managed service, or that cloud becomes a
  single point of failure. If they ever use one, it must be one per region, on that
  region's own cloud.
- **Standard services** are single-cloud by design (recommend = AWS, admin = Azure,
  notify = GCP). Each is backed by **its own provider's** managed service. If that service
  fails, the API reports itself unhealthy, the registry marks it DOWN, and clients see it as
  unavailable: the same behaviour as losing that cloud. No new risk.

## Part A: one managed service per cloud (doing this first)

| | GCP (Ishan) | AWS (Akash) | Azure (Abhishek) |
|---|---|---|---|
| API | notify-api | recommend-api | admin-api |
| Managed service | **Cloud Pub/Sub** (messaging) | **Amazon S3** (object storage) | **Azure Table Storage** (NoSQL database) |
| What changes | Order confirmations are **published** to topic `order-events` (subscription `order-events-audit` to show them) | Recommendations **read from** `recommendations.json` in an S3 bucket | Stock **read/written** in table `inventory`, survives restarts |
| Access (no keys anywhere) | Service account `notify-publisher`, publish-only, attached to the VM | IAM role on the EC2 instance, read-only on that bucket | VM managed identity with *Storage Table Data Contributor* on that storage account |
| Extra Python packages | none | `boto3` | `azure-data-tables`, `azure-identity` |
| Cost | free tier | free tier (5 GB) | a few rupees/month |

**How the code works (Ishan writes it, shared repo):**
- `services/backends.py`: each capability has a **memory** version (today's behaviour) and
  a **cloud** version, chosen by env var on your VM:
  `NOTIFY_BACKEND=pubsub`, `RECOMMEND_BACKEND=s3`, `ADMIN_BACKEND=azuretable`.
- **Default = memory**, so pulling the code changes nothing until you switch it on.
- `/health` checks the managed service every 10 s. If it's unreachable, health = 503, and the
  registry marks that API DOWN.
- `services/seed.py` uploads the S3 JSON file and fills the Table Storage table from the shared catalog.
- Storefront shows: "Published to Pub/Sub (message id …)", "Recommendations from
  S3", and an "Admin stock (Azure Table Storage)" panel.

**What each owner does (step-by-step guides will be in the repo):**
- **Akash:** `infra/aws/s3-setup.md`: create the bucket + IAM role, attach the role to
  EC2, run the seed, set `RECOMMEND_BACKEND=s3`, restart recommend-api.
- **Abhishek:** `infra/azure/table-setup.md`: create the storage account + table, enable
  the VM managed identity, grant the role, run the seed, set `ADMIN_BACKEND=azuretable`,
  restart admin-api.
- **Ishan:** Pub/Sub topic, subscription and service account. The GCP VM is stopped for
  about 1 min to attach the service account, so expect GCP entries to show STALE briefly.
- **Ryan:** the demo script gets 3 new talking points (one per managed service). No
  harness changes needed.

**Timeline:** code + guides first → each owner sets up their service (~30–45 min each) →
verification → storefront dry run.

## Part B: only if time allows
Registry **snapshots in object storage on all 3 clouds** (GCS / S3 / Azure Blob): each
agent saves its own catalog to its own cloud's bucket and restores it after a restart.
This fixes the "restart loses all registrations" limitation. Replicated by design (each
region uses only its own cloud). It touches the shared agent, so it gets its own short
design before any code.

## Please confirm
1. **Akash:** OK with S3 for recommend-api? Can you create IAM roles in your account?
2. **Abhishek:** OK with Table Storage (cheapest), or do you prefer Cosmos DB (free tier,
   more setup)? Can you assign roles / enable managed identity on your VM?
3. **Everyone:** what time are you free to do your setup (~45 min)?
