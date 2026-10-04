"""Private service targets accepted by the local registry API."""
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit


PRIVATE_NETWORKS = tuple(ip_network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))


def validate_endpoint(endpoint: str, *, allow_loopback: bool = False) -> str:
    """Require a literal private IP and port, without a scheme or URL suffix."""
    try:
        if not endpoint or any(c.isspace() for c in endpoint) or "\\" in endpoint:
            raise ValueError
        target = urlsplit("http://" + endpoint)
        address = ip_address(target.hostname or "")
        port = target.port
        if (target.username is not None or target.password is not None
                or target.path or target.query or target.fragment
                or port is None or not 1 <= port <= 65535):
            raise ValueError
        private = any(address.version == network.version and address in network
                      for network in PRIVATE_NETWORKS)
        if not private and not (allow_loopback and address.is_loopback):
            raise ValueError
        host = f"[{address}]" if address.version == 6 else str(address)
        return f"{host}:{port}"
    except (ValueError, TypeError) as exc:
        raise ValueError("endpoint must be a private IP:port") from exc


def validate_health_path(path: str) -> str:
    """Keep the probe on the registered host; do not accept an absolute URL."""
    if (not path.startswith("/") or path.startswith("//")
            or "\\" in path or any(c.isspace() for c in path)):
        raise ValueError("health_check_path must be a local path such as /health")
    target = urlsplit(path)
    if target.scheme or target.netloc or target.fragment:
        raise ValueError("health_check_path must be a local path such as /health")
    return path
