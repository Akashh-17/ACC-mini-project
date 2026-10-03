"""POST /replicate/receive — agent-to-agent only, served behind mTLS (see server.py).

Wire format and storage semantics: interfaces.md ("GCP owner" section).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request

log = logging.getLogger("replication")

PEER_SCOPE_KEY = "peer_cn"  # set by server.PeerCertH11Protocol from the client cert


def peer_region(request: Request) -> Optional[str]:
    return request.scope.get(PEER_SCOPE_KEY)


def create_replication_app(storage, region_id: str, allowed_peers) -> FastAPI:
    """storage: agent.storage.CatalogStorage (only apply_replica is used here).
    allowed_peers: region_ids whose certs may publish to this agent."""
    allowed = frozenset(allowed_peers)
    boot_id = uuid.uuid4().hex  # lets senders detect a restart and resend a snapshot
    app = FastAPI(title="replication", docs_url=None, redoc_url=None, openapi_url=None)

    def authenticated_peer(request: Request) -> str:
        peer = peer_region(request)
        if peer not in allowed:
            raise HTTPException(403, f"certificate {peer!r} is not a known peer agent")
        return peer

    @app.post("/replicate/receive")
    async def receive(request: Request, record: dict = Body(...)):
        peer = authenticated_peer(request)
        # Identity comes from the cert, never the body: a peer may only publish its own region.
        if record.get("region_id") != peer:
            raise HTTPException(403, f"peer {peer} may only publish region {peer}")
        try:
            applied = storage.apply_replica(record, peer)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if applied:
            now = time.time()
            # One JSON line per applied change — the Test Harness parses these
            # to measure convergence time.
            log.info(json.dumps({
                "event": "replication_applied",
                "receiver": region_id,
                "service_name": record["service_name"],
                "region_id": peer,
                "status": "DELETED" if record.get("deleted") else record.get("status"),
                "version": record["version"],
                "applied_at": now,
                "lag_ms": round((now - record["version"]) * 1000, 1),
            }))
        # False is an idempotent no-op (old or duplicate version), not an error.
        return {"status": "applied" if applied else "discarded"}

    @app.get("/replicate/ping")
    async def ping(request: Request):
        authenticated_peer(request)
        return {"region_id": region_id, "boot_id": boot_id, "time": time.time()}

    return app
