let TOKEN = localStorage.getItem("awg_token") || "";
let pollTimer = null;
let currentConfigPeerId = null;
let currentAdjustPeerId = null;
let currentLabelPeerId = null;
let currentCredentialsPeerId = null;
let currentSearch = "";
let currentSort = "";
const sparkHistory = [];
const SPARK_MAX_POINTS = 40;

function authHeaders(json = true) {
  const h = { Authorization: "Bearer " + TOKEN };
  if (json) h["Content-Type"] = "application/json";
  return h;
}

// ---------------- auth ----------------
async function doLogin() {
  const username = document.getElementById("login-username").value;
  const password = document.getElementById("login-password").value;
  const errEl = document.getElementById("login-error");
  errEl.textContent = "";
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const e = await res.json();
      errEl.textContent = e.detail || "Sign in failed";
      return;
    }
    const data = await res.json();
    TOKEN = data.token;
    localStorage.setItem("awg_token", TOKEN);
    showApp();
  } catch (e) {
    errEl.textContent = "Could not reach the server";
  }
}

function logout() {
  TOKEN = "";
  localStorage.removeItem("awg_token");
  clearInterval(pollTimer);
  document.getElementById("app-screen").classList.add("hidden");
  document.getElementById("login-screen").classList.remove("hidden");
}

function showApp() {
  document.getElementById("login-screen").classList.add("hidden");
  document.getElementById("app-screen").classList.remove("hidden");
  bindSortTabs();
  refreshAll();
  pollTimer = setInterval(() => { if (!document.hidden) refreshAll(); }, 5000);  // paused while the tab is in the background
}

function refreshAll() {
  loadPeers();
  loadSystem();
  loadUsageSummary();
}

// ---------------- formatting ----------------
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

function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toISOString().slice(0, 10);
}

// "offline for 3h" style durations
function fmtDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
}

async function copyToClipboard(text, btnEl) {
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
  if (btnEl) {
    const original = btnEl.textContent;
    btnEl.textContent = "Copied!";
    setTimeout(() => { btnEl.textContent = original; }, 1200);
  }
}

// ---------------- system stats ----------------
async function loadSystem() {
  const res = await fetch("/api/system", { headers: authHeaders(false) });
  if (res.status === 401) return logout();
  const s = await res.json();

  document.getElementById("endpoint-label").textContent = `${s.endpoint}:${s.port} · ${s.interface}`;
  document.getElementById("stat-total").textContent = s.total_peers;
  document.getElementById("stat-online").textContent = s.online_peers;
  document.getElementById("stat-traffic").textContent = fmtBytes(s.total_traffic_bytes);

  setGauge("cpu", s.cpu_percent);
  setGauge("ram", s.ram_percent);

  sparkHistory.push(s.online_peers);
  if (sparkHistory.length > SPARK_MAX_POINTS) sparkHistory.shift();
  drawSparkline();
}

function setGauge(prefix, percent) {
  const fill = document.getElementById(`${prefix}-fill`);
  const value = document.getElementById(`${prefix}-value`);
  fill.style.width = Math.min(100, percent) + "%";
  fill.classList.remove("warn", "danger");
  if (percent >= 90) fill.classList.add("danger");
  else if (percent >= 70) fill.classList.add("warn");
  value.textContent = percent.toFixed(0) + "%";
}

function drawSparkline() {
  const svg = document.getElementById("sparkline");
  const w = 180, h = 34;
  const max = Math.max(1, ...sparkHistory);
  const step = w / Math.max(1, SPARK_MAX_POINTS - 1);
  const offset = SPARK_MAX_POINTS - sparkHistory.length;
  const points = sparkHistory
    .map((v, i) => {
      const x = (offset + i) * step;
      const y = h - (v / max) * (h - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  svg.innerHTML = `
    <polyline points="${points}" fill="none" stroke="#57e6c8" stroke-width="1.5" />
    <text x="${w - 2}" y="10" font-family="IBM Plex Mono" font-size="9" fill="#7c879b" text-anchor="end">online</text>
  `;
}

// ---------------- today / month usage rings ----------------
function statRingSVG(label, valueBytes, frac, color) {
  const size = 68, stroke = 6, r = (size - stroke) / 2, c = 2 * Math.PI * r;
  const offset = c * (1 - Math.max(0, Math.min(1, frac)));
  const parts = fmtVolumePrecise(valueBytes).split(" ");
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" class="ring">
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--surface-2)" stroke-width="${stroke}"/>
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${color}" stroke-width="${stroke}"
      stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${offset.toFixed(2)}"
      stroke-linecap="round" transform="rotate(-90 ${size / 2} ${size / 2})"/>
    <text x="${size / 2}" y="${size / 2 - 2}" text-anchor="middle" class="ring-main" style="font-size:11px;">${parts[0]}</text>
    <text x="${size / 2}" y="${size / 2 + 11}" text-anchor="middle" class="ring-sub" style="font-size:7px;">${parts[1] || ""}</text>
  </svg><span class="ring-label">${label}</span>`;
}

async function loadUsageSummary() {
  const res = await fetch("/api/usage/summary", { headers: authHeaders(false) });
  if (!res.ok) return;
  const s = await res.json();
  const todayFrac = s.month_bytes > 0 ? Math.min(1, s.today_bytes / s.month_bytes) : (s.today_bytes > 0 ? 1 : 0);
  const monthFrac = s.all_time_bytes > 0 ? Math.min(1, s.month_bytes / s.all_time_bytes) : (s.month_bytes > 0 ? 1 : 0);
  document.getElementById("ring-today").innerHTML = statRingSVG("Today", s.today_bytes, todayFrac, "var(--accent)");
  document.getElementById("ring-month").innerHTML = statRingSVG("This month", s.month_bytes, monthFrac, "var(--warn)");
}

// ---------------- search + sort ----------------
let searchDebounce = null;
function onSearchInput() {
  currentSearch = document.getElementById("search-box").value;
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(loadPeers, 250);
}

function bindSortTabs() {
  document.querySelectorAll("#sort-tabs .tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#sort-tabs .tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentSort = btn.dataset.sort || "";
      loadPeers();
    });
  });
}

// ---------------- volume ring / days bar ----------------
function usageRingSVG(usedBytes, limitBytes) {
  const size = 60, stroke = 6, r = (size - stroke) / 2, c = 2 * Math.PI * r;
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
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" class="ring">
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--surface-2)" stroke-width="${stroke}"/>
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${color}" stroke-width="${stroke}"
      stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${offset.toFixed(2)}"
      stroke-linecap="round" transform="rotate(-90 ${size / 2} ${size / 2})"/>
    <text x="${size / 2}" y="${size / 2 - 3}" text-anchor="middle" class="ring-main">${centerText}</text>
    <text x="${size / 2}" y="${size / 2 + 9}" text-anchor="middle" class="ring-sub">${subText}</text>
  </svg>`;
}

function daysBarHTML(p) {
  if (!p.expires_at) {
    return `<div class="days-bar-wrap"><div class="days-text">unlimited</div></div>`;
  }
  const totalDays = Math.max(1, (p.expires_at - p.created_at) / 86400);
  const left = Math.max(0, p.days_left != null ? p.days_left : 0);
  const frac = Math.min(1, left / totalDays);
  let cls = "";
  if (left <= 1) cls = "danger"; else if (frac <= 0.25) cls = "warn";
  return `<div class="days-bar-wrap">
    <div class="days-text">${left.toFixed(1)}d left</div>
    <div class="days-track"><div class="days-fill ${cls}" style="width:${(frac * 100).toFixed(1)}%"></div></div>
  </div>`;
}

// ---------------- peers ----------------
async function loadPeers() {
  const params = new URLSearchParams();
  if (currentSearch) params.set("search", currentSearch);
  if (currentSort) params.set("sort", currentSort);
  const res = await fetch(`/api/peers?${params.toString()}`, { headers: authHeaders(false) });
  if (res.status === 401) return logout();
  const peers = await res.json();
  document.getElementById("users-count").textContent = `(${peers.length})`;
  const body = document.getElementById("peers-body");
  body.innerHTML = "";

  for (const p of peers) {
    const tr = document.createElement("tr");
    const offlineNote =
      !p.online && p.offline_seconds != null
        ? `<span class="offline-duration">offline for ${fmtDuration(p.offline_seconds)}</span>`
        : "";
    const geoLabel = [p.source_city, p.source_country].filter(Boolean).join(", ");
    // link straight to a map pin when we have coordinates - some networks block
    // third-party "whois"-style IP lookup pages outright, Maps links rarely are
    const geoLink =
      p.source_lat != null && p.source_lon != null
        ? `https://www.google.com/maps?q=${p.source_lat},${p.source_lon}`
        : `https://www.google.com/search?q=${encodeURIComponent(p.source_ip || "")}`;
    const sourceIpNote = p.source_ip
      ? `<div class="note">from ${escapeHtml(p.source_ip)}${geoLabel ? ` · <a href="${geoLink}" target="_blank" rel="noopener">${escapeHtml(geoLabel)}</a>` : ` · <a href="${geoLink}" target="_blank" rel="noopener">locate</a>`}</div>`
      : "";

    tr.innerHTML = `
      <td>
        <div class="status-cell">
          <span class="status-dot ${p.online ? "online" : "offline"}"></span>
          <div class="status-text-col">
            <span>${p.online ? "online" : "offline"}</span>
            ${offlineNote}
          </div>
        </div>
      </td>
      <td class="account-cell">#${p.account_number ?? "—"}</td>
      <td class="name-cell">
        ${!p.enabled ? '<span class="disabled-tag">disabled</span>' : ""}
        <span class="name" title="Click to rename" style="cursor:pointer" onclick="openLabelModal(${p.id}, '${escapeHtml(p.note || "").replace(/'/g, "&#39;")}', '${escapeHtml(p.name).replace(/'/g, "&#39;")}')">${escapeHtml(p.name)} ✎</span>
        <button class="label-edit-btn" onclick="openLabelModal(${p.id}, '${escapeHtml(p.note || "").replace(/'/g, "&#39;")}', '${escapeHtml(p.name).replace(/'/g, "&#39;")}')">${p.note ? escapeHtml(p.note) : "+ add label"}</button>
      </td>
      <td class="mono">${p.ip_address}${p.ipv6_address ? `<div class="note">${p.ipv6_address}</div>` : ""}${sourceIpNote}</td>
      <td class="ring-cell">${usageRingSVG(p.used_bytes, p.data_limit_bytes)}</td>
      <td>${daysBarHTML(p)}</td>
      <td>
        <div class="actions-cell">
          <button class="btn-ghost btn-sm" onclick="viewConfig(${p.id})">Config</button>
          <button class="btn-ghost btn-sm" onclick="openCredentialsModal(${p.id}, '${escapeHtml(p.portal_username || "")}')">Login</button>
          <button class="btn-ghost btn-sm" onclick="openAdjustModal(${p.id}, ${p.data_limit_bytes ? 1 : 0})">Adjust</button>
          <button class="btn-ghost btn-sm" onclick="resetPeer(${p.id})">Reset</button>
          <button class="btn-ghost btn-sm" onclick="toggleEnabled(${p.id}, ${!p.enabled})">${p.enabled ? "Disable" : "Enable"}</button>
          <button class="btn-danger btn-sm" onclick="deletePeer(${p.id})">Delete</button>
        </div>
      </td>`;
    body.appendChild(tr);
  }
}

// ---------------- create ----------------
function openCreateModal() {
  document.getElementById("new-name").value = "";
  document.getElementById("new-note").value = "";
  document.getElementById("new-limit").value = "";
  document.getElementById("new-days").value = "";
  document.getElementById("create-modal").classList.remove("hidden");
}

function closeModal(id) {
  document.getElementById(id).classList.add("hidden");
}

async function submitCreate() {
  const name = document.getElementById("new-name").value.trim();
  if (!name) return alert("Please enter a name");
  const note = document.getElementById("new-note").value;
  const limitVal = document.getElementById("new-limit").value;
  const daysVal = document.getElementById("new-days").value;

  const body = { name, note };
  if (limitVal) body.data_limit_gb = parseFloat(limitVal);
  if (daysVal) {
    body.duration_days = parseInt(daysVal);
    body.expires_at = Math.floor(Date.now() / 1000) + parseInt(daysVal) * 86400;
  }

  const res = await fetch("/api/peers", { method: "POST", headers: authHeaders(), body: JSON.stringify(body) });
  if (!res.ok) { const e = await res.json(); return alert(e.detail || "Failed to create user"); }
  const data = await res.json();
  closeModal("create-modal");
  refreshAll();
  showConfigContent(data.id, data.config, data.portal_username, data.portal_password);
}

// ---------------- config / qr ----------------
async function viewConfig(peerId) {
  const res = await fetch(`/api/peers/${peerId}/config`, { headers: authHeaders(false) });
  const text = await res.text();
  showConfigContent(peerId, text, null, null);
}

function showConfigContent(peerId, text, portalUser, portalPass) {
  currentConfigPeerId = peerId;
  document.getElementById("config-text").value = text;

  const box = document.getElementById("portal-creds-box");
  if (portalUser) {
    box.innerHTML = `
      <div class="credential-box">
        <div><span class="k">Portal username:</span> ${portalUser}</div>
        <div><span class="k">Portal password:</span> ${portalPass}</div>
      </div>
      <p class="hint">Share these with the user — shown only once. They can use them at /portal to fetch their config and check usage if the server address ever changes.</p>`;
  } else {
    box.innerHTML = "";
  }

  fetch(`/api/peers/${peerId}/qr`, { headers: authHeaders(false) })
    .then((r) => r.blob())
    .then((blob) => { document.getElementById("config-qr").src = URL.createObjectURL(blob); });

  document.getElementById("config-modal").classList.remove("hidden");
}

function copyConfig() {
  copyToClipboard(document.getElementById("config-text").value);
}

function downloadConfig() {
  const text = document.getElementById("config-text").value;
  const blob = new Blob([text], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `peer-${currentConfigPeerId}.conf`;
  a.click();
}

// ---------------- enable/disable/delete/reset ----------------
async function toggleEnabled(id, enable) {
  await fetch(`/api/peers/${id}`, { method: "PUT", headers: authHeaders(), body: JSON.stringify({ enabled: enable }) });
  loadPeers();
}

async function deletePeer(id) {
  if (!confirm("Delete this user? This cannot be undone.")) return;
  await fetch(`/api/peers/${id}`, { method: "DELETE", headers: authHeaders(false) });
  refreshAll();
}

async function resetPeer(id) {
  if (!confirm("Reset usage and restart the time period for this user?")) return;
  const res = await fetch(`/api/peers/${id}/reset`, { method: "POST", headers: authHeaders(false) });
  if (!res.ok) { const e = await res.json(); return alert(e.detail || "Reset failed"); }
  loadPeers();
}

// ---------------- adjust plan ----------------
function openAdjustModal(id, hasLimit) {
  currentAdjustPeerId = id;
  document.getElementById("adjust-gb").value = "";
  document.getElementById("adjust-days").value = "";
  document.getElementById("adjust-unlimited").checked = !hasLimit;
  document.getElementById("adjust-set-limit").value = "";
  document.getElementById("adjust-set-limit").disabled = !hasLimit;
  document.getElementById("adjust-modal").classList.remove("hidden");
}

async function submitAdjust() {
  const gb = document.getElementById("adjust-gb").value;
  const days = document.getElementById("adjust-days").value;
  const body = {};
  if (gb) body.add_gb = parseFloat(gb);
  if (days) body.add_days = parseInt(days);
  const res = await fetch(`/api/peers/${currentAdjustPeerId}/adjust`, {
    method: "POST", headers: authHeaders(), body: JSON.stringify(body),
  });
  if (!res.ok) { const e = await res.json(); return alert(e.detail || "Adjust failed"); }
  closeModal("adjust-modal");
  loadPeers();
}

// ---------------- set limit directly (unlimited <-> limited toggle) ----------------
function onUnlimitedToggle() {
  const unlimited = document.getElementById("adjust-unlimited").checked;
  document.getElementById("adjust-set-limit").disabled = unlimited;
}

async function submitSetLimit() {
  const unlimited = document.getElementById("adjust-unlimited").checked;
  const gbVal = document.getElementById("adjust-set-limit").value;
  const body = unlimited ? { unlimited: true } : { data_limit_gb: gbVal ? parseFloat(gbVal) : 0 };
  const res = await fetch(`/api/peers/${currentAdjustPeerId}`, {
    method: "PUT", headers: authHeaders(), body: JSON.stringify(body),
  });
  if (!res.ok) { const e = await res.json(); return alert(e.detail || "Failed to update limit"); }
  closeModal("adjust-modal");
  loadPeers();
}

// ---------------- label edit ----------------
function openLabelModal(id, currentNote, currentName) {
  currentLabelPeerId = id;
  document.getElementById("label-name").value = currentName || "";
  document.getElementById("label-input").value = currentNote || "";
  document.getElementById("label-modal").classList.remove("hidden");
}

async function submitLabel() {
  const note = document.getElementById("label-input").value;
  const name = document.getElementById("label-name").value.trim();
  if (!name) return alert("Name cannot be empty");
  const res = await fetch(`/api/peers/${currentLabelPeerId}`, {
    method: "PUT", headers: authHeaders(), body: JSON.stringify({ note, name }),
  });
  if (!res.ok) { const e = await res.json(); return alert(e.detail || "Update failed"); }
  closeModal("label-modal");
  loadPeers();
}

// ---------------- portal credentials ----------------
function openCredentialsModal(id, currentUsername) {
  currentCredentialsPeerId = id;
  document.getElementById("cred-username").value = "";
  document.getElementById("cred-password").value = "";
  document.getElementById("credentials-result").innerHTML = "";
  document.getElementById("credentials-current").innerHTML = `
    <div><span class="k">Current username:</span> ${currentUsername || "—"}
      <span class="copy-btn" onclick="copyToClipboard('${currentUsername}', this)">copy</span></div>
    <div><span class="k">Password:</span> hidden (regenerate below to see a new one)</div>`;
  document.getElementById("credentials-modal").classList.remove("hidden");
}

async function submitCredentials() {
  const username = document.getElementById("cred-username").value.trim();
  const password = document.getElementById("cred-password").value.trim();
  const body = {};
  if (username) body.username = username;
  if (password) body.password = password;

  const res = await fetch(`/api/peers/${currentCredentialsPeerId}/portal-credentials/regenerate`, {
    method: "POST", headers: authHeaders(), body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) { alert(data.detail || "Failed"); return; }

  document.getElementById("credentials-result").innerHTML = `
    <div class="credential-box">
      <div><span class="k">Username:</span> ${data.portal_username}
        <span class="copy-btn" onclick="copyToClipboard('${data.portal_username}', this)">copy</span></div>
      <div><span class="k">Password:</span> ${data.portal_password}
        <span class="copy-btn" onclick="copyToClipboard('${data.portal_password}', this)">copy</span></div>
    </div>
    <p class="hint">Shown once — share it with the user now.</p>`;
  loadPeers();
}

// ---------------- settings (admin credentials + server domain) ----------------
async function openSettingsModal() {
  document.getElementById("settings-current-pass").value = "";
  document.getElementById("settings-new-user").value = "";
  document.getElementById("settings-new-pass").value = "";
  document.getElementById("settings-error").textContent = "";
  document.getElementById("endpoint-error").textContent = "";
  document.getElementById("settings-modal").classList.remove("hidden");

  const res = await fetch("/api/admin/server-settings", { headers: authHeaders(false) });
  if (res.ok) {
    const s = await res.json();
    document.getElementById("settings-endpoint").value = s.endpoint || "";
  }
  loadQos();
}

async function submitSettings() {
  const body = {
    current_password: document.getElementById("settings-current-pass").value,
    new_username: document.getElementById("settings-new-user").value || null,
    new_password: document.getElementById("settings-new-pass").value || null,
  };
  const res = await fetch("/api/admin/credentials", { method: "PUT", headers: authHeaders(), body: JSON.stringify(body) });
  if (!res.ok) {
    const e = await res.json();
    document.getElementById("settings-error").textContent = e.detail || "Update failed";
    return;
  }
  closeModal("settings-modal");
  alert("Credentials updated. Please sign in again.");
  logout();
}

function qosText(q) {
  if (!q.enabled) return "Off: traffic is not shaped (original behaviour).";
  if (q.mode === "cake") return `Active: per-user fair sharing (cake) at ${q.applied_rate_mbit || "?"} Mbit/s` +
    (q.bandwidth_mbit ? " (your value)." : ` (measured automatically: ${q.auto_mbit} Mbit/s).`);
  if (q.mode === "fq_codel") return "Active: basic fair queueing. The server will measure its line speed automatically when no one is using it (checked every ~10 min), or enter it above.";
  return "Not active (this server's kernel may not support it).";
}

async function loadQos() {
  const res = await fetch("/api/admin/qos", { headers: authHeaders(false) });
  if (!res.ok) return;
  const q = await res.json();
  document.getElementById("qos-mbit").value = q.bandwidth_mbit || "";
  document.getElementById("qos-enabled").checked = !!q.enabled;
  document.getElementById("qos-status").textContent = qosText(q);
}

async function saveQos() {
  const v = parseInt(document.getElementById("qos-mbit").value, 10) || 0;
  const el = document.getElementById("qos-status");
  el.textContent = "Applying...";
  const res = await fetch("/api/admin/qos", { method: "PUT", headers: authHeaders(), body: JSON.stringify({ bandwidth_mbit: v, enabled: document.getElementById("qos-enabled").checked }) });
  const q = await res.json();
  el.textContent = res.ok ? qosText(q) : (q.detail || "Failed");
}

async function submitEndpoint() {
  const endpoint = document.getElementById("settings-endpoint").value.trim();
  const errEl = document.getElementById("endpoint-error");
  errEl.textContent = "";
  const res = await fetch("/api/admin/server-settings", {
    method: "PUT", headers: authHeaders(), body: JSON.stringify({ endpoint }),
  });
  if (!res.ok) { const e = await res.json(); errEl.textContent = e.detail || "Update failed"; return; }
  errEl.style.color = "var(--accent)";
  errEl.textContent = "Saved. New configs will use this address.";
  loadSystem();
}

// ---------------- backup / restore ----------------
function openBackupModal() {
  document.getElementById("restore-file").value = "";
  document.getElementById("backup-status").textContent = "";
  document.getElementById("backup-modal").classList.remove("hidden");
}

async function downloadBackup() {
  const res = await fetch("/api/backup", { headers: authHeaders(false) });
  const data = await res.json();
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `awg-backup-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
}

async function submitRestore() {
  const fileInput = document.getElementById("restore-file");
  const statusEl = document.getElementById("backup-status");
  if (!fileInput.files.length) { statusEl.textContent = "Choose a backup file first."; return; }
  const text = await fileInput.files[0].text();
  let json;
  try { json = JSON.parse(text); } catch (e) { statusEl.textContent = "Invalid JSON file."; return; }

  const res = await fetch("/api/backup/restore", { method: "POST", headers: authHeaders(), body: JSON.stringify(json) });
  const data = await res.json();
  if (!res.ok) { statusEl.textContent = data.detail || "Restore failed."; return; }
  statusEl.textContent = `Imported ${data.imported} user(s), skipped ${data.skipped} (already existed).`;
  refreshAll();
}

// ---------------- server monitor ----------------
let serverTimer = null;
let serverSort = "mem";

function openServerModal() {
  document.getElementById("server-modal").classList.remove("hidden");
  document.getElementById("server-msg").textContent = "";
  loadServer();
  clearInterval(serverTimer);
  serverTimer = setInterval(() => { if (!document.hidden) loadServer(); }, 5000);
}

function closeServerModal() {
  clearInterval(serverTimer);
  closeModal("server-modal");
}

function barRow(label, pct, detail) {
  const cls = pct >= 90 ? "crit" : pct >= 75 ? "warn" : "";
  return `<div class="srv-row"><span class="srv-k">${label}</span>
    <span class="srv-bar"><span class="srv-fill ${cls}" style="width:${Math.min(100, pct)}%"></span></span>
    <span class="srv-v mono">${detail}</span></div>`;
}

async function loadBandwidth() {
  const res = await fetch("/api/server/bandwidth", { headers: authHeaders(false) });
  const el = document.getElementById("server-bw");
  if (!res.ok) { el.textContent = ""; return; }
  const b = await res.json();
  const mode = b.mode === "cake" ? "fair sharing: ON (per user)" : b.mode === "fq_codel" ? "fair sharing: basic" : "fair sharing: off";
  let head = barRow("Download load", b.utilization_pct ?? 0,
    b.line_mbit ? `${b.total_down_mbit} / ${b.line_mbit} Mbit/s` : `${b.total_down_mbit} Mbit/s`);
  const q = b.queue || {};
  head += `<div class="hint srv-hint">${mode} · upload ${b.total_up_mbit} Mbit/s · ${b.active_users} active user(s)` +
    (b.fair_share_mbit ? ` · equal share ≈ ${b.fair_share_mbit} Mbit/s` : "") +
    (q.avg_delay_ms != null ? ` · queue delay ${q.avg_delay_ms} ms` : "") +
    (q.drops != null ? ` · dropped packets ${q.drops}` : "") + `</div>`;
  const rows = (b.users || []).map((u) => `
    <tr><td>${escapeHtml(u.name)}</td>
      <td class="mono">${u.down_mbit}</td><td class="mono">${u.up_mbit}</td>
      <td>${u.limited ? '<span class="disabled-tag">slowed (heavy)</span>' : '<span class="note">free</span>'}</td></tr>`).join("");
  el.innerHTML = head + (rows ? `<table class="srv-table"><thead><tr><th>User</th><th>↓ Mbit/s</th><th>↑ Mbit/s</th><th>State</th></tr></thead><tbody>${rows}</tbody></table>`
    : '<div class="hint srv-hint">No active traffic right now.</div>');
}

async function loadServer() {
  loadBandwidth();
  const [ovRes, prRes] = await Promise.all([
    fetch("/api/server/overview", { headers: authHeaders(false) }),
    fetch(`/api/server/processes?sort=${serverSort}&limit=15`, { headers: authHeaders(false) }),
  ]);
  if (!ovRes.ok || !prRes.ok) return;
  const o = await ovRes.json();
  const procs = await prRes.json();
  const up = fmtDuration ? fmtDuration(o.uptime_seconds) : o.uptime_seconds + "s";
  document.getElementById("server-overview").innerHTML =
    barRow("RAM (really used)", o.mem.percent, `${fmtBytes(o.mem.used)} / ${fmtBytes(o.mem.total)}`) +
    `<div class="hint srv-hint">Cache ${fmtBytes(o.mem.cache)} is reclaimable and not counted above. Free for programs: ${fmtBytes(o.mem.available)}</div>` +
    barRow("CPU", o.cpu_percent, `${o.cpu_percent}% · ${o.cpu_cores} core(s) · load ${o.load.join(" ")}`) +
    barRow("Swap", o.swap.percent, o.swap.total ? `${fmtBytes(o.swap.used)} / ${fmtBytes(o.swap.total)}` : "none") +
    barRow("Disk", o.disk.percent, `${fmtBytes(o.disk.used)} / ${fmtBytes(o.disk.total)}`) +
    `<div class="hint srv-hint">Uptime ${up} · Panel itself uses ${fmtBytes(o.panel_rss)} · Network total ↑ ${fmtBytes(o.net.sent)} ↓ ${fmtBytes(o.net.recv)}</div>`;

  document.getElementById("server-procs").innerHTML = procs.map((p) => `
    <tr>
      <td class="mono">${p.pid}</td>
      <td title="${escapeHtml(p.cmd)}">${escapeHtml(p.name)}<div class="note">${escapeHtml(p.cmd.slice(0, 48))}</div></td>
      <td class="mono">${fmtBytes(p.rss)} <span class="note">${p.mem_percent}%</span></td>
      <td class="mono">${p.cpu_percent}%</td>
      <td>${p.protected ? '<span class="note">protected</span>' :
        `<button class="btn-danger btn-sm" onclick="killProcess(${p.pid}, '${escapeHtml(p.name).replace(/'/g, "")}')">Stop</button>`}</td>
    </tr>`).join("");
  document.querySelectorAll("#server-modal .sort-tabs .tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.psort === serverSort));
}

function setProcSort(k) { serverSort = k; loadServer(); }

async function killProcess(pid, name) {
  if (!confirm(`Stop process "${name}" (PID ${pid})? Make sure you know what it is - stopping the wrong program can break the server.`)) return;
  let res = await fetch(`/api/server/processes/${pid}/kill`, { method: "POST", headers: authHeaders(), body: JSON.stringify({ force: false }) });
  let d = await res.json();
  const msg = document.getElementById("server-msg");
  msg.textContent = res.ok ? `Asked "${d.killed}" to stop.` : (d.detail || "Failed");
  setTimeout(loadServer, 1200);
}

async function freeMemory() {
  const res = await fetch("/api/server/free-memory", { method: "POST", headers: authHeaders(false) });
  const d = await res.json();
  document.getElementById("server-msg").textContent = res.ok ? `Cache cleared (${fmtBytes(d.freed_bytes)} returned).` : (d.detail || "Failed");
  loadServer();
}

async function restartPanel() {
  if (!confirm("Restart the web panel only? VPN users stay connected. The page reloads in a few seconds.")) return;
  await fetch("/api/server/restart-panel", { method: "POST", headers: authHeaders(false) });
  document.getElementById("server-msg").textContent = "Restarting panel... reloading in 6s";
  setTimeout(() => location.reload(), 6000);
}

// ---------------- boot ----------------
if (TOKEN) showApp();
