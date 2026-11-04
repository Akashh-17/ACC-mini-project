"""Ledger + replicator: three regions exchanging events through a fake Kafka."""
import json

import pytest
from fastapi.testclient import TestClient

from services.app import create_app
from services.catalog import PRODUCTS, REGIONS, allocation
from services.ledger import InvalidEvent, Ledger, validate
from services.replicator import Replicator, kafka_config

AWS, AZURE, GCP = REGIONS


class FakeMessage:
    def __init__(self, topic, offset, value, error=None):
        self._topic, self._offset, self._value, self._error = topic, offset, value, error

    def topic(self): return self._topic
    def partition(self): return 0
    def offset(self): return self._offset
    def value(self): return self._value
    def error(self): return self._error


class FakeKafka:
    """Topics are append-only lists; `up` False makes every delivery fail."""

    def __init__(self):
        self.topics = {"payments": [], "inventory-events": []}
        self.up = True


class FakeProducer:
    def __init__(self, kafka):
        self.kafka, self.queue = kafka, []

    def produce(self, topic, key, value, on_delivery):
        self.queue.append((topic, value, on_delivery))

    def poll(self, timeout=0):
        queue, self.queue = self.queue, []
        for topic, value, cb in queue:
            if self.kafka.up:
                self.kafka.topics[topic].append(value)
                cb(None, FakeMessage(topic, len(self.kafka.topics[topic]) - 1, value))
            else:
                cb("broker down", None)

    def flush(self, timeout=0):
        self.poll()


class FakeConsumer:
    def __init__(self, kafka):
        self.kafka, self.pos, self.stored = kafka, {}, {}

    def subscribe(self, topics):
        self.pos = {t: self.stored.get(t, 0) for t in topics}

    def poll(self, timeout=0):
        if not self.kafka.up:
            return None
        for topic, pos in self.pos.items():
            if pos < len(self.kafka.topics[topic]):
                self.pos[topic] = pos + 1
                return FakeMessage(topic, pos, self.kafka.topics[topic][pos])
        return None

    def store_offsets(self, message):
        self.stored[message.topic()] = message.offset() + 1

    def close(self):
        pass


def make_world(kafka=None):
    kafka = kafka or FakeKafka()
    world = {}
    for r in REGIONS:
        ledger = Ledger(":memory:", r)
        rep = Replicator(ledger, FakeProducer(kafka), FakeConsumer(kafka), ["payments", "inventory-events"])
        rep.consumer.subscribe(rep.topics)
        world[r] = (ledger, rep)
    return kafka, world


def sync(world, rounds=2):
    for _ in range(rounds):
        for _, rep in world.values():
            rep.run_once()


def share(ledger, region, pid):
    return next(i for i in ledger.stock() if i["product_id"] == pid)["by_region"][region]


# ---------------------------------------------------------------- allocation

def test_allocation_splits_all_stock():
    alloc = allocation()
    for p in PRODUCTS:
        shares = [alloc[r][p["product_id"]] for r in REGIONS]
        assert sum(shares) == p["stock"] and max(shares) - min(shares) <= 1


# ---------------------------------------------------------------- local ledger

def test_reserve_only_from_own_share():
    ledger = Ledger(":memory:", GCP)
    mine = allocation()[GCP]["p1"]
    assert ledger.reserve("o1", [("p1", mine)]) == (True, [{"product_id": "p1", "ok": True}])
    assert ledger.reserve("o2", [("p1", 1)])[0] is False
    p1 = next(i for i in ledger.stock() if i["product_id"] == "p1")
    assert p1["local_share"] == 0 and p1["global_stock"] == PRODUCTS[0]["stock"] - mine


def test_reserve_all_or_nothing_and_duplicate_lines():
    ledger = Ledger(":memory:", GCP)
    mine = allocation()[GCP]["p1"]
    ok, status = ledger.reserve("o1", [("p1", 1), ("p2", 10_000)])
    assert not ok and status == [{"product_id": "p1", "ok": True}, {"product_id": "p2", "ok": False}]
    assert share(ledger, GCP, "p1") == mine
    # the same product twice counts against the share together
    assert ledger.reserve("o2", [("p1", mine), ("p1", 1)])[0] is False
    assert ledger.reserve("o3", [("p1", 1), ("p1", 1)])[0] is True
    assert share(ledger, GCP, "p1") == mine - 2


def test_local_writes_go_to_outbox():
    ledger = Ledger(":memory:", AZURE)
    ledger.record_payment("o1", 999.0, "INR")
    ledger.reserve("o1", [("p1", 1)])
    rows = ledger.pending()
    assert [r["topic"] for r in rows] == ["payments", "inventory-events"]
    assert json.loads(rows[0]["payload"])["origin"] == AZURE


def test_transfer_moves_share():
    ledger = Ledger(":memory:", AWS)
    mine = allocation()[AWS]["p5"]
    assert ledger.transfer("p5", mine + 1, GCP) is False
    assert ledger.transfer("p5", 2, GCP) is True
    assert share(ledger, AWS, "p5") == mine - 2
    assert share(ledger, GCP, "p5") == allocation()[GCP]["p5"] + 2
    with pytest.raises(ValueError):
        ledger.transfer("p5", 1, AWS)


def test_file_ledger_shared_between_processes(tmp_path):
    # payments-api and the replicator open the same file
    path = str(tmp_path / "ledger.db")
    Ledger(path, GCP).record_payment("o1", 10.0, "INR")
    assert len(Ledger(path, GCP).pending()) == 1


# ---------------------------------------------------------------- replication

def test_payment_on_azure_reaches_aws_and_gcp():
    kafka, world = make_world()
    world[AZURE][0].record_payment("ord_1", 2499.0, "INR")
    sync(world)
    for r in (AWS, GCP):
        [p] = world[r][0].payments()
        assert p["order_id"] == "ord_1" and p["region_id"] == AZURE and p["replicated"]
    assert world[AZURE][0].payments()[0]["replicated"] is False
    assert world[AZURE][0].summary()["unsent_events"] == 0


def test_stock_converges_everywhere():
    kafka, world = make_world()
    world[AWS][0].reserve("o1", [("p1", 2)])
    world[GCP][0].reserve("o2", [("p1", 3), ("p2", 1)])
    world[AZURE][0].transfer("p1", 1, GCP)
    sync(world)
    # local_share differs by design; the global picture must be identical
    views = [[(i["product_id"], i["global_stock"], i["by_region"]) for i in world[r][0].stock()]
             for r in REGIONS]
    assert views[0] == views[1] == views[2]
    assert views[0][0][1] == PRODUCTS[0]["stock"] - 5


def test_duplicates_are_ignored():
    kafka, world = make_world()
    world[GCP][0].record_payment("o1", 10.0, "INR")
    sync(world)
    kafka.topics["payments"].append(kafka.topics["payments"][0])  # redelivery
    for _, rep in world.values():
        rep.consumer.subscribe(rep.topics)  # pretend offsets were lost
        rep.consumer.pos = {t: 0 for t in rep.topics}
    sync(world)
    assert all(len(world[r][0].payments()) == 1 for r in REGIONS)


def test_kafka_down_does_not_block_writes_and_catches_up():
    kafka, world = make_world()
    kafka.up = False
    world[AZURE][0].record_payment("o1", 10.0, "INR")
    sync(world)
    assert world[AZURE][0].summary()["unsent_events"] == 1
    assert world[AWS][0].payments() == []
    kafka.up = True
    sync(world)
    assert world[AZURE][0].summary()["unsent_events"] == 0
    assert len(world[AWS][0].payments()) == 1


def test_lost_database_is_rebuilt_from_kafka():
    kafka, world = make_world()
    world[GCP][0].record_payment("o1", 10.0, "INR")
    sync(world)
    fresh = Ledger(":memory:", GCP)
    rep = Replicator(fresh, FakeProducer(kafka), FakeConsumer(kafka), ["payments", "inventory-events"])
    rep.consumer.subscribe(rep.topics)
    rep.run_once()
    assert fresh.payments()[0]["order_id"] == "o1"


def test_bad_message_is_skipped_not_fatal():
    kafka, world = make_world()
    kafka.topics["payments"].append(b"not json")
    kafka.topics["payments"].append(json.dumps({"type": "payment_authorized", "event_id": "x",
                                                "origin": "evil-cloud", "ts": 1}).encode())
    world[GCP][0].record_payment("o1", 10.0, "INR")
    sync(world)
    assert len(world[AWS][0].payments()) == 1


def test_no_oversell_while_partitioned():
    # Kafka is down; every region sells as much as it can. Total sold never exceeds stock.
    kafka, world = make_world()
    kafka.up = False
    sold = 0
    for r in REGIONS:
        while world[r][0].reserve("o", [("p5", 1)])[0]:
            sold += 1
    assert sold == PRODUCTS[4]["stock"]
    kafka.up = True
    sync(world)
    assert all(world[r][0].stock()[4]["global_stock"] == 0 for r in REGIONS)


@pytest.mark.parametrize("event", [
    {"type": "payment_authorized", "event_id": "e", "origin": GCP, "ts": 1,
     "txn_id": "t", "order_id": "o", "amount": -5, "currency": "INR"},
    {"type": "stock_reserved", "event_id": "e", "origin": GCP, "ts": 1, "items": []},
    {"type": "stock_transferred", "event_id": "e", "origin": GCP, "ts": 1,
     "product_id": "p1", "qty": 1, "to_region": GCP},
    {"type": "refund", "event_id": "e", "origin": GCP, "ts": 1},
    ["not", "a", "dict"],
])
def test_invalid_events_rejected(event):
    with pytest.raises(InvalidEvent):
        validate(event)


def test_kafka_config_requires_settings_and_hides_nothing_extra():
    with pytest.raises(ValueError, match="KAFKA_API_SECRET"):
        kafka_config({"KAFKA_BOOTSTRAP": "b:9092", "KAFKA_API_KEY": "k", "REGION_ID": GCP})
    prod, cons = kafka_config({"KAFKA_BOOTSTRAP": "b:9092", "KAFKA_API_KEY": "k",
                               "KAFKA_API_SECRET": "s", "REGION_ID": GCP})
    assert prod["enable.idempotence"] is True and prod["acks"] == "all"
    assert cons["group.id"] == f"replicator-{GCP}" and cons["auto.offset.reset"] == "earliest"
    assert cons["enable.auto.offset.store"] is False


# ---------------------------------------------------------------- HTTP endpoints

def test_endpoints_use_shared_ledger():
    ledger = Ledger(":memory:", GCP)
    pay = TestClient(create_app("payments-api", GCP, ledger=ledger))
    inv = TestClient(create_app("inventory-check", GCP, ledger=ledger))
    txn = pay.post("/authorize", json={"order_id": "o1", "amount": 100, "currency": "INR",
                                       "card_token": "tok"}).json()["transaction_id"]
    body = pay.get("/ledger").json()
    assert body["recent"][0]["txn_id"] == txn and body["unsent_events"] == 1
    assert inv.post("/reserve", json={"order_id": "o1", "items": [{"product_id": "p1", "qty": 1}]}
                    ).json()["status"] == "reserved"
    p1 = inv.get("/stock").json()["items"][0]
    assert p1["local_share"] == allocation()[GCP]["p1"] - 1
    assert inv.post("/stock/transfer", json={"product_id": "p1", "qty": 1, "to_region": AWS}
                    ).json() == {"status": "transferred"}
    assert inv.post("/stock/transfer", json={"product_id": "p1", "qty": 1, "to_region": GCP}
                    ).status_code == 422
    assert ledger.summary()["unsent_events"] == 3


def test_declined_payment_not_recorded():
    ledger = Ledger(":memory:", GCP)
    pay = TestClient(create_app("payments-api", GCP, ledger=ledger))
    pay.post("/authorize", json={"order_id": "o1", "amount": 10**6, "currency": "INR", "card_token": "t"})
    assert ledger.payments() == []
