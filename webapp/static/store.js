// The shop: products with live stock, recommendations, cart, checkout trace.
const S = { products: [], cart: {}, viewed: "p1", busy: false, lastSeq: 0, watchTxn: null, mirrored: [] };
const $ = (id) => document.getElementById(id);

try { S.cart = JSON.parse(localStorage.getItem("cc-cart") || "{}"); } catch (_) { S.cart = {}; }
const saveCart = () => { try { localStorage.setItem("cc-cart", JSON.stringify(S.cart)); } catch (_) { /* private mode */ } };

$("brand-icon").innerHTML = ICON.cloud;
$("search-icon").innerHTML = ICON.search;
$("control-link").innerHTML = ICON.screen + "Control room";

// ---------------------------------------------------------------- products

function stockBadge(n) {
  if (n === null || n === undefined) return '<span class="stock unknown">Stock unknown</span>';
  if (n <= 0) return '<span class="stock out">Sold out</span>';
  if (n <= 5) return `<span class="stock low">Only ${n} left</span>`;
  return `<span class="stock ok">${n} in stock</span>`;
}

function renderProducts() {
  const q = $("search").value.trim().toLowerCase();
  const shown = S.products.filter((p) => !q || p.name.toLowerCase().includes(q));
  $("no-match").hidden = shown.length > 0 || !S.products.length;
  $("products").innerHTML = shown.map((p) => `
    <article class="card product">
      <div class="art" style="background:${PRODUCT_ART[p.product_id]?.bg || "#EEF2FF"}">${productArt(p.product_id)}</div>
      <div class="body">
        <h3>${esc(p.name)}</h3>
        <div class="spread" style="align-items:center"><span class="price">${inr(p.price)}</span>${stockBadge(p.stock)}</div>
        <button class="btn btn-dark" type="button" data-add="${esc(p.product_id)}" ${p.stock === 0 ? "disabled" : ""}>
          ${p.stock === 0 ? "Sold out" : "Add to cart"}</button>
      </div>
    </article>`).join("");
}

async function loadProducts() {
  const fresh = await api("/api/products");
  if (JSON.stringify(fresh) === JSON.stringify(S.products)) return;  // keep hover/focus
  S.products = fresh;
  renderProducts();
  renderCart();
}

// ---------------------------------------------------------------- recommendations

async function loadRecs() {
  const viewed = S.products.find((p) => p.product_id === S.viewed);
  if (viewed) $("recs-h").textContent = `Customers who bought the ${viewed.name} also bought`;
  try {
    const r = await api(`/api/recommendations?product_id=${encodeURIComponent(S.viewed)}`);
    const byId = Object.fromEntries(S.products.map((p) => [p.product_id, p]));
    $("recs").innerHTML = '<div class="rec-list">' + r.recommended_ids.filter((id) => byId[id]).map((id) =>
      `<button class="rec" type="button" data-add="${esc(id)}">${esc(byId[id].name)} · ${inr(byId[id].price)}</button>`).join("") + "</div>";
    const source = { s3: "Amazon S3", memory: "built-in data" }[r.source] || r.source;
    $("recs-source").textContent = `recommend-api · ${r.region} · data from ${source}`;
  } catch (e) {
    $("recs").innerHTML = `<div class="notice">${ICON.off}<span>Recommendations are unavailable right now — recommend-api runs on AWS only, so it can't fail over. The rest of the shop still works.</span></div>`;
    $("recs-source").textContent = "recommend-api · single-cloud by design";
  }
}

// ---------------------------------------------------------------- cart

function cartTotal() {
  return Object.entries(S.cart).reduce((sum, [pid, q]) => sum + (S.products.find((p) => p.product_id === pid)?.price || 0) * q, 0);
}

function renderCart() {
  const lines = Object.entries(S.cart).filter(([pid]) => S.products.some((p) => p.product_id === pid));
  const count = lines.reduce((n, [, q]) => n + q, 0);
  $("cart-button").innerHTML = `${ICON.cart}Cart <span class="cart-count">${count}</span>`;
  $("cart-lines").innerHTML = lines.length ? lines.map(([pid, q]) => {
    const p = S.products.find((x) => x.product_id === pid);
    return `<div class="cart-line">
      <span class="name">${esc(p.name)}</span>
      <span class="qty">
        <button class="btn icon-btn" type="button" data-dec="${pid}" aria-label="One fewer ${esc(p.name)}">−</button>
        <span aria-label="quantity">${q}</span>
        <button class="btn icon-btn" type="button" data-add="${pid}" aria-label="One more ${esc(p.name)}">+</button>
      </span>
      <strong style="min-width:84px;text-align:right">${inr(p.price * q)}</strong>
    </div>`;
  }).join("") : '<p class="muted" style="margin:0">Your cart is empty.</p>';
  $("cart-total").textContent = inr(cartTotal());
  $("place-order").disabled = S.busy || !lines.length;
}

function addToCart(pid) {
  S.cart[pid] = (S.cart[pid] || 0) + 1;
  S.viewed = pid;
  saveCart(); renderCart(); loadRecs();
}

function decrement(pid) {
  S.cart[pid] = (S.cart[pid] || 0) - 1;
  if (S.cart[pid] <= 0) delete S.cart[pid];
  saveCart(); renderCart();
}

// ---------------------------------------------------------------- checkout trace

const STEP_NAMES = ["Fraud check", "Reserve stock", "Payment", "Confirmation email"];

function traceShell(title, idText, body) {
  return `<div class="spread" style="align-items:center"><h2 style="font-size:20px">${title}</h2>
    <span class="mono muted" style="font-size:12px">${esc(idText)}</span></div>${body}`;
}

function renderRouting() {
  $("trace").hidden = false;
  $("trace").innerHTML = traceShell("Routing your order…", "", '<ol class="steps">' + STEP_NAMES.map((n) => `
    <li class="step"><span class="step-icon wait">${ICON.dots}</span>
      <div class="step-body"><strong>${n}</strong><span class="muted" style="font-size:13px">asking the registry for a healthy cloud</span></div></li>`).join("") + "</ol>");
}

function whyText(step) {
  const tried = step.attempts.map((a) => a.replace(": share sold out", "'s share sold out")).join(", ");
  return step.region ? `${tried} → served by ${step.region}` : `Tried ${tried}`;
}

function renderOrder(order) {
  const steps = order.steps.map((s, i) => `
    <li class="step" style="animation-delay:${i * 120}ms">
      <span class="step-icon ${s.ok ? "ok" : "bad"}">${s.ok ? ICON.check : ICON.cross}</span>
      <div class="step-body">
        <div class="step-head"><strong>${esc(s.name)}</strong>${s.region ? chip(s.region) : '<span class="badge bad">unavailable</span>'}</div>
        <span class="muted" style="font-size:13px">${esc(s.detail)}${s.ms !== null && s.ms !== undefined ? ` · ${s.ms} ms` : ""}</span>
        ${s.attempts.length ? `<span class="step-why">${ICON.arrow}<span>${esc(whyText(s))}</span></span>` : ""}
      </div>
    </li>`).join("");
  const title = order.ok ? "Order placed" : "Order not placed";
  const lead = order.ok ? "Here's how your order travelled through the clouds." : "Your card was not charged.";
  $("trace").innerHTML = traceShell(title, order.order_id,
    `<p style="margin:0;font-size:14px" class="muted">${lead} Total ${inr(order.total)}.</p><ol class="steps">${steps}</ol>
     <div class="mirror" id="mirror" ${S.watchTxn ? "" : "hidden"}>${ICON.sync}<span id="mirror-text">Mirroring payment to the other clouds via Kafka…</span></div>`);
}

async function placeOrder() {
  if (S.busy) return;
  S.busy = true; renderCart(); renderRouting();
  try {
    const order = await api("/api/order", { method: "POST", body: { cart: S.cart, email: $("email").value || "customer@example.com" } });
    const pay = order.steps.find((s) => s.name === "Payment" && s.ok);
    S.watchTxn = pay ? (pay.detail.match(/txn_[0-9a-f]+/) || [null])[0] : null;
    S.mirrored = [];
    renderOrder(order);
    if (order.ok) { S.cart = {}; saveCart(); }
    loadProducts();
  } catch (e) {
    $("trace").innerHTML = traceShell("Order not placed", "", `<div class="notice">${esc(e.message)}</div>`);
  } finally {
    S.busy = false; renderCart();
  }
}

// Kafka replication of this order's payment, reported by the server's monitor.
async function pollEvents() {
  const events = await api(`/api/events?after=${S.lastSeq}`);
  for (const e of events) {
    S.lastSeq = Math.max(S.lastSeq, e.seq);
    if (S.watchTxn && e.kind === "replicated" && e.ref === S.watchTxn) {
      S.mirrored.push(e.detail.replace(/^→\s*/, ""));
      const m = $("mirror");
      if (m) { m.hidden = false; $("mirror-text").textContent = `Payment mirrored via Kafka → ${S.mirrored.join(" · ")}`; }
    }
  }
}

async function pollHealth() {
  const st = await api("/api/state");
  const m = st.metrics;
  const allUp = m.endpoints && m.healthy === m.endpoints;
  $("health").innerHTML = m.endpoints
    ? `<span class="dot ${allUp ? "UP" : "STALE"}"></span>${allUp ? `All ${m.endpoints} services healthy on 3 clouds`
      : `${m.healthy} of ${m.endpoints} services healthy — orders still go through`}`
    : '<span class="dot none"></span>Checking the clouds…';
}

// ---------------------------------------------------------------- wiring

document.addEventListener("click", (ev) => {
  const add = ev.target.closest("[data-add]");
  if (add && !add.disabled) return addToCart(add.dataset.add);
  const dec = ev.target.closest("[data-dec]");
  if (dec) return decrement(dec.dataset.dec);
});
$("search").addEventListener("input", renderProducts);
$("cart-button").addEventListener("click", () => $("cart-panel").scrollIntoView({ behavior: "smooth", block: "start" }));
$("place-order").addEventListener("click", placeOrder);

(async () => {
  try {
    const old = await api("/api/events?after=0");  // start the feed from now
    S.lastSeq = old.reduce((n, e) => Math.max(n, e.seq), 0);
  } catch (_) { /* server will answer later */ }
  renderCart();
  try { await loadProducts(); } catch (e) { toast("Cannot reach the demo server: " + e.message); }
  loadRecs();
  every(3000, loadProducts);
  every(2000, pollHealth);
  every(1000, pollEvents);
})();
