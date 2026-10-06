// Multi-channel EEG viewer: input vs SANE output, with min/max decimation per pixel column.
import { css, setup, drawTimeline } from "./charts.js";

const REGIONS = [
  ["Frontal", /^(FP|AF|F\d|FZ)/], ["Central", /^(FC|C\d|CZ|CP)/], ["Temporal", /^(FT|T\d|TP)/],
  ["Parietal", /^(P\d|PZ|PO)/], ["Occipital", /^(O\d|OZ|I\d|IZ|CB)/],
];
const GUTTER = 62, AXIS = 24, TOP = 10;
const key = (n) => n.replace(/\s+/g, "").toUpperCase().replace(/^EEG/, "");
const $ = (id) => document.getElementById(id);

export class Viewer {
  constructor(api, onSelect) {
    this.api = api; this.onSelect = onSelect;
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
      if (c === this.sel) { ctx.fillStyle = css("--ref-soft"); ctx.fillRect(box.x, box.y + r * rowH, box.w, rowH); }
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
    if (src === "overlay") { trace(0, css("--in"), 0.55, lw); trace(1, css("--out"), 0.95, lw); }
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
