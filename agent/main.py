"""Local Registry & API plus optional GCP replication, sharing Azure storage."""
import asyncio
import logging
import os
from pathlib import Path

import uvicorn

from agent.registry import create_registry_app
from agent.storage import CatalogStorage


def create_agent():
    region_id = os.environ.get("REGION_ID", "").strip()
    if not region_id:
        raise ValueError("REGION_ID must be set")
    port = int(os.environ.get("REGISTRY_PORT", "9000"))
    if not 1 <= port <= 65535:
        raise ValueError("REGISTRY_PORT must be between 1 and 65535")
    allow_loopback = os.environ.get("REGISTRY_ALLOW_LOOPBACK", "false").lower()
    if allow_loopback not in ("true", "false"):
        raise ValueError("REGISTRY_ALLOW_LOOPBACK must be true or false")

    catalog = CatalogStorage(region_id=region_id, ttl_seconds=15)
    app = create_registry_app(catalog, os.environ.get("REGISTRY_SECRET", ""),
                              allow_loopback=allow_loopback == "true")
    replication = None
    if os.environ.get("REPLICATION_PEERS", "").strip():
        from agent.replication import ReplicationConfig

        replication = ReplicationConfig.from_env()
        if replication.port == port:
            raise ValueError("Registry and replication must use different ports")
        for path in (replication.tls.cert, replication.tls.key, replication.tls.ca):
            if not Path(path).is_file():
                raise ValueError("A configured replication TLS file is missing")
    return catalog, app, port, replication


async def main():
    catalog, app, port, replication = create_agent()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                         workers=1, log_level="info"))
    if replication is None:
        await server.serve()
        return

    from agent.replication import run_replication

    tasks = [asyncio.create_task(server.serve()),
             asyncio.create_task(run_replication(catalog, replication))]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        server.should_exit = True
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except (ValueError, KeyError) as exc:
        raise SystemExit(f"Agent configuration error: {exc}") from exc
