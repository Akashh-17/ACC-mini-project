"""Copies this cloud's ledger events to Kafka and applies every cloud's events.

    python -m services.replicator      (systemd: deploy/registry-replicator.service)

One loop, one thread:
  1. publish: unsent outbox rows -> Kafka (idempotent producer, acks=all). A row
     is marked sent only when Kafka confirms it; failures are retried later.
  2. consume: read `payments` and `inventory-events` from the beginning (own
     consumer group per region) and apply each event to the local ledger.
     Duplicates are ignored by event_id, so redelivery is harmless. The offset
     is stored only after the event is applied.

Kafka being unreachable only delays replication; payments-api and
inventory-check never wait for it. Settings come from the environment
(see deploy/replicator.env.example); the API secret is never logged.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from typing import Callable, Optional

from .ledger import INVENTORY_TOPIC, PAYMENTS_TOPIC, InvalidEvent, Ledger, open_ledger

log = logging.getLogger("replicator")


def emit(event: str, **fields) -> None:
    """One JSON line per event, like the registry's replication log."""
    log.info(json.dumps({"event": event, **fields}, sort_keys=True))


class Replicator:
    def __init__(self, ledger: Ledger, producer, consumer, topics: list[str],
                 clock: Callable[[], float] = time.time, batch: int = 100):
        self.ledger, self.producer, self.consumer = ledger, producer, consumer
        self.topics, self.clock, self.batch = topics, clock, batch
        self.in_flight: set[int] = set()
        self.kafka_ok: Optional[bool] = None

    # ---------------------------------------------------------------- publish

    def publish_pending(self) -> int:
        """Hand unsent outbox rows to the producer; returns how many were queued."""
        rows = self.ledger.pending(self.batch, exclude=frozenset(self.in_flight))
        for row in rows:
            self.in_flight.add(row["seq"])
            try:
                self.producer.produce(row["topic"], key=row["key"].encode(), value=row["payload"].encode(),
                                      on_delivery=self._delivered(row["seq"], row["topic"]))
            except BufferError:  # local queue full; retry next round
                self.in_flight.discard(row["seq"])
                break
        self.producer.poll(0)
        return len(rows)

    def _delivered(self, seq: int, topic: str):
        def callback(err, msg):
            self.in_flight.discard(seq)
            if err is not None:
                self._set_kafka(False, str(err))
                return
            self.ledger.mark_sent(seq)
            self._set_kafka(True)
            emit("kafka_published", topic=topic, seq=seq)
        return callback

    def _set_kafka(self, ok: bool, why: str = "") -> None:
        if ok != self.kafka_ok:
            (log.info if ok else log.warning)("kafka %s%s", "reachable" if ok else "UNREACHABLE",
                                              f": {why}" if why else "")
            self.kafka_ok = ok

    # ---------------------------------------------------------------- consume

    def consume(self, timeout: float = 0.5, max_messages: int = 100) -> int:
        """Apply up to max_messages events; returns how many were new."""
        applied = 0
        for _ in range(max_messages):
            msg = self.consumer.poll(timeout if applied == 0 else 0)
            if msg is None:
                break
            if msg.error():
                self._set_kafka(False, str(msg.error()))
                continue
            try:
                event = json.loads(msg.value())
                if self.ledger.apply_remote(event):
                    applied += 1
                    emit("kafka_applied", type=event["type"], origin=event["origin"],
                         event_id=event["event_id"], lag_ms=round((self.clock() - float(event["ts"])) * 1000))
            except (InvalidEvent, ValueError, TypeError) as exc:  # poison message: skip it, never block the topic
                log.warning("skipping bad message at %s[%s]@%s: %s",
                            msg.topic(), msg.partition(), msg.offset(), exc)
            self.consumer.store_offsets(message=msg)
        return applied

    # ---------------------------------------------------------------- loop

    def run_once(self) -> None:
        self.publish_pending()
        self.consume()
        self.producer.poll(0)

    def run(self, stop: Callable[[], bool]) -> None:
        self.consumer.subscribe(self.topics)
        emit("replicator_started", region_id=self.ledger.region_id, topics=self.topics)
        try:
            while not stop():
                self.run_once()
        finally:
            self.producer.flush(10)
            self.consumer.close()


# -------------------------------------------------------------------- setup

def kafka_config(env) -> tuple[dict, dict]:
    """(producer config, consumer config) for Confluent Cloud (SASL_SSL/PLAIN)."""
    missing = [n for n in ("KAFKA_BOOTSTRAP", "KAFKA_API_KEY", "KAFKA_API_SECRET", "REGION_ID")
               if not env.get(n, "").strip()]
    if missing:
        raise ValueError("missing environment variables: " + ", ".join(missing))
    region = env["REGION_ID"].strip()
    common = {"bootstrap.servers": env["KAFKA_BOOTSTRAP"].strip(),
              "security.protocol": "SASL_SSL", "sasl.mechanisms": "PLAIN",
              "sasl.username": env["KAFKA_API_KEY"].strip(),
              "sasl.password": env["KAFKA_API_SECRET"].strip(),
              "client.id": f"replicator-{region}"}
    producer = {**common, "enable.idempotence": True, "acks": "all", "linger.ms": 5,
                "message.timeout.ms": 30000}
    consumer = {**common, "group.id": env.get("KAFKA_GROUP", f"replicator-{region}").strip(),
                "auto.offset.reset": "earliest", "enable.auto.commit": True,
                "enable.auto.offset.store": False}
    return producer, consumer


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    env = os.environ
    producer_conf, consumer_conf = kafka_config(env)
    path = env.get("LEDGER_PATH", "").strip()
    if not path:
        raise ValueError("LEDGER_PATH must be set (same file as payments-api and inventory-check)")
    from confluent_kafka import Consumer, Producer  # only needed on the VMs

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(True))
    Replicator(open_ledger(path, env["REGION_ID"].strip()), Producer(producer_conf),
               Consumer(consumer_conf), [PAYMENTS_TOPIC, INVENTORY_TOPIC]).run(lambda: bool(stopping))


if __name__ == "__main__":
    main()
