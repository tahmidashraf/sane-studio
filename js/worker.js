// SANE engine worker: runs the Python SANE pipeline inside this browser with Pyodide.
// Recordings are read from memory and never sent anywhere. Messages: {id, op, args} in,
// {id, result} or {id, error} out, plus {type: "status" | "progress" | "fraction"} updates.
const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.28.3/full/";
importScripts(PYODIDE + "pyodide.js");

const PY_FILES = ["app/__init__.py", "app/engine.py", "app/formats.py", "app/analysis.py", "app/jobs.py",
                  "browser_api.py"];
const PY_HOME = "/home/pyodide/sane";
// Pure-Python wheels from PyPI, pinned to the versions the server uses.
const WHEELS = ["mne==1.12.1", "edfio==0.4.18", "pybv==0.8.1", "eeglabio==0.1.4", "pymatreader==1.3.2",
                "pooch", "xmltodict"];

let py, sane;
const status = (text, extra = {}) => postMessage({ type: "status", text, ...extra });
self.saneProgress = (s) => postMessage({ type: "progress", job: JSON.parse(s) });

async function boot() {
  status("Starting Python");
  py = await loadPyodide({ indexURL: PYODIDE });
  status("Loading NumPy and SciPy");
  await py.loadPackage(["numpy", "scipy", "h5py", "matplotlib", "micropip", "packaging", "jinja2", "decorator",
                        "tqdm", "lazy-loader", "platformdirs", "requests"]);
  status("Loading MNE-Python and the EEG file readers");
  const micropip = py.pyimport("micropip");
  await micropip.install.callKwargs(WHEELS, { deps: false });
  micropip.destroy();
  status("Loading the SANE engine");
  for (const f of PY_FILES) {
    const r = await fetch(new URL(`../py/${f}`, self.location));
    if (!r.ok) throw new Error(`could not load ${f} (${r.status})`);
    const path = `${PY_HOME}/${f}`;
    py.FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
    py.FS.writeFile(path, await r.text());
  }
  py.runPython(`import sys; sys.path.insert(0, "${PY_HOME}")`);
  sane = py.pyimport("browser_api");
  const v = JSON.parse(sane.versions());
  status(`Ready · runs in this browser (MNE ${v.mne})`, { ready: true });
}
const ready = boot().catch((e) => {
  status(`The SANE engine could not start: ${e.message}`, { failed: true });
  throw e;
});

// Python exceptions arrive with a traceback; the page needs only the message itself.
function message(err) {
  const lines = String(err?.message || err).trim().split("\n").filter((l) => l.trim());
  const last = lines[lines.length - 1] || "Unknown error";
  return last.replace(/^[A-Za-z_.]*(Error|Exception): /, "");
}

function bytesOf(path) {
  const data = py.FS.readFile(path);         // a copy, owned by JavaScript
  return data;
}

const OPS = {
  async upload(reqId, jobId, files, appended) {
    const id = jobId || sane.new_job();
    const paths = JSON.parse(sane.upload_paths(id, JSON.stringify(files.map((f) => f.name))));
    const total = files.reduce((s, f) => s + f.size, 0) || 1;
    let done = 0;
    for (let i = 0; i < files.length; i++) {
      py.FS.writeFile(paths[i], new Uint8Array(await files[i].arrayBuffer()));
      done += files[i].size;
      postMessage({ type: "fraction", id: reqId, value: done / total });
    }
    return JSON.parse(sane.inspect(id, Boolean(appended)));
  },
  job: (_, id) => JSON.parse(sane.job(id)),
  run: (_, id, params) => JSON.parse(sane.run(id, JSON.stringify(params))),
  remove: (_, id) => sane.remove(id),
  meta: (_, id, rid) => JSON.parse(sane.meta(id, rid)),
  analysis: (_, id, rid) => JSON.parse(sane.analysis(id, rid)),
  spatial: (_, id, rid, band) => JSON.parse(sane.spatial(id, rid, band)),
  window(_, id, rid, o) {
    const res = sane.window(id, rid, o.t0, o.dur, o.px, o.band, o.aux);
    const arrProxy = res.get(0);
    const buf = arrProxy.getBuffer("f32");
    const arr = new Float32Array(buf.data);   // copy out of the Python heap
    const [S, C, P] = buf.shape;
    const a = res.get(1), b = res.get(2);
    buf.release(); arrProxy.destroy(); res.destroy();
    return { arr, S, C, px: P, a, b };
  },
  downloadAll(_, id) {
    const r = JSON.parse(sane.download_all(id));
    return { name: r.name, bytes: bytesOf(r.path) };
  },
  downloadOne(_, id, rid) {
    const r = JSON.parse(sane.download_one(id, rid));
    return { name: r.name, bytes: bytesOf(r.path) };
  },
};

function transferables(result) {
  if (result?.arr) return [result.arr.buffer];
  if (result?.bytes) return [result.bytes.buffer];
  return [];
}

// One request at a time, in order: Python is single-threaded, and a run must finish
// before its results are read.
let queue = Promise.resolve();
self.onmessage = (e) => {
  const { id, op, args } = e.data;
  queue = queue.then(async () => {
    try {
      await ready;
      const result = await OPS[op](id, ...args);
      postMessage({ id, result }, transferables(result));
    } catch (err) {
      postMessage({ id, error: message(err) });
    }
  });
};
