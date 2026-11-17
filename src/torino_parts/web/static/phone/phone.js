"use strict";
/* Torino phone scanner: rear camera -> Code 128 -> laptop. */
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rid = (p) => `${p}-${crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(16).slice(2)}`;

const S = {
  mode: "lookup",        // "lookup" | "bin" (scanning a destination bin for a move)
  current: null,         // last looked-up record
  lastCode: null, lastSeen: 0, busy: false,
  detector: null, stream: null, move: null, jobs: [],
};
const SAME_LABEL_MS = 1500;  // the label must leave the view this long before the same code counts again

async function api(path, body, raw) {
  const opt = { headers: { "X-Torino": "1" }, credentials: "same-origin" };
  if (raw) { opt.method = "POST"; opt.body = raw; opt.headers["Content-Type"] = "image/jpeg"; }
  else if (body !== undefined) { opt.method = "POST"; opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, opt);
  const d = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) { const e = new Error(d.error || `HTTP ${r.status}`); e.status = r.status; throw e; }
  return d;
}
function msg(text, kind = "") { const m = $("#msg"); m.className = `msg ${kind}`; m.innerHTML = text; }
function buzz(ms) { try { navigator.vibrate && navigator.vibrate(ms); } catch {} }

/* ------------------------------------------------------------- results */
function showResult(r) {
  S.current = r.found ? r : null;
  $("#bMove").disabled = !(r.found && r.kind === "part" && r.entries.length);
  if (!r.found) { $("#result").innerHTML = ""; return; }
  let body = "";
  if (r.kind === "part") {
    body = `<h2>${esc(r.title)}</h2><div class="id">${esc(r.id)} · ${esc(r.total_qty)} on hand${r.status === "unidentified" ? " · unidentified" : ""}</div>
      <table>${r.entries.map((e) => `<tr><td>Bin <b>${esc(e.bin_code)}</b></td><td>${esc(e.condition)}</td><td class="q">${esc(e.qty)}</td></tr>`).join("") || "<tr><td>None in stock</td></tr>"}</table>`;
  } else if (r.kind === "bin") {
    body = `<h2>Bin ${esc(r.id)}</h2><div class="id">${esc(r.title)} · ${esc(r.total_qty)} items</div>
      <table>${r.items.map((i) => `<tr><td>${esc(i.name)}<div class="id">${esc(i.part_id)}</div></td><td>${esc(i.condition)}</td><td class="q">${esc(i.qty)}</td></tr>`).join("") || "<tr><td>Empty</td></tr>"}</table>`;
  } else if (r.kind === "search") {
    body = `<h2>${esc(r.title)}</h2><table>${r.results.map((p) => `<tr><td>${esc(p.name)}<div class="id">${esc(p.part_id)}</div></td><td class="q">${esc(p.total_qty)}</td></tr>`).join("")}</table>`;
  } else {
    body = `<h2>${esc(r.title)}</h2><div class="id">${esc(r.kind)} ${esc(r.id)}</div>`;
  }
  $("#result").innerHTML = `<div class="card">${body}</div>`;
}

async function onCode(code, viaServer) {
  // viaServer: the laptop already decoded the frame and returned the lookup
  const now = Date.now();
  if (!viaServer) {
    if (code === S.lastCode && now - S.lastSeen < SAME_LABEL_MS) { S.lastSeen = now; return; }
    S.lastCode = code; S.lastSeen = now;
  }
  if (S.mode === "bin") return onBinScan(code, viaServer);
  try {
    const r = viaServer || await api("/p/api/scan", { code, mode: "lookup" });
    if (r.repeat) { msg("Same label — point at the next one", ""); return; }
    if (r.found) { buzz(80); msg(`✓ ${esc(r.id)} — shown on the laptop too`, "ok"); flash(); }
    else { buzz([60, 60, 60]); msg(`✗ ${esc(code)} not found — try again or type it`, "bad"); }
    showResult(r);
  } catch (e) { msg(`✗ ${esc(e.message)} — try again`, "bad"); }
}
function flash() { const f = $("#frame"); f.classList.add("hit"); setTimeout(() => f.classList.remove("hit"), 600); }

/* -------------------------------------------------------------- camera */
async function startCamera() {
  try {
    S.stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 960 } }, audio: false });
    const v = $("#video");
    v.srcObject = S.stream;
    await v.play();
    if ("BarcodeDetector" in window) {
      try {
        const fmts = await BarcodeDetector.getSupportedFormats();
        if (fmts.includes("code_128")) S.detector = new BarcodeDetector({ formats: ["code_128"] });
      } catch {}
    }
    msg("Point the camera at a label", "");
    loop();
  } catch (e) {
    const secure = window.isSecureContext ? "" : " (this page isn't trusted HTTPS — install the certificate from the laptop's Pair Phone screen)";
    msg(`Camera unavailable: ${esc(e.message)}${secure}. Use “Type code”.`, "bad");
  }
}
async function loop() {
  const v = $("#video");
  if (!S.busy && v.readyState >= 2 && !document.hidden && !S.paused) {
    S.busy = true;
    try {
      if (S.detector) {
        const codes = await S.detector.detect(v);
        if (codes.length) await onCode(codes[0].rawValue.trim());
      } else {
        const c = $("#canvas");
        const w = 960, h = Math.round(960 * v.videoHeight / v.videoWidth);
        c.width = w; c.height = h;
        c.getContext("2d").drawImage(v, 0, 0, w, h);
        const blob = await new Promise((res) => c.toBlob(res, "image/jpeg", 0.75));
        const r = await api(`/p/api/frame?mode=${S.mode}`, undefined, blob);
        if (r.decoded) {
          const now = Date.now();
          const same = r.decoded === S.lastCode && now - S.lastSeen < SAME_LABEL_MS;
          S.lastCode = r.decoded; S.lastSeen = now;
          if (!same || S.mode === "bin") await onCode(r.decoded, r.result);
        }
      }
    } catch (e) { if (e.status === 401) return showUnpaired(); }
    S.busy = false;
  }
  setTimeout(loop, S.detector ? 150 : 330);
}

/* ---------------------------------------------------------- type code */
$("#bType").onclick = () => {
  S.paused = true;
  $("#panel").innerHTML = `<div class="panel"><h3>Type the code</h3><input id="codeIn" type="text" autocapitalize="characters" placeholder="P-00001, B-04…">
    <div class="row2"><button id="cancel">Cancel</button><button class="primary" id="go">Look up</button></div></div>`;
  $("#codeIn").focus();
  $("#cancel").onclick = closePanel;
  $("#go").onclick = async () => {
    const code = $("#codeIn").value.trim(); if (!code) return;
    closePanel(); S.lastCode = null;
    await onCode(code);
  };
};
function closePanel() { $("#panel").innerHTML = ""; S.paused = false; if (S.mode === "bin") { S.mode = "lookup"; $("#frame").classList.remove("bin"); } }

/* -------------------------------------------------------------- photo */
$("#bPhoto").onclick = async () => {
  S.paused = true;
  try { S.jobs = (await api("/p/api/jobs")).jobs; } catch { S.jobs = []; }
  const part = S.current && S.current.kind === "part" ? S.current : null;
  $("#panel").innerHTML = `<div class="panel"><h3>Attach a photo to…</h3>
    ${part ? `<label class="opt"><input type="radio" name="tgt" value="part" checked> ${esc(part.title)} (${esc(part.id)})</label>` : ""}
    <label class="opt"><input type="radio" name="tgt" value="job" ${part ? "" : "checked"}> A job:</label>
    <select id="jobSel">${S.jobs.map((j) => `<option value="${esc(j.job_id)}">${esc(j.job_id)} ${esc(j.title)}</option>`).join("") || "<option value=''>Active vehicle (no job)</option>"}</select>
    <input id="cap" type="text" placeholder="Caption (optional)" style="margin-top:10px">
    <div class="row2"><button id="cancel">Cancel</button><button class="primary" id="take">Take photo</button></div></div>`;
  $("#cancel").onclick = closePanel;
  $("#take").onclick = () => $("#photoInput").click();
};
$("#photoInput").onchange = async (ev) => {
  const file = ev.target.files[0]; if (!file) return;
  const tgt = document.querySelector("input[name=tgt]:checked")?.value;
  const body = { request_id: rid("phonephoto"), caption: $("#cap")?.value || "" };
  if (tgt === "part" && S.current) body.part_id = S.current.id; else body.job_id = $("#jobSel")?.value || "";
  msg("Uploading photo…", "");
  try {
    body.image = await shrink(file);
    let r;
    for (let i = 0; i < 3; i++) {  // same request_id on retry: the photo is saved once
      try { r = await api("/p/api/photo", body); break; } catch (e) { if (i === 2 || e.status < 500) throw e; }
    }
    buzz(80); msg(`✓ Photo ${esc(r.photo_id)} saved`, "ok");
  } catch (e) { msg(`✗ Photo not saved: ${esc(e.message)}`, "bad"); }
  ev.target.value = ""; closePanel();
};
async function shrink(file, max = 2000) {
  const img = await createImageBitmap(file);
  const s = Math.min(1, max / Math.max(img.width, img.height));
  const c = document.createElement("canvas"); c.width = Math.round(img.width * s); c.height = Math.round(img.height * s);
  c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.85);
}

/* --------------------------------------------------------------- move */
$("#bMove").onclick = () => {
  const p = S.current; if (!p || p.kind !== "part") return;
  S.move = { part: p, stock: p.entries.length === 1 ? p.entries[0] : null, to: null, qty: 1 };
  renderMove();
};
function renderMove() {
  const m = S.move;
  S.paused = m.step === "review";
  if (!m.stock) {
    $("#panel").innerHTML = `<div class="panel"><h3>Move ${esc(m.part.title)} — from which bin?</h3>
      ${m.part.entries.map((e, i) => `<label class="opt"><input type="radio" name="src" value="${i}"> Bin <b>${esc(e.bin_code)}</b> — ${esc(e.qty)} ${esc(e.condition)}</label>`).join("")}
      <div class="row2"><button id="cancel">Cancel</button><button class="primary" id="next">Next</button></div></div>`;
    $("#next").onclick = () => { const i = document.querySelector("input[name=src]:checked"); if (!i) return; m.stock = m.part.entries[+i.value]; renderMove(); };
  } else if (!m.to) {
    S.mode = "bin"; $("#frame").classList.add("bin");
    msg("Scan the DESTINATION bin label (or pick it below)", "bin");
    api("/p/api/bins").then((b) => {
      const sel = $("#binSel");
      if (sel) sel.innerHTML = `<option value="">Pick a bin…</option>` + b.bins.filter((x) => x.bin_code !== m.stock.bin_code).map((x) => `<option>${esc(x.bin_code)}</option>`).join("");
    });
    $("#panel").innerHTML = `<div class="panel"><h3>Move from ${esc(m.stock.bin_code)} to…</h3><p class="small">Scan the bin's label now, or choose:</p>
      <select id="binSel"></select><div class="row2"><button id="cancel">Cancel</button><button class="primary" id="next">Next</button></div></div>`;
    $("#next").onclick = () => { const v = $("#binSel").value; if (v) { m.to = v; endBinMode(); renderMove(); } };
  } else {
    m.step = "review"; S.paused = true;
    m.request_id = m.request_id || rid("phonemove");
    $("#panel").innerHTML = `<div class="panel"><h3>Review move</h3>
      <div class="qty"><button id="minus">−</button><span id="q">${m.qty}</span><button id="plus">+</button><span class="small">of ${esc(m.stock.qty)}</span></div>
      <p style="font-size:20px">Move <b id="qq">${m.qty}</b> × <b>${esc(m.part.title)}</b><br>from bin <b>${esc(m.stock.bin_code)}</b> (${esc(m.stock.condition)}) → bin <b>${esc(m.to)}</b></p>
      <div class="row2"><button id="cancel">Cancel</button><button class="go" id="save">Save move</button></div></div>`;
    const setQ = (q) => { m.qty = Math.max(1, Math.min(m.stock.qty, q)); $("#q").textContent = m.qty; $("#qq").textContent = m.qty; m.request_id = rid("phonemove"); };
    $("#minus").onclick = () => setQ(m.qty - 1);
    $("#plus").onclick = () => setQ(m.qty + 1);
    $("#save").onclick = async () => {
      $("#save").disabled = true;
      try {
        const r = await api("/p/api/move", { request_id: m.request_id, part_id: m.part.id, stock_id: m.stock.stock_id, from_bin: m.stock.bin_code, to_bin: m.to, qty: m.qty });
        buzz(80); msg(`✓ ${esc(r.message)}`, "ok");
        S.move = null; closePanel();
        const fresh = await api("/p/api/scan", { code: m.part.id, mode: "refresh" }); showResult(fresh);
      } catch (e) { msg(`✗ Not saved: ${esc(e.message)} — tap Save to retry`, "bad"); $("#save").disabled = false; }
    };
  }
  const c = $("#cancel"); if (c) c.onclick = () => { S.move = null; endBinMode(); closePanel(); msg("Move cancelled", ""); };
}
async function onBinScan(code, viaServer) {
  if (!S.move) { endBinMode(); return; }
  let r = viaServer;
  if (!r) { try { r = await api("/p/api/scan", { code, mode: "bin" }); } catch (e) { msg(`✗ ${esc(e.message)}`, "bad"); return; } }
  if (!r.found || r.kind !== "bin") { buzz([60, 60, 60]); msg(`That's not a bin label (${esc(code)}). Scan the bin's label.`, "warn"); return; }
  if (r.id === S.move.stock.bin_code) { msg("That's the bin it's already in — scan the destination bin", "warn"); return; }
  buzz(80); S.move.to = r.id; endBinMode(); renderMove();
}
function endBinMode() { S.mode = "lookup"; $("#frame").classList.remove("bin"); }

/* --------------------------------------------------------------- start */
function showUnpaired() { $("#app").classList.add("hidden"); $("#unpaired").classList.remove("hidden"); }
(async function start() {
  try {
    const me = await api("/p/api/me");
    $("#app").classList.remove("hidden");
    $("#demo").classList.remove("hidden");
    $("#demo").textContent = me.demo ? "DEMO DATA" : (me.mode || "").toUpperCase();
    $("#demo").style.background = me.demo ? "#ffd400" : me.mode === "Local" ? "#1d6b4f" : "#1450b8";
    $("#demo").style.color = me.demo ? "#111" : "#fff";
    $("#dev").textContent = me.device;
    $("#veh").textContent = me.active_vehicle ? `Vehicle: ${me.active_vehicle.label}` : "";
    startCamera();
  } catch (e) {
    if (e.status === 401) showUnpaired(); else { $("#unpaired").classList.remove("hidden"); $("#unpaired").innerHTML = `<h1>Can't reach the laptop</h1><p>${esc(e.message)}</p>`; }
  }
})();
document.addEventListener("visibilitychange", () => { if (!document.hidden && S.stream && S.stream.getTracks()[0].readyState === "ended") startCamera(); });
