"""Watches the three clouds for the demo pages.

Every `interval` seconds it takes one snapshot (each registry's view of every
service, each cloud's payment ledger and stock) and turns what changed since
the previous snapshot into feed events. The pages read the latest snapshot
and the feed, so the clouds are polled at the same rate however many browser
tabs are open.
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from typing import Callable, Optional

from storefront.backend import SERVICES, SHORT, Registry, ledgers, stocks

log = logging.getLogger("webapp")

LIVE_LAG_MS = 60_000  # older "lag" is a catch-up after a replicator joined, not live replication


def short(region_id: str) -> str:
    return SHORT.get(region_id, region_id)


class Monitor:
    def __init__(self, reg: Registry, interval: float = 2.0, clock: Callable[[], float] = time.time,
                 max_events: int = 200):
        self.reg, self.interval, self.clock = reg, interval, clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._snap: Optional[dict] = None
        self._events: deque[dict] = deque(maxlen=max_events)
        self._seq = 0
        self._seen_replicated: set[tuple[str, str]] = set()  # (txn_id, observer region)
        self.orders, self.order_total = 0, 0.0

    # ---------------------------------------------------------------- feed

    def add_event(self, kind: str, title: str, detail: str = "", ref: Optional[str] = None) -> int:
        """`ref` ties an event to an object the pages track (e.g. a txn_id)."""
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "time": time.strftime("%H:%M:%S", time.localtime(self.clock())),
                                 "kind": kind, "title": title, "detail": detail, "ref": ref})
            return self._seq

    def events_after(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    def record_order(self, total: float) -> None:
        with self._lock:
            self.orders += 1
            self.order_total += total

    # ---------------------------------------------------------------- snapshots

    def refresh(self) -> None:
        try:
            snap = {"views": self.reg.views(), "ledgers": ledgers(self.reg), "stocks": stocks(self.reg),
                    "taken_at": self.clock()}
        except Exception as exc:  # keep the last good snapshot; tell the feed
            log.warning("snapshot failed: %s", exc)
            self.add_event("error", "Could not refresh cloud state", f"{type(exc).__name__}: {exc}")
            return
        with self._lock:
            prev, self._snap = self._snap, snap
        if prev is None:
            self._mark_replicated(snap, emit=False)
            return
        self._status_events(prev, snap)
        self._mark_replicated(snap, emit=True)

    def _status_events(self, prev: dict, snap: dict) -> None:
        changes: dict[tuple[str, str, str, str], list[str]] = {}
        for obs, by_svc in snap["views"].items():
            for svc in SERVICES:
                new_eps, old_eps = by_svc.get(svc), prev["views"].get(obs, {}).get(svc)
                if new_eps is None or old_eps is None:  # that registry unreachable: no news
                    continue
                old = {e["region_id"]: e["status"] for e in old_eps}
                new = {e["region_id"]: e["status"] for e in new_eps}
                for region in sorted(set(old) | set(new)):
                    if old.get(region) != new.get(region):
                        key = (svc, region, old.get(region, "none"), new.get(region, "gone"))
                        changes.setdefault(key, []).append(short(obs))
        order = {"GCP": 0, "AWS": 1, "Azure": 2}
        for (svc, region, old, new), observers in changes.items():
            self.add_event("status", f"{svc} on {short(region)}: {old} → {new}",
                           "seen by " + ", ".join(sorted(observers, key=lambda o: (order.get(o, 9), o))))

    def _mark_replicated(self, snap: dict, emit: bool) -> None:
        arrivals: dict[str, dict] = {}
        for obs, book in snap["ledgers"].items():
            for p in (book or {}).get("recent", []):
                key = (p["txn_id"], obs)
                if not p.get("replicated") or key in self._seen_replicated:
                    continue
                self._seen_replicated.add(key)
                if emit and p.get("lag_ms", LIVE_LAG_MS) < LIVE_LAG_MS:
                    a = arrivals.setdefault(p["txn_id"], {"payment": p, "where": []})
                    a["where"].append(f"{short(obs)} {p['lag_ms']} ms")
        for a in arrivals.values():
            p = a["payment"]
            self.add_event("replicated", f"Payment ₹{p['amount']:,.0f} from {short(p['region_id'])} replicated",
                           "→ " + " · ".join(a["where"]), ref=p["txn_id"])

    # ---------------------------------------------------------------- reads

    def state(self) -> dict:
        with self._lock:
            snap = self._snap or {"views": {}, "ledgers": {}, "stocks": {}, "taken_at": None}
            orders, total = self.orders, self.order_total
        return {**snap, "age_s": None if snap["taken_at"] is None else round(self.clock() - snap["taken_at"], 1),
                "metrics": self._metrics(snap, orders, total)}

    def _metrics(self, snap: dict, orders: int, total: float) -> dict:
        lags = [p["lag_ms"] for book in snap["ledgers"].values() if book
                for p in book.get("recent", []) if p.get("replicated") and p.get("lag_ms", LIVE_LAG_MS) < LIVE_LAG_MS]
        healthy = endpoints = 0
        home = self.reg.clouds[self.reg.home]["region_id"]
        for obs in [home] + [r for r in snap["views"] if r != home]:
            view = snap["views"].get(obs)
            if view and any(v is not None for v in view.values()):
                eps = [e for v in view.values() if v for e in v]
                endpoints, healthy = len(eps), sum(1 for e in eps if e["status"] == "UP")
                break
        return {"replication_ms": round(statistics.median(lags)) if lags else None,
                "healthy": healthy, "endpoints": endpoints, "orders": orders, "order_total": total,
                "unsent": sum(book.get("unsent_events", 0) for book in snap["ledgers"].values() if book)}

    # ---------------------------------------------------------------- loop

    def poke(self) -> None:
        """Refresh soon (after an order or a demo action) instead of waiting a full interval."""
        self._wake.set()

    def start(self) -> None:
        def loop():
            while True:
                self.refresh()
                self._wake.wait(self.interval)
                if self._wake.is_set():
                    self._wake.clear()
                    time.sleep(0.4)  # let the change reach the registries / Kafka first
        threading.Thread(target=loop, name="cloud-monitor", daemon=True).start()
