/* netmon - web-UI
 * Author: Glenn Leving
 */
const $ = (id) => document.getElementById(id);
let CONFIG = null;
let settingsDirty = false;
let AUTHED = false;

// ---------- formatering ----------
const pad = (n) => String(n).padStart(2, "0");
function ts(t) {
  if (!t) return "-";
  const d = new Date(t * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function dur(s) {
  if (s == null) return "-";
  s = Math.round(s);
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
        m = Math.floor(s % 3600 / 60), sec = s % 60;
  if (d) return `${d}d ${h}t`;
  if (h) return `${h}t ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------- status ----------
async function refresh() {
  let data;
  try {
    data = await (await fetch("/api/status")).json();
  } catch { return; }

  if (!settingsDirty) { CONFIG = data.config; fillSettings(CONFIG); }
  renderLinks(data);
  renderTargets(data.targets);
  $("clock").textContent =
    `Sidste maaling: ${data.monitor.last_round ? ts(data.monitor.last_round) : "venter..."}`;
}

function renderLinks(d) {
  const items = [];
  const has = (k) => d.targets.some(t => t.enabled && t.kind === k);

  const anyNet = d.targets.filter(t => t.enabled && t.kind === "internet");
  const netUp = anyNet.some(t => t.status === "up");
  items.push(card("Internet", anyNet.length === 0 ? "unknown" : (netUp ? "up" : "down"),
    netUp ? "Forbindelse ok" : (anyNet.length ? "Ingen svar udefra" : "Ingen internet-vaerter")));

  if (has("lan")) {
    const l = d.link.lan;
    items.push(card("Lokalt netvaerk", l.status === "down" ? "down" : "up",
      l.status === "down" ? `Nede siden ${ts(l.since)}` : "Router svarer"));
  }
  const isp = d.link.isp;
  items.push(card("ISP", isp.status === "down" ? "down" : (isp.status === "up" ? "up" : "unknown"),
    isp.status === "down" ? `Nede siden ${ts(isp.since)}` :
    (has("lan") ? "Ingen ISP-nedbrud registreret" : "Tilfoej en LAN-vaert for ISP-detektion")));

  $("links").innerHTML = items.join("");
}
function card(label, state, sub) {
  const txt = { up: "OPPE", down: "NEDE", unknown: "UKENDT" }[state] || "UKENDT";
  return `<div class="link ${state}"><div class="label">${esc(label)}</div>
    <div class="value">${txt}</div><div class="muted small">${esc(sub)}</div></div>`;
}

function renderTargets(targets) {
  $("targets").innerHTML = targets.map(t => {
    const rtts = t.history.filter(h => h.ok && h.rtt != null).map(h => h.rtt);
    const max = Math.max(60, ...rtts);
    const spark = t.history.slice(-60).map(h => {
      const hgt = h.ok && h.rtt != null ? Math.max(8, (h.rtt / max) * 100) : 100;
      const title = h.ok ? `${h.rtt} ms - ${ts(h.ts)}` : `ingen svar - ${ts(h.ts)}`;
      return `<i class="${h.ok ? "" : "bad"}" style="height:${hgt}%" title="${title}"></i>`;
    }).join("");
    return `<div class="card">
      <div class="card-head">
        <span class="dot ${t.status}"></span>
        <span class="card-name">${esc(t.name)}</span>
        <span class="tag">${t.kind === "lan" ? "LAN" : "internet"}</span>
        <span class="card-host">${esc(t.host)}</span>
      </div>
      <div class="stats">
        <div class="stat"><b>${t.rtt != null ? t.rtt + " ms" : "-"}</b><span>seneste svartid</span></div>
        <div class="stat"><b>${t.avg_rtt24h != null ? t.avg_rtt24h + " ms" : "-"}</b><span>gns. 24t</span></div>
        <div class="stat"><b>${t.uptime24h != null ? t.uptime24h + " %" : "-"}</b><span>oppetid 24t</span></div>
        <div class="stat"><b>${t.since ? dur(Date.now() / 1000 - t.since) : "-"}</b><span>i denne status</span></div>
      </div>
      <div class="spark">${spark || '<span class="muted small">venter paa data...</span>'}</div>
      ${t.status === "down" || (t.fails > 0 && t.error)
        ? `<div class="err">${esc(t.error || "ingen svar")}${t.fails ? ` (${t.fails} fejl i traek)` : ""}</div>` : ""}
    </div>`;
  }).join("");
}

// ---------- indstillinger ----------
function fillSettings(cfg) {
  $("cfg-interval").value = cfg.interval_seconds;
  $("cfg-timeout").value = cfg.timeout_seconds;
  $("cfg-count").value = cfg.ping_count;
  $("cfg-fail").value = cfg.fail_threshold;
  $("cfg-recover").value = cfg.recover_threshold;
  $("cfg-retention").value = cfg.retention_days;
  $("target-rows").innerHTML = cfg.targets.map(rowHtml).join("");
}
function rowHtml(t) {
  return `<tr>
    <td><input type="text" class="t-name" value="${esc(t.name)}"></td>
    <td><input type="text" class="t-host" value="${esc(t.host)}" placeholder="1.1.1.1"></td>
    <td><select class="t-kind">
      <option value="internet"${t.kind === "internet" ? " selected" : ""}>Internet</option>
      <option value="lan"${t.kind === "lan" ? " selected" : ""}>LAN</option>
    </select></td>
    <td><input type="checkbox" class="t-enabled"${t.enabled ? " checked" : ""}></td>
    <td><button class="btn danger del">Fjern</button></td>
  </tr>`;
}
function collect() {
  const targets = [...$("target-rows").querySelectorAll("tr")].map(tr => ({
    name: tr.querySelector(".t-name").value.trim(),
    host: tr.querySelector(".t-host").value.trim(),
    kind: tr.querySelector(".t-kind").value,
    enabled: tr.querySelector(".t-enabled").checked,
  })).filter(t => t.host);
  return {
    interval_seconds: +$("cfg-interval").value,
    timeout_seconds: +$("cfg-timeout").value,
    ping_count: +$("cfg-count").value,
    fail_threshold: +$("cfg-fail").value,
    recover_threshold: +$("cfg-recover").value,
    retention_days: +$("cfg-retention").value,
    targets,
  };
}

// ---------- login ----------
async function checkSession() {
  try {
    const j = await (await fetch("/api/session")).json();
    setAuthed(j.authed);
  } catch { setAuthed(false); }
}
function setAuthed(on) {
  AUTHED = on;
  $("login-box").classList.toggle("hidden", on);
  $("settings-body").classList.toggle("hidden", !on);
  $("logout").classList.toggle("hidden", !on);
  if (!on) { settingsDirty = false; if (CONFIG) fillSettings(CONFIG); }
}
$("login-box").onsubmit = async (e) => {
  e.preventDefault();
  const msg = $("login-msg");
  msg.textContent = "";
  const r = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: $("password").value }),
  });
  const j = await r.json();
  if (r.ok) { $("password").value = ""; setAuthed(true); }
  else { msg.textContent = j.error || "login mislykkedes"; }
};
$("logout").onclick = async () => {
  await fetch("/api/logout", { method: "POST" });
  setAuthed(false);
};
$("pw-form").onsubmit = async (e) => {
  e.preventDefault();
  const msg = $("pw-msg");
  const r = await fetch("/api/set-password", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current: $("pw-current").value, new: $("pw-new").value }),
  });
  const j = await r.json();
  $("pw-current").value = $("pw-new").value = "";
  if (r.ok) {
    msg.textContent = "Adgangskode skiftet - log ind igen";
    msg.style.color = "var(--up)";
    setAuthed(false);
  } else {
    msg.textContent = j.error || "kunne ikke skifte";
    msg.style.color = "var(--down)";
  }
  setTimeout(() => { msg.textContent = ""; }, 5000);
};

$("toggle-settings").onclick = () => {
  $("settings").classList.toggle("hidden");
  if (!$("settings").classList.contains("hidden")) checkSession();
};
$("settings").addEventListener("input", () => { settingsDirty = true; });
$("add-target").onclick = () => {
  settingsDirty = true;
  $("target-rows").insertAdjacentHTML("beforeend",
    rowHtml({ name: "", host: "", kind: "internet", enabled: true }));
};
$("target-rows").onclick = (e) => {
  if (e.target.classList.contains("del")) { settingsDirty = true; e.target.closest("tr").remove(); }
};
$("save-config").onclick = async () => {
  const msg = $("save-msg");
  msg.textContent = "Gemmer...";
  const r = await fetch("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collect()),
  });
  const j = await r.json();
  if (r.ok) {
    settingsDirty = false;
    CONFIG = j.config;
    fillSettings(CONFIG);
    msg.textContent = "Gemt - maaling genstartet";
    msg.style.color = "var(--up)";
    refresh();
  } else if (r.status === 401) {
    setAuthed(false);
    $("login-msg").textContent = "Sessionen er udloebet - log ind igen";
    msg.textContent = "";
  } else {
    msg.textContent = "Fejl: " + j.error;
    msg.style.color = "var(--down)";
  }
  setTimeout(() => { msg.textContent = ""; }, 4000);
};
$("check-now").onclick = async () => {
  await fetch("/api/check-now", { method: "POST" });
  setTimeout(refresh, 1200);
};
$("clear-log").onclick = async () => {
  if (!confirm("Slet alle afsluttede nedetids-haendelser?")) return;
  const r = await fetch("/api/clear-log", { method: "POST" });
  if (r.status === 401) {
    setAuthed(false);
    $("settings").classList.remove("hidden");
    $("login-msg").textContent = "Log ind for at rydde loggen";
    return;
  }
  loadLog();
};
$("log-scope").onchange = loadLog;

// ---------- log ----------
async function loadLog() {
  const scope = $("log-scope").value;
  const r = await fetch(`/api/outages?limit=300&scope=${scope}`);
  const { outages } = await r.json();
  $("log-rows").innerHTML = outages.length ? outages.map(o => `<tr>
      <td><span class="badge ${o.scope}">${o.scope.toUpperCase()}</span></td>
      <td>${esc(o.name)}</td>
      <td>${ts(o.started)}</td>
      <td>${o.ended ? ts(o.ended) : '<span class="ongoing">paagaar</span>'}</td>
      <td>${o.ended ? dur(o.duration) : dur(Date.now() / 1000 - o.started)}</td>
      <td class="muted">${esc(o.detail)}</td>
    </tr>`).join("")
    : `<tr><td colspan="6" class="muted">Ingen nedetid registreret 🎉</td></tr>`;
}

checkSession();
refresh();
loadLog();
setInterval(refresh, 3000);
setInterval(loadLog, 10000);
