"""Behavior tests for Azure storage, including peer replay and partitions."""
from concurrent.futures import ThreadPoolExecutor
import unittest

from agent.storage import CatalogStorage

AZ = "azure-indiasouthcentral"
AWS = "aws-ap-south-1"
GCP = "gcp-asia-south1"


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


def data(timestamp=1000.0, status="UP"):
    return dict(endpoint="10.1.0.4:8081", health_check_path="/health",
                tier="critical", status=status, timestamp=timestamp)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = CatalogStorage(AZ, clock=self.clock)
        self.aws = CatalogStorage(AWS, clock=self.clock)

    def test_missing_records(self):
        self.assertIsNone(self.store.get("payments-api", AZ))
        self.assertEqual(self.store.list("payments-api"), [])
        self.assertFalse(self.store.delete("payments-api", AZ))

    def test_registration_keeps_complete_health_metadata(self):
        record = self.store.set("payments-api", AZ, data())
        self.assertEqual(record["health_check_path"], "/health")
        self.assertEqual(record["tier"], "critical")
        self.assertEqual(self.store.get("payments-api", AZ), record)

    def test_three_independent_regions_accumulate_local_first(self):
        gcp = CatalogStorage(GCP, clock=self.clock)
        for owner in (gcp, self.aws):
            record = owner.set("payments-api", owner.region_id, data())
            self.store.apply_replica(record, owner.region_id)
        self.store.set("payments-api", AZ, data())
        records = self.store.list("payments-api")
        self.assertEqual(records[0]["region_id"], AZ)
        self.assertEqual({r["region_id"] for r in records}, {AZ, AWS, GCP})

    def test_input_and_return_values_cannot_mutate_internal_state(self):
        original = data()
        result = self.store.set("payments-api", AZ, original)
        original["endpoint"] = "bad"
        result["status"] = "DOWN"
        read = self.store.get("payments-api", AZ)
        read["endpoint"] = "bad"
        listed = self.store.list("payments-api")
        listed[0]["tier"] = "standard"
        changes = self.store.changed_since(0)
        changes[0]["deleted"] = True
        snap = self.store.snapshot()
        snap["records"].clear()
        saved = self.store.get("payments-api", AZ)
        self.assertEqual(saved["endpoint"], "10.1.0.4:8081")
        self.assertEqual(saved["status"], "UP")
        self.assertEqual(saved["tier"], "critical")
        self.assertEqual(len(self.store.snapshot()["records"]), 1)

    def test_down_has_new_version_without_refreshing_success_timestamp(self):
        up = self.store.set("payments-api", AZ, data())
        self.clock.value += 5
        down = self.store.set("payments-api", AZ, data(status="DOWN"))
        self.assertEqual(down["timestamp"], up["timestamp"])
        self.assertGreater(down["version"], up["version"])

    def test_cursor_distinguishes_equal_clock_writes(self):
        a = self.store.set("payments-api", AZ, data())
        b = self.store.set("payments-api", AZ, data(status="DOWN"))
        self.assertGreater(b["version"], a["version"])
        self.assertGreater(b["updated_at"], a["updated_at"])
        self.assertEqual(self.store.changed_since(a["updated_at"]), [b])
        self.assertEqual(self.store.changed_since(b["updated_at"]), [])

    def test_owner_versions_and_cursors_survive_clock_rollback(self):
        a = self.store.set("payments-api", AZ, data())
        self.clock.value = 900
        b = self.store.set("payments-api", AZ, data(status="DOWN"))
        self.assertGreater(b["version"], a["version"])
        self.assertGreater(b["updated_at"], a["updated_at"])

    def test_ttl_boundary_then_stale_without_deletion(self):
        self.store.set("payments-api", AZ, data())
        self.clock.value += 15
        self.assertEqual(self.store.sweepTTL(), [])
        self.clock.value += 0.01
        changed = self.store.sweepTTL()
        self.assertEqual(changed[0]["status"], "STALE")
        self.assertEqual(self.store.get("payments-api", AZ)["status"], "STALE")
        self.assertEqual(self.store.sweepTTL(), [])

    def test_ttl_does_not_replace_down(self):
        self.store.set("payments-api", AZ, data(status="DOWN"))
        self.clock.value += 100
        self.assertEqual(self.store.sweepTTL(), [])
        self.assertEqual(self.store.get("payments-api", AZ)["status"], "DOWN")

    def test_remote_stale_is_not_republished(self):
        remote = self.aws.set("payments-api", AWS, data())
        self.store.apply_replica(remote, AWS)
        self.clock.value += 16
        self.store.sweepTTL()
        stale = self.store.get("payments-api", AWS)
        self.assertEqual(stale["status"], "STALE")
        self.assertEqual(stale["version"], remote["version"])
        self.assertEqual(self.store.changed_since(0), [])
        self.assertEqual(self.store.snapshot()["records"], [])

    def test_new_owner_health_recovers_remote_stale(self):
        old = self.aws.set("payments-api", AWS, data())
        self.store.apply_replica(old, AWS)
        self.clock.value += 16
        self.store.sweepTTL()
        fresh = self.aws.set("payments-api", AWS, data(self.clock.value))
        self.assertTrue(self.store.apply_replica(fresh, AWS))
        self.assertEqual(self.store.get("payments-api", AWS)["status"], "UP")

    def test_replication_rejects_older_and_duplicate_updates(self):
        old = self.aws.set("payments-api", AWS, data())
        self.clock.value += 5
        new = self.aws.set("payments-api", AWS, data(status="DOWN"))
        self.assertTrue(self.store.apply_replica(new, AWS))
        self.assertFalse(self.store.apply_replica(old, AWS))
        self.assertFalse(self.store.apply_replica(new, AWS))
        self.assertEqual(self.store.get("payments-api", AWS)["status"], "DOWN")

    def test_deregister_replicates_tombstone_and_blocks_resurrection(self):
        record = self.aws.set("payments-api", AWS, data())
        self.store.apply_replica(record, AWS)
        self.assertTrue(self.aws.delete("payments-api", AWS))
        deletion = self.aws.changed_since(record["updated_at"])[0]
        self.assertTrue(deletion["deleted"])
        self.assertTrue(self.store.apply_replica(deletion, AWS))
        self.assertIsNone(self.store.get("payments-api", AWS))
        self.assertFalse(self.store.apply_replica(record, AWS))
        self.assertFalse(self.store.apply_replica(deletion, AWS))

    def test_tombstone_arriving_before_registration_blocks_old_record(self):
        record = self.aws.set("payments-api", AWS, data())
        self.aws.delete("payments-api", AWS)
        deletion = self.aws.changed_since(0)[0]
        self.store.apply_replica(deletion, AWS)
        self.assertFalse(self.store.apply_replica(record, AWS))

    def test_reregister_after_delete_gets_newer_version(self):
        old = self.store.set("payments-api", AZ, data())
        self.store.delete("payments-api", AZ)
        deletion = self.store.changed_since(0)[0]
        new = self.store.set("payments-api", AZ, data())
        self.assertGreater(new["version"], deletion["version"])
        self.assertGreater(new["version"], old["version"])

    def test_endpoint_guard_prevents_delayed_deregister(self):
        self.store.set("payments-api", AZ, data())
        replacement = data()
        replacement["endpoint"] = "10.1.0.5:8081"
        self.store.set("payments-api", AZ, replacement)
        self.assertFalse(self.store.delete("payments-api", AZ, "10.1.0.4:8081"))
        self.assertTrue(self.store.delete("payments-api", AZ, "10.1.0.5:8081"))

    def test_missed_updates_remain_until_peer_acknowledgement(self):
        first = self.store.set("payments-api", AZ, data())
        cursor = first["updated_at"]
        self.aws.apply_replica(first, AZ)
        self.store.set("payments-api", AZ, data(status="DOWN"))
        self.store.set("admin-api", AZ, {**data(), "tier": "standard"})
        self.store.delete("admin-api", AZ)
        batch = self.store.changed_since(cursor)
        self.assertEqual(batch, self.store.changed_since(cursor))
        for record in batch:
            self.aws.apply_replica(record, AZ)
        cursor = max(r["updated_at"] for r in batch)
        self.assertEqual(self.aws.get("payments-api", AZ)["status"], "DOWN")
        self.assertIsNone(self.aws.get("admin-api", AZ))
        self.assertEqual(self.store.changed_since(cursor), [])

    def test_restart_receiver_rebuilds_from_owner_snapshot(self):
        self.store.set("payments-api", AZ, data())
        self.store.set("admin-api", AZ, data())
        self.store.delete("admin-api", AZ)
        receiver = CatalogStorage(AWS, clock=self.clock)
        snapshot = self.store.snapshot()
        for record in snapshot["records"]:
            receiver.apply_replica(record, AZ)
        self.assertIsNotNone(receiver.get("payments-api", AZ))
        self.assertIsNone(receiver.get("admin-api", AZ))
        self.assertEqual(self.store.changed_since(snapshot["cursor"]), [])

    def test_identity_mismatch_and_remote_local_writes_rejected(self):
        remote = self.aws.set("payments-api", AWS, data())
        with self.assertRaises(ValueError):
            self.store.apply_replica(remote, GCP)
        with self.assertRaises(ValueError):
            self.store.apply_replica({**remote, "region_id": AZ}, AZ)
        with self.assertRaises(ValueError):
            self.store.set("payments-api", AWS, data())
        with self.assertRaises(ValueError):
            self.store.delete("payments-api", AWS)

    def test_inflight_health_check_cannot_resurrect_deregistered_service(self):
        observed = self.store.set("payments-api", AZ, data())
        self.store.delete("payments-api", AZ)
        result = self.store.set("payments-api", AZ, data(),
                                expected_version=observed["version"])
        self.assertIsNone(result)
        self.assertIsNone(self.store.get("payments-api", AZ))
        self.assertTrue(self.store.changed_since(0)[0]["deleted"])

    def test_inflight_health_check_cannot_replace_newer_registration(self):
        observed = self.store.set("payments-api", AZ, data())
        replacement = {**data(), "endpoint": "10.1.0.5:8081"}
        newest = self.store.set("payments-api", AZ, replacement)
        result = self.store.set("payments-api", AZ, data(status="DOWN"),
                                expected_version=observed["version"])
        self.assertIsNone(result)
        self.assertEqual(self.store.get("payments-api", AZ), newest)

    def test_matching_health_version_applies_update(self):
        observed = self.store.set("payments-api", AZ, data())
        updated = self.store.set("payments-api", AZ, data(status="DOWN"),
                                 expected_version=observed["version"])
        self.assertEqual(updated["status"], "DOWN")
        self.assertGreater(updated["version"], observed["version"])

    def test_invalid_data_does_not_modify_existing_record(self):
        original = self.store.set("payments-api", AZ, data())
        invalid = [
            None, {}, {**data(), "status": "BROKEN"},
            {**data(), "tier": "unknown"}, {**data(), "timestamp": float("nan")},
            {**data(), "timestamp": float("inf")}, {**data(), "timestamp": True},
            {**data(), "timestamp": -1}, {**data(), "health_check_path": "health"},
            {**data(), "endpoint": ""},
        ]
        for record in invalid:
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.store.set("payments-api", AZ, record)
        self.assertEqual(self.store.get("payments-api", AZ), original)
        with self.assertRaises(ValueError):
            self.store.changed_since(float("nan"))

    def test_invalid_replica_version_and_deletion_flag(self):
        remote = self.aws.set("payments-api", AWS, data())
        for version in (None, 0, -1, True, float("inf"), "1000"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.store.apply_replica({**remote, "version": version}, AWS)
        with self.assertRaises(ValueError):
            self.store.apply_replica({**remote, "deleted": "false"}, AWS)
        self.assertEqual(self.store.list("payments-api"), [])

    def test_concurrent_peer_delivery_finishes_at_newest_version(self):
        records = [self.aws.set("payments-api", AWS, data(status="DOWN" if i % 2 else "UP"))
                   for i in range(100)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda r: self.store.apply_replica(r, AWS), reversed(records)))
        self.assertEqual(self.store.get("payments-api", AWS)["version"], records[-1]["version"])

    def test_concurrent_owner_writes_do_not_lose_distinct_services(self):
        def write(i):
            self.store.set(f"service-{i}", AZ, data())
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(100)))
        changes = self.store.changed_since(0)
        self.assertEqual(len(changes), 100)
        self.assertEqual(len({r["updated_at"] for r in changes}), 100)


if __name__ == "__main__":
    unittest.main()

