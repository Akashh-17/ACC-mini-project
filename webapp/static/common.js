// Helpers shared by store.js and control.js.
const REGION_LABEL = { "gcp-asia-south1": "GCP", "aws-ap-south-1": "AWS", "azure-indiasouthcentral": "Azure" };
const REGION_ORDER = ["gcp-asia-south1", "aws-ap-south-1", "azure-indiasouthcentral"];
const CLOUD_KEY = { GCP: "gcp", AWS: "aws", Azure: "azure" };
const CLOUD_COLOR = { GCP: "#60A5FA", AWS: "#FBBF24", Azure: "#A78BFA" };

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "content-type": "application/json" } };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (_) { /* empty body */ }
  if (!r.ok) {
    const detail = data && data.detail;
    throw Object.assign(new Error(typeof detail === "string" ? detail : `HTTP ${r.status}`), { status: r.status });
  }
  return data;
}

const inr = (n) => "₹" + Math.round(n).toLocaleString("en-IN");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const chip = (label) => `<span class="chip ${CLOUD_KEY[label] || ""}">${esc(label)}</span>`;
const regionLabel = (id) => REGION_LABEL[id] || id;

function toast(message, kind = "error") {
  let box = document.querySelector(".toasts");
  if (!box) {
    box = document.createElement("div");
    box.className = "toasts";
    box.setAttribute("role", "status");
    document.body.appendChild(box);
  }
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = message;
  box.appendChild(t);
  setTimeout(() => t.remove(), 4500);
}

// Repeats fn every ms; never overlaps itself and survives errors.
function every(ms, fn) {
  const tick = async () => {
    try { await fn(); } catch (e) { console.warn(e); }
    setTimeout(tick, ms);
  };
  tick();
}

const ICON = {
  cloud: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 18a4 4 0 0 1-.6-7.96A6 6 0 0 1 18 9a4.5 4.5 0 0 1 0 9H7z"/></svg>',
  check: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 5 5 9-10"/></svg>',
  cross: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  dots: '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="6" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="18" cy="12" r="2"/></svg>',
  arrow: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12h12m-4-4 4 4-4 4"/></svg>',
  sync: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12a8 8 0 0 1 14-5.3M20 12a8 8 0 0 1-14 5.3"/><path d="M18 3v4h-4M6 21v-4h4"/></svg>',
  cart: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 4h2l2.4 11.2a2 2 0 0 0 2 1.6h7.7a2 2 0 0 0 2-1.5L21 8H6"/><circle cx="10" cy="20" r="1"/><circle cx="17" cy="20" r="1"/></svg>',
  screen: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
  search: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  off: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3l18 18M8 18a4 4 0 0 1-.6-7.96M11 6.1A6 6 0 0 1 18 9a4.5 4.5 0 0 1 2.3 8.4"/></svg>',
};

// Line-art product pictures (no photos), each on its own tint.
const PRODUCT_ART = {
  p1: { bg: "#EEF2FF", fg: "#4338CA", svg: '<path d="M6 4a3 3 0 0 0-3 3v4a3 3 0 0 0 3 3h1V4H6zM18 4a3 3 0 0 1 3 3v4a3 3 0 0 1-3 3h-1V4h1z"/><path d="M7 14v4a2 2 0 0 0 2 2M17 14v4a2 2 0 0 1-2 2"/>' },
  p2: { bg: "#ECFDF5", fg: "#047857", svg: '<rect x="6" y="6" width="12" height="12" rx="3"/><path d="M9 6l1-3h4l1 3M9 18l1 3h4l1-3M12 10v2l1.5 1"/>' },
  p3: { bg: "#FFF7ED", fg: "#C2410C", svg: '<rect x="6" y="3" width="12" height="18" rx="3"/><circle cx="12" cy="14" r="3.5"/><circle cx="12" cy="7" r="1"/>' },
  p4: { bg: "#F5F3FF", fg: "#6D28D9", svg: '<path d="M8 7V5a4 4 0 0 1 8 0v2"/><rect x="5" y="7" width="14" height="14" rx="3"/><path d="M9 13h6v4H9z"/>' },
  p5: { bg: "#F1F5F9", fg: "#334155", svg: '<rect x="3" y="7" width="14" height="11" rx="2"/><path d="m17 11 4-2v8l-4-2"/><circle cx="10" cy="12.5" r="2.5"/>' },
};
function productArt(pid, size = 64) {
  const a = PRODUCT_ART[pid] || PRODUCT_ART.p1;
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="${a.fg}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${a.svg}</svg>`;
}
