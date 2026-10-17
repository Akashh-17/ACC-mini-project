"""Cross-cloud storefront demo.

    streamlit run storefront/app.py

Left: a small shop whose every checkout step is routed through the registry.
Right: what each region's registry currently believes, refreshed every 2 s,
plus demo controls that trigger failures through each region's proxy.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from services.catalog import PRODUCTS  # noqa: E402
from storefront.backend import (BACKEND_LABEL, SERVICES, SHORT, Registry, Unavailable,  # noqa: E402
                                admin_inventory, checkout, load_clouds, now, recommendations)

CONFIG = Path(os.environ.get("STOREFRONT_CONFIG", ROOT / "results" / "clouds.local.json"))
PRODUCTS_BY_ID = {p["product_id"]: p for p in PRODUCTS}
REGION_ORDER = ["gcp-asia-south1", "aws-ap-south-1", "azure-indiasouthcentral"]
BADGE = {"UP": "green", "DOWN": "red", "STALE": "orange"}

st.set_page_config(page_title="CrossCloud Store", page_icon=":material/storefront:", layout="wide")


@st.cache_resource
def get_registry() -> Registry:
    return Registry(load_clouds(CONFIG), home="gcp")


try:
    reg = get_registry()
except (OSError, ValueError) as exc:
    st.error(f"Cannot load cloud config {CONFIG}: {exc}", title="Configuration missing")
    st.stop()

ss = st.session_state
ss.setdefault("cart", {})
ss.setdefault("viewed", PRODUCTS[0]["product_id"])
ss.setdefault("events", [])
ss.setdefault("last_views", None)
ss.setdefault("last_order", None)


def log(msg: str) -> None:
    ss.events.insert(0, f"`{now()}` {msg}")
    del ss.events[40:]


def add_to_cart(pid: str) -> None:
    ss.cart[pid] = ss.cart.get(pid, 0) + 1
    ss.viewed = pid


def remove_from_cart(pid: str) -> None:
    ss.cart.pop(pid, None)


def inr(amount: float) -> str:
    return f"₹{amount:,.0f}"


st.title("CrossCloud Store", anchor=False)
st.caption("One shop, one backend running on AWS, Azure and GCP at the same time. "
           "Every checkout step asks the service registry where a healthy copy is, "
           "and fails over to another cloud if one breaks.")

shop, scenes = st.columns([3, 2], gap="large")

# ------------------------------------------------------------------ shop
with shop:
    st.subheader("Products", anchor=False)
    for row in range(0, len(PRODUCTS), 3):
        cols = st.columns(3)
        for col, p in zip(cols, PRODUCTS[row:row + 3]):
            with col.container(border=True, height="stretch"):
                st.markdown(f"**{p['name']}**")
                st.markdown(inr(p["price"]))
                st.button("Add to cart", key=f"add_{p['product_id']}", icon=":material/add_shopping_cart:",
                          on_click=add_to_cart, args=(p["product_id"],), width="stretch")

    with st.container(border=True):
        viewed = PRODUCTS_BY_ID[ss.viewed]
        st.markdown(f"**Customers who bought {viewed['name']} also bought**")
        try:
            rec = recommendations(reg, viewed["product_id"])
            names = [PRODUCTS_BY_ID[i]["name"] for i in rec.data["recommended_ids"] if i in PRODUCTS_BY_ID]
            st.markdown(" · ".join(names))
            source = BACKEND_LABEL.get(rec.data.get("source"), "built-in data")
            st.caption(f"recommend-api on {rec.region}, data from {source} (single-cloud by design)")
        except Unavailable:
            st.warning("Recommendations are unavailable right now. recommend-api runs on AWS only, "
                       "so it can't fail over. The rest of the shop still works.",
                       icon=":material/cloud_off:")

    with st.container(border=True):
        st.markdown("**Warehouse stock (admin-api)**")
        try:
            inv = admin_inventory(reg)
            stock = {i["product_id"]: i["stock"] for i in inv.data["items"]}
            st.markdown(" · ".join(f"{PRODUCTS_BY_ID[pid]['name']}: {qty}"
                                   for pid, qty in stock.items() if pid in PRODUCTS_BY_ID))
            st.caption(f"admin-api on {inv.region}, records in "
                       f"{BACKEND_LABEL.get(inv.data.get('source'), 'built-in data')} (single-cloud by design)")
        except Unavailable:
            st.warning("Stock admin is unavailable right now. admin-api runs on Azure only.",
                       icon=":material/cloud_off:")

    st.subheader("Cart", anchor=False)
    if not ss.cart:
        st.caption("Your cart is empty.")
    else:
        total = 0
        for pid, qty in list(ss.cart.items()):
            p = PRODUCTS_BY_ID[pid]
            total += p["price"] * qty
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"{qty} × {p['name']} — {inr(p['price'] * qty)}")
                st.button("Remove", key=f"rm_{pid}", icon=":material/delete:", type="tertiary",
                          on_click=remove_from_cart, args=(pid,))
        st.markdown(f"**Total: {inr(total)}**")
        if st.button("Place order", type="primary", icon=":material/payments:"):
            with st.status("Placing order across the clouds…", expanded=True) as status:
                ok, steps = checkout(reg, dict(ss.cart), PRODUCTS_BY_ID)
                status.update(label="Order placed" if ok else "Order not placed",
                              state="complete" if ok else "error")
            ss.last_order = (ok, steps, inr(total))
            for s in steps:
                if s.attempts:
                    log(f"{s.name}: {', '.join(s.attempts)} → retried, served by **{s.region}**")
            log(f"Order {'placed' if ok else 'failed'} ({inr(total)})")
            if ok:
                ss.cart = {}
            st.rerun()

    if ss.last_order:
        ok, steps, total = ss.last_order
        with st.container(border=True):
            st.markdown(f"**Last order — {total}: {'placed' if ok else 'not placed'}**")
            for s in steps:
                icon = ":material/check_circle:" if s.ok else ":material/error:"
                where = f" :blue-badge[via {s.region}]" if s.region else ""
                st.markdown(f"{icon} **{s.name}:** {s.detail}{where}")
                if s.attempts:
                    st.caption("Failed over after: " + "; ".join(s.attempts))

# ------------------------------------------------------------------ behind the scenes
with scenes:
    st.subheader("Behind the scenes", anchor=False)

    @st.fragment(run_every="2s")
    def registry_views():
        views = reg.views()
        prev = ss.last_views
        cols = st.columns(3)
        for col, obs in zip(cols, REGION_ORDER):
            with col.container(border=True, height="stretch"):
                st.markdown(f"**{SHORT[obs]}'s view**")
                if all(v is None for v in views[obs].values()):
                    st.error("Registry unreachable", icon=":material/cloud_off:")
                    continue
                for svc in SERVICES:
                    eps = {e["region_id"]: e["status"] for e in (views[obs][svc] or [])}
                    badges = " ".join(f":{BADGE.get(eps[r], 'gray')}-badge[{SHORT[r]}]"
                                      for r in REGION_ORDER if r in eps) or ":gray-badge[none]"
                    st.markdown(f"{svc}  \n{badges}")
        # event log: report every status change seen by any region's registry
        if prev:
            for obs in REGION_ORDER:
                for svc in SERVICES:
                    old = {e["region_id"]: e["status"] for e in (prev[obs].get(svc) or [])}
                    new = {e["region_id"]: e["status"] for e in (views[obs].get(svc) or [])}
                    for r in sorted(set(old) | set(new)):
                        if old.get(r) != new.get(r) and views[obs][svc] is not None and prev[obs][svc] is not None:
                            log(f"{SHORT[obs]} sees {svc} @{SHORT[r]}: {old.get(r, '—')} → **{new.get(r, 'gone')}**")
        ss.last_views = views
        st.caption(f"Updated {now()} · green UP · red DOWN · orange STALE")

        with st.container(border=True):
            st.markdown("**Event log**")
            with st.container(height=220):
                for e in ss.events[:25]:
                    st.markdown(e)

    registry_views()

    with st.expander("Demo controls", icon=":material/science:", expanded=True):
        def simulate(region: str, service: str, action: str) -> None:
            code = reg.simulate(region, service, action)
            log(f"Demo: {action} {service} on {SHORT[reg.clouds[region]['region_id']]} (HTTP {code})")

        st.markdown("**payments-api** (runs on all three clouds)")
        for region in ("gcp", "aws", "azure"):
            label = SHORT[reg.clouds[region]["region_id"]]
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"{label}")
                st.button("Fail", key=f"fail_pay_{region}", icon=":material/power_off:",
                          on_click=simulate, args=(region, "payments-api", "fail"))
                st.button("Recover", key=f"rec_pay_{region}", icon=":material/power:",
                          on_click=simulate, args=(region, "payments-api", "recover"))
        st.markdown("**recommend-api** (AWS only)")
        with st.container(horizontal=True):
            st.button("Fail", key="fail_rec_aws", icon=":material/power_off:",
                      on_click=simulate, args=("aws", "recommend-api", "fail"))
            st.button("Recover", key="rec_rec_aws", icon=":material/power:",
                      on_click=simulate, args=("aws", "recommend-api", "recover"))
