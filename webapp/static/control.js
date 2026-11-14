// The control room: cloud cards with fail/recover, registry views, ledgers,
// stock shares, live activity. Reads /api/state every 2 s, /api/events every 1 s.
const C = { state: null, products: [], lastSeq: 0, feed: [], busy: new Set() };
const $ = (id) => document.getElementById(id);
const CLOUD_OF_REGION = { "gcp-asia-south1": "gcp", "aws-ap-south-1": "aws", "azure-indiasouthcentral": "azure" };
const REGION_NAME = { "gcp-asia-south1": "asia-south1 · Mumbai", "aws-ap-south-1": "ap-south-1 · Mumbai",
                      "azure-indiasouthcentral": "India South Central" };
const SERVICE_ORDER = ["payments-api", "fraud-check", "inventory-check", "recommend-api", "admin-api", "notify-api"];
const CRITICAL = new Set(["payments-api", "fraud-check", "inventory-check"]);
const BACKEND = { "notify-api": "Cloud Pub/Sub", "recommend-api": "Amazon S3", "admin-api": "Azure Table Storage" };
const FEED_COLOR = { order: "#E6E8EE", failover: "#FB923C", status: "#F87171", replicated: "#34D399", demo: "#9AA3B5", error: "#F87171" };

$("brand-icon").innerHTML = ICON.cloud;
$("store-link").innerHTML = ICON.cart + "Open store";

// ---------------------------------------------------------------- helpers

// status of `service` in `region` as reported by `observer`'s registry (null if unknown)
function statusOf(observer, service, region) {
  const eps = C.state?.views?.[observer]?.[service];
  if (!eps) return null;
  const e = eps.find((x) => x.region_id === region);
  return e ? e.status : "none";
}

function firstReadable(map) {
  return REGION_ORDER.map((r) => map?.[r]).find(Boolean) || null;
}

// ---------------------------------------------------------------- metrics + header

function renderMetrics() {
  const m = C.state.metrics;
  const tiles = [
    ["Replication", m.replication_ms === null ? "—" : `${m.replication_ms} ms`,
      m.replication_ms === null ? "no live replicated payment listed yet" : "median of payments now in the ledgers"],
    ["Healthy endpoints", m.endpoints ? `${m.healthy} / ${m.endpoints}` : "—",
      m.endpoints && m.healthy < m.endpoints ? `${m.endpoints - m.healthy} not UP` : "as seen by the home registry"],
    ["Orders this session", String(m.orders), m.orders ? `${inr(m.order_total)} total` : "place one in the store"],
    ["Waiting for Kafka", String(m.unsent), m.unsent ? "changes not yet sent" : "every change sent"],
  ];
  $("metrics").innerHTML = tiles.map(([label, value, note]) => `
    <div class="card metric"><span class="label">${label}</span><span class="value">${esc(value)}</span><span class="note">${esc(note)}</span></div>`).join("");

  const books = REGION_ORDER.map((r) => C.state.ledgers?.[r]);
  const unreachable = books.filter((b) => !b).length;
  const pill = $("kafka-pill");
  if (C.state.age_s !== null && C.state.age_s > 10) {
    pill.className = "pill warn"; pill.textContent = `Cloud data ${Math.round(C.state.age_s)} s old`;
  } else if (unreachable) {
    pill.className = "pill warn"; pill.textContent = `Kafka · ${unreachable} ledger${unreachable > 1 ? "s" : ""} unreachable`;
  } else if (m.unsent) {
    pill.className = "pill warn"; pill.textContent = `Kafka · ${m.unsent} change${m.unsent > 1 ? "s" : ""} waiting`;
  } else {
    pill.className = "pill ok"; pill.innerHTML = '<span class="dot UP"></span>Kafka · all copies in sync';
  }
}

// ---------------------------------------------------------------- cloud cards

function renderClouds() {
  const stockView = (r) => C.state.stocks?.[r];
  $("clouds").innerHTML = REGION_ORDER.map((region) => {
    const label = regionLabel(region);
    const ownView = C.state.views?.[region];
    const ownReachable = ownView && Object.values(ownView).some((v) => v !== null);
    const observer = ownReachable ? region : REGION_ORDER.find((r) => C.state.views?.[r] && Object.values(C.state.views[r]).some((v) => v));
    const services = SERVICE_ORDER.filter((svc) => (C.state.views?.[observer]?.[svc] || []).some((e) => e.region_id === region));
    const share = (stockView(region)?.items || []).reduce((n, i) => n + i.local_share, 0);
    const rows = services.map((svc) => {
      const st = statusOf(observer, svc, region) || "none";
      const detail = svc === "inventory-check" && stockView(region) ? `critical · can sell ${share} units`
        : CRITICAL.has(svc) ? "critical" : BACKEND[svc] || "standard";
      const action = st === "DOWN" ? "recover" : "fail";
      const key = `${region}/${svc}`;
      return `<li class="svc">
        <span class="dot ${st}"></span>
        <span class="grow"><span class="name">${svc}</span><span class="detail">${esc(detail)}</span></span>
        <span class="badge ${st}">${st === "none" ? "—" : st}</span>
        <button class="btn btn-sm ${action === "recover" ? "recover" : ""}" type="button"
          data-sim="${CLOUD_OF_REGION[region]}|${svc}|${action}" ${C.busy.has(key) ? "disabled" : ""}
          aria-label="${action === "recover" ? "Recover" : "Fail"} ${svc} on ${label}">${action === "recover" ? "Recover" : "Fail"}</button>
      </li>`;
    }).join("");
    return `<article class="card cloud">
      <div class="stripe" style="background:${CLOUD_COLOR[label]}"></div>
      <div class="inner">
        <div class="spread" style="align-items:center">
          <div><h3 style="font-size:22px">${label}</h3><span class="muted" style="font-size:12px">${REGION_NAME[region]}</span></div>
          <span class="badge ${ownReachable ? "none" : "bad"}">${ownReachable ? "registry reachable" : "registry unreachable"}</span>
        </div>
        ${rows ? `<ul class="svc-list">${rows}</ul>` : '<p class="muted" style="margin:0">No registry could be reached.</p>'}
      </div>
    </article>`;
  }).join("");
}

// ---------------------------------------------------------------- registry views

function renderViews() {
  $("views").innerHTML = SERVICE_ORDER.map((svc) => `<tr><th scope="row">${svc}</th>${REGION_ORDER.map((obs) => {
    const eps = C.state.views?.[obs]?.[svc];
    if (eps === null || eps === undefined) return '<td><span class="badge bad">unreachable</span></td>';
    const sorted = [...eps].sort((a, b) => REGION_ORDER.indexOf(a.region_id) - REGION_ORDER.indexOf(b.region_id));
    return `<td><div class="cells">${sorted.map((e) =>
      `<span class="badge ${e.status}" title="${regionLabel(e.region_id)}: ${e.status}">${regionLabel(e.region_id)} ${e.status}</span>`).join("") || '<span class="badge none">none</span>'}</div></td>`;
  }).join("")}</tr>`).join("");
}

// ---------------------------------------------------------------- ledgers

function lagText(p) {
  if (!p.replicated) return "local";
  return p.lag_ms < 60000 ? `+${p.lag_ms} ms` : "catch-up";
}

function renderLedgers() {
  $("ledgers").innerHTML = REGION_ORDER.map((r) => {
    const book = C.state.ledgers?.[r];
    const label = regionLabel(r);
    if (!book) return `<div class="ledger"><strong>${label}'s copy</strong><span class="badge bad">unreachable</span></div>`;
    const totals = Object.values(book.payments_by_region);
    const count = totals.reduce((n, t) => n + t.count, 0);
    const sum = totals.reduce((n, t) => n + t.total, 0);
    const rows = book.recent.slice(0, 4).map((p) => `<div class="pay">
        <span>${inr(p.amount)} ${chip(regionLabel(p.region_id))}</span><span class="mono muted" style="font-size:12px">${lagText(p)}</span></div>`).join("");
    return `<div class="ledger">
      <div class="spread"><strong>${label}'s copy</strong><span class="muted" style="font-size:12px">${count} payment${count === 1 ? "" : "s"} · ${inr(sum)}</span></div>
      ${rows || '<span class="muted" style="font-size:13px">No payments yet</span>'}
      <span style="font-size:12px;color:${book.unsent_events ? "#FCD34D" : "#6EE7B7"}">${book.unsent_events
        ? `${book.unsent_events} change${book.unsent_events > 1 ? "s" : ""} waiting for Kafka` : "0 changes waiting for Kafka"}</span>
    </div>`;
  }).join("");
}

// ---------------------------------------------------------------- stock

function renderStock() {
  const view = firstReadable(C.state.stocks);
  if (!view || !C.products.length) { $("stock").innerHTML = '<p class="muted" style="margin:0">Stock unavailable</p>'; return; }
  const names = Object.fromEntries(C.products.map((p) => [p.product_id, p]));
  $("stock").innerHTML = view.items.filter((i) => names[i.product_id]).map((i) => {
    const initial = names[i.product_id].initial;
    const segs = REGION_ORDER.map((r) => {
      const n = Math.max(0, i.by_region[r] || 0);
      return `<div style="width:${(100 * n / initial).toFixed(1)}%;background:${CLOUD_COLOR[regionLabel(r)]}" title="${regionLabel(r)}: ${n}"></div>`;
    }).join("");
    const parts = REGION_ORDER.map((r) => `${regionLabel(r)} ${i.by_region[r]}`).join(" · ");
    return `<div style="display:flex;flex-direction:column;gap:6px">
      <div class="spread" style="font-size:13px"><strong>${esc(names[i.product_id].name)}</strong>
        <span style="color:#B6BDCB">${i.global_stock} of ${initial} left · ${parts}</span></div>
      <div class="bar" role="img" aria-label="${esc(names[i.product_id].name)}: ${parts}, ${initial - i.global_stock} sold">${segs}</div>
    </div>`;
  }).join("");
}

// ---------------------------------------------------------------- feed

function renderFeed(freshFrom = Infinity) {
  $("feed").innerHTML = C.feed.map((e) => `<li class="ev ${e.seq >= freshFrom ? "fresh" : ""}">
    <span class="t">${esc(e.time)}</span><span class="rail" style="background:${FEED_COLOR[e.kind] || "#9AA3B5"}"></span>
    <span style="display:flex;flex-direction:column;gap:2px;min-width:0"><span class="title">${esc(e.title)}</span>
    ${e.detail ? `<span class="detail">${esc(e.detail)}</span>` : ""}</span></li>`).join("")
    || '<li class="muted" style="font-size:13px;padding:8px">Nothing yet. Place an order or fail a service.</li>';
}

async function pollEvents() {
  const events = await api(`/api/events?after=${C.lastSeq}`);
  if (!events.length) return;
  const first = events[0].seq;
  C.lastSeq = events[events.length - 1].seq;
  C.feed = [...events.reverse(), ...C.feed].slice(0, 60);
  renderFeed(first);  // highlight what just arrived
}

// ---------------------------------------------------------------- state

async function pollState() {
  C.state = await api("/api/state");
  renderMetrics(); renderClouds(); renderViews(); renderLedgers(); renderStock();
}

// ---------------------------------------------------------------- actions

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-sim]");
  if (!btn) return;
  const [cloud, service, action] = btn.dataset.sim.split("|");
  const region = Object.keys(CLOUD_OF_REGION).find((r) => CLOUD_OF_REGION[r] === cloud);
  const key = `${region}/${service}`;
  C.busy.add(key); btn.disabled = true;
  try {
    const r = await api("/api/simulate", { method: "POST", body: { cloud, service, action } });
    if (r.status_code !== 200) toast(`${service} on ${regionLabel(region)}: HTTP ${r.status_code}`);
  } catch (e) {
    toast(`Could not ${action} ${service}: ${e.message}`);
  } finally {
    setTimeout(() => { C.busy.delete(key); }, 1500);  // until the registries have noticed
  }
});

$("transfer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = { product_id: $("t-product").value, qty: Number($("t-qty").value),
                 from_cloud: $("t-from").value, to_cloud: $("t-to").value };
  if (body.from_cloud === body.to_cloud) return toast("Pick two different clouds");
  try {
    const r = await api("/api/transfer", { method: "POST", body });
    toast(r.status === "transferred" ? "Stock moved" : "Not enough stock in that cloud's share", r.status === "transferred" ? "ok" : "error");
  } catch (e) {
    toast("Transfer failed: " + e.message);
  }
});

setInterval(() => { $("clock").textContent = "live · " + new Date().toLocaleTimeString("en-GB"); }, 1000);

(async () => {
  try {
    C.products = await api("/api/products");
    $("t-product").innerHTML = C.products.map((p) => `<option value="${p.product_id}">${esc(p.name)}</option>`).join("");
  } catch (e) { toast("Cannot reach the demo server: " + e.message); }
  try {
    const old = await api("/api/events?after=0");
    C.feed = old.reverse().slice(0, 60);
    C.lastSeq = C.feed.length ? C.feed[0].seq : 0;
    renderFeed();
  } catch (_) { renderFeed(); }
  every(2000, pollState);
  every(1000, pollEvents);
})();
