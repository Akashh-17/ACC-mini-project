"""mTLS plumbing and the single entry point the assembled agent calls."""
from __future__ import annotations

import asyncio
import ssl
from typing import Optional

import httpx
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .config import ReplicationConfig, TLSFiles
from .receiver import PEER_SCOPE_KEY, create_replication_app
from .worker import ReplicationWorker


def cert_common_name(cert: Optional[dict]) -> Optional[str]:
    """CN from ssl.SSLSocket.getpeercert(). gen-certs.sh sets CN = region_id."""
    for rdn in (cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


class PeerCertH11Protocol(H11Protocol):
    """Uvicorn doesn't pass the client certificate to the app, but the receiver
    must know which agent is calling. The handshake has already verified the
    cert against our CA (CERT_REQUIRED), so expose its CN as scope["peer_cn"]."""

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        peer = cert_common_name(transport.get_extra_info("peercert"))
        inner = self.app

        async def app_with_peer(scope, receive, send):
            scope[PEER_SCOPE_KEY] = peer
            await inner(scope, receive, send)

        self.app = app_with_peer


def make_mtls_server(app, host: str, port: int, tls: TLSFiles) -> uvicorn.Server:
    """Only clients presenting a cert signed by our CA complete the handshake.
    The CA issues exactly one cert per agent; the receiver then checks the CN."""
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        http=PeerCertH11Protocol,
        ssl_certfile=tls.cert,
        ssl_keyfile=tls.key,
        ssl_ca_certs=tls.ca,
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        log_level="info",
    )
    return uvicorn.Server(config)


def make_mtls_client(tls: TLSFiles, timeout: float) -> httpx.AsyncClient:
    ctx = ssl.create_default_context(cafile=tls.ca)
    ctx.load_cert_chain(tls.cert, tls.key)
    return httpx.AsyncClient(verify=ctx, timeout=timeout)


async def run_replication(storage, cfg: ReplicationConfig) -> None:
    """Run the /replicate/receive server and the worker until cancelled.

    storage must be the agent's single shared CatalogStorage. In the assembled agent:
        catalog = CatalogStorage(region_id=os.environ["REGION_ID"], ttl_seconds=15)
        await asyncio.gather(registry_server.serve(), run_replication(catalog, cfg))
    """
    app = create_replication_app(storage, cfg.region_id, allowed_peers=cfg.peers)
    server = make_mtls_server(app, cfg.host, cfg.port, cfg.tls)
    async with make_mtls_client(cfg.tls, cfg.timeout) as client:
        worker = ReplicationWorker(storage, cfg.peers, client, cfg.interval, cfg.snapshot_interval)
        await asyncio.gather(server.serve(), worker.run())
