"""SANE Studio web service: upload EEG, neutralize subject-level biomarkers, download."""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .formats import safe_name
from .jobs import MAX_UPLOAD_BYTES, Store

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SAMPLES = ROOT / "samples"
SAMPLE_INFO = {
    "depression-rest": {
        "title": "Resting EEG, depression study",
        "detail": "3 subjects, 64 EEG + 2 EOG channels, 250 Hz, EDF. OpenNeuro ds003478 (CC0).",
    },
    "hd128-motor": {
        "title": "128-channel motor task (synthetic)",
        "detail": "3 subjects, Biosemi 128 + Status, 256 Hz, BDF. Subject 3 carries an elevated theta biomarker.",
    },
}

app = FastAPI(title="SANE Studio", version="1.0.0", docs_url="/api/docs", redoc_url=None)
store = Store()
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.middleware("http")
async def revalidate_assets(request, call_next):
    """Browsers revalidate the UI on every load, so a deploy is visible immediately."""
    response = await call_next(request)
    if request.url.path in ("/", "/manual") or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


class RunParams(BaseModel):
    alpha: float = Field(1.0, ge=0.0, le=1.0)
    reference: Literal["auto", "cohort", "normative"] = "auto"
    spatial_model: Literal["kernel", "identity"] = "kernel"
    spatial_alpha: float | None = Field(None, ge=0.0, le=1.0)
    preserve_oob: bool = True
    anonymize: bool = True
    subjects: dict[str, str] = {}


def _job(job_id):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "This session has expired or does not exist.")
    return job


def _rec(job, rid, need_done=True):
    if rid not in job.recordings:
        raise HTTPException(404, "Unknown recording.")
    if need_done and job.state != "done":
        raise HTTPException(409, "Processing has not finished.")
    return rid


def _safe_name(name: str) -> str:
    name = safe_name(name)
    if not name or name.startswith("."):
        raise HTTPException(400, f"Invalid file name: {name!r}")
    return name


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html")


@app.get("/manual", include_in_schema=False)
def manual():
    return FileResponse(WEB / "manual.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024)}


@app.get("/api/samples")
def samples():
    out = []
    for sid, meta in SAMPLE_INFO.items():
        folder = SAMPLES / sid
        if folder.is_dir():
            files = sorted(p for p in folder.iterdir() if p.is_file())
            out.append({"id": sid, **meta, "files": len(files),
                        "size_mb": round(sum(p.stat().st_size for p in files) / 1e6, 1)})
    return out


@app.post("/api/samples/{sid}")
def start_from_sample(sid: str):
    folder = SAMPLES / sid
    if sid not in SAMPLE_INFO or not folder.is_dir():
        raise HTTPException(404, "Unknown sample dataset.")
    job = store.new()
    for f in folder.iterdir():
        if f.is_file():
            shutil.copy(f, job.dir / "upload" / f.name)
    job.inspect()
    return job.public()


@app.post("/api/jobs")
async def create_job(files: list[UploadFile] = File(...)):
    job = store.new()
    total = 0
    try:
        for up in files:
            dest = job.dir / "upload" / _safe_name(up.filename or "")
            with open(dest, "wb") as fh:
                while chunk := await up.read(1 << 20):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, f"Upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
                    fh.write(chunk)
    except HTTPException:
        store.delete(job.id)
        raise
    job.inspect()
    return job.public()


@app.post("/api/jobs/{job_id}/files")
async def add_files(job_id: str, files: list[UploadFile] = File(...)):
    job = _job(job_id)
    if job.state == "processing":
        raise HTTPException(409, "Wait for processing to finish before adding files.")
    total = sum(p.stat().st_size for p in (job.dir / "upload").iterdir())
    for up in files:
        dest = job.dir / "upload" / _safe_name(up.filename or "")
        with open(dest, "wb") as fh:
            while chunk := await up.read(1 << 20):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"Upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
                fh.write(chunk)
    job.state, job.report = "uploaded", None
    job.inspect()
    return job.public()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job(job_id).public()


@app.post("/api/jobs/{job_id}/run")
def run_job(job_id: str, params: RunParams):
    job = _job(job_id)
    if job.state == "processing":
        raise HTTPException(409, "This session is already processing.")
    if not any(r["status"] != "error" for r in job.recordings.values()):
        raise HTTPException(400, "There are no readable recordings to process.")
    job.run(params.model_dump())
    return job.public()


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    store.delete(job_id)
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/rec/{rid}/meta")
def rec_meta(job_id: str, rid: str):
    job = _job(job_id)
    return job.meta(_rec(job, rid))


@app.get("/api/jobs/{job_id}/rec/{rid}/window")
def rec_window(job_id: str, rid: str, t0: float = 0.0, dur: float = 10.0, px: int = 1200,
               band: Literal["broad", "delta", "theta", "alpha", "beta", "gamma"] = "broad",
               aux: bool = True):
    job = _job(job_id)
    _rec(job, rid)
    data, a, b = job.window(rid, t0, max(0.2, min(dur, 120.0)), max(50, min(px, 4000)), band, aux)
    return Response(data.tobytes(), media_type="application/octet-stream",
                    headers={"X-Shape": ",".join(map(str, data.shape)), "X-T0": f"{a:.6f}",
                             "X-T1": f"{b:.6f}", "Cache-Control": "no-store"})


@app.get("/api/jobs/{job_id}/rec/{rid}/analysis")
def rec_analysis(job_id: str, rid: str):
    job = _job(job_id)
    v = job.view(_rec(job, rid))
    sel = v["freqs"] <= 60
    psd_db = lambda p: np.round(10 * np.log10(np.clip(p[:, sel] * 1e12, 1e-12, None)), 2).tolist()
    nanlist = lambda a: [[None if not np.isfinite(x) else round(float(x), 4) for x in row] for row in a]
    return {"freqs": np.round(v["freqs"][sel], 3).tolist(),
            "psd_in": psd_db(v["psd_in"]), "psd_out": psd_db(v["psd_out"]),
            "power_in": (v["P_in"] * 1e12).round(5).tolist(), "power_out": (v["P_out"] * 1e12).round(5).tolist(),
            "power_ref": (v["mu"] * 1e12).round(5).tolist(), "pos2d": nanlist(v["pos2d"]),
            "fidelity": np.round(v["fidelity"], 4).tolist(),
            "env_in": np.round(v["env_in"] * 1e6, 3).tolist(), "env_out": np.round(v["env_out"] * 1e6, 3).tolist()}


@app.get("/api/jobs/{job_id}/rec/{rid}/spatial")
def rec_spatial(job_id: str, rid: str, band: int = 2):
    job = _job(job_id)
    v = job.view(_rec(job, rid))
    b = int(max(0, min(band, v["corr_in"].shape[0] - 1)))
    r = lambda m: np.round(m[b], 3).tolist()
    return {"band": b, "in": r(v["corr_in"]), "out": r(v["corr_out"]), "ref": r(v["corr_ref"])}


@app.get("/api/jobs/{job_id}/download")
def download_all(job_id: str):
    job = _job(job_id)
    if job.state != "done":
        raise HTTPException(409, "Processing has not finished.")
    return FileResponse(job.dir / "output" / "sane_outputs.zip", filename="sane_outputs.zip",
                        media_type="application/zip")


@app.get("/api/jobs/{job_id}/download/{rid}")
def download_one(job_id: str, rid: str):
    job = _job(job_id)
    _rec(job, rid)
    files = sorted((job.dir / "output" / rid).iterdir())
    if len(files) == 1:
        return FileResponse(files[0], filename=files[0].name, media_type="application/octet-stream")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.name)
    buf.seek(0)
    name = f"{Path(job.recordings[rid]['file']).stem}_sane.zip"
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.exception_handler(ValueError)
def value_error(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)
