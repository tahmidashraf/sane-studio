// Multi-channel EEG viewer: input vs SANE output, with min/max decimation per pixel column.
import { css, setup, drawTimeline, niceTicks } from "./charts.js";

const REGIONS = [
  ["Frontal", /^(FP|AF|F\d|FZ)/], ["Central", /^(FC|C\d|CZ|CP)/], ["Temporal", /^(FT|T\d|TP)/],
  ["Parietal", /^(P\d|PZ|PO)/], ["Occipital", /^(O\d|OZ|I\d|IZ|CB)/],
];
const GUTTER = 62, AXIS = 24, TOP = 10;
const key = (n) => n.replace(/\s+/g, "").toUpperCase().replace(/^EEG/, "");
const $ = (id) => document.getElementById(id);

export class Viewer {
  constructor(api, onSelect, onDiff) {
    this.api = api; this.onSelect = onSelect; this.onDiff = onDiff;
    this.mode = "split"; this.band = "broad"; this.dur = 10; this.t0 = 0;
    this.scale = null; this.aux = true; this.events = true; this.group = "all"; this.sel = null;
    this.seq = 0; this.data = null; this.layout = null;
    this.cv = { in: $("cvIn"), out: $("cvOut") };
    this.hair = {};
    for (const k of ["in", "out"]) {
      const h = document.createElement("div");
      h.style.cssText = "position:absolute;top:0;width:1px;pointer-events:none;background:var(--text-3);opacity:.6;display:none;z-index:2";
      this.cv[k].parentElement.appendChild(h); this.hair[k] = h;
      this.cv[k].addEventListener("mousemove", (e) => this.hover(e, k));
      this.cv[k].addEventListener("mouseleave", () => this.unhover());
      this.cv[k].addEventListener("click", (e) => this.click(e, k));
    }
    this.cvMap = $("cvDiffMap"); this.mapLayout = null; this.mapMode = "line";
    $("diffMapSeg").addEventListener("click", (e) => {
      const btn = e.target.closest("button"); if (!btn) return;
      for (const x of $("diffMapSeg").children) x.setAttribute("aria-pressed", String(x === btn));
      this.mapMode = btn.dataset.v; $("diffTip").hidden = true; this.render();
    });
    this.cvMap.addEventListener("mousemove", (e) => this.mapHover(e));
    this.cvMap.addEventListener("mouseleave", () => { $("diffTip").hidden = true; });
    this.cvMap.addEventListener("click", (e) => this.mapClick(e));
    this.bindControls();
    new ResizeObserver(() => { if (this.meta) this.fetchSoon(120); }).observe($("plots"));
  }

  bindControls() {
    $("viewSeg").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      for (const x of $("viewSeg").children) x.setAttribute("aria-pressed", String(x === b));
      this.mode = b.dataset.v; this.applyMode(); this.fetch();
    });
    $("bandSel").addEventListener("change", (e) => { this.band = e.target.value; this.scale = null; this.fetch(); });
    $("winSel").addEventListener("change", (e) => { this.dur = +e.target.value; this.clampT0(); this.fetch(); });
    $("chSel").addEventListener("change", (e) => { this.group = e.target.value; this.render(); });
    $("scaleUp").addEventListener("click", () => this.setScale(this.scale / 1.5));
    $("scaleDown").addEventListener("click", () => this.setScale(this.scale * 1.5));
    $("auxToggle").addEventListener("change", (e) => { this.aux = e.target.checked; this.fetch(); });
    $("evToggle").addEventListener("change", (e) => { this.events = e.target.checked; this.render(); this.timeline(); });
    $("prevWin").addEventListener("click", () => this.step(-1));
    $("nextWin").addEventListener("click", () => this.step(1));
    document.addEventListener("keydown", (e) => {
      if ($("screenWork").hidden || /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName)) return;
      if (e.key === "ArrowRight") { this.step(1); e.preventDefault(); }
      if (e.key === "ArrowLeft") { this.step(-1); e.preventDefault(); }
    });
    const tl = $("cvTimeline");
    let drag = false;
    const seek = (e) => {
      const r = tl.getBoundingClientRect();
      this.t0 = ((e.clientX - r.left) / r.width) * this.duration - this.dur / 2;
      this.clampT0(); this.timeline(); this.fetchSoon(60);
    };
    tl.addEventListener("mousedown", (e) => { drag = true; seek(e); });
    window.addEventListener("mousemove", (e) => { if (drag) seek(e); });
    window.addEventListener("mouseup", () => { drag = false; });
  }

  load(jobId, rid, meta, analysis) {
    this.jobId = jobId; this.rid = rid; this.meta = meta; this.analysis = analysis;
    this.duration = meta.N / meta.fs; this.t0 = 0; this.scale = null; this.sel = null; this.group = "all";
    this.dur = Math.min(+$("winSel").value, this.duration);
    this.buildGroups(); this.applyMode(); this.fetch();
  }

  get names() { return this.meta.ch_names.concat(this.aux ? this.meta.aux_names : []); }

  buildGroups() {
    const eeg = this.meta.ch_names, opts = [["all", `All EEG (${eeg.length})`]];
    for (const [label, rx] of REGIONS) {
      const n = eeg.filter((c) => rx.test(key(c))).length;
      if (n >= 2 && n < eeg.length) opts.push([`r:${label}`, `${label} (${n})`]);
    }
    if (eeg.length > 40) for (let a = 0; a < eeg.length; a += 32) opts.push([`p:${a}`, `Channels ${a + 1}–${Math.min(eeg.length, a + 32)}`]);
    if (this.meta.aux_names.length) opts.push(["aux", `Auxiliary only (${this.meta.aux_names.length})`]);
    $("chSel").innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  }

  visible() {
    const eeg = this.meta.ch_names, nE = eeg.length, nA = this.aux ? this.meta.aux_names.length : 0;
    const auxIdx = Array.from({ length: nA }, (_, i) => nE + i);
    let idx;
    if (this.group === "aux") return auxIdx;
    if (this.group.startsWith("r:")) {
      const rx = REGIONS.find(([l]) => l === this.group.slice(2))[1];
      idx = eeg.map((c, i) => (rx.test(key(c)) ? i : -1)).filter((i) => i >= 0);
    } else if (this.group.startsWith("p:")) {
      const a = +this.group.slice(2); idx = Array.from({ length: Math.min(32, nE - a) }, (_, i) => a + i);
    } else idx = eeg.map((_, i) => i);
    return idx.concat(auxIdx);
  }

  applyMode() {
    const split = this.mode === "split";
    $("plots").classList.toggle("single", !split);
    $("plotOut").hidden = !split;
    const cap = $("plotIn").querySelector(".cap");
    const label = { split: ["in", "INPUT"], overlay: ["out", "OVERLAY"], in: ["in", "INPUT"], out: ["out", "SANE OUTPUT"], diff: ["", "OUTPUT − INPUT"] }[this.mode];
    cap.className = `cap chip ${label[0]}`; cap.textContent = label[1];
  }

  clampT0() { this.t0 = Math.max(0, Math.min(this.t0, Math.max(0, this.duration - this.dur))); }
  step(dir) { this.t0 += dir * this.dur; this.clampT0(); this.fetch(); }
  setScale(s) { this.scale = Math.max(0.05, s); this.render(); }
  fetchSoon(ms) { clearTimeout(this._t); this._t = setTimeout(() => this.fetch(), ms); }

  async fetch() {
    if (!this.meta) return;
    const seq = ++this.seq;
    const plotW = Math.max(120, $("plotIn").clientWidth - GUTTER - 8);
    const res = await this.api.window(this.jobId, this.rid, { t0: this.t0, dur: this.dur, px: Math.round(plotW), band: this.band, aux: this.aux });
    if (seq !== this.seq) return;
    this.data = res;
    if (this.scale == null) this.scale = this.autoScale();
    this.render(); this.timeline();
  }

  autoScale() {
    const { arr, C, px } = this.data, nE = this.meta.ch_names.length, sds = [];
    for (let c = 0; c < nE; c++) {
      let s = 0, s2 = 0;
      for (let p = 0; p < px; p++) { const o = ((0 * C + c) * px + p) * 2, m = (arr[o] + arr[o + 1]) / 2; s += m; s2 += m * m; }
      sds.push(Math.sqrt(Math.max(0, s2 / px - (s / px) ** 2)));
    }
    sds.sort((a, b) => a - b);
    const med = sds[Math.floor(sds.length / 2)] || 10;
    // dense montages get more headroom so neighbouring traces stay separable
    const head = this.visible().length > 72 ? 9 : this.visible().length > 40 ? 7 : 5;
    const mag = 10 ** Math.floor(Math.log10(med * head)), v = med * head;
    return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= v * 0.8) || v;
  }

  rowHeight(n) { return n <= 16 ? 30 : n <= 40 ? 18 : n <= 72 ? 10.5 : 6.4; }

  render() {
    if (!this.data) return;
    const vis = this.visible(), rowH = this.rowHeight(vis.length);
    const H = Math.round(Math.max(260, Math.min(980, vis.length * rowH)) + TOP + AXIS);
    $("scaleVal").textContent = `${+this.scale.toPrecision(3)} µV`;
    const srcs = this.mode === "split" ? [["in", 0], ["out", 1]] : [["in", { overlay: "overlay", in: 0, out: 1, diff: "diff" }[this.mode]]];
    for (const [cvKey, src] of srcs) this.draw(this.cv[cvKey], src, vis, H);
    const t1 = Math.min(this.duration, this.t0 + this.dur);
    $("winLabel").textContent = `${fmtT(this.t0)} – ${fmtT(t1)} of ${fmtT(this.duration)}  ·  ${vis.length} channels  ·  ${this.meta.fs} Hz`;
    this.drawDiffMap(vis);
    this.onDiff?.(this.sel, this.sel == null ? null : this.diffSpans(this.sel));
  }

  // Where input and SANE output differ in the current window, for every visible channel, using
  // the visible-difference spans that the strip above the traces reports for one channel. Two
  // views: a line graph of the input-output gap with a dot at the peak of each span, and a
  // channel-by-time map of the spans.
  drawDiffMap(vis) {
    const rows = vis.map((c) => this.diffSpans(c)), n = vis.length, { a, b } = this.data, names = this.names;
    const hitRows = rows.map((d, r) => [d, vis[r]]).filter(([d]) => d.spans.length);
    const top = hitRows.reduce((m, x) => (!m || x[0].frac > m[0].frac ? x : m), null);
    $("diffMapSum").textContent = hitRows.length
      ? `${hitRows.length} of ${n} channels differ visibly in ${fmtT(a, true)} – ${fmtT(b, true)} · most: ${names[top[1]]} (${Math.round(top[0].frac * 100)}%)`
      : `no channel differs visibly at this amplitude scale; press + on Scale to look closer`;
    const line = this.mapMode === "line";
    $("diffLegLine").hidden = !line; $("diffLegMap").hidden = line;
    $("diffMapNote").textContent = line
      ? "Each grey line is one channel: how far the SANE output sits from the input over the window shown above. A pink dot marks the peak of each time span where the two traces are at least 1.5 px apart at the current amplitude scale (the dashed line), the same rule as the strip above the traces. The blue line is the selected channel, or the one that differs most. Click a dot to select its channel."
      : "One row per channel over the window shown above. A pink block marks a time span where the input and the SANE output are at least 1.5 px apart at the current amplitude scale, the same rule as the strip above the traces. The right column is the share of the window with a visible difference. Click a row to select that channel.";
    if (line) this.drawDiffLine(vis, rows, vis.includes(this.sel) ? this.sel : top ? top[1] : null);
    else this.drawDiffRaster(vis, rows);
  }

  drawDiffLine(vis, rows, focus) {
    const MT = 24;
    const { ctx, w, h } = setup(this.cvMap, 300);
    const { a, b } = this.data, span = b - a, names = this.names;
    const box = { x: GUTTER, y: MT, w: Math.max(40, w - GUTTER - 14), h: h - MT - AXIS };
    const thr = rows[0]?.thr ?? 1;
    // y range up to the 99.5th percentile of all gaps, so one artifact does not flatten the rest
    const all = [];
    for (const d of rows) for (let p = 0; p < d.gap.length; p += 2) all.push(d.gap[p]);
    all.sort((u, v) => u - v);
    const ymax = Math.max(thr * 3, (all[Math.floor(0.995 * (all.length - 1))] || 0) * 1.15);
    const xs = (t) => box.x + ((t - a) / span) * box.w;
    const ys = (v) => box.y + box.h - (Math.min(v, ymax) / ymax) * box.h;
    ctx.fillStyle = css("--surface"); ctx.fillRect(0, 0, w, h);
    // grid: seconds on x, microvolts on y
    const step = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60].find((s) => span / s <= Math.max(4, box.w / 90)) || 60;
    ctx.strokeStyle = css("--grid"); ctx.lineWidth = 1;
    ctx.font = `11px ${css("--mono")}`; ctx.fillStyle = css("--text-3"); ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (let t = Math.ceil(a / step) * step; t <= b + 1e-9; t += step) {
      const x = Math.round(xs(t)) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, box.y); ctx.lineTo(x, box.y + box.h); ctx.stroke();
      ctx.fillText(fmtT(t, step < 1), x, box.y + box.h + 6);
    }
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (const v of niceTicks(0, ymax, 4)) {
      const y = Math.round(ys(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(box.x, y); ctx.lineTo(box.x + box.w, y); ctx.stroke();
      ctx.fillText(`${+v.toPrecision(3)}`, GUTTER - 8, y);
    }
    ctx.textAlign = "right"; ctx.textBaseline = "bottom"; ctx.fillText("gap µV", GUTTER - 8, box.y - 8);
    // events
    if (this.events) {
      ctx.save(); ctx.strokeStyle = css("--bio"); ctx.setLineDash([3, 3]); ctx.globalAlpha = 0.8;
      for (const ev of this.meta.annotations) {
        if (ev.t < a || ev.t > b) continue;
        const x = Math.round(xs(ev.t)) + 0.5;
        ctx.beginPath(); ctx.moveTo(x, box.y); ctx.lineTo(x, box.y + box.h); ctx.stroke();
      }
      ctx.restore();
    }
    // threshold above which a gap is visible at the current amplitude scale
    const yt = Math.round(ys(thr)) + 0.5;
    ctx.save(); ctx.strokeStyle = css("--text-2"); ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(box.x, yt); ctx.lineTo(box.x + box.w, yt); ctx.stroke(); ctx.restore();
    // one line per channel, the highlighted channel last and on top
    ctx.save(); ctx.beginPath(); ctx.rect(box.x - 6, box.y - 6, box.w + 12, box.h + 6); ctx.clip();
    const path = (d) => {
      const dt = span / d.gap.length;
      ctx.beginPath();
      for (let p = 0; p < d.gap.length; p++) { const x = xs(a + (p + 0.5) * dt), y = ys(d.gap[p]); p ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
      ctx.stroke();
    };
    ctx.strokeStyle = css("--text-3"); ctx.lineWidth = 0.8; ctx.globalAlpha = vis.length > 40 ? 0.28 : 0.45; ctx.lineJoin = "round";
    rows.forEach((d, r) => { if (vis[r] !== focus) path(d); });
    ctx.globalAlpha = 1;
    const fr = vis.indexOf(focus);
    if (fr >= 0) { ctx.strokeStyle = css("--ref"); ctx.lineWidth = 1.3; path(rows[fr]); }
    // a dot at the peak of every visible-difference span; the highlighted channel's dots are larger
    const dots = [];
    const dot = (d, c, big) => {
      for (const s of d.spans) {
        const x = xs(s[3]), y = ys(s[4]);
        dots.push({ x, y, c, s });
        ctx.beginPath(); ctx.arc(x, y, big ? 4.2 : 2.8, 0, 2 * Math.PI);
        ctx.fillStyle = css("--diff"); ctx.globalAlpha = big ? 1 : 0.85; ctx.fill();
        if (big) { ctx.lineWidth = 1.5; ctx.strokeStyle = css("--surface"); ctx.stroke(); }
      }
    };
    rows.forEach((d, r) => { if (vis[r] !== focus) dot(d, vis[r], false); });
    if (fr >= 0) dot(rows[fr], focus, true);
    ctx.restore();
    // frame, threshold label and the name of the highlighted channel
    ctx.strokeStyle = css("--line-strong"); ctx.lineWidth = 1; ctx.strokeRect(box.x + 0.5, box.y + 0.5, box.w - 1, box.h - 1);
    // threshold key above the plot's right edge, where no data line can cover it
    ctx.font = `11px ${css("--mono")}`; ctx.fillStyle = css("--text-2"); ctx.textAlign = "right"; ctx.textBaseline = "bottom";
    const key = `visible from ${+thr.toPrecision(2)} µV`, kw = ctx.measureText(key).width;
    ctx.fillText(key, box.x + box.w, box.y - 8);
    ctx.save(); ctx.strokeStyle = css("--text-2"); ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(box.x + box.w - kw - 28, box.y - 13.5); ctx.lineTo(box.x + box.w - kw - 6, box.y - 13.5); ctx.stroke(); ctx.restore();
    if (fr >= 0) {
      ctx.font = `600 11.5px ${css("--mono")}`; ctx.fillStyle = css("--ref"); ctx.textAlign = "left"; ctx.textBaseline = "bottom";
      ctx.fillText(`${names[focus]} · ${focus === this.sel ? "selected" : "most different"} · ${rows[fr].spans.length} spans`, box.x, box.y - 8);
    }
    this.mapLayout = { kind: "line", box, dots, rows, vis, focus, xs, ys };
  }

  // Channel-by-time map: one row per visible channel, the current window on the x-axis, and the
  // share of the window with a visible difference in a column on the right.
  drawDiffRaster(vis, rows) {
    const n = vis.length, rowH = n <= 16 ? 16 : n <= 40 ? 11 : n <= 72 ? 7 : 4.5, PCT = 70, MT = 20;
    const { ctx, w, h } = setup(this.cvMap, Math.round(n * rowH + MT + AXIS));
    const { a, b } = this.data, span = b - a, nE = this.meta.ch_names.length, names = this.names;
    const box = { x: GUTTER, y: MT, w: Math.max(40, w - GUTTER - PCT), h: n * rowH };
    const xs = (t) => box.x + ((t - a) / span) * box.w;
    this.mapLayout = { kind: "map", box, rowH, vis, rows };
    ctx.fillStyle = css("--surface"); ctx.fillRect(0, 0, w, h);
    // selected row across the full width, zebra rows inside the plot so a row can be followed
    vis.forEach((c, r) => {
      const y = box.y + r * rowH;
      if (c === this.sel) { ctx.fillStyle = css("--ref-soft"); ctx.fillRect(0, y, w, rowH); }
      else if (r % 2) { ctx.fillStyle = css("--surface-2"); ctx.fillRect(box.x, y, box.w, rowH); }
    });
    // second grid and time axis, on the same steps as the traces
    const step = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60].find((s) => span / s <= Math.max(4, box.w / 90)) || 60;
    ctx.strokeStyle = css("--grid"); ctx.lineWidth = 1;
    ctx.font = `11px ${css("--mono")}`; ctx.fillStyle = css("--text-3"); ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (let t = Math.ceil(a / step) * step; t <= b + 1e-9; t += step) {
      const x = Math.round(xs(t)) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, box.y); ctx.lineTo(x, box.y + box.h); ctx.stroke();
      ctx.fillText(fmtT(t, step < 1), x, box.y + box.h + 6);
    }
    // difference spans
    const pad = rowH > 6 ? 1.5 : 0.5;
    ctx.fillStyle = css("--diff");
    rows.forEach((d, r) => {
      const y = box.y + r * rowH + pad;
      for (const [t0, t1] of d.spans) { const x0 = xs(t0); ctx.fillRect(x0, y, Math.max(1.5, xs(t1) - x0), rowH - 2 * pad); }
    });
    // events
    if (this.events) {
      ctx.save(); ctx.strokeStyle = css("--bio"); ctx.setLineDash([3, 3]); ctx.globalAlpha = 0.8;
      for (const ev of this.meta.annotations) {
        if (ev.t < a || ev.t > b) continue;
        const x = Math.round(xs(ev.t)) + 0.5;
        ctx.beginPath(); ctx.moveTo(x, box.y); ctx.lineTo(x, box.y + box.h); ctx.stroke();
      }
      ctx.restore();
    }
    // frame, and a line between EEG and auxiliary channels
    ctx.strokeStyle = css("--line-strong"); ctx.lineWidth = 1;
    ctx.strokeRect(box.x + 0.5, box.y + 0.5, box.w - 1, box.h - 1);
    const firstAux = vis.findIndex((c) => c >= nE);
    if (firstAux > 0) { const y = Math.round(box.y + firstAux * rowH) + 0.5; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
    // channel labels
    const every = Math.max(1, Math.ceil(9 / rowH));
    ctx.font = `${rowH >= 11 ? 11 : 9.5}px ${css("--mono")}`; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    vis.forEach((c, r) => {
      if (r % every && c !== this.sel) return;
      ctx.fillStyle = c === this.sel ? css("--ref") : c >= nE ? css("--text-3") : css("--text-2");
      ctx.fillText(names[c], GUTTER - 8, box.y + (r + 0.5) * rowH);
    });
    // share of the window with a visible difference, per channel
    const cx = box.x + box.w + 8, barW = PCT - 40;
    rows.forEach((d, r) => {
      const y = box.y + r * rowH + pad;
      ctx.fillStyle = css("--surface-3"); ctx.fillRect(cx, y, barW, rowH - 2 * pad);
      ctx.fillStyle = css("--diff"); ctx.fillRect(cx, y, barW * d.frac, rowH - 2 * pad);
      if (rowH >= 11 || vis[r] === this.sel) {
        ctx.fillStyle = vis[r] === this.sel ? css("--ref") : css("--text-2"); ctx.textAlign = "left";
        ctx.fillText(`${Math.round(d.frac * 100)}%`, cx + barW + 4, box.y + (r + 0.5) * rowH);
      }
    });
    ctx.font = `11px ${css("--mono")}`; ctx.fillStyle = css("--text-3"); ctx.textAlign = "left"; ctx.textBaseline = "bottom";
    ctx.fillText("% window", cx, box.y - 5);
    ctx.textAlign = "right"; ctx.fillText("channel", GUTTER - 8, box.y - 5);
  }

  // Line graph: the dot nearest to the pointer (within 9 px), else the highlighted channel at
  // the pointer's time.
  lineLocate(e) {
    const L = this.mapLayout, r = this.cvMap.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top, { box } = L;
    let best = null, bd = 81;
    for (const d of L.dots) { const q = (d.x - x) ** 2 + (d.y - y) ** 2; if (q < bd) { bd = q; best = d; } }
    if (best) return { x, y, dot: best };
    if (x < box.x || x > box.x + box.w || y < box.y || y > box.y + box.h || L.focus == null) return null;
    const d = L.rows[L.vis.indexOf(L.focus)], p = Math.min(d.gap.length - 1, Math.floor(((x - box.x) / box.w) * d.gap.length));
    return { x, y, c: L.focus, d, t: this.data.a + ((x - box.x) / box.w) * (this.data.b - this.data.a), gap: d.gap[p] };
  }

  lineTip(l) {
    if (l.dot) {
      const s = l.dot.s;
      return `<b>${this.names[l.dot.c]}</b> · ${s[3].toFixed(2)} s · gap ${s[4].toFixed(1)} µV<br><span class="rd">difference visible ${s[0].toFixed(2)}–${s[1].toFixed(2)} s</span>`;
    }
    const seen = l.gap >= l.d.thr;
    return `<b>${this.names[l.c]}</b> · ${l.t.toFixed(2)} s · gap ${l.gap.toFixed(1)} µV<br>`
      + (seen ? `<span class="rd">visible at this scale</span>` : "below the visible threshold");
  }

  mapLocate(e) {
    if (!this.mapLayout || !this.data) return null;
    if (this.mapLayout.kind === "line") return this.lineLocate(e);
    const { box, rowH, vis, rows } = this.mapLayout, r = this.cvMap.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top, row = Math.floor((y - box.y) / rowH);
    if (row < 0 || row >= vis.length) return null;
    const inPlot = x >= box.x && x <= box.x + box.w;
    const t = this.data.a + ((x - box.x) / box.w) * (this.data.b - this.data.a);
    return { x, y, row, c: vis[row], d: rows[row], t: inPlot ? t : null };
  }

  mapHover(e) {
    const l = this.mapLocate(e), tip = $("diffTip");
    if (!l) { tip.hidden = true; this.cvMap.style.cursor = "default"; return; }
    if (this.mapLayout.kind === "line") {
      tip.innerHTML = this.lineTip(l);
      this.cvMap.style.cursor = l.dot ? "pointer" : "crosshair";
    } else {
      const { d, t } = l, name = this.names[l.c];
      const hit = t == null ? null : d.spans.find(([t0, t1]) => t >= t0 && t <= t1);
      const where = t == null ? "" : ` · ${t.toFixed(2)} s`;
      const what = hit
        ? `<span class="rd">difference visible ${hit[0].toFixed(2)}–${hit[1].toFixed(2)} s</span>`
        : d.spans.length
          ? `${d.spans.length} ${d.spans.length === 1 ? "span" : "spans"} · ${d.total.toFixed(2)} s (${Math.round(d.frac * 100)}%) in this window`
          : "no visible difference in this window";
      tip.innerHTML = `<b>${name}</b>${where}<br>${what}`;
      this.cvMap.style.cursor = "pointer";
    }
    tip.hidden = false;
    const host = this.cvMap.parentElement.getBoundingClientRect(), cr = this.cvMap.getBoundingClientRect();
    tip.style.left = `${Math.max(4, Math.min(cr.left - host.left + l.x + 14, host.width - tip.offsetWidth - 4))}px`;
    tip.style.top = `${cr.top - host.top + l.y + 14}px`;
  }

  mapClick(e) {
    const l = this.mapLocate(e); if (!l) return;
    const c = l.dot ? l.dot.c : l.c;
    if (this.mapLayout.kind === "line" && !l.dot) return;
    this.sel = this.sel === c ? null : c; this.render();
    if (this.sel != null && this.sel < this.meta.ch_names.length) this.onSelect?.(this.sel);
  }

  draw(cv, src, vis, H) {
    const { ctx, w, h } = setup(cv, H);
    const { arr, C, px, a, b } = this.data;
    const box = { x: GUTTER, y: TOP, w: w - GUTTER - 8, h: h - TOP - AXIS };
    const rowH = box.h / vis.length, nE = this.meta.ch_names.length;
    this.layout = { box, rowH, vis, px };
    const span = b - a, xs = (t) => box.x + ((t - a) / span) * box.w;
    ctx.fillStyle = css("--surface"); ctx.fillRect(0, 0, w, h);
    // second grid
    const step = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60].find((s) => span / s <= Math.max(4, box.w / 90)) || 60;
    ctx.strokeStyle = css("--grid"); ctx.lineWidth = 1;
    ctx.font = `11px ${css("--mono")}`; ctx.fillStyle = css("--text-3"); ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (let t = Math.ceil(a / step) * step; t <= b + 1e-9; t += step) {
      const x = Math.round(xs(t)) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, box.y); ctx.lineTo(x, box.y + box.h); ctx.stroke();
      ctx.fillText(fmtT(t, step < 1), x, box.y + box.h + 6);
    }
    // selected row and auxiliary separator
    vis.forEach((c, r) => {
      if (c === this.sel) {
        ctx.fillStyle = css("--ref-soft"); ctx.fillRect(box.x, box.y + r * rowH, box.w, rowH);
        const d = this.diffSpans(c);
        if (d && d.spans.length) {
          const y = box.y + r * rowH;
          ctx.save();
          for (const [t0, t1] of d.spans) {
            const x0 = xs(t0), x1 = Math.max(xs(t1), x0 + 1);
            ctx.fillStyle = css("--diff-soft"); ctx.fillRect(x0, y, x1 - x0, rowH);
            ctx.fillStyle = css("--diff");
            ctx.fillRect(x0, y, Math.max(1, Math.min(1.5, x1 - x0)), rowH);
            ctx.fillRect(x1 - Math.max(1, Math.min(1.5, x1 - x0)), y, Math.max(1, Math.min(1.5, x1 - x0)), rowH);
          }
          // label the longest span inside the row when there is space for it
          const [t0, t1] = d.spans[0], x0 = xs(t0), x1 = xs(t1);
          const txt = `${t0.toFixed(2)}–${t1.toFixed(2)} s`;
          ctx.font = `600 10.5px ${css("--mono")}`;
          if (rowH >= 13 && x1 - x0 > ctx.measureText(txt).width + 10) {
            ctx.fillStyle = css("--diff"); ctx.textAlign = "center"; ctx.textBaseline = "top";
            ctx.fillText(txt, (x0 + x1) / 2, y + 1.5);
          }
          ctx.restore();
        }
      }
      if (c === nE && r > 0) { ctx.strokeStyle = css("--line-strong"); ctx.beginPath(); ctx.moveTo(0, box.y + r * rowH); ctx.lineTo(w, box.y + r * rowH); ctx.stroke(); }
    });
    // events
    if (this.events) {
      ctx.save(); ctx.strokeStyle = css("--bio"); ctx.fillStyle = css("--bio"); ctx.setLineDash([3, 3]); ctx.globalAlpha = 0.8;
      ctx.font = `10.5px ${css("--mono")}`; ctx.textAlign = "left"; ctx.textBaseline = "top";
      let lastX = -1e9;
      for (const ev of this.meta.annotations) {
        if (ev.t < a || ev.t > b) continue;
        const x = Math.round(xs(ev.t)) + 0.5;
        ctx.beginPath(); ctx.moveTo(x, box.y); ctx.lineTo(x, box.y + box.h); ctx.stroke();
        if (x - lastX > 60) { ctx.fillText(ev.label.slice(0, 14), x + 3, 1); lastX = x; }
      }
      ctx.restore();
    }
    // traces
    ctx.save(); ctx.beginPath(); ctx.rect(box.x, box.y - 2, box.w, box.h + 4); ctx.clip();
    const k = rowH / this.scale, dx = box.w / px;
    const trace = (s, color, alpha, width) => {
      ctx.globalAlpha = alpha; ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineJoin = "round"; ctx.beginPath();
      vis.forEach((c, r) => {
        const y0 = box.y + (r + 0.5) * rowH;
        for (let p = 0; p < px; p++) {
          const x = box.x + (p + 0.5) * dx, o = ((s * C + c) * px + p) * 2;
          const ylo = y0 - arr[o] * k, yhi = y0 - arr[o + 1] * k;
          p ? ctx.lineTo(x, ylo) : ctx.moveTo(x, ylo); ctx.lineTo(x, yhi);
        }
      });
      ctx.stroke(); ctx.globalAlpha = 1;
    };
    const lw = rowH < 8 ? 0.8 : 1.1;
    if (src === "overlay") {
      // shade between the input and output envelopes: the thicker the band, the larger the change
      ctx.save(); ctx.fillStyle = css("--diff-soft");
      vis.forEach((c, r) => {
        const y0 = box.y + (r + 0.5) * rowH;
        ctx.beginPath();
        for (let p = 0; p < px; p++) {
          const x = box.x + (p + 0.5) * dx, o = ((0 * C + c) * px + p) * 2;
          const v = (arr[o] + arr[o + 1]) / 2;
          p ? ctx.lineTo(x, y0 - v * k) : ctx.moveTo(x, y0 - v * k);
        }
        for (let p = px - 1; p >= 0; p--) {
          const x = box.x + (p + 0.5) * dx, o = ((1 * C + c) * px + p) * 2;
          ctx.lineTo(x, y0 - ((arr[o] + arr[o + 1]) / 2) * k);
        }
        ctx.closePath(); ctx.fill();
      });
      ctx.restore();
      trace(0, css("--in"), 0.75, lw); trace(1, css("--out"), 1, lw + 0.2);
    }
    else if (src === "diff") {
      ctx.strokeStyle = css("--bio"); ctx.lineWidth = lw; ctx.beginPath();
      vis.forEach((c, r) => {
        const y0 = box.y + (r + 0.5) * rowH;
        for (let p = 0; p < px; p++) {
          const oi = ((0 * C + c) * px + p) * 2, oo = ((1 * C + c) * px + p) * 2;
          const v = (arr[oo] + arr[oo + 1]) / 2 - (arr[oi] + arr[oi + 1]) / 2, x = box.x + (p + 0.5) * dx;
          p ? ctx.lineTo(x, y0 - v * k) : ctx.moveTo(x, y0 - v * k);
        }
      });
      ctx.stroke();
    } else trace(src, src === 0 ? css("--in") : css("--out"), 1, lw);
    ctx.restore();
    // labels
    const names = this.names, every = Math.max(1, Math.ceil(9 / rowH));
    ctx.font = `${rowH >= 12 ? 11 : 9.5}px ${css("--mono")}`; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    vis.forEach((c, r) => {
      if (r % every && c !== this.sel) return;
      ctx.fillStyle = c === this.sel ? css("--ref") : c >= nE ? css("--text-3") : css("--text-2");
      ctx.fillText(names[c], GUTTER - 8, box.y + (r + 0.5) * rowH);
    });
    // scale bar
    const sb = Math.min(rowH * 1, box.h / 3), x0 = box.x + box.w - 14, y1 = box.y + box.h - 6;
    ctx.strokeStyle = css("--text"); ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x0, y1); ctx.lineTo(x0, y1 - sb); ctx.stroke();
    ctx.fillStyle = css("--text-2"); ctx.font = `10.5px ${css("--mono")}`; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    ctx.fillText(`${+(this.scale * sb / rowH).toPrecision(2)} µV`, x0 - 5, y1 - sb / 2);
  }

  // Time spans in the current window where input and output are far enough apart to be told
  // apart on screen: the two traces are separated by at least VIS_PX pixels at the amplitude
  // scale now in use, or their min/max envelopes do not overlap at all. Short runs are
  // dropped and 1-px gaps are bridged.
  diffSpans(c) {
    if (!this.data || c == null) return null;
    const { arr, C, px, a, b } = this.data, dt = (b - a) / px;
    const rowH = this.layout?.rowH ?? this.rowHeight(this.visible().length);
    const k = rowH / this.scale;                       // pixels per microvolt, as drawn
    const VIS_PX = 1.5;
    const hit = new Uint8Array(px), gap = new Float32Array(px);
    let apart = 0, worst = 0, worstP = 0;
    for (let p = 0; p < px; p++) {
      const oi = ((0 * C + c) * px + p) * 2, oo = ((1 * C + c) * px + p) * 2;
      const iLo = arr[oi], iHi = arr[oi + 1], oLo = arr[oo], oHi = arr[oo + 1];
      const envGap = oLo > iHi ? oLo - iHi : oHi < iLo ? iLo - oHi : 0;
      const midGap = Math.abs((oLo + oHi) / 2 - (iLo + iHi) / 2);
      const sep = Math.max(envGap, midGap);
      gap[p] = sep;
      if (sep * k >= VIS_PX) { hit[p] = 1; apart++; if (sep > worst) { worst = sep; worstP = p; } }
    }
    // each span: [start s, end s, length px, time of its largest gap, that gap in µV]
    const spans = [];
    for (let p = 0; p < px; p++) {
      if (!hit[p]) continue;
      let q = p;
      while (q + 1 < px && (hit[q + 1] || (q + 2 < px && hit[q + 2]))) q++;
      if (q - p >= 1) {
        let m = p;
        for (let i = p + 1; i <= q; i++) if (gap[i] > gap[m]) m = i;
        spans.push([a + p * dt, a + (q + 1) * dt, q - p + 1, a + (m + 0.5) * dt, gap[m]]);
      }
      p = q;
    }
    spans.sort((u, v) => v[2] - u[2]);
    return { spans, frac: apart / px, worstT: a + (worstP + 0.5) * dt, worstGap: worst,
             total: spans.reduce((t, sp) => t + (sp[1] - sp[0]), 0), gap, thr: VIS_PX / k };
  }

  locate(e, cvKey) {
    if (!this.layout || !this.data) return null;
    const r = this.cv[cvKey].getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const { box, rowH, vis, px } = this.layout;
    if (x < box.x || x > box.x + box.w || y < box.y || y > box.y + box.h) return null;
    const p = Math.min(px - 1, Math.floor(((x - box.x) / box.w) * px)), row = Math.min(vis.length - 1, Math.floor((y - box.y) / rowH));
    return { x, y, p, c: vis[row], t: this.data.a + ((x - box.x) / box.w) * (this.data.b - this.data.a) };
  }

  hover(e, cvKey) {
    const l = this.locate(e, cvKey), ro = $("readout");
    if (!l) return this.unhover();
    const { arr, C, px } = this.data, mid = (s) => { const o = ((s * C + l.c) * px + l.p) * 2; return (arr[o] + arr[o + 1]) / 2; };
    ro.innerHTML = `<b>${this.names[l.c]}</b> · ${l.t.toFixed(3)} s<br><span class="ri">in ${mid(0).toFixed(1)} µV</span> · <span class="ro">out ${mid(1).toFixed(1)} µV</span>`;
    ro.hidden = false;
    const host = $("plots").getBoundingClientRect(), cr = this.cv[cvKey].getBoundingClientRect();
    const left = cr.left - host.left + l.x + 14;
    ro.style.left = `${Math.min(left, host.width - 210)}px`; ro.style.top = `${cr.top - host.top + l.y + 12}px`;
    for (const k of ["in", "out"]) {
      const hcv = this.cv[k];
      if (hcv.offsetParent === null) continue;
      this.hair[k].style.display = "block"; this.hair[k].style.left = `${l.x}px`; this.hair[k].style.height = `${hcv.clientHeight - AXIS}px`;
    }
  }

  unhover() { $("readout").hidden = true; for (const k in this.hair) this.hair[k].style.display = "none"; }

  click(e, cvKey) {
    const l = this.locate(e, cvKey); if (!l) return;
    this.sel = this.sel === l.c ? null : l.c; this.render();
    if (this.sel != null && this.sel < this.meta.ch_names.length) this.onSelect?.(this.sel);
  }

  timeline() {
    if (!this.analysis) return;
    drawTimeline($("cvTimeline"), this.analysis.env_in, this.analysis.env_out, this.duration, this.t0,
      Math.min(this.duration, this.t0 + this.dur), this.meta.annotations, this.events);
  }
}

export function fmtT(t, frac = false) {
  if (t >= 60) { const m = Math.floor(t / 60), s = t - m * 60; return `${m}:${(frac ? s.toFixed(1) : Math.round(s).toString()).padStart(frac ? 4 : 2, "0")}`; }
  return `${frac ? t.toFixed(1) : Math.round(t * 10) / 10} s`;
}
