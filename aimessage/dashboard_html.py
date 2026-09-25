"""The web dashboard (Desktop v1.0, web-first). Served by the control API at `/`.

Self-contained HTML/CSS/JS, no build step, no external requests. It reads the bearer token from the
URL fragment (`http://host:port/#<token>`) — the fragment never reaches the server — and polls the
token-gated JSON endpoints same-origin (so no CORS). All peer-supplied text is rendered via
textContent (never innerHTML) so a hostile query string can't inject markup/script.
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIMessage node</title>
<style>
  :root { color-scheme: light dark; --bg:#0f1115; --card:#1a1d24; --fg:#e6e8ee; --mut:#9aa3b2;
          --acc:#5b9dff; --ok:#3ecf8e; --bad:#ff6b6b; --line:#2a2e37; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 ui-sans-serif,system-ui,sans-serif; }
  header { display:flex; align-items:center; gap:.6rem; padding:1rem 1.25rem; border-bottom:1px solid var(--line); }
  header h1 { font-size:1rem; margin:0; font-weight:600; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--mut); }
  .dot.ok { background:var(--ok); } .dot.bad { background:var(--bad); }
  main { padding:1.25rem; display:grid; gap:1rem; max-width:840px; margin:0 auto; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:1rem 1.25rem; }
  .card h2 { font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; color:var(--mut);
             margin:0 0 .6rem; }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .id { color:var(--mut); font-size:.82rem; word-break:break-all; }
  .bar { height:10px; background:#0c0e12; border-radius:6px; overflow:hidden; border:1px solid var(--line); }
  .bar > i { display:block; height:100%; background:var(--acc); width:0; transition:width .4s; }
  .evrow { display:flex; gap:.6rem; align-items:baseline; padding:.4rem 0; border-top:1px solid var(--line); }
  .evrow:first-child { border-top:0; }
  .badge { font-size:.7rem; padding:.1rem .45rem; border-radius:999px; background:#20242d; color:var(--acc);
           white-space:nowrap; }
  .evbody { color:var(--fg); } .evbody .q { color:var(--mut); }
  .ts { margin-left:auto; color:var(--mut); font-size:.72rem; white-space:nowrap; }
  .empty { color:var(--mut); font-style:italic; }
  .note { color:var(--mut); font-size:.8rem; }
</style></head>
<body>
<header><span id="dot" class="dot"></span><h1>AIMessage node</h1>
  <span id="conn" class="note" style="margin-left:auto"></span></header>
<main>
  <section class="card">
    <h2>Node</h2>
    <div class="id mono" id="nodeid">—</div>
  </section>
  <section class="card">
    <h2>Artifact storage</h2>
    <div class="bar"><i id="storebar"></i></div>
    <div class="note" id="storetxt" style="margin-top:.5rem">—</div>
  </section>
  <section class="card" id="askcard" style="display:none">
    <h2>Ask the mesh</h2>
    <div style="display:flex; gap:.5rem">
      <input id="q" placeholder="e.g. which patients qualify for CCM?" spellcheck="false"
             style="flex:1; background:#0c0e12; color:var(--fg); border:1px solid var(--line);
                    border-radius:8px; padding:.5rem .7rem; font:inherit">
      <button id="askbtn" style="background:var(--acc); color:#08111f; border:0; border-radius:8px;
              padding:.5rem 1rem; font:inherit; font-weight:600; cursor:pointer">Ask</button>
    </div>
    <div id="answers" style="margin-top:.6rem"></div>
    <div id="wroteback" style="margin-top:.3rem; color:var(--acc); font-size:.85rem"></div>
  </section>
  <section class="card">
    <h2>Recent activity</h2>
    <div id="events"><div class="empty">waiting for events…</div></div>
  </section>
  <section class="card" id="memcard" style="display:none">
    <h2>My shared memories</h2>
    <div id="memories"></div>
  </section>
  <p class="note">Token comes from the URL fragment and is never sent to the server.</p>
</main>
<script>
const token = location.hash.slice(1);
const base = location.origin;
const $ = (id) => document.getElementById(id);

async function api(path) {
  const r = await fetch(base + path, { headers: { Authorization: "Bearer " + token } });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
async function apiPost(path, obj) {
  const r = await fetch(base + path, { method: "POST",
    headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
    body: JSON.stringify(obj) });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
function renderMemoryList(boxId, items, emptyMsg) {
  const box = $(boxId); box.textContent = "";
  if (!items || !items.length) { const e = document.createElement("div"); e.className = "empty";
    e.textContent = emptyMsg; box.appendChild(e); return; }
  for (const m of items) {
    const row = document.createElement("div"); row.className = "evrow";
    const b = document.createElement("span"); b.className = "badge"; b.textContent = m.type || "memory";
    const body = document.createElement("span"); body.className = "evbody";
    body.textContent = m.content || "";                          // textContent → no injection
    const src = document.createElement("span"); src.className = "ts";
    src.textContent = (m.origin_node || "").slice(0, 10) + "…";
    row.append(b, body, src); box.appendChild(row);
  }
}
async function doAsk() {
  const q = $("q").value.trim(); if (!q) return;
  const btn = $("askbtn"); btn.disabled = true; btn.textContent = "…";
  try {
    const res = await apiPost("/ask", { query: q });
    renderMemoryList("answers", res.answers, "no answers from the mesh");
    const wb = res.wrote_back || 0;                    // Phase 7: absorbed into the local hub
    $("wroteback").textContent = wb > 0 ? ("↳ absorbed " + wb + " answer(s) into your hub") : "";
  } catch (e) {
    $("wroteback").textContent = "";
    renderMemoryList("answers", [], e.message === "429" ? "rate limited — wait a moment" : "ask failed");
  } finally { btn.disabled = false; btn.textContent = "Ask"; }
}
function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B","KiB","MiB","GiB","TiB"]; let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length-1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
}
function evSummary(ev) {
  const d = ev.detail || {};
  const who = (d.asker || d.peer || "").slice(0, 12);
  if (ev.type === "query.received") return { txt: `${who}… asked`, q: d.query || "" };
  if (ev.type === "answer.sent")    return { txt: `answered ${who}… (${d.count ?? 0})`, q: "" };
  if (ev.type === "answer.received")return { txt: `${d.count ?? 0} answer(s) received`, q: "" };
  if (ev.type === "storage.full")   return { txt: `storage full (${fmtBytes(d.free)} free)`, q: "" };
  return { txt: JSON.stringify(d), q: "" };
}
function renderEvents(list) {
  const box = $("events"); box.textContent = "";
  if (!list.length) { const e = document.createElement("div"); e.className = "empty";
    e.textContent = "no events yet"; box.appendChild(e); return; }
  for (const ev of list.slice().reverse()) {
    const row = document.createElement("div"); row.className = "evrow";
    const b = document.createElement("span"); b.className = "badge"; b.textContent = ev.type;
    const body = document.createElement("span"); body.className = "evbody";
    const s = evSummary(ev);
    body.textContent = s.txt;                                    // textContent → no injection
    if (s.q) { const q = document.createElement("span"); q.className = "q";
      q.textContent = ' "' + s.q.slice(0, 80) + '"'; body.appendChild(q); }
    const ts = document.createElement("span"); ts.className = "ts";
    ts.textContent = new Date((ev.ts || 0) * 1000).toLocaleTimeString();
    row.append(b, body, ts); box.appendChild(row);
  }
}
function setConn(ok) {
  $("dot").className = "dot " + (ok ? "ok" : "bad");
  $("conn").textContent = ok ? "connected" : "disconnected";
}
async function tick() {
  try {
    const st = await api("/status");
    $("nodeid").textContent = st.node_id || "—";
    $("askcard").style.display = st.can_ask ? "" : "none";        // show Ask only if the node supports it
    try {
      const mems = await api("/memories");
      if (mems && mems.length) { $("memcard").style.display = "";
        renderMemoryList("memories", mems, ""); } else { $("memcard").style.display = "none"; }
    } catch (e) { $("memcard").style.display = "none"; }
    try {
      const s = await api("/storage");
      if (s && s.quota_bytes) {
        const pct = Math.min(100, 100 * (s.total_bytes || 0) / s.quota_bytes);
        $("storebar").style.width = pct + "%";
        $("storetxt").textContent = `${fmtBytes(s.total_bytes)} / ${fmtBytes(s.quota_bytes)} · ${s.count||0} artifacts`;
      } else { $("storetxt").textContent = "no artifact cache on this node"; }
    } catch (e) { $("storetxt").textContent = "—"; }
    renderEvents(await api("/events?limit=50"));
    setConn(true);
  } catch (e) { setConn(false); }
}
$("askbtn").addEventListener("click", doAsk);
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") doAsk(); });
if (!token) { $("conn").textContent = "no token in URL (#<token>)"; }
else { tick(); setInterval(tick, 2000); }
</script>
</body></html>
"""
