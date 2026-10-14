/*
 * This page can be served two ways:
 *  1. From the panel itself, at http://SERVER:PORT/portal/  (same-origin API calls)
 *  2. As a standalone static site (e.g. GitHub Pages), in which case it reads
 *     ./mirrors.json for a list of candidate server addresses and tries each
 *     one until it finds one that responds. This means if a server's IP gets
 *     blocked, the admin can spin up a new server, restore the JSON backup,
 *     add its address to mirrors.json, and users can still reach their
 *     account from the very same page/link without reinstalling anything.
 *
 * Honesty note: this does not make the panel itself unblockable — it only
 * removes the need to redistribute a new link/app to every user. If the
 * page's own hosting (e.g. GitHub Pages) is blocked too, this doesn't help.
 */
const LS_BASE = "awg_portal_api_base";
const LS_TOKEN = "awg_portal_token";

async function probe(base) {
  try {
    const res = await fetch(`${base}/api/system`, { method: "GET", cache: "no-store" });
    // 401 still means "server is reachable and speaking our API"
    return res.status === 401 || res.ok;
  } catch (e) {
    return false;
  }
}

async function resolveApiBase() {
  const candidates = [];
  const cached = localStorage.getItem(LS_BASE);
  if (cached !== null) candidates.push(cached);
  if (!candidates.includes("")) candidates.push(""); // same-origin

  try {
    const res = await fetch("./mirrors.json", { cache: "no-store" });
    if (res.ok) {
      const data = await res.json();
      for (const m of data.mirrors || []) {
        const clean = m.replace(/\/$/, "");
        if (!candidates.includes(clean)) candidates.push(clean);
      }
    }
  } catch (e) {
    /* no mirrors.json present — fine, same-origin only */
  }

  for (const base of candidates) {
    if (await probe(base)) {
      localStorage.setItem(LS_BASE, base);
      return base;
    }
  }
  return candidates[0] || "";
}

function show(id) {
  for (const v of ["login-view", "status-view", "config-view"]) {
    document.getElementById(v).classList.toggle("hidden", v !== id);
  }
}

async function portalLogin() {
  const username = document.getElementById("p-username").value.trim();
  const password = document.getElementById("p-password").value;
  const errEl = document.getElementById("p-error");
  errEl.textContent = "";
  document.getElementById("p-source").textContent = "Looking for your server…";

  const base = await resolveApiBase();
  document.getElementById("p-source").textContent = "";

  try {
    const res = await fetch(`${base}/api/portal/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      errEl.textContent = e.detail || "Invalid username or password";
      return;
    }
    const data = await res.json();
    localStorage.setItem(LS_TOKEN, data.token);
    localStorage.setItem(LS_BASE, base);
    await loadStatus();
  } catch (e) {
    errEl.textContent = "Could not reach any known server.";
  }
}

function portalLogout() {
  localStorage.removeItem(LS_TOKEN);
  show("login-view");
}

function authHeaders() {
  return { Authorization: "Bearer " + localStorage.getItem(LS_TOKEN) };
}

function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + " " + units[i];
}

// 3 decimal places, switches to GB once the value reaches 1 GB
function fmtVolumePrecise(bytes) {
  const gb = bytes / 1024 ** 3;
  if (gb >= 1) return gb.toFixed(3) + " GB";
  const mb = bytes / 1024 ** 2;
  if (mb >= 1) return mb.toFixed(3) + " MB";
  return (bytes / 1024).toFixed(3) + " KB";
}

function usageRingSVG(usedBytes, limitBytes) {
  const size = 140, stroke = 10, r = (size - stroke) / 2, c = 2 * Math.PI * r;
  let remainingFrac, centerText, subText, color;

  if (limitBytes) {
    const remaining = Math.max(0, limitBytes - usedBytes);
    remainingFrac = limitBytes > 0 ? Math.min(1, remaining / limitBytes) : 0;
    const parts = fmtVolumePrecise(remaining).split(" ");
    centerText = parts[0];
    subText = parts[1] + " left";
    color = remainingFrac <= 0.05 ? "var(--danger)" : remainingFrac <= 0.25 ? "var(--warn)" : "var(--accent)";
  } else {
    remainingFrac = 1;
    centerText = "∞";
    subText = "unlimited";
    color = "var(--accent)";
  }

  const offset = c * (1 - remainingFrac);
  const usedLine = `${fmtVolumePrecise(usedBytes)} used`;
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--surface-2)" stroke-width="${stroke}"/>
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${color}" stroke-width="${stroke}"
      stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${offset.toFixed(2)}"
      stroke-linecap="round" transform="rotate(-90 ${size / 2} ${size / 2})"/>
    <text x="${size / 2}" y="${size / 2 - 6}" text-anchor="middle" class="ring-main">${centerText}</text>
    <text x="${size / 2}" y="${size / 2 + 12}" text-anchor="middle" class="ring-sub">${subText}</text>
    <text x="${size / 2}" y="${size / 2 + 28}" text-anchor="middle" class="ring-used">${usedLine}</text>
  </svg>`;
}

let lastStatus = null;

async function loadStatus() {
  const base = localStorage.getItem(LS_BASE) || "";
  try {
    const res = await fetch(`${base}/api/portal/status`, { headers: authHeaders() });
    if (res.status === 401) { portalLogout(); return; }
    if (!res.ok) throw new Error("bad status");
    const s = await res.json();
    lastStatus = s;
    renderStatus(s);
    show("status-view");
  } catch (e) {
    document.getElementById("p-error").textContent = "Lost connection to the server — trying to reconnect…";
    const newBase = await resolveApiBase();
    if (newBase !== base) {
      loadStatus();
    }
  }
}

function renderStatus(s) {
  document.getElementById("p-name").textContent = s.name;
  const dot = document.getElementById("p-dot");
  dot.classList.toggle("online", s.online);
  document.getElementById("p-online-label").textContent = s.online ? "online" : "offline";

  document.getElementById("p-ring-wrap").innerHTML = usageRingSVG(s.used_bytes, s.data_limit_bytes);

  const timeText = document.getElementById("p-time-text");
  const daysFill = document.getElementById("p-days-fill");
  if (s.expires_at) {
    const left = Math.max(0, s.remaining_days || 0);
    timeText.textContent = `${left.toFixed(1)} day(s) left`;
    // we don't know the plan's original length from this endpoint, so the
    // bar simply shows urgency (red under 1 day, amber under a week)
    const frac = Math.min(1, left / 30);
    daysFill.style.width = (frac * 100).toFixed(1) + "%";
    daysFill.classList.remove("warn", "danger");
    if (left <= 1) daysFill.classList.add("danger");
    else if (left <= 7) daysFill.classList.add("warn");
  } else {
    timeText.textContent = "unlimited";
    daysFill.style.width = "100%";
    daysFill.classList.remove("warn", "danger");
  }
}

async function showConfig() {
  const base = localStorage.getItem(LS_BASE) || "";
  const res = await fetch(`${base}/api/portal/config`, { headers: authHeaders() });
  const text = await res.text();
  document.getElementById("p-config-text").value = text;
  fetch(`${base}/api/portal/qr`, { headers: authHeaders() })
    .then((r) => r.blob())
    .then((blob) => { document.getElementById("p-qr").src = URL.createObjectURL(blob); });
  show("config-view");
}

function backToStatus() { show("status-view"); }

async function copyPortalConfig(btnEl) {
  const text = document.getElementById("p-config-text").value;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  }
  if (event && event.target) {
    const btn = event.target;
    const original = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => { btn.textContent = original; }, 1200);
  }
}

function downloadPortalConfig() {
  const text = document.getElementById("p-config-text").value;
  const blob = new Blob([text], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "my-vpn.conf";
  a.click();
}

// ---------------- boot ----------------
if (localStorage.getItem(LS_TOKEN)) {
  loadStatus();
} else {
  show("login-view");
}
