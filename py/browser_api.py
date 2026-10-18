"""SANE Studio in the browser: the server's job pipeline (app/jobs.py), called from a Web Worker.

The engine, the format readers and writers, the analysis and the job logic are the server's
files, unchanged. This module replaces what does not exist in a browser:
  * the web endpoints of app/main.py become plain functions that return JSON strings;
  * threads do not run in Pyodide, so a job is processed synchronously inside the worker and
    reports its progress to the page through a JavaScript callback;
  * the memory-mapped work arrays become ordinary in-memory arrays (Pyodide's file system
    lives in memory anyway, and mapping it would hold every array twice).
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

os.environ.setdefault("SANE_DATA_DIR", "/tmp/sane-studio")
os.environ.setdefault("MNE_DONTWRITE_HOME", "true")

import numpy as np  # noqa: E402

from app import formats as F  # noqa: E402
from app import jobs as J  # noqa: E402

# ---------------------------------------------------------------- in-memory work arrays
_MEM: dict[str, object] = {}


class _MemArray(np.ndarray):
    """A plain array that accepts the flush() calls made on memory maps."""

    def flush(self):
        pass


class _Format:
    def open_memmap(self, filename, mode="r+", dtype=None, shape=None, **_):
        arr = np.zeros(shape, dtype=dtype).view(_MemArray)
        _MEM[str(filename)] = arr
        return arr


class _Lib:
    format = _Format()

    def __getattr__(self, name):
        return getattr(np.lib, name)


class _NumpyInMemory:
    """Stands in for numpy inside app/jobs.py: work files are kept in _MEM instead of on disk."""

    lib = _Lib()

    def __getattr__(self, name):
        return getattr(np, name)

    def load(self, file, mmap_mode=None, **kw):
        key = str(file)
        return _MEM[key] if key in _MEM else np.load(file, **kw)

    def save(self, file, arr, **_):
        _MEM[str(file)] = np.asarray(arr)

    def savez_compressed(self, file, **arrays):
        _MEM[str(file)] = {k: np.asarray(v) for k, v in arrays.items()}


J.np = _NumpyInMemory()


# ---------------------------------------------------------------- progress to the page
def _notify(job, force=False):
    now = time.monotonic()
    if not force and now - getattr(job, "_last_note", 0.0) < 0.15:
        return
    job._last_note = now
    try:
        import js
        js.saneProgress(json.dumps(_public(job)))
    except Exception:  # noqa: BLE001  (outside a browser there is nobody to tell)
        pass


class BrowserJob(J.Job):
    def set_stage(self, stage, progress):
        super().set_stage(stage, progress)
        _notify(self)


def _public(job):
    out = job.public()
    out["expires_in_s"] = None            # nothing expires: the session lives in this tab
    return out


# ---------------------------------------------------------------- sessions
_JOBS: dict[str, BrowserJob] = {}


def _job(job_id):
    job = _JOBS.get(job_id)
    if job is None:
        raise ValueError("This session no longer exists: it ended when the page was reloaded or closed.")
    return job


def _drop(job_id):
    job = _JOBS.pop(job_id, None)
    if job is None:
        return
    prefix = str(job.dir)
    for key in [k for k in _MEM if k.startswith(prefix)]:
        del _MEM[key]
    shutil.rmtree(job.dir, ignore_errors=True)
    gc.collect()


def new_job():
    """A fresh session; one tab holds one session, so older ones are freed first."""
    for job_id in list(_JOBS):
        _drop(job_id)
    job = BrowserJob()
    _JOBS[job.id] = job
    return job.id


def upload_paths(job_id, names_json):
    """Validated destination paths for the files the page is about to write."""
    job = _job(job_id)
    if job.state == "processing":
        raise ValueError("Wait for processing to finish before adding files.")
    paths = []
    for name in json.loads(names_json):
        safe = F.safe_name(name)
        if not safe or safe.startswith("."):
            raise ValueError(f"Invalid file name: {name!r}")
        paths.append(str(job.dir / "upload" / safe))
    return json.dumps(paths)


def inspect(job_id, appended=False):
    job = _job(job_id)
    if appended:
        job.state, job.report = "uploaded", None
    job.inspect()
    return json.dumps(_public(job))


def job(job_id):
    return json.dumps(_public(_job(job_id)))


def remove(job_id):
    _drop(job_id)
    return job_id


def _params(p):
    alpha = float(p.get("alpha", 1.0))
    spatial_alpha = p.get("spatial_alpha")
    if not 0.0 <= alpha <= 1.0:
        raise ValueError("alpha must be between 0 and 1")
    if spatial_alpha is not None:
        spatial_alpha = float(spatial_alpha)
        if not 0.0 <= spatial_alpha <= 1.0:
            raise ValueError("spatial_alpha must be between 0 and 1")
    reference = p.get("reference", "auto")
    if reference not in ("auto", "cohort", "normative"):
        raise ValueError("reference must be auto, cohort or normative")
    spatial_model = p.get("spatial_model", "kernel")
    if spatial_model not in ("kernel", "identity"):
        raise ValueError("spatial_model must be kernel or identity")
    return {"alpha": alpha, "reference": reference, "spatial_model": spatial_model,
            "spatial_alpha": spatial_alpha, "preserve_oob": bool(p.get("preserve_oob", True)),
            "anonymize": bool(p.get("anonymize", True)),
            "subjects": {str(k): str(v) for k, v in (p.get("subjects") or {}).items()}}


def run(job_id, params_json):
    """Process the session to the end (the worker is busy meanwhile; progress goes to the page)."""
    job = _job(job_id)
    if not any(r["status"] != "error" for r in job.recordings.values()):
        raise ValueError("There are no readable recordings to process.")
    job.params = _params(json.loads(params_json))
    job.state, job.error, job.report = "processing", None, None
    job.set_stage("Starting", 0.0)
    try:
        job._process()
        job.state, job.stage, job.progress = "done", "Complete", 1.0
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        job.state, job.error = "error", str(exc)
    gc.collect()
    _notify(job, force=True)
    return json.dumps(_public(job))


# ---------------------------------------------------------------- results (as app/main.py)
def _done(job, rid):
    if rid not in job.recordings:
        raise ValueError("Unknown recording.")
    if job.state != "done":
        raise ValueError("Processing has not finished.")


def meta(job_id, rid):
    job = _job(job_id)
    _done(job, rid)
    return json.dumps(job.meta(rid))


def window(job_id, rid, t0=0.0, dur=10.0, px=1200, band="broad", aux=True):
    """Min/max envelopes in uV, (2, C, px, 2) float32, plus the window's start and end in s."""
    job = _job(job_id)
    _done(job, rid)
    if band not in ("broad", "delta", "theta", "alpha", "beta", "gamma"):
        band = "broad"
    data, a, b = job.window(rid, float(t0), max(0.2, min(float(dur), 120.0)),
                            max(50, min(int(px), 4000)), band, bool(aux))
    return np.ascontiguousarray(data, dtype=np.float32), float(a), float(b)


def analysis(job_id, rid):
    job = _job(job_id)
    _done(job, rid)
    v = job.view(rid)
    sel = v["freqs"] <= 60

    def psd_db(p):
        return np.round(10 * np.log10(np.clip(p[:, sel] * 1e12, 1e-12, None)), 2).tolist()

    def nanlist(a):
        return [[None if not np.isfinite(x) else round(float(x), 4) for x in row] for row in a]

    return json.dumps({
        "freqs": np.round(v["freqs"][sel], 3).tolist(),
        "psd_in": psd_db(v["psd_in"]), "psd_out": psd_db(v["psd_out"]),
        "power_in": (v["P_in"] * 1e12).round(5).tolist(), "power_out": (v["P_out"] * 1e12).round(5).tolist(),
        "power_ref": (v["mu"] * 1e12).round(5).tolist(), "pos2d": nanlist(v["pos2d"]),
        "fidelity": np.round(v["fidelity"], 4).tolist(),
        "env_in": np.round(v["env_in"] * 1e6, 3).tolist(), "env_out": np.round(v["env_out"] * 1e6, 3).tolist()})


def spatial(job_id, rid, band=2):
    job = _job(job_id)
    _done(job, rid)
    v = job.view(rid)
    b = int(max(0, min(int(band), v["corr_in"].shape[0] - 1)))
    return json.dumps({"band": b, "in": np.round(v["corr_in"][b], 3).tolist(),
                       "out": np.round(v["corr_out"][b], 3).tolist(),
                       "ref": np.round(v["corr_ref"][b], 3).tolist()})


def download_all(job_id):
    job = _job(job_id)
    if job.state != "done":
        raise ValueError("Processing has not finished.")
    return json.dumps({"path": str(job.dir / "output" / "sane_outputs.zip"), "name": "sane_outputs.zip"})


def download_one(job_id, rid):
    """One output file as it is, or several (a BrainVision triplet, EEGLAB .set + .fdt) as a zip."""
    job = _job(job_id)
    _done(job, rid)
    files = sorted((job.dir / "output" / rid).iterdir())
    if len(files) == 1:
        return json.dumps({"path": str(files[0]), "name": files[0].name})
    name = f"{Path(job.recordings[rid]['file']).stem}_sane.zip"
    target = job.dir / "work" / name
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.name)
    return json.dumps({"path": str(target), "name": name})


def versions():
    import mne
    import scipy
    return json.dumps({"mne": mne.__version__, "numpy": np.__version__, "scipy": scipy.__version__})


__all__ = ["new_job", "upload_paths", "inspect", "job", "remove", "run", "meta", "window",
           "analysis", "spatial", "download_all", "download_one", "versions"]
