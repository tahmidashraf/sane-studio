// Client for the SANE engine. The engine runs in a Web Worker in this browser (js/worker.js),
// so recordings are processed on this computer and never uploaded. Same interface as the
// server version's client, plus downloadAll/downloadOne, which save files from memory.
const worker = new Worker(new URL("./worker.js", import.meta.url));
const pending = new Map();
const mirror = {};               // latest public state of each session, kept current during a run
let seq = 0;

function setStatus(text, { ready = false, failed = false } = {}) {
  const el = document.getElementById("engineStatus");
  if (!el) return;
  el.textContent = text;
  el.dataset.state = failed ? "failed" : ready ? "ready" : "loading";
}
setStatus("Preparing the SANE engine in this browser. The first visit downloads about 50 MB; later visits load from the browser cache.");

worker.onmessage = (e) => {
  const m = e.data;
  if (m.type === "status") return setStatus(m.text, m);
  if (m.type === "progress") { mirror[m.job.id] = m.job; return; }
  if (m.type === "fraction") { pending.get(m.id)?.onProgress?.(m.value); return; }
  const p = pending.get(m.id);
  if (!p) return;
  pending.delete(m.id);
  m.error ? p.reject(new Error(m.error)) : p.resolve(m.result);
};
worker.onerror = (e) => setStatus(`The SANE engine stopped: ${e.message || "unknown error"}`, { failed: true });

function call(op, args = [], onProgress) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject, onProgress });
    worker.postMessage({ id, op, args });
  });
}
const remember = (job) => (mirror[job.id] = job);

function save(bytes, name) {
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

export const api = {
  samples: async () => (await fetch("samples/index.json")).json(),
  async fromSample(id) {
    const s = (await api.samples()).find((x) => x.id === id);
    if (!s) throw new Error("Unknown sample dataset.");
    const files = await Promise.all(s.names.map(async (n) => {
      const r = await fetch(`samples/${id}/${n}`);
      if (!r.ok) throw new Error(`Could not load the sample file ${n}.`);
      return new File([await r.blob()], n);
    }));
    return api.upload(files);
  },
  upload: (files, onProgress) => call("upload", [null, files, false], onProgress).then(remember),
  addFiles: (id, files, onProgress) => call("upload", [id, files, true], onProgress).then(remember),
  // While a run is in progress the worker is busy computing; its progress messages keep the
  // mirror current, so polling reads the mirror instead of waiting in the worker's queue.
  job: (id) => (mirror[id]?.state === "processing" ? Promise.resolve(mirror[id]) : call("job", [id]).then(remember)),
  run(id, params) {
    const job = remember({ ...mirror[id], state: "processing", stage: "Starting", progress: 0, error: null, report: null });
    call("run", [id, params]).then(remember)
      .catch((e) => remember({ ...mirror[id], state: "error", error: e.message }));
    return Promise.resolve(job);
  },
  remove: (id) => { delete mirror[id]; return call("remove", [id]); },
  meta: (id, rid) => call("meta", [id, rid]),
  analysis: (id, rid) => call("analysis", [id, rid]),
  spatial: (id, rid, band) => call("spatial", [id, rid, band]),
  window: (id, rid, opts) => call("window", [id, rid, opts]),
  async downloadAll(id) { const r = await call("downloadAll", [id]); save(r.bytes, r.name); },
  async downloadOne(id, rid) { const r = await call("downloadOne", [id, rid]); save(r.bytes, r.name); },
};
