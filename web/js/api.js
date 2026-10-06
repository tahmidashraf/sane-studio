// Thin client for the SANE Studio API.
async function json(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try { detail = (await r.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return r.json();
}

function uploadXhr(url, files, onProgress) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
      xhr.status < 300 ? resolve(body) : reject(new Error(body?.detail || `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("Network error during upload"));
    xhr.send(fd);
  });
}

export const api = {
  samples: () => json("/api/samples"),
  fromSample: (id) => json(`/api/samples/${id}`, { method: "POST" }),
  upload: (files, onProgress) => uploadXhr("/api/jobs", files, onProgress),
  addFiles: (id, files, onProgress) => uploadXhr(`/api/jobs/${id}/files`, files, onProgress),
  job: (id) => json(`/api/jobs/${id}`),
  run: (id, params) => json(`/api/jobs/${id}/run`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params) }),
  remove: (id) => fetch(`/api/jobs/${id}`, { method: "DELETE" }),
  meta: (id, rid) => json(`/api/jobs/${id}/rec/${rid}/meta`),
  analysis: (id, rid) => json(`/api/jobs/${id}/rec/${rid}/analysis`),
  spatial: (id, rid, band) => json(`/api/jobs/${id}/rec/${rid}/spatial?band=${band}`),
  async window(id, rid, { t0, dur, px, band, aux }) {
    const q = new URLSearchParams({ t0, dur, px, band, aux });
    const r = await fetch(`/api/jobs/${id}/rec/${rid}/window?${q}`);
    if (!r.ok) throw new Error(`Could not load signal window (${r.status})`);
    const [S, C, P] = r.headers.get("X-Shape").split(",").map(Number);
    return { arr: new Float32Array(await r.arrayBuffer()), S, C, px: P,
             a: +r.headers.get("X-T0"), b: +r.headers.get("X-T1") };
  },
};
