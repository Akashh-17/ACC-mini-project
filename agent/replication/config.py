from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TLSFiles:
    cert: str
    key: str
    ca: str


@dataclass(frozen=True)
class ReplicationConfig:
    region_id: str
    peers: dict[str, str]  # peer region_id -> base URL, e.g. "https://203.0.113.5:9443"
    tls: TLSFiles
    host: str = "0.0.0.0"
    port: int = 9443
    interval: float = 2.0
    timeout: float = 2.0
    snapshot_interval: float = 30.0

    @classmethod
    def from_env(cls) -> "ReplicationConfig":
        """Env vars:
        REGION_ID              gcp-asia-south1
        REPLICATION_PEERS      aws-ap-south-1=https://1.2.3.4:9443,azure-indiasouthcentral=https://5.6.7.8:9443
        TLS_CERT / TLS_KEY / TLS_CA   paths to this agent's cert, key, and the shared CA cert
        REPLICATION_HOST (0.0.0.0), REPLICATION_PORT (9443),
        REPLICATION_INTERVAL (2.0), REPLICATION_TIMEOUT (2.0),
        REPLICATION_SNAPSHOT_INTERVAL (30.0)  full-snapshot backstop, seconds
        """
        return cls(
            region_id=os.environ["REGION_ID"],
            peers=parse_peers(os.environ.get("REPLICATION_PEERS", "")),
            tls=TLSFiles(
                cert=os.environ["TLS_CERT"],
                key=os.environ["TLS_KEY"],
                ca=os.environ["TLS_CA"],
            ),
            host=os.environ.get("REPLICATION_HOST", "0.0.0.0"),
            port=int(os.environ.get("REPLICATION_PORT", "9443")),
            interval=float(os.environ.get("REPLICATION_INTERVAL", "2.0")),
            timeout=float(os.environ.get("REPLICATION_TIMEOUT", "2.0")),
            snapshot_interval=float(os.environ.get("REPLICATION_SNAPSHOT_INTERVAL", "30.0")),
        )


def parse_peers(raw: str) -> dict[str, str]:
    peers: dict[str, str] = {}
    for item in filter(None, (p.strip() for p in raw.split(","))):
        name, sep, url = item.partition("=")
        if not sep or not name or not url:
            raise ValueError(f"bad REPLICATION_PEERS item {item!r}, expected region_id=https://host:port")
        peers[name.strip()] = url.strip().rstrip("/")
    return peers
