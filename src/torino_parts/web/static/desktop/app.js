"use strict";
/* Torino desktop UI. Talks only to the local server (127.0.0.1) with a per-session token. */

const qs = new URLSearchParams(location.search);
const TOKEN = qs.get("t") || sessionStorage.getItem("torino-t") || "";
sessionStorage.setItem("torino-t", TOKEN);
if (qs.get("t")) history.replaceState(null, "", "/" + (location.hash || "#/rebuild"));

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rid = (p = "ui") => `${p}-${crypto.randomUUID()}`;
const S = { status: null, seq: 0, version: null, minutes: null, followScans: true, lastScan: null };
const STATE_LABEL = { ready: "Ready", in_progress: "In progress", ready_with_checks: "Ready · check first", blocked: "Blocked",
  on_hold: "On hold", done: "Done", cancelled: "Cancelled", planned: "Planned" };
const NEED_LABEL = { ready: "On hand", needs_check: "On hand · check", missing: "Missing", short: "Short", installed: "Installed", away: "At outside shop" };
const SHOP_LABEL = { considering: "Considering", ready: "Ready to buy", ordered: "Ordered", received: "Received", cancelled: "Cancelled" };
const money$ = (n) => n == null ? "—" : (n < 0 ? "-$" : "$") + Math.abs(+n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

async function api(path, body) {
  const opt = { headers: { "X-Torino-Token": TOKEN } };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch { data = { error: `HTTP ${r.status}` }; }
  if (!r.ok) { const e = new Error(data.error || `HTTP ${r.status}`); e.data = data; e.status = r.status; throw e; }
  return data;
}
const media = (path) => `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(TOKEN)}`;
const photoSrc = (link) => media(`/media/photo?link=${encodeURIComponent(link)}`);
const badge = (s, label) => `<span class="badge b-${esc(s)}">${esc(label || STATE_LABEL[s] || NEED_LABEL[s] || s)}</span>`;
const when = (iso) => iso ? esc(String(iso).replace("T", " ").slice(0, 16)) : "";
const activeVid = () => S.status?.active_vehicle?.vehicle_id || "";
const jlink = (id, title) => `<a href="#/job/${esc(id)}">${esc(id)}${title ? " · " + esc(title) : ""}</a>`;
const plink = (id, name) => `<a href="#/part/${esc(id)}">${esc(name || id)}</a>`;
const blink = (code) => `<a href="#/bin/${encodeURIComponent(code)}">${esc(code)}</a>`;

function toast(html, kind = "", ms = 5000) {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.innerHTML = html;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), ms);
  return t;
}
function errMsg(e) {
  const d = e.data || {};
  let m = esc(d.error || e.message);
  if (d.options) m += "<ul>" + d.options.map((o) => `<li>${esc(o.bin_code)}: ${esc(o.qty)} ${esc(o.condition)} (${esc(o.stock_id)})</li>`).join("") + "</ul>";
  if (d.missing_bin) m += `<br>Create bin ${esc(d.missing_bin)} first (Parts &amp; Bins).`;
  return m;
}
function showError(e) { toast(errMsg(e), "bad", 9000); }

/* ------------------------------------------------------------ status bar */
async function refreshStatus() {
  try { S.status = await api("/api/status"); } catch (e) { S.status = null; $("#errorBanner").hidden = false; $("#errorBanner").textContent = e.message; return; }
  const st = S.status;
  $("#demoBanner").hidden = !st.demo;
  const eb = $("#errorBanner");
  if (st.error) {
    eb.hidden = false;
    eb.innerHTML = `${esc(st.error)} <button class="btn sm" data-go="#/settings">Open Settings</button>`;
  } else eb.hidden = true;
  const wb = $("#chipWorkbook");
  wb.className = "chip " + (!st.workbook.ok ? "bad" : st.demo ? "demo" : st.local ? "local" : "google");
  wb.textContent = st.demo ? "DEMO data" : st.local ? (st.workbook.ok ? "LOCAL · this PC" : "Local: problem")
    : st.workbook.ok ? `GOOGLE · ${st.workbook.name || "workbook"}` : "Google: sign in needed";
  wb.title = { Demo: "Sample records, not your real garage", Local: "Your records in a database on this PC (works offline)", Google: "Your records in your Google Sheet and Drive" }[st.mode] || "";
  const pr = $("#chipPrinter");
  pr.className = "chip " + ({ ok: "ok", warn: "warn", bad: "bad", info: "info" }[st.printer.level] || "info");
  pr.textContent = "Printer: " + st.printer.text;
  const ph = $("#chipPhone");
  ph.className = "chip " + ({ ok: "ok", warn: "warn", info: "info" }[st.phone.level] || "info");
  ph.textContent = st.phone.text;
  const sel = $("#vehicleSel");
  const vs = st.vehicles || [];
  sel.innerHTML = vs.length ? vs.map((v) => `<option value="${esc(v.vehicle_id)}" ${v.vehicle_id === activeVid() ? "selected" : ""}>${esc(v.label)}</option>`).join("")
    : `<option value="">No vehicles yet</option>`;
  S.version = S.version ?? st.data_version;
}

$("#vehicleSel").addEventListener("change", async (ev) => {
  try { await api("/api/active-vehicle", { vehicle_id: ev.target.value }); await refreshStatus(); toast(`Active vehicle: ${esc(S.status.active_vehicle.label)}`, "ok"); render(); }
  catch (e) { showError(e); }
});

$("#scanForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("#scanBox").value.trim();
  if (!q) return;
  await openCode(q);
  $("#scanBox").select();
});

async function openCode(q) {
  try {
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    if (r.kind === "part") location.hash = `#/part/${r.part.part_id}`;
    else if (r.kind === "bin") location.hash = `#/bin/${encodeURIComponent(r.bin.bin_code)}`;
    else if (r.kind === "job") location.hash = `#/job/${r.job.job_id}`;
    else if (r.kind === "vehicle") location.hash = `#/vehicle/${r.vehicle.vehicle_id}`;
    else location.hash = `#/parts?q=${encodeURIComponent(q)}`;
  } catch (e) { showError(e); }
}

/* --------------------------------------------------------------- router */
const VIEWS = {};
function route() {
  const h = location.hash.replace(/^#\/?/, "") || "rebuild";
  const [pathPart, query] = h.split("?");
  const [name, ...rest] = pathPart.split("/");
  return { name, arg: rest.map(decodeURIComponent).join("/"), params: new URLSearchParams(query || "") };
}
let rendering = 0;
async function render() {
  const r = route();
  const my = ++rendering;
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === ({ job: "jobs", part: "parts", bin: "parts", vehicle: "vehicles", away: "parts" }[r.name] || r.name)));
  const view = VIEWS[r.name] || VIEWS.rebuild;
  try {
    const html = await view(r);
    if (my !== rendering) return;
    $("#main").innerHTML = html;
    view.after && view.after(r);
  } catch (e) {
    if (my !== rendering) return;
    $("#main").innerHTML = `<div class="box bad"><b>Couldn't load this page.</b><br>${errMsg(e)}</div>`;
  }
}
window.addEventListener("hashchange", render);

/* -------------------------------------------------------- shared pieces */
function vehicleStrip(label, extra = "") {
  return `<div class="vehicle-strip">Vehicle: ${esc(label)} ${extra}</div>`;
}
function noVehicle() {
  return `<div class="empty"><h2>No vehicle yet</h2><p>Add your first vehicle to start planning the rebuild.</p>
    <a class="btn big primary" href="#/vehicles">Add a vehicle</a></div>`;
}
function jobCard(j, opts = {}) {
  const pulls = (j.pull_list || []).map((p) => `<li>Pull <b>${esc(p.take)} × ${esc(p.name)}</b> from ${p.bins.map(esc).join(", ")}</li>`).join("");
  const checks = (j.checks || []).map((c) => `<li>${esc(c)}</li>`).join("");
  const blockers = (j.blockers || []).map((b) => typeof b === "string" ? `<li>${esc(b)}</li>`
    : `<li><b>${esc(b.text)}</b><div class="clear">To clear: ${esc(b.clear)}</div></li>`).join("");
  const time = j.remaining_min != null ? `${j.remaining_min} min left` : j.estimate_min ? `${j.estimate_min} min` : "no estimate";
  return `<div class="card state-${esc(j.state)}">
    <div class="spread"><div>${badge(j.state)} <span class="muted small">${esc(j.system)} · P${esc(j.priority)} · ${esc(time)} · steps ${esc(j.steps)}</span></div>
    <span class="muted small">${esc(j.job_id)}</span></div>
    <h3><a href="#/job/${esc(j.job_id)}">${esc(j.title)}</a></h3>
    ${j.next_step ? `<div>Next step: <b>${esc(j.next_step)}</b></div>` : ""}
    ${pulls ? `<ul>${pulls}</ul>` : ""}
    ${checks ? `<div class="box warn"><b>Check first</b><ul>${checks}</ul></div>` : ""}
    ${blockers ? `<div class="box bad"><b>Blocked</b><ul>${blockers}</ul></div>` : ""}
    ${j.unblocks && j.unblocks.length ? `<div class="small">Finishing this unblocks ${j.unblocks.map((x) => jlink(x)).join(", ")}</div>` : ""}
    ${opts.noOpen ? "" : `<div class="row" style="margin-top:8px"><a class="btn" href="#/job/${esc(j.job_id)}">Open job</a></div>`}
  </div>`;
}
function activityList(acts) {
  if (!acts.length) return `<div class="empty">Nothing recorded yet.</div>`;
  return `<ul class="activity">${acts.map((a) => `<li><time>${when(a.timestamp)}</time>
    <div>${esc(a.summary)} ${a.job_id ? `<span class="small">(${jlink(a.job_id)})</span>` : ""} <span class="src">${esc(a.source || "")}</span></div></li>`).join("")}</ul>`;
}
function photoGrid(photos) {
  if (!photos.length) return `<div class="empty">No photos yet.</div>`;
  return `<div class="photos">${photos.map((p) => `<figure><a href="${esc(photoSrc(p.link))}" target="_blank"><img loading="lazy" src="${esc(photoSrc(p.link))}" alt=""></a>
    <figcaption><b>${esc(p.photo_id)}</b> ${esc(p.caption || "")}<br><span class="muted">${esc(p.stage || "")} ${p.job_id ? jlink(p.job_id) : ""} ${p.part_id ? plink(p.part_id) : ""} · ${when(p.taken_at)}</span></figcaption></figure>`).join("")}</div>`;
}
function photoUpload(target) {
  return `<form class="row" data-submit="photo" data-target='${esc(JSON.stringify(target))}'>
    <input type="hidden" name="request_id" value="${rid("photo")}">
    <input type="file" name="file" accept="image/*" required>
    <input name="caption" placeholder="Caption (optional)">
    <select name="stage"><option value="">Stage…</option><option>before</option><option>in place</option><option>during</option><option>after</option></select>
    <button class="btn">Add photo</button></form>`;
}
async function fileToDataUrl(file, max = 2000) {
  const img = await createImageBitmap(file);
  const scale = Math.min(1, max / Math.max(img.width, img.height));
  const c = document.createElement("canvas");
  c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
  c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.85);
}

/* ------------------------------------------------------------- Rebuild */
VIEWS.rebuild = async () => {
  await (S.status ? Promise.resolve() : refreshStatus());
  const vid = activeVid();
  if (!vid) return noVehicle();
  const m = S.minutes;
  const [n, acts, res] = await Promise.all([api(`/api/next?vehicle=${vid}${m ? `&minutes=${m}` : ""}`), api(`/api/activity?vehicle=${vid}&limit=12`),
    api(`/api/resume?vehicle=${vid}`).catch(() => null)]);
  const chips = [[null, "Any time"], [15, "15 min"], [30, "30 min"], [60, "1 hour"], [120, "2 hours"]]
    .map(([v, l]) => `<button class="btn ${S.minutes === v ? "on" : ""}" data-act="minutes" data-arg="${v ?? ""}">${l}</button>`).join("");
  let timeBlock = "";
  if (m) {  // each job is listed once: fits_in_time here, the other ready jobs under "Also ready"
    timeBlock = `<h2>Fits in ${m} minutes with what you have (${n.fits_in_time.length})</h2>
      ${n.fits_in_time.length ? `<div class="cards">${n.fits_in_time.map((j) => jobCard(j)).join("")}</div>` : `<div class="box info">${esc(n.note || "Nothing fits.")}</div>`}
      ${n.no_estimate.length ? `<p class="muted">No time estimate yet: ${n.no_estimate.map((x) => jlink(x)).join(", ")} — add one to include them.</p>` : ""}
      ${n.quick_checks ? `<div class="box warn"><b>Quick checks you could do</b><ul>${n.quick_checks.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>` : ""}`;
  }
  return `<div class="spread"><div><h1>${esc(n.vehicle)}</h1><div class="muted">What can I do next? Based on job dependencies and the parts you have.</div></div>
    <div class="row"><button class="btn big primary" data-act="newJob">+ New job</button><button class="btn big" data-act="removal">Record removal</button></div></div>
    ${res ? resumeBox(res) : ""}
    <div class="timechips">${chips}</div>
    ${timeBlock}
    <h2>${m ? "Also ready (longer, or no estimate)" : "Do next"} (${n.do_next.length})</h2>
    ${n.do_next.length ? `<div class="cards">${n.do_next.map((j) => jobCard(j)).join("")}</div>` : `<div class="empty">${m ? "No other job is ready." : "No job is ready. See what's blocking below."}</div>`}
    <h2>Waiting on parts or earlier work (${n.blocked.length})</h2>
    ${n.blocked.length ? `<div class="cards">${n.blocked.map((j) => jobCard(j)).join("")}</div>` : `<div class="empty">Nothing is blocked.</div>`}
    ${n.unblock_actions.length ? `<h2>What would unblock the most</h2><div class="card"><ul>${n.unblock_actions.map((a) => `<li>${esc(a.action)} <span class="muted">→ unblocks ${a.unblocks.map((x) => jlink(x)).join(", ")}</span></li>`).join("")}</ul></div>` : ""}
    <h2>Recent changes</h2><div class="card">${activityList(acts.activity)}</div>`;
};

/* ------------------------------------------------------------- Vehicles */
VIEWS.vehicles = async () => {
  const { vehicles } = await api("/api/vehicles");
  const cards = vehicles.map((v) => `<div class="card"><div class="spread"><h3><a href="#/vehicle/${esc(v.vehicle_id)}">${esc(v.label)}</a></h3>
      ${v.vehicle_id === activeVid() ? badge("ready", "Active") : `<button class="btn sm" data-act="activate" data-arg="${esc(v.vehicle_id)}">Make active</button>`}</div>
      <div class="muted small">${esc([v.engine, v.vin && "VIN " + v.vin, v.notes].filter(Boolean).join(" · "))}</div></div>`).join("");
  return `<h1>Vehicles</h1><div class="cards">${cards || ""}</div>
    <h2>Add a vehicle</h2>
    <form class="stack card" data-submit="addVehicle"><input type="hidden" name="request_id" value="${rid("veh")}">
      <div class="grid2"><label class="f">Year<input name="year" type="number" min="1886" max="2100" required></label>
      <label class="f">Make<input name="make" required placeholder="Ford"></label>
      <label class="f">Model<input name="model" required placeholder="Torino"></label>
      <label class="f">Nickname<input name="nickname" placeholder="optional"></label>
      <label class="f">Trim<input name="trim" placeholder="optional"></label>
      <label class="f">Engine<input name="engine" placeholder="optional"></label>
      <label class="f">VIN<input name="vin" placeholder="optional"></label></div>
      <label class="f">Notes<input name="notes"></label>
      <label class="row"><input type="checkbox" name="make_active" checked> Make this the active vehicle</label>
      <div><button class="btn big primary">Save vehicle</button></div></form>`;
};

VIEWS.vehicle = async (r) => {
  const v = await api(`/api/vehicle/${encodeURIComponent(r.arg)}`);
  const d = v.detail;
  const states = Object.entries(v.jobs_by_state).map(([k, n]) => `${badge(k)} ${n}`).join(" ");
  const sys = Object.entries(v.systems).map(([k, x]) => `<tr><td>${esc(k)}</td><td class="num">${x.open}</td><td class="num">${x.done}</td></tr>`).join("");
  return `<h1>${esc(v.label)}</h1>
    <div class="row">${v.vehicle_id === activeVid() ? badge("ready", "Active vehicle") : `<button class="btn" data-act="activate" data-arg="${esc(v.vehicle_id)}">Make active</button>`}
      <a class="btn" href="#/jobs?vehicle=${esc(v.vehicle_id)}">Jobs</a><a class="btn" href="#/photos?vehicle=${esc(v.vehicle_id)}">Photos</a></div>
    <p>${states || "No jobs yet."}</p>
    <div class="grid2"><div><h2>Systems</h2><table><tr><th>System</th><th>Open</th><th>Done</th></tr>${sys || "<tr><td colspan=3>—</td></tr>"}</table></div>
    <div><h2>Installed on this vehicle (${d.installed_parts.length})</h2><table><tr><th>Part</th><th>Qty</th><th>When</th><th>From</th></tr>
      ${d.installed_parts.map((i) => `<tr><td>${plink(i.part_id, i.name)}</td><td class="num">${esc(i.qty)}</td><td>${when(i.installed_at)}</td><td>${esc(i.from_bin || "")}</td></tr>`).join("") || "<tr><td colspan=4>Nothing yet</td></tr>"}</table></div></div>
    <h2>Parts that came off this vehicle (in stock)</h2>
    <table><tr><th>Part</th><th>Bin</th><th>Qty</th><th>Condition</th></tr>${d.donated_stock.map((s) => `<tr><td>${plink(s.part_id, s.name)}</td><td>${blink(s.bin_code)}</td><td class="num">${esc(s.qty)}</td><td>${esc(s.condition)}</td></tr>`).join("") || "<tr><td colspan=4>None</td></tr>"}</table>
    <h2>Recent activity</h2><div class="card">${activityList(v.recent_activity)}</div>`;
};

/* -------------------------------------------------------------- Jobs */
VIEWS.jobs = async (r) => {
  const vid = r.params.get("vehicle") || activeVid();
  if (!vid) return noVehicle();
  const closed = r.params.get("closed") === "1";
  const { jobs } = await api(`/api/jobs?vehicle=${vid}${closed ? "&closed=1" : ""}`);
  const bySys = {};
  jobs.forEach((j) => (bySys[j.system || "other"] ||= []).push(j));
  const sections = Object.keys(bySys).sort().map((s) => `<h2>${esc(s)} (${bySys[s].length})</h2>
    <div class="tablewrap"><table><tr><th>Job</th><th>State</th><th>Parts</th><th>Time</th><th>Steps</th><th>Blocked by / check</th></tr>
    ${bySys[s].map((j) => `<tr><td>${jlink(j.job_id, j.title)}</td><td>${badge(j.state)}</td>
      <td class="small">${j.parts.needed ? `${j.parts.ready} ready · ${j.parts.to_check} to check · ${j.parts.missing} missing · ${j.parts.installed} installed` : "—"}</td>
      <td>${j.remaining_min != null ? esc(j.remaining_min) + " min" : "—"}</td><td>${esc(j.steps)}</td>
      <td class="small">${[...j.blockers, ...j.checks].map(esc).join("<br>")}</td></tr>`).join("")}</table></div>`).join("");
  return `<div class="spread"><h1>Rebuild jobs</h1><div class="row">
    <a class="btn" href="#/jobs?vehicle=${esc(vid)}${closed ? "" : "&closed=1"}">${closed ? "Hide" : "Show"} done &amp; cancelled</a>
    <button class="btn big primary" data-act="newJob">+ New job</button></div></div>
    ${vehicleStrip(S.status.vehicles.find((v) => v.vehicle_id === vid)?.label || vid)}
    ${sections || `<div class="empty">No jobs yet. Plan the first one with <b>+ New job</b>.</div>`}`;
};

VIEWS.job = async (r) => {
  const j = await api(`/api/job/${encodeURIComponent(r.arg)}`);
  S.job = j;
  const open = !["done", "cancelled"].includes(j.status);
  const statusBtns = [
    j.status !== "in_progress" && open ? `<button class="btn go" data-act="jobStatus" data-arg="in_progress">Start / resume</button>` : "",
    open && j.status !== "on_hold" ? `<button class="btn" data-act="hold">Put on hold</button>` : "",
    open ? `<button class="btn primary" data-act="jobStatus" data-arg="done">Mark done</button>` : `<button class="btn" data-act="jobStatus" data-arg="planned">Reopen</button>`,
  ].join("");
  const left = j.where_you_left_off;
  const steps = j.checklist.map((s) => `<li class="${s.done ? "done" : ""}"><button class="tick" data-act="step" data-arg="${esc(s.step_id)}" data-done="${s.done ? 1 : 0}" aria-label="toggle">${s.done ? "✓" : ""}</button>
    <span>${esc(s.text)}</span>${s.done_at ? `<span class="muted small">${when(s.done_at)}</span>` : ""}</li>`).join("");
  const needs = j.parts_needed.map((n) => {
    const pull = n.pull_from.map((p) => `${blink(p.bin_code)}: ${esc(p.qty)} ${esc(p.condition)}`).join("<br>");
    const acts = [];
    if (n.part_id && n.qty_remaining > 0 && n.on_hand > 0 && open) acts.push(`<button class="btn go sm" data-act="install" data-arg="${esc(n.need_id)}">Install…</button>`);
    if (n.qty_remaining > 0 && (n.check !== "none" || ["unverified", "unknown", "not_listed"].includes(n.fitment)) && n.check_status !== "passed")
      acts.push(`<button class="btn sm" data-act="check" data-arg="${esc(n.need_id)}" data-v="passed">Check passed</button><button class="btn sm danger" data-act="check" data-arg="${esc(n.need_id)}" data-v="failed">Failed</button>`);
    if (!n.part_id) acts.push(`<button class="btn sm" data-act="linkPart" data-arg="${esc(n.need_id)}">Link part…</button>`);
    if (open && ["missing", "short"].includes(n.status)) acts.push(`<button class="btn sm" data-act="needToShop" data-arg="${esc(n.need_id)}">Add to shopping list</button>`);
    if (n.away) acts.push(...n.away.map((c) => `<button class="btn sm go" data-act="returnPart" data-arg="${esc(c.custody_id)}" data-qty="${esc(c.qty_away)}" data-bin="${esc(c.from_bin)}" data-holder="${esc(c.holder)}">Back from ${esc(c.holder)}…</button>`));
    if (n.part_id && n.pull_from.length) acts.push(`<button class="btn sm" data-act="labelPart" data-arg="${esc(n.part_id)}" data-bin="${esc(n.pull_from[0].bin_code)}">Label</button>`);
    return `<tr><td>${n.part_id ? plink(n.part_id, n.name) : `<b>${esc(n.name)}</b><div class="muted small">no part record</div>`}
        ${n.fitment ? `<div class="small">Fitment: ${badge(n.fitment, n.fitment_note || n.fitment.replace("_", " "))}</div>` : ""}
        ${n.check !== "none" ? `<div class="small">Check: ${esc(n.check.replace("_", " "))} ${badge(n.check_status)}</div>` : ""}
        ${n.shared_with ? `<div class="small muted">Also needed by ${n.shared_with.map((s) => jlink(s.job_id)).join(", ")}</div>` : ""}</td>
      <td class="num">${esc(n.qty_needed)}</td><td class="num">${esc(n.on_hand)}</td><td class="num">${esc(n.qty_installed)}</td>
      <td>${badge(n.status)}</td><td class="small">${pull || "—"}</td><td><div class="row">${acts.join("")}</div></td></tr>`;
  }).join("");
  return `${vehicleStrip(j.vehicle, `<small>· ${esc(j.job_id)} · ${esc(j.system)} · ${esc(j.kind)} · priority ${esc(j.priority)}</small>`)}
    <div class="spread"><div><h1>${esc(j.title)} ${badge(j.state)}</h1>${j.goal ? `<div><b>Goal:</b> ${esc(j.goal)}</div>` : ""}
      <div class="muted small">${j.estimate_min ? `Estimate ${esc(j.estimate_min)} min · ${esc(j.remaining_min)} min left` : "No time estimate"} ${j.hold_reason ? "· On hold: " + esc(j.hold_reason) : ""}</div></div>
      <div class="row">${statusBtns}<button class="btn" data-act="editJob">Edit</button></div></div>
    <div class="box info"><b>Where you left off:</b> ${left.last_activity ? `${when(left.last_activity.timestamp)} — ${esc(left.last_activity.summary)}` : "nothing recorded yet"}
      ${left.next_step ? `<br><b>Next step:</b> ${esc(left.next_step.text)}` : ""}</div>
    ${j.blockers.length ? `<div class="box bad"><b>Blocked</b><ul>${j.blockers.map((b) => `<li><b>${esc(b.text)}</b><div class="clear">To clear: ${esc(b.clear)}</div></li>`).join("")}</ul></div>` : ""}
    ${j.checks.length ? `<div class="box warn"><b>On hand, but check before installing</b><ul>${j.checks.map((c) => `<li>${esc(c.text)}</li>`).join("")}</ul></div>` : ""}
    <h2>Parts &amp; hardware</h2>
    <div class="tablewrap"><table><tr><th>Part</th><th>Needed</th><th>On hand</th><th>Installed</th><th>Status</th><th>Pull from</th><th></th></tr>${needs || `<tr><td colspan=7 class="muted">No parts listed.</td></tr>`}</table></div>
    <form class="row" data-submit="addNeed" style="margin-top:10px"><input type="hidden" name="request_id" value="${rid("need")}">
      <input name="part_id" placeholder="Part ID (P-…)" size="10"><input name="description" placeholder="…or describe it" size="28">
      <input name="qty" type="number" min="1" value="1" style="width:80px">
      <select name="check"><option value="none">No check</option><option value="inspect">Inspect first</option><option value="verify_fitment">Verify fitment</option></select>
      <button class="btn">Add part need</button></form>
    <h2>Checklist (${esc(j.steps)})</h2><ul class="steps">${steps || `<li class="muted">No steps yet.</li>`}</ul>
    <form class="row" data-submit="addSteps" style="margin-top:10px"><input type="hidden" name="request_id" value="${rid("steps")}">
      <input name="add_steps" placeholder="New step" size="40" required><button class="btn">Add step</button></form>
    <h2>Depends on</h2>
    ${j.depends_on.length ? `<ul>${j.depends_on.map((d) => `<li>${jlink(d.job_id, d.title)} ${badge(d.status)} <button class="btn sm" data-act="rmDep" data-arg="${esc(d.job_id)}">Remove</button></li>`).join("")}</ul>` : `<p class="muted">No earlier jobs required.</p>`}
    ${j.unblocks.length ? `<p>Finishing this unblocks: ${j.unblocks.map((u) => jlink(u.job_id, u.title)).join(", ")}</p>` : ""}
    <form class="row" data-submit="addDep"><input type="hidden" name="request_id" value="${rid("dep")}"><input name="dep" placeholder="Job ID (J-…)" required size="12"><button class="btn">Add dependency</button></form>
    <h2>Notes &amp; activity</h2>
    ${j.notes ? `<div class="card">${esc(j.notes)}</div>` : ""}
    <form class="row" data-submit="note" style="margin:10px 0"><input type="hidden" name="request_id" value="${rid("note")}"><input name="note" placeholder="What did you do / find? (saved to the activity log)" size="60" required><button class="btn primary">Add note</button></form>
    <div class="card">${activityList(j.activity)}</div>
    <h2>Photos</h2>${photoUpload({ job_id: j.job_id })}<div style="margin-top:10px">${photoGrid(j.photos)}</div>
    <h2>Specifications</h2>
    ${j.specs.length ? `<table><tr><th>Item</th><th>Value</th><th>Source</th><th></th></tr>${j.specs.map((s) => `<tr><td>${esc(s.item)}</td><td><b>${esc(s.value)} ${esc(s.unit || "")}</b></td><td class="small">${esc(s.source)}</td><td>${badge(s.verified)}</td></tr>`).join("")}</table>` : `<p class="muted">No specs saved.</p>`}
    <form class="row" data-submit="spec" style="margin-top:10px"><input type="hidden" name="request_id" value="${rid("spec")}">
      <input name="item" placeholder="e.g. Caliper bolt torque" required><input name="value" placeholder="value" size="8" required><input name="unit" placeholder="unit" size="6">
      <input name="source" placeholder="Source (manual, page) — required" size="34" required><label class="row small"><input type="checkbox" name="verified"> I checked it in the source</label><button class="btn">Save spec</button></form>
    ${j.installed.length ? `<h2>Installed for this job</h2><table><tr><th>Part</th><th>Qty</th><th>From bin</th><th>When</th></tr>${j.installed.map((i) => `<tr><td>${plink(i.part_id)}</td><td class="num">${esc(i.qty)}</td><td>${esc(i.from_bin)}</td><td>${when(i.installed_at)}</td></tr>`).join("")}</table>` : ""}`;
};

/* ------------------------------------------------------- Parts & bins */
VIEWS.parts = async (r) => {
  const q = r.params.get("q") || "";
  const [res, bins, away] = await Promise.all([q ? api(`/api/parts?q=${encodeURIComponent(q)}&all=1`) : Promise.resolve(null), api("/api/bins"), api("/api/away")]);
  const results = res ? (res.results.length ? `<div class="tablewrap"><table><tr><th>Part</th><th>Side</th><th>Total</th><th>Where</th><th>Fits</th></tr>${res.results.map((p) => `<tr><td>${plink(p.part_id, p.label_name)}<div class="muted small">${esc(p.part_id)} ${p.status === "unidentified" ? badge("unknown", "unidentified") : ""}</div></td>
      <td>${esc(p.side_position || "")}</td><td class="num">${esc(p.total_qty)}</td>
      <td class="small">${p.stock.map((s) => `${blink(s.bin_code)}: ${esc(s.qty)} ${esc(s.condition)}`).join("<br>") || "none in stock"}</td>
      <td class="small">${p.fitment.map((f) => `${esc(f.fits)} ${badge(f.verified)}`).join("<br>") || esc(p.fitment_scope)}</td></tr>`).join("")}</table></div>` : `<div class="empty">No parts match “${esc(q)}”.</div>`) : "";
  return `<div class="spread"><h1>Parts &amp; Bins</h1><div class="row"><button class="btn primary big" data-act="receive">Receive stock</button><button class="btn big" data-act="removal">Record removal</button><button class="btn big" data-act="newBin">New bin</button></div></div>
    <form class="row" data-submit="partSearch"><input name="q" value="${esc(q)}" placeholder="Search parts: left door hinge, P-00001, D0OZ…" size="40"><button class="btn primary">Search</button></form>
    ${results}
    ${away.count ? `<h2>At outside shops (${away.count})</h2>${awayTable(away.away)}` : ""}
    <h2>Bins &amp; work areas</h2><div class="cards">${bins.bins.map((b) => `<div class="card"><h3>${blink(b.bin_code)} ${b.kind === "work_area" ? `<span class="small muted">work area</span>` : ""}</h3><div>${esc(b.description || "")}</div><div class="muted small">${esc(b.location || "")} · ${esc(b.entries)} entries · ${esc(b.total_qty)} items</div></div>`).join("")}</div>`;
};

VIEWS.part = async (r) => {
  const p = await api(`/api/part/${encodeURIComponent(r.arg)}`);
  S.part = p;
  // Empty rows stay visible (history) but only offer a count correction: nothing to move, send or label.
  const stock = p.stock.map((s) => `<tr class="${s.qty > 0 ? "" : "muted"}"><td>${blink(s.bin_code)}${s.place === "work_area" ? ` <span class="small muted">work area${s.place_name ? ": " + esc(s.place_name) : ""}</span>` : ""}</td>
    <td>${esc(s.condition)}</td><td class="num">${esc(s.qty)}</td><td class="small">${esc(s.donor_vehicle || "")}</td>
    <td><div class="row">${s.qty > 0 ? `<button class="btn sm" data-act="move" data-arg="${esc(s.stock_id)}" data-bin="${esc(s.bin_code)}" data-qty="${esc(s.qty)}">Move</button>
    <button class="btn sm" data-act="sendOut" data-arg="${esc(s.stock_id)}" data-bin="${esc(s.bin_code)}" data-qty="${esc(s.qty)}">Send out…</button>` : `<span class="small muted">empty</span>`}
    <button class="btn sm" data-act="adjust" data-arg="${esc(s.stock_id)}" data-qty="${esc(s.qty)}">Correct count</button>
    ${s.qty > 0 ? `<button class="btn sm" data-act="labelPart" data-arg="${esc(p.part_id)}" data-bin="${esc(s.bin_code)}">Print label</button>` : ""}</div></td></tr>`).join("");
  const away = p.away ? `<h2>Away from the shop (${esc(p.away_qty)})</h2>${awayTable(p.away.map((c) => ({ ...c, name: p.label_name })))}` : "";
  return `<div class="spread"><div><h1>${esc(p.label_name)}</h1><div class="muted">${esc(p.part_id)} · ${esc(p.status)} ${p.side_position ? "· " + esc(p.side_position) : ""}</div></div>
    <div class="row"><button class="btn primary" data-act="receive" data-arg="${esc(p.part_id)}">Receive more</button></div></div>
    ${p.status === "unidentified" ? `<div class="box warn">Unidentified part. ${esc(p.notes || "")}</div>` : ""}
    <div class="grid2"><div class="card"><b>In the shop:</b> <span class="num">${esc(p.total_qty)}</span>${p.away_qty ? ` · <b>away:</b> <span class="num">${esc(p.away_qty)}</span> (${p.away.map((c) => esc(c.holder)).join(", ")})` : ""}<br>${Object.entries(p.total_by_condition).map(([c, n]) => `${esc(c)}: ${esc(n)}`).join(" · ")}
      ${p.oem_number ? `<br>OEM #: ${esc(p.oem_number)}` : ""}${p.stamped_number ? `<br>Stamped: ${esc(p.stamped_number)}` : ""}${p.notes ? `<br>${esc(p.notes)}` : ""}</div>
    <div class="card"><b>Fits</b> (${esc(p.fitment_scope)})<ul>${p.fitment.map((f) => `<li>${esc(f.fits)} ${badge(f.verified)}</li>`).join("") || "<li>not recorded</li>"}</ul></div></div>
    <h2>Where it is in the shop</h2><table><tr><th>Bin / area</th><th>Condition</th><th>Qty</th><th>Came from</th><th></th></tr>${stock || `<tr><td colspan=5>None in stock</td></tr>`}</table>
    ${away}
    ${p.needed_by.length ? `<h2>Needed by jobs</h2><p>${p.needed_by.map((n) => jlink(n.job_id)).join(", ")}</p>` : ""}
    ${p.installed_on ? `<h2>Installed on</h2><ul>${p.installed_on.map((i) => `<li>${esc(i.vehicle)} — ${esc(i.qty)} on ${when(i.installed_at)}</li>`).join("")}</ul>` : ""}
    <h2>Photos</h2>${photoUpload({ part_id: p.part_id })}<div style="margin-top:10px">${photoGrid(p.photos)}</div>`;
};

VIEWS.bin = async (r) => {
  const b = await api(`/api/bin/${encodeURIComponent(r.arg)}`);
  return `<div class="spread"><div><h1>Bin ${esc(b.bin_code)}</h1><div class="muted">${esc(b.description || "")} ${esc(b.location || "")}</div></div>
    <button class="btn big" data-act="labelBin" data-arg="${esc(b.bin_code)}">Print bin label</button></div>
    <table><tr><th>Part</th><th>Condition</th><th>Qty</th><th>Came from</th></tr>${b.items.map((i) => `<tr><td>${plink(i.part_id, i.name)} <span class="muted small">${esc(i.side_position || "")}</span></td><td>${esc(i.condition)}</td><td class="num">${esc(i.qty)}</td><td class="small">${esc(i.donor_vehicle || "")}</td></tr>`).join("") || `<tr><td colspan=4>Empty</td></tr>`}</table>`;
};

/* ---------------------------------------------------------------- Photos */
VIEWS.photos = async (r) => {
  const vid = r.params.get("vehicle") || activeVid();
  const { photos } = await api(`/api/photos${vid ? `?vehicle=${vid}` : ""}`);
  const { jobs } = vid ? await api(`/api/jobs?vehicle=${vid}`) : { jobs: [] };
  return `<h1>Photos</h1>${vid ? vehicleStrip(S.status.vehicles.find((v) => v.vehicle_id === vid)?.label || vid) : ""}
    <form class="row card" data-submit="photoAny"><input type="hidden" name="request_id" value="${rid("photo")}">
      <input type="file" name="file" accept="image/*" required>
      <select name="job_id"><option value="">Whole vehicle</option>${jobs.map((j) => `<option value="${esc(j.job_id)}">${esc(j.job_id)} ${esc(j.title)}</option>`).join("")}</select>
      <input name="part_id" placeholder="Part ID (optional)" size="10"><input name="caption" placeholder="Caption"><button class="btn primary">Add photo</button></form>
    <p class="muted">Tip: take photos with your phone — pair it, scan a part or pick a job, then tap <b>Photo</b>.</p>
    ${photoGrid(photos)}`;
};

/* ---------------------------------------------------------------- Labels */
VIEWS.labels = async () => {
  const [{ jobs, printer }, s, rd] = await Promise.all([api("/api/labels"), api("/api/printer"), api("/api/setup")]);
  const set = s.settings;
  const resolveBtns = (j) => ["uncertain", "failed", "pending"].includes(j.status)
    ? `<button class="btn sm" data-act="resolve" data-arg="${esc(j.job_id)}" data-v="reprint">Print again</button>
       ${j.status === "uncertain" ? `<button class="btn sm" data-act="resolve" data-arg="${esc(j.job_id)}" data-v="mark_printed">It printed</button>` : ""}
       <button class="btn sm danger" data-act="resolve" data-arg="${esc(j.job_id)}" data-v="cancel">Cancel</button>`
    : (j.part_id && j.part_id !== "P-TEST" ? `<button class="btn sm" data-act="${j.kind === "bin" ? "labelBin" : "labelPart"}" data-arg="${esc(j.kind === "bin" ? j.bin_code : j.part_id)}" data-bin="${esc(j.bin_code)}">Reprint</button>` : "");
  const attention = jobs.filter((j) => ["uncertain", "failed"].includes(j.status));
  const pr = rd.printer;
  return `<h1>Labels &amp; printer</h1>
    <div class="ready-card ${esc(pr.level)}" style="margin-bottom:12px"><h3>Label printer ${badge(pr.ready ? "ready" : pr.level === "bad" ? "failed" : "pending", pr.ready ? "ready" : "not ready")}</h3>
      <div>${esc(pr.text)}</div><div class="small muted">Windows printer: ${pr.queue_exists ? "found" : pr.queue_exists === false ? "not found" : "?"} · b-PAC: ${pr.bpac ? "installed" : "missing"} · template: ${pr.template_exists ? "found" : "missing"} · mode: ${esc(pr.mode)}</div>
      <div class="next">${pr.next ? (pr.next.act ? `<button class="btn primary" data-act="${esc(pr.next.act)}" data-arg="${esc(pr.next.arg || "")}">${esc(pr.next.label)}</button>` : `<b>Next:</b> ${esc(pr.next.label)}`) : ""}</div>
      ${pr.ready ? "" : `<p class="small">Until it is ready, labels you request are saved with a preview below and print later — nothing is lost.</p>`}</div>
    <form class="card stack" data-submit="labelPreview" style="margin-bottom:12px"><h3>Preview a label</h3>
      <p class="small muted">Shows exactly what will print on ${esc(set.tape_mm)} mm tape. Nothing is queued or printed.</p>
      ${partPicker("part_id", "", "Part")}<label class="f">Bin<input name="bin_code" placeholder="A-01"></label>
      <div><button class="btn">Show preview</button></div><div id="previewOut"></div></form>
    <div class="grid2"><div class="card"><h3>Printer: ${badge(printer.ready ? "ready" : printer.level === "bad" ? "failed" : "pending", printer.text)}</h3>
      <div class="small">Mode: <b>${esc(set.mode)}</b> · Queue: ${esc(set.queue)} · Tape: ${esc(set.tape_mm)} mm · Auto-print after intake: ${set.auto_print ? "on" : "off"}<br>
      Template: ${esc(set.template || "not set")}<br>${printer.detail ? esc(printer.detail) : ""}
      ${printer.tape_detected ? `<br>Tape loaded: ${esc(printer.tape_detected)}` : ""}</div>
      <div class="row" style="margin-top:10px"><button class="btn primary" data-act="testLabel">Print test label</button><button class="btn" data-act="processQueue">Send pending labels</button></div></div>
    <form class="card stack" data-submit="printerSettings"><h3>Printer settings</h3>
      <div class="grid2"><label class="f">Mode<select name="mode"><option value="preview" ${set.mode === "preview" ? "selected" : ""}>Preview only</option><option value="live" ${set.mode === "live" ? "selected" : ""}>Live (PT-P700 via b-PAC)</option></select></label>
      <label class="f">TZe tape width<select name="tape_mm">${[3.5, 6, 9, 12, 18, 24].map((t) => `<option ${+set.tape_mm === t ? "selected" : ""}>${t}</option>`).join("")}</select></label>
      <label class="f">Windows printer queue<input name="queue" value="${esc(set.queue)}"></label>
      <label class="f">Auto-print after intake<select name="auto_print"><option value="false">Off</option><option value="true" ${set.auto_print ? "selected" : ""}>On</option></select></label></div>
      <div class="f"><b style="font-size:16px">Label template (.lbx)</b><div class="row"><input name="template" value="${esc(set.template)}" placeholder="C:\\Users\\you\\Documents\\Labels\\torino-24mm.lbx" style="flex:1;min-width:260px">
        <button type="button" class="btn" data-act="pickTemplate">Browse…</button></div><span class="hint" style="font-weight:400;color:var(--muted);font-size:14px">Must match the tape you have loaded. No P-touch Editor needed: <b>Create template</b> makes one for the tape width above.</span></div>
      <div class="row"><button class="btn primary">Save printer settings</button><button type="button" class="btn" data-act="makeTemplate">Create template for this tape</button></div></form></div>
    ${attention.length ? `<div class="box bad"><b>${attention.length} label job(s) need a decision.</b> “Uncertain” means the printer may already have printed it — check the printer before printing again.</div>` : ""}
    <h2>Label jobs</h2>
    <div class="tablewrap"><table><tr><th>Preview</th><th>Label</th><th>Status</th><th>When</th><th></th></tr>
    ${jobs.map((j) => `<tr><td>${j.preview_path ? `<img class="label-preview" src="${esc(media("/media/label/" + j.job_id))}" alt="">` : ""}</td>
      <td><b>${esc(j.part_name)}</b><br><span class="small">${esc(j.part_id)} · bin ${esc(j.bin_code)} · ${esc(j.kind)} · ${esc(j.job_id)}</span></td>
      <td>${badge(j.status)}<div class="small">${esc(j.last_error || j.message)}</div></td><td class="small">${when(j.updated_at)}</td><td><div class="row">${resolveBtns(j)}</div></td></tr>`).join("") || `<tr><td colspan=5>No labels yet.</td></tr>`}</table></div>`;
};

/* -------------------------------------------------------------- Settings */
VIEWS.settings = async () => {
  const [g, devs, mcp, rd, data] = await Promise.all([api("/api/google"), api("/api/devices"), api("/api/mcp-commands"),
    api("/api/setup"), api("/api/data")]);
  const st = S.status;
  const flow = g.flow;
  const opt = (key, title, body, btn) => `<div class="source-opt ${st.backend === key ? "on" : ""}"><h3>${title} ${st.backend === key ? badge("ready", "in use") : ""}</h3>
    <p class="small">${body}</p>${st.backend === key ? "" : btn}</div>`;
  const backups = data.backups.map((b) => `<tr><td>${esc(b.name)}${b.error ? `<div class="small" style="color:var(--bad)">${esc(b.error)}</div>` : ""}</td>
    <td>${when(b.created_at)}</td><td>${esc(b.reason || "")}</td><td class="num">${esc(b.records ?? "")}</td><td class="num">${esc(b.photo_files ?? "")}</td>
    <td>${b.error ? "" : `<button class="btn sm" data-act="restorePreview" data-arg="${esc(b.path)}">Restore…</button>`}</td></tr>`).join("");
  return `<h1>Settings</h1>
    <h2>Setup status</h2>${readinessCards(rd)}
    <h2 id="data">Data source</h2>
    <div class="source-opts">
      ${opt("local", "Local — this PC", "Your real records in a database on this PC. Works offline, no Google account. Back it up below.",
        `<button class="btn primary" data-act="backend" data-arg="local">Use Local</button>`)}
      ${opt("google", "Google — Sheet + Drive", "Your real records in your own Google Sheet, photos in your Drive. Needs internet and sign-in.",
        `<button class="btn primary" data-act="backend" data-arg="google" ${g.configured ? "" : "disabled"}>Use Google</button>${g.configured ? "" : `<div class="small muted">Connect Google first (below).</div>`}`)}
      ${opt("demo", "Demo — sample records", "Made-up records to try things out. Never mixed with your real records.",
        `<button class="btn" data-act="backend" data-arg="demo">Use Demo</button>`)}</div>
    ${st.demo ? `<div class="row" style="margin-top:8px"><button class="btn danger" data-act="demoReset">Reset demo data</button></div>` : ""}
    <h3>Backups of Local data</h3><div class="card">
      <div class="row"><button class="btn primary" data-act="backupNow" ${st.backend === "google" ? "disabled" : ""}>Back up now</button>
        <span class="small muted">Folder: ${esc(data.backup_folder)}</span><button class="btn sm" data-act="pickBackupFolder">Change folder…</button>
        <button class="btn sm" data-act="restorePick">Restore from a file…</button></div>
      <p class="small muted">Each backup holds every record and every photo/receipt. Local mode also makes one automatic backup per day. Restoring first saves a backup of what is there now.</p>
      <div class="tablewrap"><table><tr><th>Backup</th><th>Made</th><th>Why</th><th>Records</th><th>Photos</th><th></th></tr>${backups || `<tr><td colspan=6>No backups yet.</td></tr>`}</table></div></div>
    <h3>Move data between Local and Google</h3><div class="card">
      <p class="small">Copies every record with the same IDs (jobs, parts, photos, shopping, expenses stay linked) and copies the photo files. The source is not changed. The target must be empty, so nothing is mixed.</p>
      <div class="row"><button class="btn" data-act="transferPreview" data-arg="local_to_google" ${data.google_configured ? "" : "disabled"}>Local → Google…</button>
        <button class="btn" data-act="transferPreview" data-arg="google_to_local" ${data.google_configured ? "" : "disabled"}>Google → Local…</button>
        ${data.google_configured ? "" : `<span class="small muted">Connect Google first.</span>`}</div></div>
    <h2 id="google">Google Drive (optional)</h2><div class="card">
      <p>${g.configured ? `Workbook <b>${esc(g.workbook_name)}</b> is set up.` : "Not connected yet."} ${g.signed_in ? "Signed in." : ""}</p>
      ${g.has_client_secret ? "" : `<p>Step 1: in Google Cloud create a <b>Desktop app</b> OAuth client and download its JSON (see the setup guide). Then choose it here:</p>`}
      <form class="row" data-submit="googleConnect"><input type="file" name="secret" accept=".json,application/json" ${g.has_client_secret ? "" : "required"}>
        <button class="btn primary">${g.configured ? "Reconnect Google" : "Connect Google"}</button></form>
      <p class="small muted">A browser window opens for Google sign-in. Torino asks only for access to files it creates (drive.file).</p>
      ${flow.state !== "idle" ? `<div class="box ${flow.state === "error" ? "bad" : flow.state === "done" ? "ok" : "info"}"><b>${esc(flow.state)}</b><ul>${(flow.log || []).map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>` : ""}</div>
    <h2>Phone</h2><div class="card"><div class="row"><button class="btn big primary" data-act="pair">Pair Phone</button></div>
      <table style="margin-top:10px"><tr><th>Device</th><th>Paired</th><th>Last seen</th><th></th></tr>${devs.devices.map((d) => `<tr><td>${esc(d.name)} ${d.connected ? badge("ready", "connected") : ""}</td><td>${esc(d.paired_at)}</td><td>${d.last_seen_s != null ? esc(Math.round(d.last_seen_s / 60)) + " min ago" : "—"}</td><td><button class="btn sm danger" data-act="revoke" data-arg="${esc(d.device_id)}">Unpair</button></td></tr>`).join("") || `<tr><td colspan=4>No phones paired.</td></tr>`}</table></div>
    <h2>Codex &amp; Claude Code</h2><div class="card"><p>Run once in PowerShell to give Codex the same records and actions:</p>
      <pre class="cmd">${esc(mcp.codex)}</pre><button class="btn sm" data-act="copy" data-arg="${esc(mcp.codex)}">Copy</button>
      <p>Claude Code:</p><pre class="cmd">${esc(mcp.claude)}</pre><button class="btn sm" data-act="copy" data-arg="${esc(mcp.claude)}">Copy</button></div>
    <p class="muted small">Torino ${esc(st.version)}</p>`;
};
VIEWS.settings.after = (r) => { if (r.arg) document.getElementById(r.arg)?.scrollIntoView({ block: "start" }); };

/* ------------------------------------------------- shared v0.3 pieces */
function resumeBox(r) {
  if (!r.job) {
    const last = r.last_activity ? `Last: ${when(r.last_activity.timestamp)} — ${esc(r.last_activity.summary)}` : "Nothing recorded yet.";
    return `<div class="card resume"><b>Where you left off.</b> ${last}</div>`;
  }
  const pulls = (r.pull_list || []).map((p) => `<li>Pull <b>${esc(p.take)} × ${esc(p.name)}</b> from ${p.from.map((f) => `${blink(f.bin_code)} (${esc(f.qty)} ${esc(f.condition)})`).join(", ")}</li>`).join("");
  return `<div class="card resume"><div class="spread"><div class="muted small">WHERE YOU LEFT OFF</div>${badge(r.job.state)}</div>
    <div class="big">${jlink(r.job.job_id, r.job.title)} <span class="muted small">steps ${esc(r.job.steps)}</span></div>
    ${r.next_step ? `<div style="font-size:20px;margin:6px 0">Next step: <b>${esc(r.next_step.text)}</b></div>` : `<div>All checklist steps are ticked.</div>`}
    ${pulls ? `<ul>${pulls}</ul>` : ""}
    ${r.blockers?.length ? `<div class="box bad"><ul>${r.blockers.map((b) => `<li><b>${esc(b.text)}</b><div class="clear">To clear: ${esc(b.clear)}</div></li>`).join("")}</ul></div>` : ""}
    ${r.last_on_this_job ? `<div class="small muted">Last on this job: ${when(r.last_on_this_job.timestamp)} — ${esc(r.last_on_this_job.summary)}</div>` : ""}
    ${r.last_note ? `<div class="small muted">Last note: ${esc(r.last_note.body.slice(0, 160))}</div>` : ""}
    <div class="row" style="margin-top:8px"><a class="btn primary" href="#/job/${esc(r.job.job_id)}">Resume this job</a><a class="btn" href="#/progress">Progress</a></div></div>`;
}

/* Part search / scan picker. A scanner gun types the code + Enter; typing searches as you go. */
function partPicker(name = "part_id", value = "", label = "Part") {
  return `<div class="f picker" data-picker="${esc(name)}"><b style="font-size:16px">${esc(label)}</b>
    <input type="search" class="pick-q" placeholder="Search or scan: left door hinge, P-00001…" autocomplete="off">
    <input type="hidden" name="${esc(name)}" value="${esc(value)}">
    <div class="pick-chosen">${value ? `Chosen: ${esc(value)} <button type="button" class="btn sm" data-act="pickClear">Change</button>` : ""}</div>
    <div class="pick-results"></div></div>`;
}
let pickTimer = null;
document.addEventListener("input", (ev) => {
  const qbox = ev.target.closest(".pick-q");
  if (!qbox) return;
  clearTimeout(pickTimer);
  pickTimer = setTimeout(() => pickSearch(qbox.closest(".picker"), qbox.value.trim()), 220);
});
document.addEventListener("keydown", async (ev) => {
  const qbox = ev.target.closest(".pick-q");
  if (!qbox || ev.key !== "Enter") return;
  ev.preventDefault();  // a scan ends with Enter: open the exact code
  const code = qbox.value.trim();
  if (!code) return;
  try {
    const r = await api(`/api/search?q=${encodeURIComponent(code)}`);
    if (r.kind === "part") pickSet(qbox.closest(".picker"), r.part.part_id, r.part.label_name);
    else pickSearch(qbox.closest(".picker"), code);
  } catch (e) { showError(e); }
});
async function pickSearch(pk, q) {
  const box = $(".pick-results", pk);
  if (!q) { box.innerHTML = ""; return; }
  const r = await api(`/api/parts?q=${encodeURIComponent(q)}&all=1`);
  box.innerHTML = r.results.slice(0, 8).map((p) => `<button type="button" data-act="pickPart" data-arg="${esc(p.part_id)}" data-name="${esc(p.label_name)}">
    <b>${esc(p.label_name)}</b> <span class="muted small">${esc(p.part_id)} · ${esc(p.total_qty)} on hand ${p.stock.map((s) => esc(s.bin_code)).join(" ")}</span></button>`).join("")
    || `<div class="muted small">No part matches “${esc(q)}”.</div>`;
}
function pickSet(pk, id, name) {
  $(`input[type=hidden]`, pk).value = id;
  $(".pick-chosen", pk).innerHTML = `Chosen: <b>${esc(name || id)}</b> (${esc(id)}) <button type="button" class="btn sm" data-act="pickClear">Change</button>`;
  $(".pick-results", pk).innerHTML = "";
  $(".pick-q", pk).value = "";
  pk.dispatchEvent(new CustomEvent("picked", { bubbles: true, detail: { id } }));
}
function readinessCards(rd) {
  const card = (key, title) => {
    const x = rd[key];
    const nx = x.next;
    const btn = !nx ? "" : nx.act ? `<button class="btn primary" data-act="${esc(nx.act)}" data-arg="${esc(nx.arg || "")}">${esc(nx.label)}</button>`
      : nx.go ? `<a class="btn primary" href="${esc(nx.go)}">${esc(nx.label)}</a>` : `<div><b>Next:</b> ${esc(nx.label)}</div>`;
    return `<div class="ready-card ${esc(x.level)}"><h3>${esc(title)} ${badge(x.ready ? "ready" : x.level === "bad" ? "failed" : "pending", x.ready ? "ready" : x.optional ? "optional" : "not ready")}</h3>
      <div>${esc(x.text)}</div><div class="next">${btn}</div></div>`;
  };
  return `<div class="ready-cards">${card("google", "Google Drive")}${card("printer", "Label printer")}${card("phone", "Phone scanner")}</div>`;
}

/* Native Windows file dialog in Torino.exe; a typed path in a plain browser. undefined = cancelled. */
async function pickPath(kind) {
  const bridge = window.pywebview && window.pywebview.api && window.pywebview.api.pick_file;
  if (bridge) { const p = await window.pywebview.api.pick_file(kind, ""); return p || undefined; }
  const p = window.prompt(kind === "folder" ? "Folder path:" : "Full path of the file:");
  return p === null ? undefined : p.trim();
}
async function restorePreview(path) {
  const p = await api("/api/data/restore", { path });
  const b = p.backup, c = p.current;
  const rows = Object.keys(b.counts).filter((k) => b.counts[k] || c.counts[k])
    .map((k) => `<tr><td>${esc(k)}</td><td class="num">${esc(b.counts[k])}</td><td class="num">${esc(c.counts[k])}</td></tr>`).join("");
  openDialog("Restore backup: preview", `<p><b>${esc(b.name)}</b> — made ${when(b.created_at)} by Torino ${esc(b.app_version || "")} from ${esc(b.backend)} data, ${esc(b.photo_files)} photo files.</p>
    <table><tr><th>Records</th><th>In the backup</th><th>In Local now</th></tr>${rows}</table>
    <div class="box warn">${esc(p.will)}</div>
    <div class="row"><button class="btn big primary" data-act="restoreConfirm" data-arg="${esc(path)}">Restore into Local</button><button class="btn big" data-act="closeDlg">Cancel</button></div>`);
}
function fileToDataUrlRaw(file) {
  return new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = bad; r.readAsDataURL(file); });
}

/* ------------------------------------------------------------ Progress */
VIEWS.progress = async () => {
  const vid = activeVid();
  if (!vid) return noVehicle();
  const p = await api(`/api/progress?vehicle=${vid}`);
  const t = p.totals;
  const pct = t.steps_total ? Math.round(100 * t.steps_done / t.steps_total) : 0;
  const sys = Object.entries(p.by_system).map(([k, x]) => `<tr><td>${esc(k)}</td><td class="num">${x.done}</td><td class="num">${x.open}</td></tr>`).join("");
  return `${vehicleStrip(p.vehicle)}<h1>Progress</h1>
    <div class="totals"><div class="total left"><div class="k">Jobs done</div><div class="v">${t.done} / ${t.jobs}</div><div class="progress-bar"><span style="width:${t.percent_jobs_done}%"></span></div></div>
      <div class="total spent"><div class="k">Checklist steps</div><div class="v">${t.steps_done} / ${t.steps_total}</div><div class="progress-bar"><span style="width:${pct}%"></span></div></div>
      <div class="total"><div class="k">In progress</div><div class="v">${t.in_progress}</div></div>
      <div class="total over"><div class="k">Blocked / on hold</div><div class="v">${t.blocked}</div></div>
      <div class="total"><div class="k">Parts installed</div><div class="v">${t.parts_installed}</div></div></div>
    ${resumeBox(p.resume)}
    <h2>Active work (${p.active.length})</h2>
    ${p.active.length ? `<div class="cards">${p.active.map((j) => jobCard(j)).join("")}</div>` : `<div class="empty">Nothing in progress.</div>`}
    <h2>Blocked (${p.blocked.length})</h2>
    ${p.blocked.length ? `<div class="cards">${p.blocked.map((j) => jobCard(j)).join("")}</div>` : `<div class="empty">Nothing is blocked.</div>`}
    ${p.away.length ? `<h2>Parts at outside shops (${p.away.length})</h2>${awayTable(p.away)}` : ""}
    <h2>Completed (${p.completed.length})</h2>
    ${p.completed.length ? `<table><tr><th>Job</th><th>System</th><th>Steps</th><th>Finished</th></tr>${p.completed.map((j) => `<tr><td>${jlink(j.job_id, j.title)}</td><td>${esc(j.system)}</td><td>${esc(j.steps)}</td><td>${when(j.completed_at)}</td></tr>`).join("")}</table>` : `<div class="empty">No finished jobs yet.</div>`}
    <div class="grid2"><div><h2>By system</h2><table><tr><th>System</th><th>Done</th><th>Open</th></tr>${sys || "<tr><td colspan=3>—</td></tr>"}</table></div>
    <div><h2>Recent milestones</h2><div class="card">${activityList(p.recent)}</div></div></div>`;
};
function awayTable(rows) {
  return `<div class="tablewrap"><table><tr><th>Part</th><th>Who has it</th><th>Why</th><th>Qty away</th><th>Since</th><th>Expected back</th><th></th></tr>
    ${rows.map((c) => `<tr><td>${plink(c.part_id, c.name || c.part_id)}${c.blocks_jobs && c.blocks_jobs.length ? `<div class="small">Blocks ${c.blocks_jobs.map((j) => jlink(j)).join(", ")}</div>` : ""}</td>
      <td><b>${esc(c.holder)}</b></td><td>${esc(c.reason || "")}</td><td class="num">${esc(c.qty_away)}${c.qty_returned ? `<div class="small muted">${esc(c.qty_returned)} back</div>` : ""}</td>
      <td>${when(c.sent_at)}${c.days_away != null ? `<div class="small muted">${esc(c.days_away)} days</div>` : ""}</td>
      <td>${esc(c.expected_back || "—")} ${c.overdue ? badge("failed", "overdue") : ""}</td>
      <td><button class="btn sm go" data-act="returnPart" data-arg="${esc(c.custody_id)}" data-qty="${esc(c.qty_away)}" data-bin="${esc(c.from_bin)}" data-holder="${esc(c.holder)}">Record return…</button></td></tr>`).join("")}</table></div>`;
}

/* ------------------------------------------------------------ Notebook */
VIEWS.notebook = async (r) => {
  const vid = activeVid();
  const q = r.params.get("q") || "", job = r.params.get("job") || "", tag = r.params.get("tag") || "";
  const [nb, jobs, photos] = await Promise.all([
    api(`/api/notebook?vehicle=${vid}&q=${encodeURIComponent(q)}${job ? `&job=${job}` : ""}${tag ? `&tag=${encodeURIComponent(tag)}` : ""}`),
    vid ? api(`/api/jobs?vehicle=${vid}&closed=1`) : { jobs: [] }, vid ? api(`/api/photos?vehicle=${vid}`) : { photos: [] }]);
  const notes = nb.notes.map((n) => `<div class="note">${n.photo ? `<a href="${esc(photoSrc(n.photo.link))}" target="_blank"><img src="${esc(photoSrc(n.photo.link))}" alt=""></a>` : ""}
    <div><div class="meta">${when(n.created_at)} · ${esc(n.note_id)} · ${esc(n.source || "")}</div>
    ${n.title ? `<h3 style="margin:2px 0">${esc(n.title)}</h3>` : ""}<div style="white-space:pre-wrap">${esc(n.body)}</div>
    <div class="small">${n.job ? `Job ${jlink(n.job.job_id, n.job.title)} ` : ""}${n.part ? `· Part ${plink(n.part.part_id, n.part.name)} ` : ""}${n.photo ? `· Photo ${esc(n.photo.photo_id)}` : ""}</div>
    ${n.tags ? `<div class="tags">${n.tags.split(",").map((t) => `<a href="#/notebook?tag=${encodeURIComponent(t.trim())}"><span>#${esc(t.trim())}</span></a>`).join("")}</div>` : ""}</div></div>`).join("");
  return `<h1>Notebook</h1>${vid ? vehicleStrip(S.status.active_vehicle.label) : ""}
    <form class="row" data-submit="nbSearch"><input name="q" value="${esc(q)}" placeholder="Search notes: bore, torque, sleeve…" size="34">
      <select name="job"><option value="">All jobs</option>${jobs.jobs.map((j) => `<option value="${esc(j.job_id)}" ${j.job_id === job ? "selected" : ""}>${esc(j.job_id)} ${esc(j.title)}</option>`).join("")}</select>
      <button class="btn primary">Search</button>${q || job || tag ? `<a class="btn" href="#/notebook">Clear</a>` : ""}</form>
    ${tag ? `<p>Tag: <b>#${esc(tag)}</b></p>` : ""}
    <details class="card" ${q || job || tag ? "" : "open"} style="margin:12px 0"><summary><b>New note</b></summary>
    <form class="stack" data-submit="addNote" style="margin-top:10px"><input type="hidden" name="request_id" value="${rid("note")}">
      <label class="f">Title<input name="title" placeholder="optional"></label>
      <label class="f">Note<textarea name="body" required placeholder="What did you do, find, measure or decide?"></textarea></label>
      <div class="grid2"><label class="f">Job<select name="job_id"><option value="">—</option>${jobs.jobs.map((j) => `<option value="${esc(j.job_id)}">${esc(j.job_id)} ${esc(j.title)}</option>`).join("")}</select></label>
      <label class="f">Photo<select name="photo_id"><option value="">—</option>${photos.photos.slice(0, 80).map((p) => `<option value="${esc(p.photo_id)}">${esc(p.photo_id)} ${esc(p.caption || "")}</option>`).join("")}</select></label>
      <label class="f">Tags<input name="tags" placeholder="brakes, measure"></label></div>
      ${partPicker("part_id", "", "Part (optional)")}
      <div><button class="btn big primary">Save note</button></div></form></details>
    <h2>${nb.count} note(s)</h2>${notes || `<div class="empty">No notes${q ? ` matching “${esc(q)}”` : " yet"}.</div>`}`;
};

/* ------------------------------------------------------------ Shopping */
VIEWS.shopping = async () => {
  const vid = activeVid();
  if (!vid) return noVehicle();
  const [s, jobs] = await Promise.all([api(`/api/shopping?vehicle=${vid}`), api(`/api/jobs?vehicle=${vid}`)]);
  S.shopping = s;
  const item = (i) => {
    const acts = [];
    if (i.status === "considering") acts.push(`<button class="btn sm" data-act="shopStatus" data-arg="${esc(i.item_id)}" data-v="ready">Ready to buy</button>`);
    if (["considering", "ready"].includes(i.status)) acts.push(`<button class="btn sm primary" data-act="shopOrder" data-arg="${esc(i.item_id)}">Ordered…</button>`);
    if (["ready", "ordered"].includes(i.status)) acts.push(`<button class="btn sm go" data-act="shopReceive" data-arg="${esc(i.item_id)}">Receive…</button>`);
    if (i.status === "ordered" && !i.paid) acts.push(`<button class="btn sm" data-act="shopPaid" data-arg="${esc(i.item_id)}">Record payment…</button>`);
    if (!["received", "cancelled"].includes(i.status)) acts.push(`<button class="btn sm danger" data-act="shopStatus" data-arg="${esc(i.item_id)}" data-v="cancelled">Cancel</button>`);
    return `<div class="shop-item"><div class="spread"><h4>${esc(i.qty)} × ${esc(i.name)}</h4>${badge(i.status, SHOP_LABEL[i.status])}</div>
      <div class="small">${esc(i.item_id)} ${i.vendor ? "· " + esc(i.vendor) : ""} ${i.est_price != null ? `· ~${money$(i.est_price)} each (${money$(i.est_total)})` : ""}
      ${i.link ? `· <a href="${esc(i.link)}" target="_blank" rel="noopener">link</a>` : ""}</div>
      ${i.job_id ? `<div class="small">For ${jlink(i.job_id, i.job_title)}${i.need_id ? ` (${esc(i.need_id)})` : ""}</div>` : ""}
      ${i.part_id ? `<div class="small">Part ${plink(i.part_id)}</div>` : ""}
      ${i.partially_received ? `<div class="small"><b>${esc(i.qty_received)} received, ${esc(i.qty_to_come)} still to come</b></div>` : ""}
      ${i.paid != null ? `<div class="small">Paid ${money$(i.paid)} (${(i.expense_ids || []).map(esc).join(", ")})</div>` : ""}
      ${i.order_ref ? `<div class="small">Order ${esc(i.order_ref)}</div>` : ""}${i.notes ? `<div class="small muted">${esc(i.notes)}</div>` : ""}
      <div class="row" style="margin-top:6px">${acts.join("")}</div></div>`;
  };
  const col = (st) => `<div class="shop-col"><h2>${SHOP_LABEL[st]} (${s.by_status[st].length})</h2>
    <div class="small muted">Estimated ${money$(s.est_totals[st])}</div>${s.by_status[st].map(item).join("") || `<div class="empty">—</div>`}</div>`;
  return `${vehicleStrip(s.vehicle)}<div class="spread"><h1>Shopping list</h1><span class="muted">Buying happens outside Torino; this list tracks it.</span></div>
    <details class="card" style="margin:10px 0"><summary><b>+ Add item</b></summary>
    <form class="stack" data-submit="addItem" style="margin-top:10px"><input type="hidden" name="request_id" value="${rid("shop")}">
      <div class="grid2"><label class="f">What<input name="name" required placeholder="Front rotor 11.3 in"></label><label class="f">Qty<input name="qty" type="number" min="1" value="1"></label>
      <label class="f">Status<select name="status"><option value="considering">Considering</option><option value="ready">Ready to buy</option><option value="ordered">Ordered</option></select></label>
      <label class="f">Est. price each ($)<input name="est_price" inputmode="decimal" placeholder="45.50"></label>
      <label class="f">Vendor<input name="vendor" placeholder="RockAuto"></label><label class="f">Link<input name="link" type="url" placeholder="https://…"></label>
      <label class="f">For job<select name="job_id"><option value="">—</option>${jobs.jobs.map((j) => `<option value="${esc(j.job_id)}">${esc(j.job_id)} ${esc(j.title)}</option>`).join("")}</select></label>
      <label class="f">Category<input name="category" value="parts"></label></div>
      ${partPicker("part_id", "", "Inventory part (optional)")}
      <label class="f">Notes<input name="notes"></label><div><button class="btn big primary">Add to list</button></div></form></details>
    <div class="shop-cols">${col("considering")}${col("ready")}${col("ordered")}</div>
    <h2>Received (${s.by_status.received.length})</h2>${s.by_status.received.map(item).join("") || `<div class="empty">Nothing received yet.</div>`}
    ${s.by_status.cancelled.length ? `<details><summary>Cancelled (${s.by_status.cancelled.length})</summary>${s.by_status.cancelled.map(item).join("")}</details>` : ""}`;
};

/* --------------------------------------------------------------- Money charts
   Inline SVG (works offline). One data hue (validated #2a63c2); the budget is a dashed reference line in ink, not a
   second series. Only ACTUAL spending is plotted; outstanding orders / possible purchases are stated separately. */
const CHART_BLUE = "#2a63c2", CHART_INK = "#333", CHART_MUTED = "#6b6b6b", CHART_GRID = "#e3e5df";
function niceMax(v) {  // axis top: 10% headroom, rounded up to a round number
  if (v <= 0) return 100;
  const raw = v * 1.1, p = 10 ** Math.floor(Math.log10(raw));
  return Math.ceil(raw / p) * p;
}
function spendChart(m) {
  const pts = m.timeline || [];
  if (!pts.length) return `<div class="empty">No spending recorded yet — the graph starts with your first expense.</div>`;
  const W = 820, H = 280, L = 78, R = 24, T = 22, B = 40;
  const day = (d) => Date.parse(d + "T12:00:00");
  let x0 = day(pts[0].date), x1 = day(pts[pts.length - 1].date);
  const today = Date.parse(new Date().toISOString().slice(0, 10) + "T12:00:00");
  if (today > x1) x1 = today;
  if (x1 - x0 < 7 * 864e5) x0 = x1 - 7 * 864e5;
  const spentMax = Math.max(...pts.map((p) => p.cumulative));
  // Keep the budget line only when it doesn't flatten the spending line into the axis (< 25 % used -> stated in text)
  const showBudget = m.budget != null && spentMax >= 0.25 * m.budget;
  const top = niceMax(Math.max(showBudget ? m.budget : 0, spentMax));
  const X = (t) => L + (t - x0) / (x1 - x0) * (W - L - R), Y = (v) => T + (1 - Math.max(0, v) / top) * (H - T - B);
  // step line: the running total jumps on the day money is spent
  let d = `M${X(x0).toFixed(1)},${Y(0).toFixed(1)}`;
  for (const p of pts) { const x = X(day(p.date)).toFixed(1); d += ` H${x} V${Y(p.cumulative).toFixed(1)}`; }
  d += ` H${X(x1).toFixed(1)}`;
  const area = d + ` V${Y(0).toFixed(1)} Z`;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * top);
  const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="${CHART_GRID}"/>
    <text x="${L - 8}" y="${Y(v) + 5}" text-anchor="end" font-size="15" fill="${CHART_MUTED}">${esc(money$(v).replace(".00", ""))}</text>`).join("");
  const fmtD = (t) => new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const xt = [x0, x0 + (x1 - x0) / 2, x1].map((t, i) => `<text x="${X(t)}" y="${H - 12}" font-size="15" fill="${CHART_MUTED}" text-anchor="${["start", "middle", "end"][i]}">${fmtD(t)}</text>`).join("");
  const budget = showBudget ? `<line x1="${L}" x2="${W - R}" y1="${Y(m.budget)}" y2="${Y(m.budget)}" stroke="${CHART_INK}" stroke-width="2" stroke-dasharray="7 5"/>
    <text x="${W - R}" y="${Y(m.budget) - 7}" text-anchor="end" font-size="16" font-weight="700" fill="${CHART_INK}">Budget ${esc(money$(m.budget))}</text>` : "";
  const dots = pts.map((p) => {
    const cx = X(day(p.date)), cy = Y(p.cumulative);
    const tip = `${p.date}: ${p.net < 0 ? "refund " : "spent "}${money$(Math.abs(p.net))} · total ${money$(p.cumulative)}`;
    return `<circle cx="${cx}" cy="${cy}" r="5" fill="${CHART_BLUE}" stroke="#fff" stroke-width="2"/>
      <circle cx="${cx}" cy="${cy}" r="14" fill="transparent" data-tip="${esc(tip)}"><title>${esc(tip)}</title></circle>`;
  }).join("");
  const last = pts[pts.length - 1];
  const over = m.budget != null && last.cumulative > m.budget;
  return `<div class="chart" role="img" aria-label="Actual spending over time${m.budget != null ? " against the budget" : ""}">
    <svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet">${grid}
      <path d="${area}" fill="${CHART_BLUE}" fill-opacity=".10"/><path d="${d}" fill="none" stroke="${CHART_BLUE}" stroke-width="2.5" stroke-linejoin="round"/>
      ${budget}${dots}
      <text x="${W - R}" y="${Y(last.cumulative) + 22}" font-size="16" font-weight="700" fill="${CHART_INK}" text-anchor="end">${esc(money$(last.cumulative))} spent</text>
      ${xt}</svg>
    <div class="chart-note">${over ? `<b>⚠ Over budget by ${esc(money$(last.cumulative - m.budget))}.</b> ` : ""}
      ${m.budget != null && !showBudget ? `Budget ${esc(money$(m.budget))} is above this scale — ${Math.round(100 * last.cumulative / m.budget)} % used so far. ` : ""}
      Line = money actually spent (expenses − refunds). <b>Not in the line:</b> outstanding orders ${esc(money$(m.outstanding_orders_total))}
      · possible purchases ${esc(money$(m.possible_purchases_total))}.</div></div>`;
}
function categoryChart(m) {
  const rows = (m.by_category || []).filter((c) => c.spent > 0 || (m.category_budgets || []).some((b) => b.category === c.category));
  const budgets = Object.fromEntries((m.category_budgets || []).map((b) => [b.category, b.budget]));
  for (const b of m.category_budgets || []) if (!rows.some((r) => r.category === b.category)) rows.push({ category: b.category, spent: 0 });
  if (!rows.length) return `<div class="empty">No spending by category yet.</div>`;
  const top = niceMax(Math.max(...rows.map((r) => Math.max(r.spent, budgets[r.category] || 0))));
  const W = 820, L = 170, R = 130, rowH = 36;
  const H = rows.length * rowH + 10;
  const Xw = (v) => Math.max(0, v) / top * (W - L - R);
  const bars = rows.map((r, i) => {
    const y = 6 + i * rowH, w = Xw(r.spent), b = budgets[r.category];
    const tip = `${r.category}: ${money$(r.spent)} spent${b != null ? ` of ${money$(b)} budget` : ""}`;
    return `<text x="${L - 10}" y="${y + 18}" text-anchor="end" font-size="16" fill="${CHART_INK}">${esc(r.category)}</text>
      ${w > 0 ? `<path d="M${L},${y + 4} h${Math.max(w - 4, 0)} a4,4 0 0 1 4,4 v10 a4,4 0 0 1 -4,4 h-${Math.max(w - 4, 0)} Z" fill="${CHART_BLUE}"/>` : ""}
      ${b != null ? `<line x1="${L + Xw(b)}" x2="${L + Xw(b)}" y1="${y}" y2="${y + 26}" stroke="${CHART_INK}" stroke-width="3"/>
        ${Xw(b) - w > 110 ? `<text x="${L + Xw(b) + 7}" y="${y + 18}" font-size="14" fill="${CHART_MUTED}">budget ${esc(money$(b).replace(".00", ""))}</text>` : ""}` : ""}
      <text x="${L + w + 8}" y="${y + 18}" font-size="16" font-weight="700" fill="${CHART_INK}">${esc(money$(r.spent))}${b != null && r.spent > b ? " ⚠ over budget" : ""}</text>
      <rect x="0" y="${y}" width="${W}" height="${rowH - 4}" fill="transparent" data-tip="${esc(tip)}"><title>${esc(tip)}</title></rect>`;
  }).join("");
  return `<div class="chart" role="img" aria-label="Actual spending by category">
    <svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMinYMin meet">${bars}</svg>
    <div class="chart-note">Bars = actual spending per category${Object.keys(budgets).length ? "; ▮ dark tick = that category's budget" : ""}. Set category budgets below.</div></div>`;
}
document.addEventListener("mouseover", (ev) => {
  const t = ev.target.closest && ev.target.closest("[data-tip]");
  let tip = $("#chartTip");
  if (!t) { if (tip) tip.hidden = true; return; }
  if (!tip) { tip = document.createElement("div"); tip.id = "chartTip"; tip.className = "chart-tip"; document.body.appendChild(tip); }
  tip.textContent = t.dataset.tip; tip.hidden = false;
  const r = t.getBoundingClientRect();
  tip.style.left = `${Math.min(window.innerWidth - 300, r.left + r.width / 2 + 10)}px`; tip.style.top = `${r.top + window.scrollY - 40}px`;
});

/* --------------------------------------------------------------- Money */
VIEWS.money = async () => {
  const vid = activeVid();
  if (!vid) return noVehicle();
  const [m, jobs] = await Promise.all([api(`/api/money?vehicle=${vid}`), api(`/api/jobs?vehicle=${vid}&closed=1`)]);
  const used = m.budget ? Math.min(100, Math.round(100 * m.spent / m.budget)) : 0;
  const table = (rows, key, label) => rows.length ? `<table><tr><th>${label}</th><th>Spent</th></tr>${rows.map((r) => `<tr><td>${key === "job_id" && r.job_id !== "(none)" ? jlink(r.job_id, r.title) : esc(r[key])}</td><td class="num">${money$(r.spent)}</td></tr>`).join("")}</table>` : `<div class="empty">—</div>`;
  return `${vehicleStrip(m.vehicle)}<h1>Money</h1>
    <div class="totals">
      <div class="total spent"><div class="k">Actually spent</div><div class="v">${money$(m.spent)}</div><div class="small muted">expenses − refunds (${money$(m.refunds_total)} refunded)</div></div>
      <div class="total ${m.remaining != null && m.remaining < 0 ? "over" : "left"}"><div class="k">Budget left</div><div class="v">${m.budget == null ? "no budget" : money$(m.remaining)}</div>
        ${m.budget != null ? `<div class="bar ${m.remaining < 0 ? "over" : ""}"><span style="width:${used}%"></span></div><div class="small muted">of ${money$(m.budget)}</div>` : ""}</div>
      <div class="total out"><div class="k">Outstanding orders</div><div class="v">${money$(m.outstanding_orders_total)}</div><div class="small muted">ordered, no payment recorded (estimate)</div></div>
      <div class="total maybe"><div class="k">Possible purchases</div><div class="v">${money$(m.possible_purchases_total)}</div><div class="small muted">considering / ready (estimate)</div></div></div>
    <p class="small muted">${esc(m.explain)}</p>
    <div class="card chart-card"><h3>Spending over time</h3>${spendChart(m)}</div>
    <div class="card chart-card"><h3>Spending by category</h3>${categoryChart(m)}</div>
    <div class="row" style="margin:8px 0"><button class="btn" data-act="moneyExport">Save as spreadsheet (CSV)</button></div>
    ${m.received_without_payment ? `<div class="box warn"><b>Received but no payment recorded:</b> ${m.received_without_payment.map((i) => `${esc(i.item_id)} ${esc(i.name)}`).join(", ")}</div>` : ""}
    <div class="grid2"><form class="card stack" data-submit="expense"><h3>Record spending or a refund</h3><input type="hidden" name="request_id" value="${rid("exp")}">
      <div class="grid2"><label class="f">Amount ($)<input name="amount" inputmode="decimal" required placeholder="42.50"></label>
      <label class="f">Type<select name="kind"><option value="expense">Spent</option><option value="refund">Refund (money back)</option></select></label>
      <label class="f">Date<input name="date" type="date" value="${new Date().toISOString().slice(0, 10)}"></label>
      <label class="f">Vendor<input name="vendor" placeholder="Harbor Freight"></label>
      <label class="f">Category<select name="category">${m.categories.map((c) => `<option>${esc(c)}</option>`).join("")}</select></label>
      <label class="f">Job<select name="job_id"><option value="">—</option>${jobs.jobs.map((j) => `<option value="${esc(j.job_id)}">${esc(j.job_id)} ${esc(j.title)}</option>`).join("")}</select></label></div>
      <label class="f">What for<input name="description"></label>
      <label class="f">Receipt (photo or PDF)<input type="file" name="receipt" accept="image/*,application/pdf"></label>
      <div><button class="btn big primary">Save</button></div></form>
    <form class="card stack" data-submit="budget"><h3>Budget</h3><input type="hidden" name="request_id" value="${rid("budget")}">
      <div class="grid2"><label class="f">Amount ($)<input name="amount" inputmode="decimal" required value="${esc(m.budget ?? "")}"></label>
      <label class="f">For<select name="category"><option value="">Whole project</option>${m.categories.map((c) => `<option>${esc(c)}</option>`).join("")}</select></label></div>
      <div><button class="btn primary">Save budget</button></div>
      ${m.category_budgets.length ? `<table><tr><th>Category</th><th>Budget</th><th>Spent</th><th>Left</th></tr>${m.category_budgets.map((c) => `<tr><td>${esc(c.category)}</td><td>${money$(c.budget)}</td><td>${money$(c.spent)}</td><td class="${c.remaining < 0 ? "b-failed" : ""}">${money$(c.remaining)}</td></tr>`).join("")}</table>` : ""}</form></div>
    <div class="grid2"><div><h2>By category</h2>${table(m.by_category, "category", "Category")}</div><div><h2>By job</h2>${table(m.by_job, "job_id", "Job")}</div><div><h2>By vendor</h2>${table(m.by_vendor, "vendor", "Vendor")}</div></div>
    ${m.outstanding_orders.length ? `<h2>Outstanding orders</h2><ul>${m.outstanding_orders.map((i) => `<li>${esc(i.item_id)} ${esc(i.qty)} × ${esc(i.name)} ${esc(i.vendor || "")} ~${money$(i.est_total)} <button class="btn sm" data-act="shopPaid" data-arg="${esc(i.item_id)}">Record payment…</button></li>`).join("")}</ul>` : ""}
    <h2>Expenses &amp; refunds (${m.expenses.length})</h2>
    <div class="tablewrap"><table><tr><th>Date</th><th>What</th><th>Vendor</th><th>Category</th><th>Linked</th><th>Amount</th><th>Receipt</th><th></th></tr>
    ${m.expenses.map((e) => `<tr class="${e.voided ? "voided" : ""}"><td>${esc(e.date)}</td><td>${esc(e.description || "")}<div class="small muted">${esc(e.expense_id)}${e.voided ? ` · <b>voided</b> ${esc(e.voided)}` : ""}</div></td><td>${esc(e.vendor || "")}</td><td>${esc(e.category || "")}</td>
      <td class="small">${e.job_id ? jlink(e.job_id) : ""} ${e.item_id ? esc(e.item_id) : ""} ${e.part_id ? plink(e.part_id) : ""}</td>
      <td class="num">${e.kind === "refund" ? `<span style="color:var(--ok)">−${money$(e.amount)}</span>` : money$(e.amount)}</td>
      <td>${e.receipt_link ? `<a href="${esc(photoSrc(e.receipt_link))}" target="_blank">view</a>` : ""}</td>
      <td>${e.voided ? "" : `<button class="btn sm" data-act="voidExpense" data-arg="${esc(e.expense_id)}" data-what="${esc(`${money$(e.amount)} ${e.vendor || ""} ${e.date}`)}">Void…</button>`}</td></tr>`).join("") || `<tr><td colspan=8>Nothing recorded yet.</td></tr>`}</table></div>
    <p class="small muted">Entered something by mistake? <b>Void</b> keeps it on record with the reason but stops counting it. To fix an amount, void it and record the right one.</p>`;
};

/* ------------------------------------------------------------- dialogs */
const dlg = $("#dlg");
function openDialog(title, body) {
  dlg.innerHTML = `<div class="dlg-head"><span>${title}</span><button class="btn sm" data-act="closeDlg">Close</button></div><div class="dlg-body">${body}</div>`;
  if (!dlg.open) dlg.showModal();
}
function closeDialog() { if (dlg.open) dlg.close(); }

async function jobForm(job) {
  const vid = job ? job.vehicle_id : activeVid();
  const { jobs } = await api(`/api/jobs?vehicle=${vid}`);
  const systems = ["body", "brakes", "cooling", "drivetrain", "electrical", "engine", "exhaust", "fuel", "interior", "steering", "suspension", "trim", "wheels & tires", "other"];
  const f = job || {};
  openDialog(job ? `Edit ${esc(job.job_id)}` : "New rebuild job", `${vehicleStrip(S.status.vehicles.find((v) => v.vehicle_id === vid)?.label || vid)}
    <form class="stack" data-submit="${job ? "editJob" : "createJob"}"><input type="hidden" name="request_id" value="${rid("job")}"><input type="hidden" name="vehicle_id" value="${esc(vid)}">
      <label class="f">Title<input name="title" required value="${esc(f.title || "")}" placeholder="Rebuild front brake calipers"></label>
      <div class="grid2"><label class="f">System<select name="system">${systems.map((s) => `<option ${f.system === s ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      <label class="f">Kind<select name="kind">${["assembly", "teardown", "repair", "inspection", "other"].map((s) => `<option ${f.kind === s ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      <label class="f">Priority<select name="priority">${[1, 2, 3, 4, 5].map((p) => `<option value="${p}" ${(f.priority || 3) === p ? "selected" : ""}>${p}${p === 1 ? " (highest)" : p === 5 ? " (lowest)" : ""}</option>`).join("")}</select></label>
      <label class="f">Your time estimate (min)<input name="estimate_min" type="number" min="1" value="${esc(f.estimate_min || "")}"></label></div>
      <label class="f">Goal<input name="goal" value="${esc(f.goal || "")}" placeholder="What does done look like?"></label>
      ${job ? "" : `<label class="f">Checklist<span class="hint">One step per line</span><textarea name="steps"></textarea></label>
      <label class="f">Must happen first<select name="depends_on" multiple size="5">${jobs.map((j) => `<option value="${esc(j.job_id)}">${esc(j.job_id)} ${esc(j.title)}</option>`).join("")}</select><span class="hint">Ctrl+click to choose several</span></label>`}
      <label class="f">Notes<textarea name="notes">${esc(f.notes || "")}</textarea></label>
      <div><button class="btn big primary">${job ? "Save changes" : "Create job"}</button></div></form>`);
}

async function installDialog(needId) {
  const j = S.job;
  const n = j.parts_needed.find((x) => x.need_id === needId);
  const opts = n.pull_from.map((p, i) => `<label><input type="radio" name="stock_id" value="${esc(p.stock_id)}" ${i === 0 ? "checked" : ""}> Bin <b>${esc(p.bin_code)}</b> — ${esc(p.qty)} ${esc(p.condition)}</label>`).join("");
  openDialog("Record installation", `${vehicleStrip(j.vehicle)}
    <p style="font-size:20px">Installing <b>${esc(n.name)}</b> (${esc(n.part_id)}) for ${jlink(j.job_id, j.title)}</p>
    ${n.check !== "none" && n.check_status !== "passed" ? `<div class="box warn">This part still needs: ${esc(n.check.replace("_", " "))}. Record the check result first if you haven't.</div>` : ""}
    <form class="stack" data-submit="install"><input type="hidden" name="request_id" value="${rid("install")}"><input type="hidden" name="need_id" value="${esc(n.need_id)}">
      <div class="f">Taken from<div class="radio-list">${opts}</div></div>
      <label class="f">Quantity<input name="qty" type="number" min="1" max="${esc(Math.min(n.qty_remaining, n.on_hand))}" value="${esc(Math.min(n.qty_remaining, n.on_hand))}"></label>
      <label class="f">Notes<input name="notes" placeholder="optional"></label>
      <div><button class="btn big go">Confirm: installed on ${esc(j.vehicle.split(" ").slice(1).join(" "))}</button></div>
      <p class="small muted">Updates the bin stock, this vehicle's installation history and the job together.</p></form>`);
}

async function receiveDialog(partId) {
  const { bins } = await api("/api/bins");
  openDialog("Receive stock", `<form class="stack" data-submit="receive"><input type="hidden" name="request_id" value="${rid("receive")}">
    ${partPicker("part_id", partId || "", "Existing part — search, or scan its label")}
    ${partId ? "" : `<fieldset class="card" style="border:2px dashed var(--line)"><legend><b>…or a new part</b> (leave the part above empty)</legend>
      <div class="grid2"><label class="f">Name<input name="name" placeholder="Door hinge, upper"></label><label class="f">Side / position<input name="side_position" placeholder="left front"></label></div></fieldset>`}
    <div class="grid2"><label class="f">Bin<select name="bin_code">${bins.map((b) => `<option>${esc(b.bin_code)}</option>`).join("")}</select></label>
    <label class="f">Qty<input name="qty" type="number" min="1" value="1"></label>
    <label class="f">Condition<select name="condition"><option>used</option><option>new</option><option>remanufactured</option><option>core</option><option>damaged</option></select></label></div>
    <label class="row"><input type="checkbox" name="print_label" checked> Print label after saving</label>
    <div><button class="btn big primary">Save</button></div></form>`);
}

async function removalDialog() {
  const vid = activeVid();
  const [{ bins }, { jobs }] = await Promise.all([api("/api/bins"), api(`/api/jobs?vehicle=${vid}`)]);
  const jobOpts = (sel) => `<option value="">—</option>` + jobs.map((j) => `<option value="${esc(j.job_id)}" ${sel(j) ? "selected" : ""}>${esc(j.job_id)} ${esc(j.title)}</option>`).join("");
  openDialog("Record removal (teardown)", `${vehicleStrip(S.status.active_vehicle.label)}
    <form class="stack" data-submit="removal"><input type="hidden" name="request_id" value="${rid("removal")}">
      <div class="grid2"><label class="f">Existing part ID<input name="part_id" placeholder="optional"></label>
      <label class="f">…or name of the part<input name="name" placeholder="Door hinge bolt"></label>
      <label class="f">Removed from<input name="removed_from" placeholder="left door, upper hinge" required></label>
      <label class="f">Qty<input name="qty" type="number" min="1" value="1"></label>
      <label class="f">Bag / bin<select name="bin_code">${bins.map((b) => `<option>${esc(b.bin_code)}</option>`).join("")}</select></label>
      <label class="f">Condition<select name="condition"><option>used</option><option>damaged</option><option>core</option></select></label>
      <label class="f">Teardown job<select name="job_id">${jobOpts((j) => j.kind === "teardown")}</select></label>
      <label class="f">Goes back in job<select name="reinstall_job_id">${jobOpts(() => false)}</select></label></div>
      <label class="f">Photo of it in place<input type="file" name="file" accept="image/*"></label>
      <label class="row"><input type="checkbox" name="print_label" checked> Print a label for the bag/bin</label>
      <div><button class="btn big primary">Save removal</button></div></form>`);
}

async function pairDialog() {
  try {
    const p = await api("/api/pair", {});
    openDialog("Pair your phone", `<p>Phone and laptop must be on the <b>same home Wi-Fi</b>.</p>
      <div class="qr"><figure><img src="${p.setup_qr}" alt="setup QR"><figcaption><b>1. First time only:</b> install the certificate<br><code>${esc(p.setup_url)}</code></figcaption></figure>
      <figure><img src="${p.pair_qr}" alt="pair QR"><figcaption><b>2. Pair:</b> scan with the phone camera<br><span class="small">Code works once, for ${Math.round(p.expires_in_s / 60)} minutes</span></figcaption></figure></div>
      <p class="small muted">Laptop address: ${esc(p.ips.join(", ") || "not on a network")} · If the phone can't connect, allow Torino through Windows Firewall on <b>Private</b> networks.</p>
      ${p.ca_fingerprint ? `<p class="small muted">Certificate fingerprint: <code style="word-break:break-all">${esc(p.ca_fingerprint)}</code></p>` : ""}`);
  } catch (e) { showError(e); }
}

/* -------------------------------------------------------------- actions */
const ACT = {
  closeDlg: closeDialog,
  pair: pairDialog,
  newJob: () => jobForm(null),
  editJob: () => jobForm(S.job),
  removal: removalDialog,
  receive: (el) => receiveDialog(el.dataset.arg),
  minutes: (el) => { S.minutes = el.dataset.arg ? +el.dataset.arg : null; render(); },
  activate: async (el) => { await api("/api/active-vehicle", { vehicle_id: el.dataset.arg }); await refreshStatus(); render(); },
  jobStatus: async (el) => {
    const r = await api(`/api/job/${S.job.job_id}`, { request_id: rid("status"), status: el.dataset.arg });
    toast(`${esc(S.job.job_id)} → ${esc(STATE_LABEL[el.dataset.arg] || el.dataset.arg)}${r.warnings.length ? "<br>⚠ " + r.warnings.map(esc).join("<br>⚠ ") : ""}`, r.warnings.length ? "" : "ok", 8000);
    render();
  },
  hold: () => openDialog("Put job on hold", `<form class="stack" data-submit="hold"><input type="hidden" name="request_id" value="${rid("hold")}">
      <label class="f">Why? What are you waiting for?<input name="hold_reason" required></label><div><button class="btn big primary">Put on hold</button></div></form>`),
  step: async (el) => {
    const done = el.dataset.done === "1";
    await api(`/api/job/${S.job.job_id}`, { request_id: rid("step"), [done ? "steps_undone" : "steps_done"]: [el.dataset.arg] });
    render();
  },
  install: (el) => installDialog(el.dataset.arg),
  check: async (el) => {
    await api(`/api/need/${el.dataset.arg}`, { request_id: rid("check"), check_status: el.dataset.v });
    toast(`Check recorded: ${esc(el.dataset.v)}`, el.dataset.v === "passed" ? "ok" : "bad"); render();
  },
  linkPart: (el) => openDialog("Link an inventory part", `<form class="stack" data-submit="linkPart"><input type="hidden" name="request_id" value="${rid("link")}"><input type="hidden" name="need_id" value="${esc(el.dataset.arg)}">
      ${partPicker("part_id", "", "Part — search or scan")}<div><button class="btn big primary">Link</button></div></form>`),
  pickPart: (el) => pickSet(el.closest(".picker"), el.dataset.arg, el.dataset.name),
  pickClear: (el) => { const pk = el.closest(".picker"); $("input[type=hidden]", pk).value = ""; $(".pick-chosen", pk).innerHTML = ""; $(".pick-q", pk).focus(); },
  sendOut: (el) => {
    const p = S.part;
    openDialog(`Send out: ${esc(p.label_name)}`, `<form class="stack" data-submit="sendOut"><input type="hidden" name="request_id" value="${rid("out")}">
      <input type="hidden" name="part_id" value="${esc(p.part_id)}"><input type="hidden" name="stock_id" value="${esc(el.dataset.arg)}">
      <p>From <b>${esc(el.dataset.bin)}</b> (${esc(el.dataset.qty)} there). It leaves the shop; its condition and fitment stay as they are.</p>
      <div class="grid2"><label class="f">Who has it<input name="holder" required placeholder="Smith Machine Shop"></label>
      <label class="f">Qty<input name="qty" type="number" min="1" max="${esc(el.dataset.qty)}" value="${esc(el.dataset.qty)}"></label>
      <label class="f">Why<input name="reason" placeholder="rebuild, plating, measure…"></label>
      <label class="f">Expected back<input name="expected_back" type="date"></label></div>
      <label class="f">Notes<input name="notes"></label><div><button class="btn big primary">Record: sent out</button></div></form>`);
  },
  returnPart: async (el) => {
    const { bins } = await api("/api/bins");
    openDialog(`Back from ${esc(el.dataset.holder)}`, `<form class="stack" data-submit="returnPart"><input type="hidden" name="request_id" value="${rid("ret")}">
      <input type="hidden" name="custody_id" value="${esc(el.dataset.arg)}"><p>${esc(el.dataset.qty)} still at <b>${esc(el.dataset.holder)}</b> (${esc(el.dataset.arg)}).</p>
      <div class="grid2"><label class="f">How many came back<input name="qty" type="number" min="1" max="${esc(el.dataset.qty)}" value="${esc(el.dataset.qty)}"></label>
      <label class="f">Into<select name="to_bin">${bins.map((b) => `<option ${b.bin_code === el.dataset.bin ? "selected" : ""}>${esc(b.bin_code)}</option>`).join("")}</select></label>
      <label class="f">Condition now<select name="condition"><option value="">unchanged</option><option>remanufactured</option><option>new</option><option>used</option><option>damaged</option></select></label></div>
      <label class="f">Notes<input name="notes" placeholder="e.g. honed +.010"></label><div><button class="btn big go">Record return</button></div></form>`);
  },
  needToShop: (el) => {
    const n = S.job.parts_needed.find((x) => x.need_id === el.dataset.arg);
    const want = Math.max(1, n.qty_remaining - n.on_hand);
    openDialog("Add to shopping list", `${vehicleStrip(S.job.vehicle)}<form class="stack" data-submit="addItem"><input type="hidden" name="request_id" value="${rid("shop")}">
      <input type="hidden" name="job_id" value="${esc(S.job.job_id)}"><input type="hidden" name="need_id" value="${esc(n.need_id)}"><input type="hidden" name="part_id" value="${esc(n.part_id || "")}">
      <div class="grid2"><label class="f">What<input name="name" required value="${esc(n.name)}"></label><label class="f">Qty<input name="qty" type="number" min="1" value="${want}"></label>
      <label class="f">Status<select name="status"><option value="considering">Considering</option><option value="ready">Ready to buy</option><option value="ordered">Ordered</option></select></label>
      <label class="f">Est. price each ($)<input name="est_price" inputmode="decimal"></label><label class="f">Vendor<input name="vendor"></label><label class="f">Link<input name="link" type="url"></label></div>
      <div><button class="btn big primary">Add</button></div></form>`);
  },
  shopStatus: async (el) => { await api(`/api/shopping/${el.dataset.arg}`, { request_id: rid("shopst"), status: el.dataset.v }); toast(`${esc(el.dataset.arg)} → ${esc(SHOP_LABEL[el.dataset.v])}`, "ok"); render(); },
  shopOrder: (el) => openDialog(`Ordered ${esc(el.dataset.arg)}`, `<form class="stack" data-submit="shopOrder"><input type="hidden" name="request_id" value="${rid("order")}"><input type="hidden" name="item_id" value="${esc(el.dataset.arg)}">
      <p class="small">You ordered it outside Torino. Record it here.</p>
      <div class="grid2"><label class="f">Order / confirmation #<input name="order_ref"></label><label class="f">Vendor<input name="vendor" placeholder="(keep)"></label>
      <label class="f">Paid, total ($)<input name="paid_amount" inputmode="decimal" placeholder="leave empty if not paid yet"></label><label class="f">Paid on<input name="paid_date" type="date" value="${new Date().toISOString().slice(0, 10)}"></label></div>
      <div><button class="btn big primary">Save: ordered</button></div></form>`),
  shopPaid: (el) => openDialog(`Payment for ${esc(el.dataset.arg)}`, `<form class="stack" data-submit="shopOrder"><input type="hidden" name="request_id" value="${rid("paid")}"><input type="hidden" name="item_id" value="${esc(el.dataset.arg)}">
      <div class="grid2"><label class="f">Paid, total ($)<input name="paid_amount" inputmode="decimal" required></label><label class="f">Paid on<input name="paid_date" type="date" value="${new Date().toISOString().slice(0, 10)}"></label></div>
      <p class="small muted">Recorded once as an expense linked to this item; receiving the delivery later does not count it again.</p>
      <div><button class="btn big primary">Save payment</button></div></form>`),
  shopReceive: async (el) => {
    const [{ bins }, s] = await Promise.all([api("/api/bins"), api(`/api/shopping?vehicle=${activeVid()}`)]);
    const it = s.items.find((x) => x.item_id === el.dataset.arg);
    const left = it.qty - it.qty_received;
    openDialog(`Delivery: ${esc(it.name)}`, `<form class="stack" data-submit="shopReceive"><input type="hidden" name="request_id" value="${rid("rcv")}"><input type="hidden" name="item_id" value="${esc(it.item_id)}">
      <p>${esc(it.qty_received)} of ${esc(it.qty)} received so far. Enter what arrived now — fewer is fine (partial delivery).</p>
      <div class="grid2"><label class="f">Arrived now<input name="qty" type="number" min="1" max="${left}" value="${left}"></label>
      <label class="f">Into bin<select name="bin_code">${bins.map((b) => `<option>${esc(b.bin_code)}</option>`).join("")}</select></label>
      <label class="f">Condition<select name="condition"><option>new</option><option>remanufactured</option><option>used</option></select></label></div>
      ${it.part_id ? `<p>Goes to part ${plink(it.part_id)}.</p>` : partPicker("part_id", "", "Existing part (leave empty to create one named after the item)")}
      <label class="row"><input type="checkbox" name="print_label" checked> Print its label</label>
      <div><button class="btn big go">Save delivery</button></div></form>`);
  },
  voidExpense: (el) => openDialog(`Void ${esc(el.dataset.arg)}`, `<form class="stack" data-submit="voidExpense"><input type="hidden" name="request_id" value="${rid("void")}">
      <input type="hidden" name="expense_id" value="${esc(el.dataset.arg)}"><p>${esc(el.dataset.what)}</p>
      <label class="f">Why?<input name="reason" required placeholder="entered twice / wrong amount"></label>
      <div><button class="btn big danger">Void it (keep on record, stop counting)</button></div></form>`),
  moneyExport: async () => { const r = await api("/api/money/export", { vehicle_id: activeVid() }); toast(`Saved: ${esc(r.path)}`, "ok", 10000); },
  backupNow: async () => { const r = await api("/api/data/backup", {}); toast(`Backup saved: ${esc(r.name)} (${esc(r.records)} records, ${esc(r.photo_files)} photos)`, "ok", 8000); render(); },
  pickBackupFolder: async () => {
    const f = await pickPath("folder");
    if (f === undefined) return;
    await api("/api/data/backup-folder", { folder: f || "" }); toast("Backup folder saved", "ok"); render();
  },
  restorePick: async () => { const f = await pickPath("backup"); if (f) await restorePreview(f); },
  restorePreview: (el) => restorePreview(el.dataset.arg),
  restoreConfirm: async (el) => {
    const r = await api("/api/data/restore", { path: el.dataset.arg, confirm: true });
    closeDialog(); toast(`Restored ${esc(r.backup.records)} records (${esc(r.photos_copied)} photo files copied). The previous data was saved first: ${esc(r.safety_backup || "")}`, "ok", 12000);
    S.version = null; await refreshStatus(); render();
  },
  transferPreview: async (el) => {
    const p = await api("/api/data/transfer", { direction: el.dataset.arg });
    const rows = Object.keys(p.source_counts).filter((k) => p.source_counts[k] || p.target_counts[k])
      .map((k) => `<tr><td>${esc(k)}</td><td class="num">${esc(p.source_counts[k])}</td><td class="num">${esc(p.target_counts[k])}</td></tr>`).join("");
    openDialog(`Transfer ${esc(p.from)} → ${esc(p.to)}: preview`, `<p><b>${esc(p.records)}</b> records and <b>${esc(p.photo_files_to_copy)}</b> photo files would be copied. ${esc(p.keeps)}</p>
      <table><tr><th>Records</th><th>In ${esc(p.from)}</th><th>Already in ${esc(p.to)}</th></tr>${rows}</table>
      ${p.problems.length ? `<div class="box bad">${p.problems.map(esc).join("<br>")}</div>` : `<div class="box info">${esc(p.from)} is not changed. Afterwards choose which source to use.</div>`}
      <div class="row">${p.ok ? `<button class="btn big primary" data-act="transferConfirm" data-arg="${esc(el.dataset.arg)}">Copy now</button>` : ""}<button class="btn big" data-act="closeDlg">Close</button></div>`);
  },
  transferConfirm: async (el) => {
    toast("Copying… this can take a while with many photos.", "", 6000);
    const r = await api("/api/data/transfer", { direction: el.dataset.arg, confirm: true });
    closeDialog(); toast(`Copied ${esc(r.records)} records and ${esc(r.photos_copied)} photos to ${esc(r.to)} (same IDs, checked).${r.warnings.length ? "<br>⚠ " + r.warnings.map(esc).join("<br>⚠ ") : ""}`, "ok", 12000); render();
  },
  pickTemplate: async (el) => {
    const f = await pickPath("template");
    if (f) { el.closest("form").querySelector("[name=template]").value = f; toast("Template chosen — click Save printer settings", "", 4000); }
  },
  useQueue: async (el) => { await api("/api/printer", { queue: el.dataset.arg }); toast(`Using printer '${esc(el.dataset.arg)}'`, "ok"); await refreshStatus(); render(); },
  goLive: async () => { await api("/api/printer", { mode: "live" }); toast("Live printing on", "ok"); await refreshStatus(); render(); },
  rmDep: async (el) => { await api(`/api/job/${S.job.job_id}`, { request_id: rid("rmdep"), remove_depends_on: [el.dataset.arg] }); render(); },
  labelPart: async (el) => {
    const r = await api("/api/labels/part", { request_id: rid("label"), part_id: el.dataset.arg, bin_code: el.dataset.bin });
    labelToast(r.label);
  },
  labelBin: async (el) => { const r = await api("/api/labels/bin", { request_id: rid("binlabel"), bin_code: el.dataset.arg }); labelToast(r.label); },
  testLabel: async () => { const r = await api("/api/printer/test", {}); labelToast(r.job); render(); refreshStatus(); },
  makeTemplate: async (el) => {
    const tape = el.closest("form")?.querySelector("[name=tape_mm]")?.value || el.dataset.arg || S.status?.printer?.tape_mm;
    const r = await api("/api/printer/template", { tape_mm: tape });
    toast(`Template for ${r.tape_mm} mm tape saved: ${r.template}${r.bpac_ok === false ? " (b-PAC check failed — see Labels page)" : ""}`);
    render(); refreshStatus();
  },
  processQueue: async () => { const r = await api("/api/labels/process", {}); toast(`${r.processed.length} label job(s) processed`); render(); },
  resolve: async (el) => { const r = await api(`/api/labels/${el.dataset.arg}/resolve`, { action: el.dataset.v }); labelToast(r.job); render(); refreshStatus(); },
  move: (el) => openDialog("Move stock", `<form class="stack" data-submit="move"><input type="hidden" name="request_id" value="${rid("move")}">
      <input type="hidden" name="stock_id" value="${esc(el.dataset.arg)}"><input type="hidden" name="from_bin" value="${esc(el.dataset.bin)}">
      <p>From bin <b>${esc(el.dataset.bin)}</b> (${esc(el.dataset.qty)} there)</p>
      <div class="grid2"><label class="f">To bin<input name="to_bin" required placeholder="B-04"></label><label class="f">Qty<input name="qty" type="number" min="1" max="${esc(el.dataset.qty)}" value="1"></label></div>
      <label class="row"><input type="checkbox" name="print_label"> Print a label for the new bin</label><div><button class="btn big primary">Move</button></div></form>`),
  adjust: (el) => openDialog("Correct count", `<form class="stack" data-submit="adjust"><input type="hidden" name="request_id" value="${rid("adjust")}">
      <input type="hidden" name="stock_id" value="${esc(el.dataset.arg)}"><p>Currently recorded: <b>${esc(el.dataset.qty)}</b></p>
      <div class="grid2"><label class="f">Counted quantity<input name="new_qty" type="number" min="0" required></label><label class="f">Reason<input name="reason" required value="recount"></label></div>
      <div><button class="btn big primary">Save count</button></div></form>`),
  newBin: () => openDialog("New bin", `<form class="stack" data-submit="newBin"><input type="hidden" name="request_id" value="${rid("bin")}">
      <div class="grid2"><label class="f">Code<input name="bin_code" required placeholder="B-05 or BENCH"></label><label class="f">Description<input name="description"></label><label class="f">Location<input name="location"></label>
      <label class="f">Kind<select name="kind"><option value="bin">Bin / box / bag</option><option value="work_area">Work area (bench, car, booth)</option></select></label></div>
      <label class="row"><input type="checkbox" name="label" checked> Print bin label</label><div><button class="btn big primary">Create</button></div></form>`),
  backend: async (el) => { await api("/api/backend", { backend: el.dataset.arg }); S.version = null; await refreshStatus(); location.hash = "#/rebuild"; render(); },
  demoReset: async () => { if (!confirmInline("demoReset")) return; await api("/api/demo/reset", {}); await refreshStatus(); location.hash = "#/rebuild"; render(); toast("Demo data reset", "ok"); },
  revoke: async (el) => { await api(`/api/devices/${el.dataset.arg}/revoke`, {}); render(); refreshStatus(); },
  copy: async (el) => { try { await navigator.clipboard.writeText(el.dataset.arg); toast("Copied", "ok", 2000); } catch { toast("Select the text and copy it", "", 3000); } },
  openScan: (el) => openCode(el.dataset.arg),
};
const pendingConfirm = {};
function confirmInline(key) {
  if (pendingConfirm[key] && Date.now() - pendingConfirm[key] < 5000) { delete pendingConfirm[key]; return true; }
  pendingConfirm[key] = Date.now();
  toast("Click again within 5 seconds to confirm.", "", 5000);
  return false;
}
function labelToast(job) {
  const kind = ["printed", "previewed"].includes(job.status) ? "ok" : ["uncertain", "failed"].includes(job.status) ? "bad" : "";
  const why = job.status === "pending" ? "Printer not ready — the label is kept and prints when the printer is back. This is what it will look like:"
    : job.status === "previewed" ? "Preview mode — nothing was sent to a printer." : (job.last_error || job.message);
  toast(`<b>Label ${esc(job.status)}</b> — ${esc(job.part_name)} (${esc(job.bin_code)})<br><span class="small">${esc(why)}</span>
    ${job.preview_path ? `<br><img class="label-preview" src="${esc(media("/media/label/" + job.job_id))}">` : ""}`, kind, 8000);
}

document.addEventListener("click", async (ev) => {
  const go = ev.target.closest("[data-go]");
  if (go) { location.hash = go.dataset.go; return; }
  const el = ev.target.closest("[data-act]");
  if (!el || !ACT[el.dataset.act]) return;
  ev.preventDefault();
  el.disabled = true;
  try { await ACT[el.dataset.act](el); } catch (e) { showError(e); } finally { el.disabled = false; }
});

/* -------------------------------------------------------------- forms */
const fd = (form) => Object.fromEntries(new FormData(form).entries());
const SUBMIT = {
  partSearch: (f, d) => { location.hash = `#/parts?q=${encodeURIComponent(d.q)}`; },
  nbSearch: (f, d) => { location.hash = `#/notebook?q=${encodeURIComponent(d.q || "")}${d.job ? `&job=${d.job}` : ""}`; },
  addNote: async (f, d) => {
    const r = await api("/api/notebook", { ...d, vehicle_id: activeVid() });
    toast(`Note ${esc(r.note_id)} saved`, "ok"); render();
  },
  addItem: async (f, d) => {
    const r = await api("/api/shopping", { ...d, vehicle_id: activeVid(), qty: +d.qty || 1 });
    closeDialog(); toast(`${esc(r.item_id)} added to the shopping list`, "ok"); render();
  },
  shopOrder: async (f, d) => {
    const body = { request_id: d.request_id, ...(d.order_ref !== undefined ? { status: "ordered" } : {}) };
    for (const k of ["order_ref", "vendor", "paid_amount", "paid_date"]) if (d[k]) body[k] = d[k];
    const r = await api(`/api/shopping/${d.item_id}`, body);
    closeDialog(); toast(`${esc(r.item_id)} saved${r.expense_id ? ` · payment ${esc(r.expense_id)} recorded` : ""}`, "ok"); render();
  },
  shopReceive: async (f, d) => {
    const body = { request_id: d.request_id, bin_code: d.bin_code, qty: +d.qty, condition: d.condition, print_label: !!d.print_label };
    if (d.part_id) body.part_id = d.part_id;
    const r = await api(`/api/shopping/${d.item_id}/receive`, body);
    closeDialog();
    toast(`${esc(r.message)}${r.qty_still_to_come ? `<br>${esc(r.qty_still_to_come)} still to come.` : "<br>Order complete."}`, "ok", 8000);
    if (r.label && r.label.job_id) labelToast(r.label);
    render();
  },
  expense: async (f, d) => {
    const file = f.querySelector("input[type=file]").files[0];
    const body = { ...d, vehicle_id: activeVid() };
    delete body.receipt;
    if (file) body.receipt_base64 = file.type === "application/pdf" ? await fileToDataUrlRaw(file) : await fileToDataUrl(file);
    const r = await api("/api/money/expense", body);
    toast(`${esc(r.expense.expense_id)} saved · spent so far ${money$(r.totals.spent)}`, "ok"); render();
  },
  voidExpense: async (f, d) => {
    const r = await api("/api/money/void", d);
    closeDialog(); toast(`${esc(r.expense_id)} voided · spent now ${money$(r.totals.spent)}`, "ok"); render();
  },
  budget: async (f, d) => { await api("/api/money/budget", { ...d, vehicle_id: activeVid() }); toast("Budget saved", "ok"); render(); },
  labelPreview: async (f, d) => {
    if (!d.part_id) throw new Error("Choose a part first");
    const r = await api("/api/labels/preview", { part_id: d.part_id, bin_code: d.bin_code });
    $("#previewOut").innerHTML = `<img class="label-preview" style="max-height:140px" src="${r.image}" alt="label preview"><div class="small muted">${esc(r.tape_mm)} mm tape</div>`;
  },
  sendOut: async (f, d) => {
    const r = await api("/api/away/send", { ...d, qty: +d.qty });
    closeDialog(); toast(esc(r.message), "ok", 8000); render();
  },
  returnPart: async (f, d) => {
    const r = await api(`/api/away/${d.custody_id}/return`, { request_id: d.request_id, qty: +d.qty, to_bin: d.to_bin, condition: d.condition, notes: d.notes });
    closeDialog(); toast(esc(r.message), "ok", 8000); render();
  },
  addVehicle: async (f, d) => { await api("/api/vehicles", { ...d, make_active: !!d.make_active }); await refreshStatus(); toast("Vehicle saved", "ok"); location.hash = "#/rebuild"; },
  createJob: async (f, d) => {
    const deps = $$("select[name=depends_on] option:checked", f).map((o) => o.value);
    const r = await api("/api/jobs", { ...d, depends_on: deps, estimate_min: d.estimate_min ? +d.estimate_min : null });
    closeDialog(); toast(`${esc(r.job_id)} created`, "ok"); location.hash = `#/job/${r.job_id}`;
  },
  editJob: async (f, d) => {
    await api(`/api/job/${S.job.job_id}`, { request_id: d.request_id, title: d.title, system: d.system, kind: d.kind, priority: +d.priority,
      estimate_min: d.estimate_min ? +d.estimate_min : null, goal: d.goal, notes: d.notes });
    closeDialog(); render();
  },
  hold: async (f, d) => { await api(`/api/job/${S.job.job_id}`, { request_id: d.request_id, status: "on_hold", hold_reason: d.hold_reason }); closeDialog(); render(); },
  addSteps: async (f, d) => { await api(`/api/job/${S.job.job_id}`, { request_id: d.request_id, add_steps: [d.add_steps] }); render(); },
  addDep: async (f, d) => { await api(`/api/job/${S.job.job_id}`, { request_id: d.request_id, add_depends_on: [d.dep.trim().toUpperCase()] }); render(); },
  note: async (f, d) => { await api(`/api/job/${S.job.job_id}`, { request_id: d.request_id, note: d.note }); toast("Note saved", "ok"); render(); },
  addNeed: async (f, d) => {
    if (!d.part_id && !d.description) throw new Error("Give a part ID or a description");
    await api(`/api/job/${S.job.job_id}`, { request_id: d.request_id, add_parts: [{ part_id: d.part_id.trim().toUpperCase(), description: d.description, qty: +d.qty, check: d.check }] });
    render();
  },
  linkPart: async (f, d) => { await api(`/api/need/${d.need_id}`, { request_id: d.request_id, part_id: d.part_id.trim().toUpperCase() }); closeDialog(); render(); },
  spec: async (f, d) => { await api("/api/specs", { ...d, job_id: S.job.job_id, verified: !!d.verified }); toast("Spec saved with its source", "ok"); render(); },
  install: async (f, d) => {
    const r = await api(`/api/job/${S.job.job_id}/install`, { request_id: d.request_id, need_id: d.need_id, stock_id: d.stock_id, qty: +d.qty, notes: d.notes });
    closeDialog(); toast(`<b>Installed.</b> ${esc(r.message)}<br>Left in ${esc(r.from_bin)}: ${esc(r.qty_left_in_bin)}`, "ok", 8000); render();
  },
  receive: async (f, d) => {
    const body = { request_id: d.request_id, bin_code: d.bin_code, qty: +d.qty, condition: d.condition, print_label: !!d.print_label };
    if (d.part_id) body.part_id = d.part_id.trim().toUpperCase();
    else if ((d.name || "").trim()) body.new_part = { name: d.name.trim(), side_position: d.side_position || "" };
    else throw new Error("Choose a part (search or scan it), or type a name for a new part.");
    const r = await api("/api/stock/receive", body);
    closeDialog(); toast(esc(r.message), "ok"); if (r.label && r.label.job_id) labelToast(r.label); location.hash = `#/part/${r.part.part_id}`; render();
  },
  removal: async (f, d) => {
    const body = { request_id: d.request_id, vehicle_id: activeVid(), bin_code: d.bin_code, qty: +d.qty, condition: d.condition, removed_from: d.removed_from,
      job_id: d.job_id, reinstall_job_id: d.reinstall_job_id, print_label: !!d.print_label };
    if (d.part_id) body.part_id = d.part_id.trim().toUpperCase(); else if (d.name) body.new_part = { name: d.name }; else throw new Error("Give a part ID or a name");
    const file = f.querySelector("input[type=file]").files[0];
    if (file) body.photo_base64 = await fileToDataUrl(file);
    const r = await api("/api/removal", body);
    closeDialog(); toast(esc(r.message) + (r.reinstall_need_id ? `<br>Linked to ${esc(r.reinstall_job_id)} as ${esc(r.reinstall_need_id)}` : ""), "ok", 8000);
    if (r.label && r.label.job_id) labelToast(r.label); render();
  },
  move: async (f, d) => {
    const p = route().arg;
    const r = await api("/api/stock/move", { ...d, part_id: p, qty: +d.qty, to_bin: d.to_bin.trim().toUpperCase(), print_label: !!d.print_label });
    closeDialog(); toast(esc(r.message), "ok"); render();
  },
  adjust: async (f, d) => { const r = await api("/api/stock/adjust", { ...d, new_qty: +d.new_qty }); closeDialog(); toast(esc(r.message), "ok"); render(); },
  newBin: async (f, d) => {
    await api("/api/bins", d); closeDialog(); toast(`Bin ${esc(d.bin_code)} created`, "ok");
    if (d.label) { const r = await api("/api/labels/bin", { request_id: rid("binlabel"), bin_code: d.bin_code }); labelToast(r.label); }
    render();
  },
  photo: async (f, d) => {
    const target = JSON.parse(f.dataset.target);
    const file = f.querySelector("input[type=file]").files[0];
    await api("/api/photos", { request_id: d.request_id, ...target, vehicle_id: target.job_id ? undefined : activeVid() || undefined, caption: d.caption, stage: d.stage, photo_base64: await fileToDataUrl(file) });
    toast("Photo saved", "ok"); render();
  },
  photoAny: async (f, d) => {
    const file = f.querySelector("input[type=file]").files[0];
    await api("/api/photos", { request_id: d.request_id, vehicle_id: activeVid(), job_id: d.job_id, part_id: d.part_id.trim().toUpperCase(), caption: d.caption, photo_base64: await fileToDataUrl(file) });
    toast("Photo saved", "ok"); render();
  },
  printerSettings: async (f, d) => { await api("/api/printer", { ...d, tape_mm: d.tape_mm, auto_print: d.auto_print }); toast("Printer settings saved", "ok"); await refreshStatus(); render(); },
  googleConnect: async (f, d) => {
    const file = f.querySelector("input[type=file]").files[0];
    await api("/api/google/connect", { client_secret: file ? await file.text() : "" });
    toast("Google sign-in opened in your browser. Finish it there.", "", 10000); render();
  },
};
document.addEventListener("submit", async (ev) => {
  const f = ev.target.closest("form[data-submit]");
  if (!f) return;
  ev.preventDefault();
  const btn = f.querySelector("button:not([type=button])");
  if (btn) btn.disabled = true;
  try { await SUBMIT[f.dataset.submit](f, fd(f)); }
  catch (e) { showError(e); }  // request_id stays the same, so pressing the button again is a safe retry
  finally { if (btn) btn.disabled = false; }
});

/* -------------------------------------------------------------- polling */
function busyEditing() {
  const a = document.activeElement;
  return dlg.open || (a && ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName) && a.id !== "scanBox" && a.id !== "vehicleSel");
}
async function poll() {
  try {
    const p = await api(`/api/poll?after=${S.seq}`);
    S.seq = p.seq;
    for (const ev of p.events) handleEvent(ev);
    if (S.status && (p.phone.text !== S.status.phone.text)) refreshStatus();
    if (p.google_flow && S.status && p.google_flow.state !== S.status.google_flow.state) { await refreshStatus(); if (route().name === "settings") render(); }
    if (S.version && p.data_version !== S.version) {
      S.version = p.data_version;
      await refreshStatus();
      if (!busyEditing()) render();
    } else S.version = p.data_version;
  } catch (e) { /* server restarting or token issue; keep trying */ }
  setTimeout(poll, 2500);
}
function handleEvent(ev) {
  if (ev.type === "scan") {
    const r = ev.result;
    let body = `<b>📱 ${esc(ev.device)} scanned ${esc(ev.code)}</b><br>`;
    if (!r.found) body += `Not found: ${esc(r.message || "")}`;
    else if (r.kind === "part") body += `${esc(r.title)} — ${esc(r.total_qty)} on hand<br>${r.entries.map((e) => `${esc(e.bin_code)}: ${esc(e.qty)} ${esc(e.condition)}`).join(" · ") || "none in stock"}`;
    else body += `${esc(r.kind)}: ${esc(r.title)}`;
    toast(body, r.found ? "ok" : "bad", 9000);
    if (r.found && S.followScans && !busyEditing()) openCode(r.id);
  } else if (ev.type === "paired") {
    toast(`📱 ${esc(ev.device)} paired`, "ok"); closeDialog(); refreshStatus();
  } else if (ev.type === "photo") {
    toast(`📷 Photo ${esc(ev.photo_id)} received from ${esc(ev.device)}`, "ok"); if (!busyEditing()) render();
  } else if (ev.type === "move") {
    toast(`📦 ${esc(ev.message)} (from ${esc(ev.device)})`, "ok"); if (!busyEditing()) render();
  }
}

(async function start() {
  if (!TOKEN) { $("#main").innerHTML = `<div class="box bad">Open Torino from Torino.exe.</div>`; return; }
  await refreshStatus();
  if (!location.hash) location.hash = "#/rebuild";
  await render();
  poll();
  setInterval(refreshStatus, 20000);
})();
