"""Three logical regions in memory; no network or cloud credentials needed."""
from agent.storage import CatalogStorage

AZ = "azure-indiasouthcentral"
AWS = "aws-ap-south-1"
GCP = "gcp-asia-south1"


def main():
    now = [1000.0]
    owners = {region: CatalogStorage(region, clock=lambda: now[0])
              for region in (AZ, AWS, GCP)}

    def record(endpoint, tier="critical"):
        return dict(endpoint=endpoint, health_check_path="/health", tier=tier,
                    status="UP", timestamp=now[0])

    def sync():
        for source in owners.values():
            for target in owners.values():
                if source is not target:
                    for change in source.snapshot()["records"]:
                        target.apply_replica(change, source.region_id)

    def show(label):
        entries = owners[AZ].list("payments-api")
        print(label)
        print("  Azure sees:", ", ".join(f'{r["region_id"]}={r["status"]}' for r in entries))

    for i, (region, owner) in enumerate(owners.items(), 1):
        owner.set("payments-api", region, record(f"10.{i}.0.4:8081"))
    owners[AZ].set("admin-api", AZ, record("10.1.0.4:8084", "standard"))
    sync()
    show("1. Three critical endpoints merge; Azure is returned first.")
    assert len(owners[AZ].list("payments-api")) == 3

    now[0] += 5
    failed = owners[GCP].get("payments-api", GCP)
    failed["status"] = "DOWN"
    owners[GCP].set("payments-api", GCP, failed)
    sync()
    show("2. GCP publishes DOWN without refreshing last successful health time.")
    assert owners[AZ].get("payments-api", GCP)["status"] == "DOWN"

    owners[AZ].delete("admin-api", AZ)
    sync()
    print("3. Azure admin-api deletion reaches both peers.")
    assert all(not s.list("admin-api") for s in owners.values())

    # No synchronization occurs in this interval (simulated partition).
    now[0] += 16
    for owner in owners.values():
        local = owner.get("payments-api", owner.region_id)
        local["status"], local["timestamp"] = "UP", now[0]
        owner.set("payments-api", owner.region_id, local)
        owner.sweepTTL()
    show("4. During the partition Azure stays UP; remote AWS entry becomes STALE.")
    assert owners[AZ].get("payments-api", AZ)["status"] == "UP"
    assert owners[AZ].get("payments-api", AWS)["status"] == "STALE"

    sync()
    show("5. Reconnection applies current owner snapshots; all endpoints recover.")
    assert all(r["status"] == "UP" for r in owners[AZ].list("payments-api"))
    print("PASS: local storage and merge demonstration completed.")
    print("This demo does not test real HTTP, mTLS, VPN connectivity or request retries.")


if __name__ == "__main__":
    main()

