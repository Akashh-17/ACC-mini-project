# Kafka replication of payments and stock (Confluent Cloud)

A payment or stock change made on one cloud shows up on the other two.

```
payments-api / inventory-check ──write──▶ ledger.db (SQLite, + outbox row, one transaction)
                                              │
                         registry-replicator ─┤ publish outbox ──▶ Confluent Cloud ──▶ consume ─┐
                                              ◀──────── apply every cloud's events ─────────────┘
```

- **Local first.** `/authorize` and `/reserve` commit to the VM's own ledger and
  return; they never wait for Kafka. If Kafka is unreachable, changes queue in
  the outbox and are sent when it is back.
- **Idempotent.** Every event has an `event_id`; applying it twice does nothing.
  Events commute, so order does not matter either.
- **Per-cloud stock allocation.** Each product's stock is split between the
  three clouds (`services/catalog.py: allocation()`). A cloud sells only from its
  own share, so the clouds can never oversell together, even while cut off from
  Kafka. The storefront tries another cloud when one's share is sold out, and
  `POST /inventory-check/stock/transfer` moves share between clouds.

Topics: `payments` (`payment_authorized`), `inventory-events`
(`stock_reserved`, `stock_transferred`). Event format: `services/ledger.py`.

## 1. Confluent Cloud (Ishan, once)

1. Sign up at confluent.cloud and create an environment `crosscloud`.
2. Create a **Basic** cluster in a Mumbai region (GCP `asia-south1`).
   Copy the **bootstrap server** (Cluster settings → Endpoints).
3. Topics → create `payments` and `inventory-events`, 1 partition each.
   In each topic's settings set retention to **Infinite**: a new or wiped
   VM rebuilds its ledger by reading the topics from the beginning.
4. One service account and API key **per cloud**, so a leaked key can be
   revoked without touching the others:
   - Accounts → Service accounts → add `replicator-aws`, `replicator-azure`,
     `replicator-gcp`.
   - For each: ACLs: topics `payments` and `inventory-events` → READ, WRITE,
     DESCRIBE; consumer group prefix `replicator-` → READ; cluster →
     IDEMPOTENT_WRITE.
   - API keys → add key → granular access → that service account. Download
     the key file and send it to the owner of that cloud **privately** (not
     in the group chat, never in the repo).

## 2. Each VM (Akash: AWS, Abhishek: Azure, Ishan: GCP), ~30 min

```bash
cd <repo folder> && git pull
.venv/bin/pip install -r services/requirements-kafka.txt

# Shared ledger file for payments-api, inventory-check and the replicator
sudo install -d -o registry -g registry -m 750 /var/lib/registry
echo 'LEDGER_PATH=/var/lib/registry/ledger.db' | sudo tee -a \
    /etc/registry/services/payments-api.env /etc/registry/services/inventory-check.env

# Replicator settings: your REGION_ID, bootstrap server, your own API key
sudo install -m 600 deploy/replicator.env.example /etc/registry/replicator.env
sudo nano /etc/registry/replicator.env

sudo cp deploy/registry-replicator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart registry-service@payments-api registry-service@inventory-check
sudo systemctl enable --now registry-replicator
```

`REGION_ID` must be exactly `aws-ap-south-1`, `azure-indiasouthcentral` or
`gcp-asia-south1`. If the repo is not at `/opt/registry`, use the same path
the other units use.

### Check it

```bash
journalctl -u registry-replicator -n 20 --no-pager   # replicator_started, no "UNREACHABLE"
curl -s localhost:8081/ledger | python3 -m json.tool  # payments from all clouds
curl -s localhost:8083/stock  | python3 -m json.tool  # local_share and global_stock
```

Make a test payment on your VM and watch another cloud's log:

```bash
curl -s -X POST localhost:8081/authorize -H 'content-type: application/json' \
  -d '{"order_id":"kafka-test","amount":10,"currency":"INR","card_token":"tok"}'
# on another VM:
journalctl -u registry-replicator -f | grep kafka_applied   # shows lag_ms
```

### Problems

| Symptom | Cause |
|---|---|
| `kafka UNREACHABLE: ... Authentication failed` | Wrong key/secret, or the key belongs to another cluster |
| `... TOPIC_AUTHORIZATION_FAILED` / `GROUP_AUTHORIZATION_FAILED` | ACLs from step 1.4 missing |
| `ModuleNotFoundError: confluent_kafka` | `pip install` ran in a different venv than the unit uses |
| `unable to open database file` | `/var/lib/registry` missing or not owned by `registry` |
| `/ledger` shows only local payments | Replicator not running on this VM or the other one |

## 3. Demo and measurement (Ryan)

- Storefront → "Payments and stock replicated through Kafka" shows each
  cloud's own copy. Fail payments on GCP and AWS, place an order (it is paid on
  Azure), and watch it appear in the AWS and GCP copies with its lag.
- Replication time: `journalctl -u registry-replicator | grep kafka_applied`
  on each VM; `lag_ms` is the time from the payment to it being applied there
  (depends on VM clocks, which all sync with NTP).
- Kafka outage: stop `registry-replicator` on one VM, place orders there
  (they succeed, `/ledger` shows `unsent_events`), start it again and watch
  them arrive on the others.

## Cleanup after grading

Delete the Confluent cluster (or the whole environment) and the API keys.
