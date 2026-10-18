// Canvas charts for SANE Studio. Every colour comes from the theme tokens.
export const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export function setup(cv, cssH, cssW) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = cssW ?? (cv.parentElement.clientWidth || 300);
  cv.style.height = `${cssH}px`;
  cv.style.width = cssW ? `${cssW}px` : "100%";
  cv.width = Math.round(w * dpr);
  cv.height = Math.round(cssH * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, cssH);
  return { ctx, w, h: cssH };
}

export const BANDS = [["delta", 1, 4, "δ"], ["theta", 4, 8, "θ"], ["alpha", 8, 13, "α"], ["beta", 13, 30, "β"], ["gamma", 30, 45, "γ"]];

function niceTicks(lo, hi, n = 5) {
  const span = hi - lo || 1;
  const step0 = span / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

function axes(ctx, box, xTicks, yTicks, xs, ys, fmtX = (v) => v, fmtY = (v) => v) {
  ctx.save();
  ctx.font = `11px ${css("--mono")}`;
  ctx.fillStyle = css("--text-3");
  ctx.strokeStyle = css("--grid");
  ctx.lineWidth = 1;
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (const v of yTicks) {
    const y = ys(v);
    ctx.beginPath(); ctx.moveTo(box.x, y); ctx.lineTo(box.x + box.w, y); ctx.stroke();
    ctx.fillText(fmtY(v), box.x - 8, y);
  }
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (const v of xTicks) {
    const x = xs(v);
    ctx.beginPath(); ctx.moveTo(x, box.y); ctx.lineTo(x, box.y + box.h); ctx.stroke();
    ctx.fillText(fmtX(v), x, box.y + box.h + 7);
  }
  ctx.restore();
}

// ------------------------------------------------------------------ spectrum
export function drawPsd(cv, freqs, inDb, outDb, { height = 300, title = "" } = {}) {
  const { ctx, w, h } = setup(cv, height);
  const box = { x: 52, y: 26, w: w - 66, h: h - 60 };
  const fmax = Math.min(freqs[freqs.length - 1], 60);
  const all = inDb.concat(outDb).filter(Number.isFinite);
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
  const xs = (f) => box.x + (f / fmax) * box.w;
  const ys = (v) => box.y + (1 - (v - lo) / (hi - lo)) * box.h;
  // band shading, with the biomarker bands emphasised
  for (const [name, a, b, sym] of BANDS) {
    if (a >= fmax) continue;
    const bio = name === "theta" || name === "beta";
    ctx.fillStyle = bio ? css("--bio-soft") : "transparent";
    ctx.fillRect(xs(a), box.y, xs(Math.min(b, fmax)) - xs(a), box.h);
    ctx.fillStyle = bio ? css("--bio") : css("--text-3");
    ctx.font = `600 11px ${css("--mono")}`;
    ctx.textAlign = "center"; ctx.textBaseline = "bottom";
    ctx.fillText(sym, (xs(a) + xs(Math.min(b, fmax))) / 2, box.y - 6);
  }
  axes(ctx, box, niceTicks(0, fmax, 6), niceTicks(lo, hi, 5), xs, ys, (v) => `${v}`, (v) => `${Math.round(v)}`);
  const line = (vals, color, width, alpha = 1) => {
    ctx.save(); ctx.globalAlpha = alpha; ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineJoin = "round";
    ctx.beginPath();
    let started = false;
    freqs.forEach((f, i) => { if (f > fmax || !Number.isFinite(vals[i])) return; const x = xs(f), y = ys(vals[i]); started ? ctx.lineTo(x, y) : (ctx.moveTo(x, y), started = true); });
    ctx.stroke(); ctx.restore();
  };
  line(inDb, css("--in"), 2, 0.9);
  line(outDb, css("--out"), 2.2);
  ctx.fillStyle = css("--text-3"); ctx.font = `11px ${css("--mono")}`;
  ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
  ctx.fillText("frequency (Hz)", box.x + box.w / 2, h - 4);
  ctx.save(); ctx.translate(13, box.y + box.h / 2); ctx.rotate(-Math.PI / 2);
  ctx.fillText("power (dB re 1 µV²/Hz)", 0, 0); ctx.restore();
  if (title) { ctx.textAlign = "left"; ctx.fillStyle = css("--text-2"); ctx.fillText(title, box.x + 6, box.y + 14); }
}

// ------------------------------------------------------------------ band-power distributions
// For each band: the spread over channels (5-95% whisker, inter-quartile box, median tick) for
// input and output, the reference level, and an arrow for the median shift. Far more of the data
// than a bar of means, and it shows whether the whole montage moved or only a few channels.
function quantiles(v) {
  const a = v.filter(Number.isFinite).sort((x, y) => x - y);
  if (!a.length) return null;
  const q = (f) => { const i = f * (a.length - 1), lo = Math.floor(i), hi = Math.ceil(i); return a[lo] + (a[hi] - a[lo]) * (i - lo); };
  return { p05: q(0.05), p25: q(0.25), p50: q(0.5), p75: q(0.75), p95: q(0.95), n: a.length };
}

export function drawBandShift(cv, bands, inCols, outCols, refCols, { height = 300, yLabel = "relative power" } = {}) {
  const { ctx, w, h } = setup(cv, height);
  const box = { x: 54, y: 20, w: w - 66, h: h - 74 };
  const qin = inCols.map(quantiles), qout = outCols.map(quantiles);
  const refs = refCols.map((c) => quantiles(c)?.p50 ?? 0);
  const all = qin.concat(qout).filter(Boolean).flatMap((q) => [q.p05, q.p95]).concat(refs);
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.12 || 0.05; lo = Math.max(0, lo - pad); hi += pad;
  const ys = (v) => box.y + (1 - (v - lo) / (hi - lo)) * box.h;
  const slot = box.w / bands.length;
  // biomarker bands carry a soft backdrop, as in the spectrum
  bands.forEach(([name], i) => {
    if (name !== "theta" && name !== "beta") return;
    ctx.fillStyle = css("--bio-soft");
    ctx.fillRect(box.x + slot * i, box.y, slot, box.h);
  });
  axes(ctx, box, [], niceTicks(lo, hi, 5), () => 0, ys, () => "", (v) => v.toFixed(2));
  const halfBox = Math.min(17, slot * 0.17);
  bands.forEach(([name, sym], i) => {
    const cx = box.x + slot * (i + 0.5);
    const pos = [cx - slot * 0.19, cx + slot * 0.19];
    [[qin[i], css("--in")], [qout[i], css("--out")]].forEach(([q, color], k) => {
      if (!q) return;
      const x = pos[k];
      ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(x, ys(q.p05)); ctx.lineTo(x, ys(q.p95)); ctx.stroke();      // whisker
      for (const v of [q.p05, q.p95]) { ctx.beginPath(); ctx.moveTo(x - 4, ys(v)); ctx.lineTo(x + 4, ys(v)); ctx.stroke(); }
      ctx.globalAlpha = 0.26;
      ctx.fillRect(x - halfBox / 2, ys(q.p75), halfBox, Math.max(1, ys(q.p25) - ys(q.p75)));  // IQR
      ctx.globalAlpha = 1;
      ctx.strokeRect(x - halfBox / 2, ys(q.p75), halfBox, Math.max(1, ys(q.p25) - ys(q.p75)));
      ctx.lineWidth = 2.4;
      ctx.beginPath(); ctx.moveTo(x - halfBox / 2, ys(q.p50)); ctx.lineTo(x + halfBox / 2, ys(q.p50)); ctx.stroke();
    });
    // median shift, drawn as an arrow from input to output
    if (qin[i] && qout[i]) {
      const y0 = ys(qin[i].p50), y1 = ys(qout[i].p50), xm = cx;
      if (Math.abs(y1 - y0) > 2) {
        ctx.strokeStyle = css("--diff"); ctx.fillStyle = css("--diff"); ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.moveTo(pos[0] + halfBox / 2 + 2, y0); ctx.lineTo(xm, y0);
        ctx.lineTo(xm, y1); ctx.lineTo(pos[1] - halfBox / 2 - 6, y1); ctx.stroke();
        const dir = Math.sign(y1 - y0) || 1, tipX = pos[1] - halfBox / 2 - 2;
        ctx.beginPath(); ctx.moveTo(tipX, y1); ctx.lineTo(tipX - 5, y1 - 3.2); ctx.lineTo(tipX - 5, y1 + 3.2); ctx.closePath(); ctx.fill();
        const d = qout[i].p50 - qin[i].p50;
        ctx.font = `600 10.5px ${css("--mono")}`; ctx.textAlign = "center"; ctx.textBaseline = dir > 0 ? "bottom" : "top";
        ctx.fillText(`${d > 0 ? "+" : ""}${d.toFixed(3)}`, xm, y0 - dir * 3);
      }
    }
    // reference level
    ctx.strokeStyle = css("--ref"); ctx.lineWidth = 1.6; ctx.setLineDash([5, 3]);
    ctx.beginPath(); ctx.moveTo(cx - slot * 0.34, ys(refs[i])); ctx.lineTo(cx + slot * 0.34, ys(refs[i])); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = css("--text-2"); ctx.font = `600 13px ${css("--mono")}`;
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillText(sym, cx, box.y + box.h + 7);
    ctx.fillStyle = css("--text-3"); ctx.font = `10.5px ${css("--mono")}`;
    ctx.fillText(name, cx, box.y + box.h + 23);
  });
  // legend
  let lx = box.x;
  ctx.font = `11px ${css("--mono")}`; ctx.textBaseline = "middle"; ctx.textAlign = "left";
  const items = [["input", css("--in")], ["SANE output", css("--out")], ["reference", css("--ref")], ["median shift", css("--diff")]];
  for (const [label, color] of items) {
    ctx.fillStyle = color; ctx.fillRect(lx, h - 11, 12, 7);
    ctx.fillStyle = css("--text-2"); ctx.fillText(label, lx + 17, h - 7);
    lx += ctx.measureText(label).width + 44;
  }
  if (yLabel) { ctx.save(); ctx.translate(13, box.y + box.h / 2); ctx.rotate(-Math.PI / 2); ctx.textAlign = "center"; ctx.fillStyle = css("--text-3"); ctx.fillText(yLabel, 0, 0); ctx.restore(); }
  const nch = qin[0]?.n ?? 0;
  ctx.fillStyle = css("--text-3"); ctx.font = `10.5px ${css("--mono")}`; ctx.textAlign = "right"; ctx.textBaseline = "top";
  ctx.fillText(`box: IQR · whisker: 5-95% · ${nch} channels`, box.x + box.w, box.y - 14);
}

// ------------------------------------------------------------------ grouped bars
export function drawGroupedBars(cv, cats, groups, { height = 280, fmt = (v) => v.toFixed(2), yLabel = "" } = {}) {
  const { ctx, w, h } = setup(cv, height);
  const box = { x: 52, y: 18, w: w - 64, h: h - 62 };
  const hi = Math.max(...groups.flatMap((g) => g.values)) * 1.15 || 1;
  const ys = (v) => box.y + (1 - v / hi) * box.h;
  axes(ctx, box, [], niceTicks(0, hi, 4), () => 0, ys, () => "", (v) => fmt(v));
  const slot = box.w / cats.length, bw = Math.min(26, (slot * 0.72) / groups.length);
  cats.forEach((c, i) => {
    const cx = box.x + slot * (i + 0.5);
    groups.forEach((g, k) => {
      const x = cx - (groups.length * bw) / 2 + k * bw;
      ctx.fillStyle = g.color;
      const y = ys(g.values[i]);
      ctx.beginPath(); ctx.roundRect(x + 1, y, bw - 2, box.y + box.h - y, [3, 3, 0, 0]); ctx.fill();
    });
    ctx.fillStyle = css("--text-2"); ctx.font = `600 12px ${css("--mono")}`;
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillText(c, cx, box.y + box.h + 8);
  });
  let lx = box.x;
  ctx.font = `11px ${css("--mono")}`; ctx.textBaseline = "middle"; ctx.textAlign = "left";
  for (const g of groups) {
    ctx.fillStyle = g.color; ctx.fillRect(lx, h - 12, 12, 8);
    ctx.fillStyle = css("--text-2"); ctx.fillText(g.label, lx + 17, h - 8);
    lx += ctx.measureText(g.label).width + 40;
  }
  if (yLabel) { ctx.save(); ctx.translate(12, box.y + box.h / 2); ctx.rotate(-Math.PI / 2); ctx.textAlign = "center"; ctx.fillStyle = css("--text-3"); ctx.fillText(yLabel, 0, 0); ctx.restore(); }
}

// ------------------------------------------------------------------ per-channel bars
export function drawChannelBars(cv, vin, vout, ref, { height = 240 } = {}) {
  const { ctx, w, h } = setup(cv, height);
  const box = { x: 44, y: 12, w: w - 54, h: h - 34 };
  const all = vin.concat(vout, [ref]).filter(Number.isFinite);
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.1 || 0.1; lo -= pad; hi += pad;
  const ys = (v) => box.y + (1 - (v - lo) / (hi - lo)) * box.h;
  axes(ctx, box, [], niceTicks(lo, hi, 4), () => 0, ys, () => "", (v) => v.toFixed(1));
  const n = vin.length, slot = box.w / n, bw = Math.max(1, slot * 0.38);
  const y0 = ys(Math.max(lo, Math.min(hi, ref)));
  for (let i = 0; i < n; i++) {
    const x = box.x + slot * i + slot * 0.1;
    ctx.fillStyle = css("--in"); ctx.globalAlpha = 0.75;
    ctx.fillRect(x, Math.min(y0, ys(vin[i])), bw, Math.abs(ys(vin[i]) - y0) || 1);
    ctx.globalAlpha = 1; ctx.fillStyle = css("--out");
    ctx.fillRect(x + bw, Math.min(y0, ys(vout[i])), bw, Math.abs(ys(vout[i]) - y0) || 1);
  }
  ctx.strokeStyle = css("--ref"); ctx.lineWidth = 1.5; ctx.setLineDash([5, 4]);
  ctx.beginPath(); ctx.moveTo(box.x, y0); ctx.lineTo(box.x + box.w, y0); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = css("--ref"); ctx.font = `11px ${css("--mono")}`; ctx.textAlign = "right"; ctx.textBaseline = "bottom";
  ctx.fillText(`reference ${ref.toFixed(2)}`, box.x + box.w, y0 - 3);
  ctx.fillStyle = css("--text-3"); ctx.textAlign = "center"; ctx.textBaseline = "top";
  ctx.fillText(`${n} EEG channels`, box.x + box.w / 2, box.y + box.h + 8);
}

// ------------------------------------------------------------------ colour maps
const VIRIDIS = [[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]];
export function viridis(t) {
  t = Math.max(0, Math.min(1, t));
  const s = t * (VIRIDIS.length - 1), i = Math.min(VIRIDIS.length - 2, Math.floor(s)), f = s - i;
  return VIRIDIS[i].map((c, k) => Math.round(c + f * (VIRIDIS[i + 1][k] - c)));
}
export function diverging(t) { // t in [-1, 1]
  t = Math.max(-1, Math.min(1, t));
  const lo = [59, 111, 209], mid = [244, 246, 250], hi = [209, 68, 59];
  const a = t < 0 ? lo : hi, f = Math.abs(t);
  return mid.map((c, k) => Math.round(c + f * (a[k] - c)));
}
export const viridisCss = "linear-gradient(90deg, rgb(68,1,84), rgb(59,82,139), rgb(33,145,140), rgb(94,201,98), rgb(253,231,37))";

// ------------------------------------------------------------------ topographic map
export function drawTopo(cv, pos, vals, vmin, vmax, { size } = {}) {
  const W = size ?? Math.min(cv.parentElement.clientWidth || 260, 300);
  const { ctx } = setup(cv, W, W);
  const R = W * 0.4, cx = W / 2, cy = W / 2 + W * 0.03;
  const pts = pos.map((p, i) => (p && p[0] != null && Number.isFinite(vals[i]) ? [p[0], p[1], vals[i]] : null)).filter(Boolean);
  if (pts.length < 3) {
    ctx.fillStyle = css("--text-3"); ctx.font = `12px ${css("--sans")}`; ctx.textAlign = "center";
    ctx.fillText("No electrode positions in this file", cx, cy);
    return;
  }
  const G = 72, img = ctx.createImageData(G, G);
  for (let gy = 0; gy < G; gy++) for (let gx = 0; gx < G; gx++) {
    const x = (gx / (G - 1)) * 2 - 1, y = 1 - (gy / (G - 1)) * 2, o = (gy * G + gx) * 4;
    if (x * x + y * y > 1.0) { img.data[o + 3] = 0; continue; }
    let num = 0, den = 0, exact = null;
    for (const [px, py, v] of pts) {
      const d2 = (x - px) ** 2 + (y - py) ** 2;
      if (d2 < 1e-6) { exact = v; break; }
      const wgt = 1 / (d2 * d2); num += wgt * v; den += wgt;
    }
    const v = exact ?? num / den;
    const [r, g, b] = viridis((v - vmin) / (vmax - vmin || 1));
    img.data[o] = r; img.data[o + 1] = g; img.data[o + 2] = b; img.data[o + 3] = 255;
  }
  const off = new OffscreenCanvas(G, G); off.getContext("2d").putImageData(img, 0, 0);
  ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.clip();
  ctx.imageSmoothingEnabled = true; ctx.drawImage(off, cx - R, cy - R, 2 * R, 2 * R); ctx.restore();
  ctx.strokeStyle = css("--text-2"); ctx.lineWidth = 1.6;
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(cx - R * 0.12, cy - R * 0.99); ctx.lineTo(cx, cy - R * 1.13); ctx.lineTo(cx + R * 0.12, cy - R * 0.99); ctx.stroke();
  for (const s of [-1, 1]) { ctx.beginPath(); ctx.ellipse(cx + s * R * 1.03, cy, R * 0.06, R * 0.17, 0, 0, Math.PI * 2); ctx.stroke(); }
  ctx.fillStyle = "rgba(255,255,255,.85)"; ctx.strokeStyle = "rgba(0,0,0,.45)"; ctx.lineWidth = 0.8;
  const dot = pts.length > 80 ? 1.4 : 2.2;
  for (const [px, py] of pts) { ctx.beginPath(); ctx.arc(cx + px * R, cy - py * R, dot, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
}

// ------------------------------------------------------------------ correlation heatmap
export function drawHeatmap(cv, M) {
  const W = Math.min(cv.parentElement.clientWidth || 260, 320);
  const { ctx } = setup(cv, W, W);
  const n = M.length, img = ctx.createImageData(n, n);
  for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
    const [r, g, b] = diverging(M[i][j]), o = (i * n + j) * 4;
    img.data[o] = r; img.data[o + 1] = g; img.data[o + 2] = b; img.data[o + 3] = 255;
  }
  const off = new OffscreenCanvas(n, n); off.getContext("2d").putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, 0, 0, W, W);
  ctx.strokeStyle = css("--line-strong"); ctx.strokeRect(0.5, 0.5, W - 1, W - 1);
}

// ------------------------------------------------------------------ timeline
export function drawTimeline(cv, envIn, envOut, duration, t0, t1, events, showEvents) {
  const { ctx, w, h } = setup(cv, 54);
  ctx.fillStyle = css("--surface-2"); ctx.fillRect(0, 0, w, h);
  const n = envIn.length;
  const hi = Math.max(...envIn, ...envOut) || 1;
  const sorted = [...envIn].sort((a, b) => a - b), p95 = sorted[Math.floor(0.95 * (n - 1))] || hi;
  const cap = Math.min(hi, p95 * 2.5);
  const y = (v) => h - 4 - (Math.min(v, cap) / cap) * (h - 10);
  const area = (env, color, alpha, fill) => {
    ctx.beginPath(); ctx.moveTo(0, h);
    env.forEach((v, i) => ctx.lineTo((i / (n - 1)) * w, y(v)));
    ctx.lineTo(w, h); ctx.closePath();
    ctx.globalAlpha = alpha; ctx.fillStyle = color;
    if (fill) ctx.fill(); else { ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.stroke(); }
    ctx.globalAlpha = 1;
  };
  area(envIn, css("--in"), 0.28, true);
  area(envOut, css("--out"), 0.95, false);
  if (showEvents && events?.length) {
    ctx.fillStyle = css("--bio");
    const step = Math.max(1, Math.floor(events.length / 400));
    for (let k = 0; k < events.length; k += step) ctx.fillRect((events[k].t / duration) * w, 0, 1, 7);
  }
  const a = (t0 / duration) * w, b = (t1 / duration) * w;
  ctx.fillStyle = "rgba(127,127,127,.18)"; ctx.fillRect(a, 0, Math.max(2, b - a), h);
  ctx.strokeStyle = css("--text"); ctx.lineWidth = 1.5; ctx.strokeRect(a + 0.75, 0.75, Math.max(2, b - a) - 1.5, h - 1.5);
}
