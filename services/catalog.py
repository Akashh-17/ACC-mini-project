"""Small hardcoded product catalog shared by the stub services and the storefront.

Prices are in INR. `stock` is the total starting stock: inventory-check splits
it between the clouds (allocation()) and replicates sales through Kafka
(services/ledger.py); admin-api keeps its own warehouse copy.
"""
from __future__ import annotations

PRODUCTS = [
    {"product_id": "p1", "name": "Wireless Earbuds", "price": 2499, "stock": 25},
    {"product_id": "p2", "name": "Smart Watch", "price": 7999, "stock": 15},
    {"product_id": "p3", "name": "Bluetooth Speaker", "price": 3499, "stock": 20},
    {"product_id": "p4", "name": "Laptop Backpack", "price": 1899, "stock": 30},
    {"product_id": "p5", "name": "4K Action Camera", "price": 18999, "stock": 8},
]

# "Customers also bought": the memory backend serves this directly; the S3
# backend serves the same map from recommendations.json (uploaded by services.seed).
RECOMMENDATIONS = {
    "p1": ["p3", "p2", "p4"],
    "p2": ["p1", "p5", "p4"],
    "p3": ["p1", "p4", "p2"],
    "p4": ["p5", "p1", "p3"],
    "p5": ["p4", "p2", "p3"],
}

PAYMENT_LIMIT = 50000      # payments-api declines amounts above this
FRAUD_THRESHOLD = 20000    # fraud-check flags amounts above this

# inventory-check stock is split between the clouds (per-cloud allocation): each
# cloud sells only from its own share, so the clouds can never oversell together.
REGIONS = ["aws-ap-south-1", "azure-indiasouthcentral", "gcp-asia-south1"]


def starting_stock() -> dict[str, int]:
    return {p["product_id"]: p["stock"] for p in PRODUCTS}


def allocation() -> dict[str, dict[str, int]]:
    """{region_id: {product_id: starting share}}; remainders go to the first regions."""
    out: dict[str, dict[str, int]] = {r: {} for r in REGIONS}
    for p in PRODUCTS:
        base, extra = divmod(p["stock"], len(REGIONS))
        for i, r in enumerate(REGIONS):
            out[r][p["product_id"]] = base + (1 if i < extra else 0)
    return out
