"""Local ledger of payments and stock changes, replicated across clouds via Kafka.

One SQLite file per VM (LEDGER_PATH), shared by payments-api, inventory-check
and the replicator (services/replicator.py). A local change is written together
with an outbox row in one transaction, so it is durable before anything is
sent; the replicator publishes the outbox and applies every cloud's events.

Events are facts that commute (a payment happened, N units left a region's
share), so applying them in any order gives the same result, and the event_id
primary key makes a duplicate delivery a no-op.

Stock uses per-cloud allocation: a region's share is its starting allocation
plus every stock move recorded for it. Only the owning region ever decrements
its share (sales, transfers out), inside a local transaction, so no
combination of clouds can oversell even while Kafka is unreachable.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Callable, Optional

from .catalog import REGIONS, allocation

PAYMENTS_TOPIC = "payments"
INVENTORY_TOPIC = "inventory-events"
EVENT_TYPES = {"payment_authorized": PAYMENTS_TOPIC,
               "stock_reserved": INVENTORY_TOPIC,
               "stock_transferred": INVENTORY_TOPIC}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    origin      TEXT NOT NULL,
    ts          REAL NOT NULL,
    received_at REAL NOT NULL,
    body        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    txn_id    TEXT PRIMARY KEY,
    order_id  TEXT NOT NULL,
    amount    REAL NOT NULL,
    currency  TEXT NOT NULL,
    region_id TEXT NOT NULL,
    ts        REAL NOT NULL,
    event_id  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_moves (
    event_id   TEXT NOT NULL,
    region_id  TEXT NOT NULL,
    product_id TEXT NOT NULL,
    delta      INTEGER NOT NULL,
    PRIMARY KEY (event_id, region_id, product_id)
);
CREATE TABLE IF NOT EXISTS outbox (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    topic    TEXT NOT NULL,
    key      TEXT NOT NULL,
    payload  TEXT NOT NULL,
    sent_at  REAL
);
CREATE INDEX IF NOT EXISTS outbox_unsent ON outbox (seq) WHERE sent_at IS NULL;
"""


class InvalidEvent(ValueError):
    """A message that is not a well-formed ledger event (skipped, never applied)."""


def validate(event: dict) -> dict:
    """Check an event's shape before it touches the database."""
    def need(cond: bool, why: str) -> None:
        if not cond:
            raise InvalidEvent(why)

    need(isinstance(event, dict), "not a JSON object")
    try:
        etype = event["type"]
        need(etype in EVENT_TYPES, f"unknown event type {etype!r}")
        need(isinstance(event["event_id"], str) and bool(event["event_id"]), "event_id missing")
        need(event["origin"] in REGIONS, f"unknown origin {event['origin']!r}")
        float(event["ts"])
        if etype == "payment_authorized":
            need(all(isinstance(event[k], str) for k in ("txn_id", "order_id", "currency")), "bad ids")
            need(float(event["amount"]) >= 0, "negative amount")
        elif etype == "stock_reserved":
            need(isinstance(event["items"], list) and bool(event["items"]), "no items")
            for item in event["items"]:
                need(isinstance(item["product_id"], str) and int(item["qty"]) > 0, "bad item")
        else:  # stock_transferred: issued by the giving region (origin)
            need(event["to_region"] in REGIONS and event["to_region"] != event["origin"], "bad to_region")
            need(isinstance(event["product_id"], str) and int(event["qty"]) > 0, "bad transfer")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, InvalidEvent):
            raise
        raise InvalidEvent(f"malformed {event.get('type')}: {exc!r}") from exc
    return event


class Ledger:
    """Thread-safe within a process; SQLite (WAL) arbitrates between processes."""

    def __init__(self, path: str, region_id: str, clock: Callable[[], float] = time.time):
        self.region_id, self.clock = region_id, clock
        self.alloc = allocation()
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------ transactions

    def _tx(self):
        ledger = self

        class Tx:
            def __enter__(self):
                ledger._lock.acquire()
                ledger.db.execute("BEGIN IMMEDIATE")  # take the write lock before reading shares
                return ledger.db

            def __exit__(self, exc_type, exc, tb):
                try:
                    ledger.db.execute("COMMIT" if exc_type is None else "ROLLBACK")
                finally:
                    ledger._lock.release()

        return Tx()

    def _apply(self, db: sqlite3.Connection, event: dict) -> bool:
        """Insert an event and its projections; False if it was already applied."""
        cur = db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                         (event["event_id"], event["type"], event["origin"], float(event["ts"]),
                          self.clock(), json.dumps(event, sort_keys=True)))
        if cur.rowcount == 0:
            return False
        etype, eid = event["type"], event["event_id"]
        if etype == "payment_authorized":
            db.execute("INSERT OR IGNORE INTO payments VALUES (?,?,?,?,?,?,?)",
                       (event["txn_id"], event["order_id"], float(event["amount"]),
                        event["currency"], event["origin"], float(event["ts"]), eid))
        elif etype == "stock_reserved":
            for item in event["items"]:
                self._move(db, eid, event["origin"], item["product_id"], -int(item["qty"]))
        else:
            qty = int(event["qty"])
            self._move(db, eid, event["origin"], event["product_id"], -qty)
            self._move(db, eid, event["to_region"], event["product_id"], qty)
        return True

    @staticmethod
    def _move(db, event_id, region_id, product_id, delta):
        # Same product twice in one reservation adds up into one row.
        db.execute("INSERT INTO stock_moves VALUES (?,?,?,?) "
                   "ON CONFLICT (event_id, region_id, product_id) DO UPDATE "
                   "SET delta = delta + excluded.delta", (event_id, region_id, product_id, delta))

    def _record_local(self, db, event: dict) -> dict:
        self._apply(db, event)
        key = event.get("txn_id") or event.get("order_id") or event.get("product_id")
        db.execute("INSERT INTO outbox (event_id, topic, key, payload) VALUES (?,?,?,?)",
                   (event["event_id"], EVENT_TYPES[event["type"]], key,
                    json.dumps(event, sort_keys=True)))
        return event

    def _new_event(self, etype: str, **fields) -> dict:
        return {"event_id": uuid.uuid4().hex, "type": etype, "origin": self.region_id,
                "ts": self.clock(), **fields}

    def _share(self, db, region_id: str, product_id: str) -> int:
        moved = db.execute("SELECT COALESCE(SUM(delta), 0) FROM stock_moves "
                           "WHERE region_id = ? AND product_id = ?", (region_id, product_id)).fetchone()[0]
        return self.alloc.get(region_id, {}).get(product_id, 0) + moved

    # ------------------------------------------------------------ local writes

    def record_payment(self, order_id: str, amount: float, currency: str) -> dict:
        event = self._new_event("payment_authorized", txn_id=f"txn_{uuid.uuid4().hex[:12]}",
                                order_id=order_id, amount=amount, currency=currency)
        with self._tx() as db:
            return self._record_local(db, event)

    def reserve(self, order_id: str, items: list[tuple[str, int]]) -> tuple[bool, list[dict]]:
        """All-or-nothing reservation from this region's share."""
        wanted: dict[str, int] = {}
        for pid, qty in items:
            wanted[pid] = wanted.get(pid, 0) + qty
        with self._tx() as db:
            status = [{"product_id": pid, "ok": self._share(db, self.region_id, pid) >= wanted[pid]}
                      for pid, _ in items]
            if all(s["ok"] for s in status):
                self._record_local(db, self._new_event(
                    "stock_reserved", order_id=order_id,
                    items=[{"product_id": p, "qty": q} for p, q in wanted.items()]))
                return True, status
        return False, status

    def transfer(self, product_id: str, qty: int, to_region: str) -> bool:
        """Give part of this region's share to another region."""
        if to_region not in REGIONS or to_region == self.region_id:
            raise ValueError(f"cannot transfer to {to_region!r}")
        with self._tx() as db:
            if self._share(db, self.region_id, product_id) < qty:
                return False
            self._record_local(db, self._new_event("stock_transferred", product_id=product_id,
                                                   qty=qty, to_region=to_region))
            return True

    # ------------------------------------------------------------ replication

    def apply_remote(self, event: dict) -> bool:
        """Apply an event read from Kafka (any origin, including our own after
        a lost database). Returns False for a duplicate. Raises InvalidEvent."""
        validate(event)
        with self._tx() as db:
            return self._apply(db, event)

    def pending(self, limit: int = 100, exclude: frozenset = frozenset()) -> list[sqlite3.Row]:
        with self._lock:
            rows = self.db.execute("SELECT seq, topic, key, payload FROM outbox WHERE sent_at IS NULL "
                                   "ORDER BY seq LIMIT ?", (limit + len(exclude),)).fetchall()
        return [r for r in rows if r["seq"] not in exclude][:limit]

    def mark_sent(self, seq: int) -> None:
        with self._lock:
            self.db.execute("UPDATE outbox SET sent_at = ? WHERE seq = ?", (self.clock(), seq))

    # ------------------------------------------------------------ reads

    def stock(self) -> list[dict]:
        with self._lock:
            moved: dict[tuple[str, str], int] = {
                (r["region_id"], r["product_id"]): r["d"] for r in self.db.execute(
                    "SELECT region_id, product_id, SUM(delta) AS d FROM stock_moves "
                    "GROUP BY region_id, product_id")}
        out = []
        for pid in self.alloc[REGIONS[0]]:
            by_region = {r: self.alloc[r][pid] + moved.get((r, pid), 0) for r in REGIONS}
            out.append({"product_id": pid, "local_share": by_region.get(self.region_id, 0),
                        "global_stock": sum(by_region.values()), "by_region": by_region})
        return out

    def payments(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT p.*, e.received_at FROM payments p JOIN events e USING (event_id) "
                "ORDER BY p.ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"txn_id": r["txn_id"], "order_id": r["order_id"], "amount": r["amount"],
                 "currency": r["currency"], "region_id": r["region_id"], "ts": r["ts"],
                 "replicated": r["region_id"] != self.region_id,
                 "lag_ms": round((r["received_at"] - r["ts"]) * 1000) if r["region_id"] != self.region_id else 0}
                for r in rows]

    def summary(self) -> dict:
        with self._lock:
            by_region = {r["region_id"]: {"count": r["n"], "total": r["total"]} for r in self.db.execute(
                "SELECT region_id, COUNT(*) AS n, SUM(amount) AS total FROM payments GROUP BY region_id")}
            unsent = self.db.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0]
        return {"region_id": self.region_id, "payments_by_region": by_region, "unsent_events": unsent}


def open_ledger(path: Optional[str], region_id: str) -> Ledger:
    """LEDGER_PATH unset -> private in-memory ledger (tests, local runs)."""
    return Ledger(path or ":memory:", region_id)
