"""Real owner-only health probes and the storage TTL sweep."""
import asyncio
import logging
import time

import httpx

from .validation import validate_endpoint, validate_health_path


log = logging.getLogger("registry.health")


class HealthScheduler:
    def __init__(self, catalog, client: httpx.AsyncClient, *, interval=5.0,
                 timeout=2.0, clock=time.time, allow_loopback=False):
        self.catalog = catalog
        self.client = client
        self.interval = interval
        self.timeout = timeout
        self.clock = clock
        self.allow_loopback = allow_loopback
        self._slots = asyncio.Semaphore(8)

    async def _probe(self, observed: dict) -> None:
        record = dict(observed)
        try:
            endpoint = validate_endpoint(record["endpoint"], allow_loopback=self.allow_loopback)
            path = validate_health_path(record["health_check_path"])
            async with self._slots:
                response = await asyncio.wait_for(
                    self.client.get("http://" + endpoint + path,
                                    timeout=self.timeout, follow_redirects=False),
                    timeout=self.timeout,
                )
            healthy = response.is_success
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
            healthy = False

        record["status"] = "UP" if healthy else "DOWN"
        if healthy:
            record["timestamp"] = self.clock()
        # A failed check keeps the last successful timestamp. Storage advances
        # version itself, and rejects results for deleted/replaced entries.
        self.catalog.set(record["service_name"], self.catalog.region_id, record,
                         expected_version=observed["version"])

    async def run_once(self) -> None:
        records = self.catalog.snapshot()["records"]
        await asyncio.gather(*(self._probe(record) for record in records
                               if not record["deleted"]
                               and record["region_id"] == self.catalog.region_id))

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.run_once()
            except Exception:
                log.exception("health-check cycle failed")
            await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - started)))

    async def sweep(self) -> None:
        # Independent of slow probes: remote freshness still expires on time.
        while True:
            self.catalog.sweepTTL()
            await asyncio.sleep(self.interval)
