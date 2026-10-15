"""Small hardcoded product catalog shared by the stub services and the storefront.

Prices are in INR. `stock` is each service instance's starting stock; every
inventory-check / admin-api process keeps its own copy (no shared database).
"""
from __future__ import annotations

PRODUCTS = [
    {"product_id": "p1", "name": "Wireless Earbuds", "price": 2499, "stock": 25},
    {"product_id": "p2", "name": "Smart Watch", "price": 7999, "stock": 15},
    {"product_id": "p3", "name": "Bluetooth Speaker", "price": 3499, "stock": 20},
    {"product_id": "p4", "name": "Laptop Backpack", "price": 1899, "stock": 30},
    {"product_id": "p5", "name": "4K Action Camera", "price": 18999, "stock": 8},
]

PAYMENT_LIMIT = 50000      # payments-api declines amounts above this
FRAUD_THRESHOLD = 20000    # fraud-check flags amounts above this


def starting_stock() -> dict[str, int]:
    return {p["product_id"]: p["stock"] for p in PRODUCTS}
