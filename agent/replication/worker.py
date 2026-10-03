"""Replication worker: every `interval` seconds, push this region's changes to
every peer's /replicate/receive. Protocol follows interfaces.md:

- per-peer acknowledged cursor, advanced only after the whole batch succeeds
- full snapshot() on first contact, when the peer's boot_id changes (it
  restarted with an empty catalog), and every `snapshot_interval` as a backstop
- otherwise changed_since(cursor) deltas
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

import httpx

log = logging.getLogger("replication")


class PeerState:
    def __init__(self) -> None:
        self.cursor: Optional[float] = None  # None = next sync must be a snapshot
        self.boot_id: Optional[str] = None
        self.last_snapshot = float("-inf")
        self.reachable = True


class ReplicationWorker:
    def __init__(
        self,
        storage,
        peers: dict[str, str],
        client: httpx.AsyncClient,
        interval: float = 2.0,
        snapshot_interval: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        """storage: agent.storage.CatalogStorage (uses changed_since and snapshot).
        peers: peer region_id -> base URL, e.g. "https://203.0.113.5:9443"."""
        self.storage = storage
        self.peers = peers
        self.client = client
        self.interval = interval
        self.snapshot_interval = snapshot_interval
        self.clock = clock
        self.state = {peer: PeerState() for peer in peers}

    async def run_once(self) -> None:
        await asyncio.gather(*(self._sync_peer(peer) for peer in self.peers))

    async def _sync_peer(self, peer: str) -> None:
        base, st = self.peers[peer], self.state[peer]
        try:
            info = await self._ping(peer, base)
        except (httpx.HTTPError, ValueError) as exc:
            self._mark_unreachable(peer, exc)
            return

        if info["boot_id"] != st.boot_id:
            if st.boot_id is not None:
                log.info("peer %s restarted; resending snapshot", peer)
            st.boot_id, st.cursor = info["boot_id"], None

        if st.cursor is None or self.clock() - st.last_snapshot >= self.snapshot_interval:
            snap = self.storage.snapshot()
            records, new_cursor, is_snapshot = snap["records"], snap["cursor"], True
        else:
            records = self.storage.changed_since(st.cursor)
            new_cursor = max((r["updated_at"] for r in records), default=st.cursor)
            is_snapshot = False

        for record in records:
            try:
                resp = await self.client.post(f"{base}/replicate/receive", json=record)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                # Keep the old cursor: the whole batch is retried next cycle.
                self._mark_unreachable(peer, exc, pending=len(records))
                return

        st.cursor = new_cursor
        if is_snapshot:
            st.last_snapshot = self.clock()
        if not st.reachable:
            log.info("peer %s reachable again", peer)
        st.reachable = True

    async def _ping(self, peer: str, base: str) -> dict:
        resp = await self.client.get(f"{base}/replicate/ping")
        resp.raise_for_status()
        info = resp.json()
        if info.get("region_id") != peer or not info.get("boot_id"):
            raise ValueError(f"{base} answered as {info.get('region_id')!r}, expected {peer!r}")
        return info

    def _mark_unreachable(self, peer: str, exc: Exception, pending: int = 0) -> None:
        # Log on the transition only, so a long partition doesn't flood the log.
        if self.state[peer].reachable:
            log.warning("replication to %s failed (%d pending): %s %s",
                        peer, pending, type(exc).__name__, exc)
        self.state[peer].reachable = False

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("replication cycle crashed")
            await asyncio.sleep(self.interval)
