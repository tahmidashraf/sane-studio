// SANE Studio application controller.
import { api } from "./api.js";
import { Viewer, fmtT } from "./viewer.js";
import { BANDS, css, drawPsd, drawBandShift, drawChannelBars, drawTopo, drawHeatmap, viridisCss } from "./charts.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const SCREENS = ["screenUpload", "screenConfig", "screenProc", "screenWork"];
const state = { job: null, rid: null, meta: null, analysis: null, tab: "spectrum", spBand: 2, params: { reference: "auto", spatial_model: "kernel" } };
const viewer = new Viewer(api, (c) => { $("psdChan").value = String(c); renderSpectrum(); }, showDiff);

// Strip above the traces: for the selected channel, when in the current window the input and
// the SANE output are far enough apart to be told apart on screen.
function showDiff(ch, d) {
  const bar = $("diffBar");
  if (ch == null || !d) { bar.hidden = true; return; }
  const name = viewer.names[ch];
  if (!d.spans.length) {
    bar.innerHTML = `<span class="ch">${esc(name)}</span><span class="muted">input and output stay within one pixel column of each other in this window: no separation to see here.</span>`;
    bar.hidden = false; return;
  }
  const list = d.spans.slice(0, 4).map(([a, b]) => `${a.toFixed(2)}–${b.toFixed(2)} s`).join(" · ");
  const more = d.spans.length > 4 ? ` +${d.spans.length - 4} more` : "";
  bar.innerHTML = `<span class="ch">${esc(name)}</span>`
    + `<span>difference visible for <b>${d.total.toFixed(2)} s</b> of this window `
    + `(${Math.round(d.frac * 100)}%), in ${d.spans.length} ${d.spans.length === 1 ? "span" : "spans"}</span>`
    + `<span class="spans">${list}${more}</span>`
    + `<span class="spacer"></span>`
    + `<span class="muted">largest gap at ${d.worstT.toFixed(2)} s</span>`;
  bar.hidden = false;
}

// ------------------------------------------------------------------ helpers
function show(id) { for (const s of SCREENS) $(s).hidden = s !== id; window.scrollTo({ top: 0 }); }
let toastTimer;
function toast(msg, err = false) {
  const t = $("toast"); t.textContent = msg; t.className = `toast${err ? " err" : ""}`; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), err ? 7000 : 3500);
}
const fmtNum = (v, d = 3) => (v == null || !Number.isFinite(v) ? "—" : v.toFixed(d));
const dbMean = (rows) => rows[0].map((_, f) => 10 * Math.log10(rows.reduce((s, r) => s + 10 ** (r[f] / 10), 0) / rows.length));
const rel = (row) => { const s = row.reduce((a, b) => a + b, 0) || 1; return row.map((v) => v / s); };
const bandIdx = (name) => state.job.report.bands.indexOf(name);
const logTb = (row) => Math.log(row[bandIdx("theta")] / row[bandIdx("beta")]);

// ------------------------------------------------------------------ theme
$("themeBtn").addEventListener("click", () => {
  const dark = getComputedStyle(document.documentElement).colorScheme.includes("dark");
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("sane-theme", document.documentElement.dataset.theme); } catch (_) { /* private mode */ }
  if (!$("screenWork").hidden) { viewer.render(); viewer.timeline(); renderTab(); }
});

// ------------------------------------------------------------------ session chip
let expiresAt = 0;
function setSession(job) {
  expiresAt = job.expires_in_s == null ? 0 : Date.now() + job.expires_in_s * 1000;
  $("sessionChip").hidden = false; $("newSession").hidden = false; tickSession();
}
function tickSession() {
  if (!expiresAt) { $("sessionText").textContent = "Session · only in this browser tab"; return; }
  const m = Math.max(0, Math.round((expiresAt - Date.now()) / 60000));
  $("sessionText").textContent = `Session · data deleted in ${m} min`;
}
setInterval(tickSession, 30000);
$("newSession").addEventListener("click", async () => {
  if (state.job) await api.remove(state.job.id).catch(() => {});
  state.job = null; history.replaceState(null, "", location.pathname);
  $("sessionChip").hidden = true; $("newSession").hidden = true; show("screenUpload");
});

// ------------------------------------------------------------------ upload
async function filesFromDrop(dt) {
  const out = [], walk = async (entry) => {
    if (entry.isFile) out.push(await new Promise((res, rej) => entry.file(res, rej)));
    else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do { batch = await new Promise((res, rej) => reader.readEntries(res, rej)); for (const e of batch) await walk(e); } while (batch.length);
    }
  };
  const entries = [...dt.items].map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) return [...dt.files];
  for (const e of entries) await walk(e);
  return out;
}
const EEG_EXT = /\.(edf|bdf|vhdr|vmrk|eeg|set|fdt|fif|gz)$/i;

async function uploadFiles(files, append = false) {
  files = files.filter((f) => EEG_EXT.test(f.name));
  if (!files.length) return toast("No supported EEG files were found in that selection.", true);
  const drop = $("drop"), h2 = drop.querySelector("h2"), old = h2.textContent;
  try {
    const mb = files.reduce((s, f) => s + f.size, 0) / 1e6;
    const onProgress = (p) => (h2.textContent = `Opening ${files.length} file${files.length > 1 ? "s" : ""} · ${Math.round(p * 100)}% of ${mb.toFixed(1)} MB`);
    const job = append && state.job ? await api.addFiles(state.job.id, files, onProgress) : await api.upload(files, onProgress);
    openJob(job);
  } catch (e) { toast(e.message, true); } finally { h2.textContent = old; }
}

$("pickFiles").addEventListener("click", () => $("fileInput").click());
$("pickFolder").addEventListener("click", () => $("folderInput").click());
$("addMore").addEventListener("click", () => { $("fileInput").dataset.append = "1"; $("fileInput").click(); });
$("fileInput").addEventListener("change", (e) => {
  const append = e.target.dataset.append === "1"; delete e.target.dataset.append;
  uploadFiles([...e.target.files], append); e.target.value = "";
});
$("folderInput").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
const drop = $("drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
drop.addEventListener("drop", async (e) => { e.preventDefault(); uploadFiles(await filesFromDrop(e.dataTransfer)); });

async function loadSamples() {
  try {
    const list = await api.samples();
    $("samples").innerHTML = list.map((s) => `
      <button class="sample" data-id="${s.id}"><b>${esc(s.title)}</b><small>${esc(s.detail)}</small>
      <span class="meta">${s.files} files · ${s.size_mb} MB · runs in seconds</span></button>`).join("");
    for (const b of $("samples").querySelectorAll(".sample")) b.addEventListener("click", async () => {
      b.disabled = true;
      try { openJob(await api.fromSample(b.dataset.id)); } catch (e) { toast(e.message, true); } finally { b.disabled = false; }
    });
  } catch (_) { $("samples").innerHTML = ""; }
}

// ------------------------------------------------------------------ configure
function openJob(job) {
  state.job = job; history.replaceState(null, "", `#${job.id}`); setSession(job);
  if (job.state === "done") return openWorkspace();
  if (job.state === "processing") return poll();
  renderConfig(); show("screenConfig");
}

function renderConfig() {
  const recs = state.job.recordings, ready = recs.filter((r) => r.status !== "error");
  $("recCount").textContent = `${recs.length} recording${recs.length === 1 ? "" : "s"}`;
  $("recTotals").textContent = `${ready.length} ready · ${recs.reduce((s, r) => s + r.size_mb, 0).toFixed(1)} MB`;
  $("recRows").innerHTML = recs.map((r) => {
    const bad = r.status === "error";
    const comp = r.companions?.length ? `<div class="pass-list">+ ${r.companions.map(esc).join(", ")}</div>` : "";
    const warn = r.warnings?.length ? `<div class="warn-msg">${r.warnings.map(esc).join("; ")}</div>` : "";
    const reason = bad ? `<div class="err-msg"><b>Can't be processed:</b> ${esc(sentence(r.message))}</div>` : "";
    return `<tr class="${bad ? "rejected" : ""}">
      <td><div class="file">${esc(r.file).replace(/_/g, "_<wbr>")}</div>${comp}${reason}${bad ? "" : passSummary(r.passthrough)}${warn}</td>
      <td><span class="chip fmt">${esc(r.format)}</span></td>
      <td class="num">${r.n_eeg ?? "—"} / ${r.n_channels ?? "—"}</td>
      <td class="num">${r.fs ? `${+r.fs.toFixed(1)} Hz` : "—"}</td>
      <td class="num">${r.duration_s != null ? fmtT(r.duration_s) : "—"}</td>
      <td class="num">${r.n_events ?? "—"}</td>
      <td>${bad ? `<span class="muted">—</span>`
        : `<input class="input sub-in" data-rid="${r.rid}" value="${esc(r.subject)}" aria-label="Subject ID for ${esc(r.file)}">`}</td>
      <td>${bad ? `<span class="chip err">Skipped</span>` : `<span class="chip ok">Ready</span>`}</td></tr>`;
  }).join("");
  $("uploadNotes").textContent = state.job.notes.join(" ");
  for (const inp of $("recRows").querySelectorAll(".sub-in")) inp.addEventListener("input", refHint);
  $("runBtn").textContent = ready.length
    ? `Neutralize ${ready.length} recording${ready.length === 1 ? "" : "s"}` : "No recordings can be processed";
  $("runBtn").disabled = !ready.length;
  refHint();
}

// "this is an EyeLink file" -> "This is an EyeLink file."
const sentence = (s) => { s = String(s || "").trim(); return s && s[0].toUpperCase() + s.slice(1) + (/[.!?]$/.test(s) ? "" : "."); };

// Channels kept unchanged, counted by reason; the full list only on request.
function passSummary(list) {
  if (!list?.length) return "";
  const kind = (reason) => {
    const m = /^(\w+) channel$/.exec(reason);
    if (m) return { mag: "MEG", grad: "MEG", eog: "EOG", ecg: "ECG", emg: "EMG", stim: "trigger", misc: "misc" }[m[1]] ?? m[1];
    if (reason.startsWith("sensor")) return "sensor";
    if (reason.startsWith("not a scalp electrode")) return "non-electrode";
    if (reason.startsWith("non-EEG")) return "non-EEG";
    return reason.replace(" signal", "");
  };
  const counts = new Map();
  for (const p of list) counts.set(kind(p.reason), (counts.get(kind(p.reason)) ?? 0) + 1);
  const summary = [...counts].map(([k, n]) => `${n} ${k}`).join(", ");
  if (list.length <= 6) {
    return `<div class="pass-list">Kept unchanged: ${list.map((p) => `${esc(p.name)} <span class="why">(${esc(kind(p.reason))})</span>`).join(", ")}</div>`;
  }
  return `<details class="pass-list"><summary>Kept unchanged: ${esc(summary)} channels</summary>
    <div class="pass-all">${list.map((p) => esc(p.name)).join(", ")}</div></details>`;
}

function subjectsMap() {
  const m = {}; for (const inp of $("recRows").querySelectorAll(".sub-in")) m[inp.dataset.rid] = inp.value.trim() || inp.dataset.rid;
  return m;
}
function refHint() {
  const n = new Set(Object.values(subjectsMap())).size, ref = state.params.reference;
  const cohort = (ref === "auto" || ref === "cohort") && n >= 2;
  $("refHint").textContent = n === 0
    ? "Add a readable EEG recording to choose a reference."
    : cohort
    ? `${n} subjects detected: each subject is aligned to the cohort average, channel by channel, so the group's typical profile is kept.`
    : ref === "cohort" ? "Cohort needs at least two subjects; the normative reference will be used instead."
    : "Each recording is aligned to a built-in normative profile. Upload several subjects to use a cohort reference.";
  spatialAuto(cohort);
  // the normative spatial model matters only when spatial alignment is on and there is no cohort
  $("spatialField").style.opacity = cohort || +$("spAlpha").value === 0 ? 0.5 : 1;
}
// Spatial alignment defaults to automatic: full when aligning to a measured cohort,
// off against the generic head model, where it would reconstruct channels from neighbours.
function spatialAuto(cohort) {
  const s = $("spAlpha");
  $("spAuto").hidden = !state.spTouched;
  if (state.spTouched) { $("spAutoNote").textContent = ""; return; }
  s.value = cohort ? $("alpha").value : 0;
  s.style.setProperty("--fill", `${s.value * 100}%`);
  $("spAlphaVal").textContent = cohort ? "Auto · full" : "Auto · off";
  $("spAutoNote").textContent = cohort
    ? "Auto: full, because the reference is your own cohort."
    : "Auto: off against the built-in normative profile, so every channel keeps its own signal; raise it to also remove the spatial fingerprint.";
}
function bindSeg(id, k) {
  $(id).addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    for (const x of $(id).children) x.setAttribute("aria-pressed", String(x === b));
    state.params[k] = b.dataset.v; refHint();
  });
}
bindSeg("refSeg", "reference"); bindSeg("spatialSeg", "spatial_model");
$("alpha").addEventListener("input", (e) => {
  $("alphaVal").textContent = (+e.target.value).toFixed(2);
  e.target.style.setProperty("--fill", `${e.target.value * 100}%`);
  if (!state.spTouched) refHint();
});
$("spAlpha").addEventListener("input", (e) => {
  state.spTouched = true;
  $("spAlphaVal").textContent = (+e.target.value).toFixed(2);
  e.target.style.setProperty("--fill", `${e.target.value * 100}%`);
  refHint();
});
$("spAuto").addEventListener("click", () => { state.spTouched = false; refHint(); });
$("runBtn").addEventListener("click", async () => {
  try {
    const job = await api.run(state.job.id, {
      alpha: +$("alpha").value, spatial_alpha: state.spTouched ? +$("spAlpha").value : null,
      reference: state.params.reference, spatial_model: state.params.spatial_model,
      preserve_oob: $("oob").checked, anonymize: $("anon").checked, subjects: subjectsMap() });
    state.job = job; poll();
  } catch (e) { toast(e.message, true); }
});
$("reconfigure").addEventListener("click", () => { renderConfig(); show("screenConfig"); });

// ------------------------------------------------------------------ processing
const STEP_AT = [0, 0.42, 0.45, 0.6, 0.92];
async function poll() {
  show("screenProc");
  for (;;) {
    let job;
    try { job = await api.job(state.job.id); } catch (e) { toast(e.message, true); return show("screenUpload"); }
    state.job = job;
    $("procBar").style.width = `${Math.round(job.progress * 100)}%`;
    $("procStage").textContent = job.stage || "Starting";
    [...$("procSteps").children].forEach((s, i) => {
      const next = STEP_AT[i + 1] ?? 2;
      s.className = job.progress >= next ? "done" : job.progress >= STEP_AT[i] ? "on" : "";
    });
    if (job.state === "done") return openWorkspace();
    if (job.state === "error") { toast(job.error || "Processing failed", true); renderConfig(); return show("screenConfig"); }
    await new Promise((r) => setTimeout(r, 600));
  }
}

// ------------------------------------------------------------------ workspace
function openWorkspace() {
  show("screenWork");
  const recs = state.job.report.recordings;
  $("railCount").textContent = recs.length;
  $("recList").innerHTML = recs.map((r) => `
    <button class="rec-item" data-rid="${r.rid}">
      <span class="t"><span class="chip fmt">${esc(r.format)}</span><b title="${esc(r.file)}">${esc(r.file)}</b></span>
      <span class="d">${esc(r.subject)} · ${r.eeg_channels} EEG ch</span>
      <span class="delta">θ/β <span class="a">${fmtNum(r.biomarkers_in.theta_beta, 2)}</span> → <span class="b">${fmtNum(r.biomarkers_out.theta_beta, 2)}</span></span>
    </button>`).join("");
  for (const b of $("recList").querySelectorAll(".rec-item")) b.addEventListener("click", () => selectRec(b.dataset.rid));
  $("dlAll").onclick = () => api.downloadAll(state.job.id).catch((e) => toast(e.message, true));
  selectRec(recs.find((r) => r.rid === state.rid)?.rid || recs[0].rid);
}

async function selectRec(rid) {
  state.rid = rid;
  for (const b of $("recList").querySelectorAll(".rec-item")) b.setAttribute("aria-current", String(b.dataset.rid === rid));
  const r = state.job.report.recordings.find((x) => x.rid === rid);
  try {
    const [meta, analysis] = await Promise.all([api.meta(state.job.id, rid), api.analysis(state.job.id, rid)]);
    state.meta = meta; state.analysis = analysis;
  } catch (e) { return toast(e.message, true); }
  $("recTitle").textContent = r.file;
  const multi = r.outputs.length > 1;
  $("dlOneLabel").textContent = multi ? `Download ${r.format} bundle (.zip)` : `Download ${r.outputs[0]}`;
  $("dlOne").onclick = () => api.downloadOne(state.job.id, rid).catch((e) => toast(e.message, true));
  const refLabel = r.reference === "cohort" ? `cohort reference · ${r.reference_subjects} subjects` : r.reference.replace("normative-", "normative · ");
  $("recFacts").innerHTML = [
    `<span class="chip fmt">${esc(r.format)}</span>`, `<span class="chip">${esc(r.subject)}</span>`,
    `<span class="chip">${r.eeg_channels} EEG${r.passthrough_channels ? ` + ${r.passthrough_channels} aux` : ""}</span>`,
    `<span class="chip">${state.meta.fs} Hz</span>`, `<span class="chip">${fmtT(state.meta.N / state.meta.fs)}</span>`,
    `<span class="chip">${r.events} events</span>`, `<span class="chip out">${esc(refLabel)}</span>`].join("");
  const notes = r.notes || [];
  $("recNotes").hidden = !notes.length;
  $("recNotes").textContent = notes.join(" ");
  renderKpis(r);
  $("psdChan").innerHTML = `<option value="mean">Mean of all EEG channels</option>` +
    meta_names().map((n, i) => `<option value="${i}">${esc(n)}</option>`).join("");
  viewer.load(state.job.id, rid, state.meta, state.analysis);
  renderTab();
}
const meta_names = () => state.meta.ch_names;

function kpi(title, a, b, sub, { better = "lower", digits = 3, single = false, max } = {}) {
  const m = max ?? Math.max(a ?? 0, b ?? 0, 1e-9);
  const meter = single
    ? `<div class="meter"><i class="mo" style="left:0;width:${Math.max(2, (b / m) * 100)}%"></i></div>`
    : `<div class="meter"><i class="mi" style="left:0;width:${Math.max(2, ((a ?? 0) / m) * 100)}%"></i></div>
       <div class="meter" style="margin-top:4px"><i class="mo" style="left:0;width:${Math.max(2, ((b ?? 0) / m) * 100)}%"></i></div>`;
  const arrow = better === "lower" ? "↓ lower is better" : "↑ higher is better";
  return `<div class="panel kpi"><div class="k"><span>${title}</span></div>
    <div class="v">${single ? "" : `<span class="a">${fmtNum(a, digits)}</span><span class="arr">→</span>`}<span class="b">${fmtNum(b, digits)}</span></div>
    ${meter}<div class="s">${sub}</div><div class="s" style="margin-top:2px">${arrow}</div></div>`;
}

function renderKpis(r) {
  const ref = r.biomarkers_ref.theta_beta, c = state.job.report.cohort;
  const dIn = Math.abs(r.biomarkers_in.theta_beta - ref), dOut = Math.abs(r.biomarkers_out.theta_beta - ref);
  const [lamHead, lamSub] = c.n_subjects >= 2
    ? ["no shared channels", `Λ compares the same channels across subjects; these ${c.n_subjects} subjects' montages have ${c.common_channels ?? 0} channel names in common.`]
    : ["needs 2+ subjects", "Upload several subjects to measure cohort identifiability."];
  const lam = c.lambda_in != null
    ? kpi("Trait Leakage Index Λ", c.lambda_in, c.lambda_out, `Cohort identifiability of band-power features · ${c.n_subjects} subjects`)
    : `<div class="panel kpi"><div class="k"><span>Trait Leakage Index Λ</span></div><div class="v"><span class="b" style="font-size:18px;color:var(--text-3)">${lamHead}</span></div><div class="s">${esc(lamSub)}</div></div>`;
  const spatialOff = r.spatial_alpha === 0;
  $("kpis").innerHTML = [
    kpi("θ/β biomarker deviation", dIn, dOut, `|log θ/β − reference|; reference ${fmtNum(ref, 2)}`),
    kpi("Spatial fingerprint", r.fingerprint_in, r.fingerprint_out, spatialOff
      ? `Spatial alignment was ${r.spatial_alpha_auto ? "off (auto, normative reference)" : "set to 0"}, so channel relationships are kept as recorded`
      : "Distance of band correlations from the reference"),
    lam,
    kpi("Brain dynamics kept", null, r.dynamics_mean ?? r.fidelity_mean,
      `Within-band correlation with the input on clean signal · waveform similarity ${fmtNum(r.fidelity_mean, 2)}`,
      { better: "higher", single: true, max: 1 }),
  ].join("");
}

// ------------------------------------------------------------------ analytics tabs
$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.tab = b.dataset.t;
  for (const x of $("tabs").children) x.setAttribute("aria-selected", String(x === b));
  for (const t of ["spectrum", "bands", "topo", "spatial", "report"]) $(`tab-${t}`).hidden = t !== state.tab;
  renderTab();
});
$("psdChan").addEventListener("change", renderSpectrum);
$("topoSel").addEventListener("change", renderTopo);
window.addEventListener("resize", () => { clearTimeout(window._rt); window._rt = setTimeout(() => !$("screenWork").hidden && renderTab(), 150); });

function renderTab() {
  if (!state.analysis) return;
  ({ spectrum: renderSpectrum, bands: renderBands, topo: renderTopo, spatial: renderSpatial, report: renderReport })[state.tab]();
}

function renderSpectrum() {
  const A = state.analysis, sel = $("psdChan").value;
  const inDb = sel === "mean" ? dbMean(A.psd_in) : A.psd_in[+sel];
  const outDb = sel === "mean" ? dbMean(A.psd_out) : A.psd_out[+sel];
  drawPsd($("cvPsd"), A.freqs, inDb, outDb, { height: 320 });
  const pick = (rows) => (sel === "mean" ? rows[0].map((_, b) => rows.reduce((s, r) => s + r[b], 0) / rows.length) : rows[+sel]);
  const pin = pick(A.power_in), pout = pick(A.power_out), pref = pick(A.power_ref);
  const card = (k, a, b, ref, d = 2) => `<div class="rcard"><div class="k">${k}</div>
    <div class="v"><span style="color:var(--in);font-size:15px">${fmtNum(a, d)}</span> <span class="faint" style="font-size:14px">→</span> <span style="color:var(--out)">${fmtNum(b, d)}</span></div>
    <div class="s">reference ${fmtNum(ref, d)}</div></div>`;
  $("psdStats").innerHTML = card("log θ/β", logTb(pin), logTb(pout), logTb(pref)) +
    card("relative θ power", rel(pin)[bandIdx("theta")], rel(pout)[bandIdx("theta")], rel(pref)[bandIdx("theta")]) +
    card("relative β power", rel(pin)[bandIdx("beta")], rel(pout)[bandIdx("beta")], rel(pref)[bandIdx("beta")]);
  $("psdNote").textContent = "Shaded violet: theta and beta, the bands of the biomarker. Outside 1–45 Hz the output follows the input exactly when out-of-band preservation is on.";
}

function renderBands() {
  const A = state.analysis, bands = state.job.report.bands;
  // one column of per-channel relative power per band, so the figure can show the spread
  const cols = (rows) => bands.map((_, b) => rows.map((row) => rel(row)[b]));
  const labels = bands.map((b) => [b, BANDS.find((x) => x[0] === b)[3]]);
  drawBandShift($("cvBands"), labels, cols(A.power_in), cols(A.power_out), cols(A.power_ref),
    { height: 320, yLabel: "relative power" });
  const med = (v) => { const a = v.filter(Number.isFinite).sort((x, y) => x - y); return a.length ? a[Math.floor(a.length / 2)] : 0; };
  const shifts = bands.map((b, i) => {
    const d = med(cols(A.power_out)[i]) - med(cols(A.power_in)[i]);
    return `${BANDS.find((x) => x[0] === b)[3]} ${d > 0 ? "+" : ""}${d.toFixed(3)}`;
  }).join(" · ");
  $("bandsNote").textContent = `Each band shows the spread over channels: box = middle half, whisker = 5th to 95th percentile, thick line = median. The pink arrow is the median shift from input to output, and the dashed blue line is the reference the defense aims at. Median shift per band: ${shifts}.`;
  const dev = (rows) => rows.map((row, c) => logTb(row) - logTb(A.power_ref[c]));
  drawChannelBars($("cvTb"), dev(A.power_in), dev(A.power_out), 0, { height: 260 });
}

function topoValues(rows, kind) {
  return rows.map((row) => (kind === "tb" ? logTb(row) : rel(row)[bandIdx(kind)]));
}
function renderTopo() {
  const A = state.analysis, kind = $("topoSel").value;
  if (kind !== "tb" && bandIdx(kind) < 0) return;
  const sets = [A.power_in, A.power_out, A.power_ref].map((r) => topoValues(r, kind));
  const all = sets.flat().filter(Number.isFinite).sort((a, b) => a - b);
  const lo = all[Math.floor(0.02 * (all.length - 1))], hi = all[Math.floor(0.98 * (all.length - 1))];
  [["cvTopoIn", 0], ["cvTopoOut", 1], ["cvTopoRef", 2]].forEach(([id, k]) => drawTopo($(id), A.pos2d, sets[k], lo, hi));
  $("topoBar").innerHTML = `<span>${fmtNum(lo, 2)}</span><i style="background:${viridisCss}"></i><span>${fmtNum(hi, 2)}</span>`;
  const located = A.pos2d.filter((p) => p[0] != null).length;
  $("topoNote").textContent = `${located} of ${A.pos2d.length} channels have positions (from the file, or matched to the 10-05 system). All three maps share one colour scale.`;
}

async function renderSpatial() {
  const bands = state.job.report.bands;
  $("spBandSeg").innerHTML = bands.map((b, i) => `<button data-b="${i}" aria-pressed="${i === state.spBand}">${BANDS.find((x) => x[0] === b)[3]} ${b}</button>`).join("");
  for (const btn of $("spBandSeg").children) btn.onclick = () => { state.spBand = +btn.dataset.b; renderSpatial(); };
  state.spBand = Math.min(state.spBand, bands.length - 1);
  const S = await api.spatial(state.job.id, state.rid, state.spBand);
  drawHeatmap($("cvSpIn"), S.in); drawHeatmap($("cvSpOut"), S.out); drawHeatmap($("cvSpRef"), S.ref);
  const dist = (M) => {
    let num = 0, den = 0;
    for (let i = 0; i < M.length; i++) for (let j = 0; j < M.length; j++) { num += (M[i][j] - S.ref[i][j]) ** 2; if (i !== j) den += S.ref[i][j] ** 2; }
    return Math.sqrt(num / Math.max(den, 1e-12));
  };
  $("spDistIn").textContent = `distance ${dist(S.in).toFixed(3)}`;
  $("spDistOut").textContent = `distance ${dist(S.out).toFixed(3)}`;
}

function renderReport() {
  const rep = state.job.report, c = rep.cohort;
  // floor, never round up: a 99.998% reduction must not read as 100%
  const pct = (v) => (v == null ? "—" : `${(Math.floor(v * 10000) / 100).toFixed(2)}%`);
  const attack = (a) => (a ? `ρ ${a.rho.toFixed(2)} (p ${a.p < 0.001 ? "<0.001" : a.p.toFixed(3)})` : "—");
  let html = `<div class="report-grid">
    <div class="rcard"><div class="k">Subjects / recordings</div><div class="v">${c.n_subjects} / ${c.n_recordings}</div><div class="s">strength α = ${rep.alpha}</div></div>
    <div class="rcard"><div class="k">θ/β between-subject variance</div><div class="v" style="color:var(--out)">${c.theta_beta_var_reduction != null ? `−${pct(c.theta_beta_var_reduction)}` : "—"}</div><div class="s">${c.theta_beta_var_in != null ? `${c.theta_beta_var_in.toFixed(4)} → ${c.theta_beta_var_out.toExponential(1)}` : "needs 2+ subjects"}</div></div>
    <div class="rcard"><div class="k">Trait Leakage Index Λ</div><div class="v"><span style="color:var(--in);font-size:15px">${fmtNum(c.lambda_in)}</span> → <span style="color:var(--out)">${fmtNum(c.lambda_out)}</span></div><div class="s">mean subject identifiability (τ = 0)</div></div>
    <div class="rcard"><div class="k">Adaptive biomarker attacker</div><div class="v" style="font-size:16px">${attack(c.attack_out)}</div><div class="s">${c.attack_in ? `before: ${attack(c.attack_in)}` : "leave-one-subject-out ridge · needs 5+ subjects"}</div></div>
  </div>`;
  if (c.theta_beta_in) {
    html += `<div class="label" style="margin:6px 0 8px">θ/β per subject (log, common channels)</div><div class="table-wrap"><table class="data"><thead><tr><th>Subject</th><th class="num">Input</th><th class="num">SANE output</th><th class="num">Change</th></tr></thead><tbody>` +
      Object.keys(c.theta_beta_in).map((s) => `<tr><td class="mono">${esc(s)}</td><td class="num" style="color:var(--in)">${fmtNum(c.theta_beta_in[s])}</td><td class="num" style="color:var(--out)">${fmtNum(c.theta_beta_out[s])}</td><td class="num">${fmtNum(c.theta_beta_out[s] - c.theta_beta_in[s])}</td></tr>`).join("") + `</tbody></table></div>`;
  }
  html += `<div class="label" style="margin:18px 0 8px">Recordings</div><div class="table-wrap"><table class="data report-table"><thead><tr><th>File</th><th>Reference</th><th class="num">Fit windows</th><th class="num">Fingerprint</th><th class="num">Power deviation</th><th class="num">Dynamics kept</th><th class="num">Waveform</th><th class="num">Events</th><th>Output</th></tr></thead><tbody>` +
    rep.recordings.map((r) => `<tr><td class="file">${esc(r.file).replace(/_/g, "_<wbr>")}</td><td>${esc(r.reference)}${r.reference === "cohort" ? ` · ${(r.coverage * 100).toFixed(0)}% covered` : ""}</td>
      <td class="num">${(r.fit_windows_kept * 100).toFixed(0)}%</td><td class="num">${fmtNum(r.fingerprint_in)} → ${fmtNum(r.fingerprint_out)}</td>
      <td class="num">${fmtNum(r.power_dev_in)} → ${fmtNum(r.power_dev_out)}</td>
      <td class="num" title="${esc(Object.entries(r.dynamics_bands || {}).map(([b, v]) => `${b} ${fmtNum(v, 3)}`).join(" · "))}">${fmtNum(r.dynamics_mean)}</td>
      <td class="num">${fmtNum(r.fidelity_mean)}</td><td class="num">${r.events}</td>
      <td class="mono" style="font-size:12px">${r.outputs.map((o) => esc(o).replace(/_/g, "_<wbr>")).join("<br>")}</td></tr>`).join("") + `</tbody></table></div>`;
  html += `<div class="scope"><b>Scope.</b> SANE neutralizes subject-level spectral and spatial biomarkers, the per-channel band-power profile and the per-band channel covariance. Trial-level dynamics, events and auxiliary channels are preserved. Windows with saturation, spikes or flat signal are excluded from fitting (“Fit windows”) and from the fidelity scores. “Dynamics kept” is the within-band correlation of each channel with its input; it ignores the intended rescaling of bands and drops only when channels are remixed by spatial alignment. SANE is not a complete anonymization method: individual traits outside these statistics, such as alpha peak frequency, can remain. SANE Studio runs SANE in standard mode. A detector retrained with diagnosis labels on the released cohort can still find part of a diagnosis, so for clinical releases the paper recommends secure mode, which works on whole trials and is not part of SANE Studio.</div>
    <div style="margin-top:14px"><button class="btn" id="exportReport">Export report (JSON)</button></div>`;
  $("reportBody").innerHTML = html;
  $("exportReport").onclick = () => {
    const blob = new Blob([JSON.stringify(rep, null, 2)], { type: "application/json" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "SANE_REPORT.json" });
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
}

// ------------------------------------------------------------------ boot
loadSamples();
function routeFromHash() {
  const id = location.hash.slice(1);
  if (!/^[0-9a-f]{12}$/.test(id)) return show("screenUpload");
  if (id === state.job?.id) return;
  api.job(id).then(openJob).catch(() => {
    history.replaceState(null, "", location.pathname); show("screenUpload");
    toast("That session ended when the page was reloaded or closed. Its data is gone.", true);
  });
}
window.addEventListener("hashchange", routeFromHash);
routeFromHash();
