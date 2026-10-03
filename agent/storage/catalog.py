"""Thread-safe in-memory catalog. Integration contract: interfaces.md."""
from copy import deepcopy
import math
from threading import RLock
import time


class CatalogStorage:
    """Share one instance across the agent. Returned values are copies."""

    def __init__(self, region_id, ttl_seconds=15.0, clock=time.time):
        self._key("validation", region_id)
        self._number(ttl_seconds, "ttl_seconds")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.region_id, self.ttl_seconds, self._clock = region_id, ttl_seconds, clock
        self._lock = RLock()
        self._records, self._changes, self._cursor = {}, {}, 0.0

    @staticmethod
    def _number(value, name):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite number")
        try:
            valid = math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid or value < 0:
            raise ValueError(f"{name} must be non-negative and finite")

    @staticmethod
    def _key(service_name, region_id):
        for value in (service_name, region_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("service_name and region_id must be non-empty strings")
        return service_name, region_id

    def _now(self):
        now = self._clock()
        self._number(now, "clock")
        return now

    def _tick(self, now):
        self._cursor = max(now, math.nextafter(self._cursor, math.inf))
        return self._cursor

    def _version(self, key, now):
        previous = self._changes.get(key, {}).get("version", 0.0)
        return max(now, math.nextafter(previous, math.inf))

    def _normalise(self, data):
        if not isinstance(data, dict):
            raise ValueError("data must be a dictionary")
        fields = ("endpoint", "health_check_path", "tier", "status", "timestamp")
        missing = [f for f in fields if f not in data]
        if missing:
            raise ValueError("Missing fields: " + ", ".join(missing))
        if not isinstance(data["endpoint"], str) or not data["endpoint"].strip():
            raise ValueError("endpoint must be a non-empty string")
        if (not isinstance(data["health_check_path"], str)
                or not data["health_check_path"].startswith("/")):
            raise ValueError("health_check_path must start with /")
        if data["tier"] not in ("critical", "standard"):
            raise ValueError("tier must be critical or standard")
        if data["status"] not in ("UP", "DOWN", "STALE"):
            raise ValueError("status must be UP, DOWN or STALE")
        self._number(data["timestamp"], "timestamp")
        return {f: deepcopy(data[f]) for f in fields}

    def set(self, service_name, region_id, data, *, expected_version=None):
        """Publish a local record; remote writes use apply_replica.

        Preserve last-success timestamp on DOWN writes. Storage owns version
        and updated_at; caller-supplied versions are ignored.
        Health checks pass expected_version from the record they probed. A
        deleted/replaced/changed record returns None instead of being overwritten.
        """
        key = self._key(service_name, region_id)
        if region_id != self.region_id:
            raise ValueError("set is local-only; use apply_replica")
        record = self._normalise(data)
        if expected_version is not None:
            self._number(expected_version, "expected_version")
            if expected_version <= 0:
                raise ValueError("expected_version must be positive")
        with self._lock:
            if expected_version is not None:
                current = self._records.get(key)
                if current is None or current["version"] != expected_version:
                    return None
            now = self._now()
            record.update(service_name=service_name, region_id=region_id,
                          version=self._version(key, now),
                          updated_at=self._tick(now), deleted=False)
            self._records[key] = record
            self._changes[key] = deepcopy(record)
            return deepcopy(record)

    def get(self, service_name, region_id):
        key = self._key(service_name, region_id)
        with self._lock:
            return deepcopy(self._records.get(key))

    def list(self, service_name):
        self._key(service_name, self.region_id)
        with self._lock:
            records = [deepcopy(r) for key, r in self._records.items()
                       if key[0] == service_name]
        return sorted(records, key=lambda r: (r["region_id"] != self.region_id,
                                              r["region_id"]))

    def delete(self, service_name, region_id, endpoint=None):
        """Remove a local entry, retain a tombstone. Missing/mismatch: False."""
        key = self._key(service_name, region_id)
        if region_id != self.region_id:
            raise ValueError("delete is local-only; use apply_replica")
        with self._lock:
            current = self._records.get(key)
            if current is None or (endpoint is not None and current["endpoint"] != endpoint):
                return False
            now = self._now()
            tombstone = dict(service_name=service_name, region_id=region_id,
                             version=self._version(key, now),
                             updated_at=self._tick(now), deleted=True)
            del self._records[key]
            self._changes[key] = tombstone
            return True

    def changed_since(self, ts):
        """Latest owner records/deletions with updated_at > ts; cursor ordered."""
        self._number(ts, "ts")
        with self._lock:
            changes = [deepcopy(r) for r in self._changes.values()
                       if r["region_id"] == self.region_id and r["updated_at"] > ts]
        return sorted(changes, key=lambda r: r["updated_at"])

    def snapshot(self):
        """Atomic owner snapshot with tombstones/cursor for peer reconnect."""
        with self._lock:
            return {"cursor": self._cursor,
                    "records": deepcopy([r for r in self._changes.values()
                                         if r["region_id"] == self.region_id])}

    def apply_replica(self, data, peer_region_id):
        """Atomically accept newer peer-owned data; ignore equal/older versions.

        peer_region_id must come from verified mTLS identity, not request body.
        """
        if not isinstance(data, dict):
            raise ValueError("replica must be a dictionary")
        key = self._key(data.get("service_name"), data.get("region_id"))
        if key[1] != peer_region_id or key[1] == self.region_id:
            raise ValueError("peer may only publish its own remote region")
        version = data.get("version")
        self._number(version, "version")
        if version <= 0:
            raise ValueError("version must be positive")
        deleted = data.get("deleted", False)
        if not isinstance(deleted, bool):
            raise ValueError("deleted must be boolean")
        record = {} if deleted else self._normalise(data)
        with self._lock:
            previous = self._changes.get(key)
            if previous is not None and version <= previous["version"]:
                return False
            record.update(service_name=key[0], region_id=key[1], version=version,
                          updated_at=self._tick(self._now()), deleted=deleted)
            self._changes[key] = deepcopy(record)
            if deleted:
                self._records.pop(key, None)
            else:
                self._records[key] = record
            return True

    def sweepTTL(self):
        """Mark age > TTL STALE, preserve DOWN. Returns changed records.

        Run every ~5 seconds. Remote expiry is observer-local: never changes
        owner version or creates outgoing updates.
        """
        with self._lock:
            now = self._now()
            changed = []
            for key, record in self._records.items():
                if (record["status"] not in ("DOWN", "STALE")
                        and now - record["timestamp"] > self.ttl_seconds):
                    record["status"] = "STALE"
                    if key[1] == self.region_id:
                        record["version"] = self._version(key, now)
                        record["updated_at"] = self._tick(now)
                        self._changes[key] = deepcopy(record)
                    changed.append(deepcopy(record))
            return changed

