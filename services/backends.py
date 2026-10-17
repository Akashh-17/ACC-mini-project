"""Storage/messaging backends for the single-cloud standard services.

Each standard service is backed by its own cloud's managed service:
    notify-api    -> Google Cloud Pub/Sub   (NOTIFY_BACKEND=pubsub)
    recommend-api -> Amazon S3              (RECOMMEND_BACKEND=s3)
    admin-api     -> Azure Table Storage    (ADMIN_BACKEND=azuretable)
The default for each is an in-memory backend (the original stub behaviour), so
nothing changes until an owner opts in. Critical services use no backend.

Credentials are never stored here: Pub/Sub uses the GCE VM's attached service
account (metadata server), S3 uses the EC2 instance role (boto3 default chain),
Azure uses the VM's managed identity. Cloud SDKs are imported lazily.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Callable, Mapping, Optional

import httpx

from .catalog import PRODUCTS, RECOMMENDATIONS, starting_stock

log = logging.getLogger("services")


class BackendError(Exception):
    """The managed service could not be reached or refused the request."""


# ---------------------------------------------------------------- health

class HealthCache:
    """Runs `check` at most every `interval` seconds; /health reads the cached
    result so a slow managed service can't stall the registry's 2 s probe."""

    def __init__(self, check: Callable[[], None], interval: float = 10.0,
                 clock: Callable[[], float] = time.monotonic):
        self._check, self.interval, self.clock = check, interval, clock
        self._checked_at: Optional[float] = None
        self._ok = True

    def healthy(self) -> bool:
        now = self.clock()
        if self._checked_at is None or now - self._checked_at >= self.interval:
            try:
                self._check()
                self._ok = True
            except Exception as exc:  # any failure means "dependency unavailable"
                if self._ok:
                    log.warning("managed service check failed: %s", exc)
                self._ok = False
            self._checked_at = now
        return self._ok

    def force_recheck(self) -> None:
        self._checked_at = None


# ---------------------------------------------------------------- notify

class LogNotifier:
    name = "log"

    def publish(self, message: dict) -> str:
        log.warning("notify (log backend): %s", json.dumps(message))
        return f"log-{uuid.uuid4().hex[:12]}"

    def check(self) -> None:
        pass


class PubSubNotifier:
    """Publishes JSON messages to a Pub/Sub topic over the REST API, using an
    access token from the GCE metadata server (the VM's attached service account)."""

    name = "pubsub"
    METADATA_TOKEN = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

    def __init__(self, project: str, topic: str, client: Optional[httpx.Client] = None,
                 clock: Callable[[], float] = time.time, timeout: float = 1.5):
        self.topic_url = f"https://pubsub.googleapis.com/v1/projects/{project}/topics/{topic}"
        self.client = client or httpx.Client(timeout=timeout)
        self.clock = clock
        self._token: Optional[str] = None
        self._expires = 0.0

    def _auth(self) -> dict:
        if self._token is None or self.clock() >= self._expires - 60:
            r = self.client.get(self.METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
            r.raise_for_status()
            body = r.json()
            self._token, self._expires = body["access_token"], self.clock() + body["expires_in"]
        return {"Authorization": f"Bearer {self._token}"}

    def publish(self, message: dict) -> str:
        data = base64.b64encode(json.dumps(message).encode()).decode()
        try:
            r = self.client.post(f"{self.topic_url}:publish", headers=self._auth(),
                                 json={"messages": [{"data": data,
                                                     "attributes": {"event": str(message.get("event", ""))}}]})
            r.raise_for_status()
            return r.json()["messageIds"][0]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise BackendError(f"Pub/Sub publish failed: {exc}") from exc

    def check(self) -> None:
        try:
            self.client.get(self.topic_url, headers=self._auth()).raise_for_status()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise BackendError(f"Pub/Sub topic unreachable: {exc}") from exc


# ---------------------------------------------------------------- recommend

class MemoryRecommendations:
    name = "memory"

    def get(self, product_id: str) -> list[str]:
        return list(RECOMMENDATIONS.get(product_id, []))

    def check(self) -> None:
        pass


class S3Recommendations:
    """Reads {product_id: [ids]} from one JSON object in S3 (cached 30 s)."""

    name = "s3"

    def __init__(self, bucket: str, key: str = "recommendations.json", region: str = "ap-south-1",
                 client=None, clock: Callable[[], float] = time.monotonic, ttl: float = 30.0):
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("s3", region_name=region, config=Config(
                connect_timeout=1, read_timeout=1.5, retries={"max_attempts": 1}))
        self.bucket, self.key, self.client, self.clock, self.ttl = bucket, key, client, clock, ttl
        self._data: Optional[dict] = None
        self._loaded_at = 0.0

    def get(self, product_id: str) -> list[str]:
        if self._data is None or self.clock() - self._loaded_at >= self.ttl:
            try:
                body = self.client.get_object(Bucket=self.bucket, Key=self.key)["Body"].read()
                self._data, self._loaded_at = json.loads(body), self.clock()
            except Exception as exc:
                raise BackendError(f"S3 read failed: {exc}") from exc
        return list(self._data.get(product_id, []))

    def check(self) -> None:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key)
        except Exception as exc:
            raise BackendError(f"S3 object unreachable: {exc}") from exc


# ---------------------------------------------------------------- admin inventory

class MemoryInventory:
    name = "memory"

    def __init__(self):
        self.stock = starting_stock()

    def items(self) -> list[dict]:
        return [{"product_id": pid, "stock": qty} for pid, qty in self.stock.items()]

    def update(self, product_id: str, stock: int) -> None:
        if product_id not in self.stock:
            raise KeyError(product_id)
        self.stock[product_id] = stock

    def check(self) -> None:
        pass


class AzureTableInventory:
    """Stock records in an Azure Table: PartitionKey 'inventory', RowKey product_id."""

    name = "azuretable"
    PARTITION = "inventory"

    def __init__(self, account_url: str = "", table: str = "inventory", table_client=None, not_found=None):
        if table_client is None:
            from azure.core.exceptions import ResourceNotFoundError
            from azure.data.tables import TableClient
            from azure.identity import ManagedIdentityCredential
            table_client = TableClient(endpoint=account_url, table_name=table,
                                       credential=ManagedIdentityCredential(),
                                       connection_timeout=1.5, read_timeout=1.5)
            not_found = ResourceNotFoundError
        self.table, self.not_found = table_client, not_found or LookupError

    def items(self) -> list[dict]:
        try:
            rows = self.table.list_entities()
            return sorted(({"product_id": r["RowKey"], "stock": int(r["stock"])} for r in rows),
                          key=lambda i: i["product_id"])
        except Exception as exc:
            raise BackendError(f"Azure Table read failed: {exc}") from exc

    def update(self, product_id: str, stock: int) -> None:
        try:
            entity = self.table.get_entity(partition_key=self.PARTITION, row_key=product_id)
        except self.not_found as exc:
            raise KeyError(product_id) from exc
        except Exception as exc:
            raise BackendError(f"Azure Table read failed: {exc}") from exc
        try:
            entity["stock"] = stock
            self.table.update_entity(entity)
        except Exception as exc:
            raise BackendError(f"Azure Table write failed: {exc}") from exc

    def check(self) -> None:
        try:
            next(iter(self.table.list_entities(results_per_page=1)), None)
        except Exception as exc:
            raise BackendError(f"Azure Table unreachable: {exc}") from exc


# ---------------------------------------------------------------- factory

def _need(env: Mapping[str, str], *names: str) -> list[str]:
    missing = [n for n in names if not env.get(n, "").strip()]
    if missing:
        raise ValueError("missing environment variables: " + ", ".join(missing))
    return [env[n].strip() for n in names]


def backend_from_env(service_name: str, env: Mapping[str, str]):
    """Pick the backend for a service from its environment; None for critical services."""
    if service_name == "notify-api":
        kind = env.get("NOTIFY_BACKEND", "log").strip()
        if kind == "log":
            return LogNotifier()
        if kind == "pubsub":
            project, topic = _need(env, "PUBSUB_PROJECT", "PUBSUB_TOPIC")
            return PubSubNotifier(project, topic)
        raise ValueError(f"NOTIFY_BACKEND must be log or pubsub, not {kind!r}")
    if service_name == "recommend-api":
        kind = env.get("RECOMMEND_BACKEND", "memory").strip()
        if kind == "memory":
            return MemoryRecommendations()
        if kind == "s3":
            (bucket,) = _need(env, "S3_BUCKET")
            return S3Recommendations(bucket, env.get("S3_KEY", "recommendations.json"),
                                     env.get("S3_REGION", "ap-south-1"))
        raise ValueError(f"RECOMMEND_BACKEND must be memory or s3, not {kind!r}")
    if service_name == "admin-api":
        kind = env.get("ADMIN_BACKEND", "memory").strip()
        if kind == "memory":
            return MemoryInventory()
        if kind == "azuretable":
            (url,) = _need(env, "AZURE_TABLE_ACCOUNT_URL")
            return AzureTableInventory(url, env.get("AZURE_TABLE_NAME", "inventory"))
        raise ValueError(f"ADMIN_BACKEND must be memory or azuretable, not {kind!r}")
    return None


def catalog_seed_rows() -> list[dict]:
    return [{"PartitionKey": AzureTableInventory.PARTITION, "RowKey": p["product_id"], "stock": p["stock"]}
            for p in PRODUCTS]
