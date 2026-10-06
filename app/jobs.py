"""Job lifecycle: upload, inspection, background processing, artifacts, and expiry."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

import numpy as np

from . import analysis as A
from . import engine as E
from . import formats as F

DATA_ROOT = Path(os.environ.get("SANE_DATA_DIR", Path(tempfile.gettempdir()) / "sane-studio"))
TTL_S = int(os.environ.get("SANE_TTL_MINUTES", "60")) * 60
MAX_UPLOAD_BYTES = int(os.environ.get("SANE_MAX_UPLOAD_MB", "1024")) * 1024 * 1024
CHUNK = 500_000
_PROCESS_SLOTS = threading.BoundedSemaphore(int(os.environ.get("SANE_WORKERS", "2")))


def _f(x, nd=4):
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def _fallback_note(coverage):
    share = ("No other subject in this upload has channels with these names"
             if coverage == 0 else f"Only {100 * coverage:.0f}% of these channel names appear in another subject's recording")
    return f"{share}, so this recording was matched to the normative reference instead of the cohort."


def _finite(obj):
    """Replace NaN/inf anywhere in a report with None so it always serializes as JSON."""
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        return float(obj) if np.isfinite(obj) else None
    return obj


class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.dir = DATA_ROOT / self.id
        for sub in ("upload", "work", "output"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.created = self.touched = time.time()
        self.state, self.stage, self.progress, self.error = "uploaded", "", 0.0, None
        self.specs, self.recordings, self.notes = [], {}, []
        self.params, self.report = {}, None
        self.lock = threading.Lock()

    # ------------------------------------------------------------ public view
    def public(self):
        self.touched = time.time()
        return {"id": self.id, "state": self.state, "stage": self.stage,
                "progress": round(self.progress, 3), "error": self.error, "notes": self.notes,
                "params": self.params, "expires_in_s": int(max(0, TTL_S - (time.time() - self.created))),
                "recordings": [self.recordings[s.rid] for s in self.specs],
                "report": self.report}

    def set_stage(self, stage, progress):
        with self.lock:
            self.stage, self.progress = stage, float(min(1.0, max(0.0, progress)))

    # ------------------------------------------------------------ inspection
    def inspect(self):
        files = sorted(p for p in (self.dir / "upload").iterdir() if p.is_file())
        self.specs, self.notes = F.group_uploads(files)
        for spec in self.specs:
            rec = {"rid": spec.rid, "file": spec.header.name, "format": F.FORMAT_LABEL[spec.fmt],
                   "companions": [c.name for c in spec.companions], "warnings": list(spec.warnings),
                   "subject": F.suggest_subject(spec), "status": "ready",
                   "size_mb": round(sum(p.stat().st_size for p in [spec.header, *spec.companions]) / 1e6, 2)}
            try:
                raw = F.read(spec, preload=False)
                eeg, passthrough, why = F.classify_channels(raw)
                rec.update({"n_channels": len(raw.ch_names), "n_eeg": len(eeg), "fs": raw.info["sfreq"],
                            "duration_s": round(raw.n_times / raw.info["sfreq"], 2),
                            "n_events": len(raw.annotations),
                            "passthrough": [{"name": n, "reason": r} for n, r in why.items()]})
                if len(eeg) < 2:
                    rec.update(status="error", message="fewer than two EEG channels were found")
                elif raw.n_times / raw.info["sfreq"] < E.MIN_DURATION_S:
                    rec.update(status="error", message=f"recording is shorter than {E.MIN_DURATION_S:.0f} s")
            except Exception as exc:  # noqa: BLE001
                rec.update(status="error", message=str(exc))
            self.recordings[spec.rid] = rec
        if not self.specs:
            self.notes.append("No EEG recordings were found in the upload.")

    # ------------------------------------------------------------ processing
    def run(self, params):
        self.params = params
        self.state, self.error, self.report = "processing", None, None
        threading.Thread(target=self._run_guarded, daemon=True).start()

    def _run_guarded(self):
        with _PROCESS_SLOTS:
            try:
                self._process()
                self.state, self.stage, self.progress = "done", "Complete", 1.0
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self.state, self.error = "error", str(exc)

    def _path(self, rid, name):
        return self.dir / "work" / f"{rid}_{name}"

    def _process(self):
        p = self.params
        alpha = float(p.get("alpha", 1.0))
        reference = p.get("reference", "auto")
        spatial = p.get("spatial_model", "kernel")
        preserve_oob = bool(p.get("preserve_oob", True))
        anonymize = bool(p.get("anonymize", True))
        subjects = p.get("subjects", {})
        for rec in self.recordings.values():          # a re-run starts clean
            if rec.get("status") == "error" and str(rec.get("message", "")).startswith("could not be"):
                rec.update(status="ready", message="")
        specs = [s for s in self.specs if self.recordings[s.rid]["status"] != "error"]
        if not specs:
            raise ValueError("no readable recordings to process")
        shutil.rmtree(self.dir / "output", ignore_errors=True)
        (self.dir / "output").mkdir()
        bands = E.active_bands(min(self.recordings[s.rid]["fs"] for s in specs))

        # 1. statistics of every recording; a failing file is reported, not fatal
        info = {}
        for k, spec in enumerate(specs):
            self.set_stage(f"Analyzing {spec.header.name}", 0.02 + 0.38 * k / len(specs))
            try:
                info[spec.rid] = self._analyze(spec, bands, subjects)
            except Exception as exc:  # noqa: BLE001
                self._fail(spec, f"could not be analyzed: {exc}")
        if not info:
            raise ValueError("none of the recordings could be processed")
        specs = [s for s in specs if s.rid in info]

        # 2. reference and operators (one unit per subject x montage x sampling rate)
        self.set_stage("Building the reference", 0.42)
        ops = self._operators(info, bands, alpha, reference, spatial)

        # 3. neutralize, write in the original format, measure
        n = len(specs)
        for k, spec in enumerate(specs):
            base = 0.45 + 0.45 * k / n
            self.set_stage(f"Neutralizing {spec.header.name}", base)
            try:
                self._neutralize(spec, info[spec.rid], ops, bands, alpha, preserve_oob, anonymize, base, n)
            except Exception as exc:  # noqa: BLE001
                self._fail(spec, f"could not be written: {exc}")
                info.pop(spec.rid)
        if not info:
            raise ValueError("none of the recordings could be written")

        # 4. report and packages
        self.set_stage("Building the privacy report", 0.92)
        self.report = self._report(info, bands, alpha, preserve_oob)
        self.set_stage("Packaging downloads", 0.96)
        with zipfile.ZipFile(self.dir / "output" / "sane_outputs.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for d in info.values():
                for f in d["files"]:
                    z.write(f, f.name)
            z.writestr("SANE_REPORT.json", json.dumps(self.report, indent=2))
        meta = {rid: {"ch_names": d["ch_names"], "aux_names": d["aux_names"], "fs": d["fs"], "N": d["N"],
                      "annotations": d["ann"], "bands": [b[0] for b in bands]} for rid, d in info.items()}
        (self.dir / "work" / "meta.json").write_text(json.dumps(meta))

    def _fail(self, spec, message):
        traceback.print_exc()
        self.recordings[spec.rid].update(status="error", message=message)

    def _analyze(self, spec, bands, subjects):
        raw = F.read(spec, preload=True)
        eeg, passthrough, why = F.classify_channels(raw)
        if len(eeg) < 2:
            raise ValueError("fewer than two EEG channels")
        fs, N = float(raw.info["sfreq"]), int(raw.n_times)
        x_in = np.lib.format.open_memmap(self._path(spec.rid, "in.npy"), "w+", np.float32, (int(len(eeg)), N))
        aux_idx = [i for i in passthrough if raw.get_channel_types([i])[0] not in ("stim",)]
        aux = np.lib.format.open_memmap(self._path(spec.rid, "aux.npy"), "w+", np.float32,
                                        (max(1, len(aux_idx)), N))
        for a in range(0, N, CHUNK):
            x_in[:, a:a + CHUNK] = raw.get_data(picks=eeg, start=a, stop=min(N, a + CHUNK))
            if aux_idx:
                aux[:, a:a + CHUNK] = raw.get_data(picks=aux_idx, start=a, stop=min(N, a + CHUNK))
        x_in.flush()
        aux.flush()
        keep = E.artifact_mask(x_in, fs, F.bad_sample_mask(raw))
        st = E.welch_stats(x_in, fs, bands, keep=keep)
        np.save(self._path(spec.rid, "csd.npy"), st["csd"])
        ann = [{"t": round(float(a["onset"] - raw.first_time), 4), "d": round(float(a["duration"]), 4),
                "label": str(a["description"])} for a in raw.annotations][:5000]
        return dict(
            spec=spec, eeg=eeg, ch_names=[raw.ch_names[i] for i in eeg],
            aux_names=[raw.ch_names[i] for i in aux_idx], fs=fs, N=N, keep=keep, st=st,
            pos=F.electrode_positions(raw, eeg), ann=ann, n_events=len(raw.annotations),
            subject=str(subjects.get(spec.rid) or self.recordings[spec.rid]["subject"]),
            avg_ref=E.is_average_referenced(st["R"].sum(0)))

    def _operators(self, info, bands, alpha, reference, spatial):
        groups = {}
        for rid, d in info.items():
            key = (d["subject"], tuple(E.norm_name(c) for c in d["ch_names"]), d["fs"])
            groups.setdefault(key, []).append(rid)
        units = []
        for g, (key, rids) in enumerate(groups.items()):
            w = np.array([info[r]["keep"].sum() for r in rids], float)
            w /= w.sum()
            first = info[rids[0]]
            csd_path = self.dir / "work" / f"unit{g}_csd.npy"
            np.save(csd_path, sum(wi * np.load(self._path(r, "csd.npy")) for wi, r in zip(w, rids)))
            units.append(dict(
                key=f"unit{g}", subject=key[0], ch_names=first["ch_names"], rids=rids,
                P=sum(wi * info[r]["st"]["P"] for wi, r in zip(w, rids)),
                R=sum(wi * info[r]["st"]["R"] for wi, r in zip(w, rids)),
                csd=(lambda path=csd_path: np.load(path)), csd_freqs=first["st"]["csd_freqs"],
                scale=first["st"]["scale"], weight=float(sum(info[r]["N"] for r in rids)),
                avg_ref=all(info[r]["avg_ref"] for r in rids), pos=first["pos"]))
            for r in rids:
                info[r]["unit"] = f"unit{g}"
        return E.build_operators(units, bands, alpha, reference=reference, spatial_model=spatial,
                                 spatial_alpha=self.params.get("spatial_alpha"))

    def _neutralize(self, spec, d, ops, bands, alpha, preserve_oob, anonymize, base, n):
        op = ops[d["unit"]]
        x_in = np.load(self._path(spec.rid, "in.npy"), mmap_mode="r")
        x_out = np.lib.format.open_memmap(self._path(spec.rid, "out.npy"), "w+", np.float32,
                                          tuple(int(v) for v in x_in.shape))
        E.apply_operator(x_in, x_out, d["fs"], bands, op["M"], preserve_oob=preserve_oob, avg_ref=d["avg_ref"],
                         progress=lambda f: self.set_stage(self.stage, base + 0.3 * f * 0.45 / n))
        x_out.flush()
        self.set_stage(f"Writing {F.FORMAT_LABEL[spec.fmt]} output", base + 0.32 * 0.45 / n)
        raw = F.read(spec, preload=True)
        for a in range(0, d["N"], CHUNK):
            raw._data[d["eeg"], a:a + CHUNK] = x_out[:, a:a + CHUNK]
        if spec.fmt == "fif":
            raw.info["description"] = (f"Processed by SANE (alpha={alpha}, reference={op['mode']}); "
                                       "subject-level spectral and spatial biomarkers neutralized.")
        files = F.write(raw, spec, self.dir / "output" / spec.rid, processed=d["eeg"], anonymize=anonymize)
        del raw
        st_out = E.welch_stats(x_out, d["fs"], bands, keep=d["keep"])
        mask = A.window_sample_mask(d["N"], d["st"]["starts"], int(round(E.WIN_S * d["fs"])))
        fid, fid_band = A.fidelity(x_in, x_out, d["fs"], bands, mask)
        np.savez_compressed(
            self._path(spec.rid, "view.npz"),
            psd_in=d["st"]["psd"].astype(np.float32), psd_out=st_out["psd"].astype(np.float32),
            freqs=d["st"]["freqs"], P_in=d["st"]["P"], P_out=st_out["P"], mu=op["mu"],
            corr_in=np.stack([E.corr_from_cov(r) for r in d["st"]["R"]]).astype(np.float32),
            corr_out=np.stack([E.corr_from_cov(r) for r in st_out["R"]]).astype(np.float32),
            corr_ref=np.stack([E.corr_from_cov(r) for r in op["Rref"]]).astype(np.float32),
            env_in=A.envelope(x_in), env_out=A.envelope(x_out),
            pos2d=A.project_2d(d["pos"]), fidelity=fid.astype(np.float32),
            fidelity_band=fid_band.astype(np.float32))
        d.update(st_out=st_out, files=files, fidelity=fid, fidelity_band=fid_band, op=op)
        self.recordings[spec.rid]["outputs"] = [f.name for f in files]

    def _report(self, info, bands, alpha, preserve_oob):
        recs, cohort_units = [], []
        for rid, d in info.items():
            op, P_in, P_out = d["op"], d["st"]["P"], d["st_out"]["P"]
            recs.append({
                "rid": rid, "file": d["spec"].header.name, "subject": d["subject"],
                "format": F.FORMAT_LABEL[d["spec"].fmt], "reference": op["mode"],
                "reference_subjects": op["n_subjects_in_reference"], "coverage": _f(op["coverage"], 3),
                "eeg_channels": len(d["ch_names"]), "passthrough_channels": len(d["aux_names"]),
                "fit_windows_kept": _f(d["keep"].mean(), 3), "average_reference": d["avg_ref"],
                "biomarkers_in": A.biomarkers(P_in, bands), "biomarkers_out": A.biomarkers(P_out, bands),
                "biomarkers_ref": A.biomarkers(op["mu"], bands),
                "fingerprint_in": _f(A.fingerprint_distance(d["st"]["R"], op["Rref"])),
                "fingerprint_out": _f(A.fingerprint_distance(d["st_out"]["R"], op["Rref"])),
                "power_dev_in": _f(A.power_deviation(P_in, op["mu"])),
                "power_dev_out": _f(A.power_deviation(P_out, op["mu"])),
                "fidelity_mean": _f(np.mean(d["fidelity"])), "fidelity_min": _f(np.min(d["fidelity"])),
                "dynamics_mean": _f(np.mean(d["fidelity_band"])),
                "dynamics_bands": {b[0]: _f(np.mean(v)) for b, v in zip(bands, d["fidelity_band"])},
                "spatial_alpha": _f(op["spatial_alpha"], 2),
                "spatial_alpha_auto": self.params.get("spatial_alpha") is None,
                "events": d["n_events"], "out_of_band_preserved": preserve_oob,
                "writer": d["spec"].writer,
                "notes": list(d["spec"].warnings) + ([_fallback_note(op["coverage"])] if op.get("cohort_fallback") else []),
                "outputs": [f.name for f in d["files"]]})
            cohort_units.append(dict(subject=d["subject"], ch_names=d["ch_names"], P_in=P_in, P_out=P_out,
                                     feats_in=d["st"]["feats"], feats_out=d["st_out"]["feats"]))
        return _finite({"alpha": alpha, "bands": [b[0] for b in bands], "recordings": recs,
                        "cohort": A.cohort_report(cohort_units, bands)})

    # ------------------------------------------------------------ viewer data
    def meta(self, rid):
        return json.loads((self.dir / "work" / "meta.json").read_text())[rid]

    def view(self, rid):
        return np.load(self._path(rid, "view.npz"))

    def window(self, rid, t0, dur, px, band="broad", aux=True):
        """Min/max envelopes (uV) per pixel column for input and output: (2, C, px, 2)."""
        m = self.meta(rid)
        fs, N = m["fs"], m["N"]
        a = int(max(0, min(N - 1, round(t0 * fs))))
        b = int(max(a + 2, min(N, round((t0 + dur) * fs))))
        x_in = np.load(self._path(rid, "in.npy"), mmap_mode="r")
        x_out = np.load(self._path(rid, "out.npy"), mmap_mode="r")
        blocks = [x_in, x_out]
        if aux and m["aux_names"]:
            ax = np.load(self._path(rid, "aux.npy"), mmap_mode="r")
            blocks = [np.vstack([x_in[:, a:b], ax[:, a:b]]), np.vstack([x_out[:, a:b], ax[:, a:b]])]
        else:
            blocks = [x_in[:, a:b], x_out[:, a:b]]
        out = []
        for blk in blocks:
            seg = np.asarray(blk, np.float64) * 1e6
            if band != "broad":
                seg = _display_band(seg, fs, band)
            out.append(_minmax(seg, px))
        return np.stack(out).astype(np.float32), a / fs, b / fs


def _display_band(seg, fs, band):
    lo, hi = {n: (l, h) for n, l, h in E.BAND_DEFS}[band]
    n = seg.shape[1]
    pad = min(n - 1, int(fs))
    x = np.pad(seg, ((0, 0), (pad, pad)), mode="reflect") if pad > 0 else seg
    X = np.fft.rfft(x, axis=1)
    f = np.fft.rfftfreq(x.shape[1], 1.0 / fs)
    w = E._step(f, lo) - E._step(f, hi)
    return np.fft.irfft(X * w, n=x.shape[1], axis=1)[:, pad:pad + n]


def _minmax(seg, px):
    C, n = seg.shape
    px = max(1, min(px, n))
    edges = np.linspace(0, n, px + 1).astype(int)
    lo = np.minimum.reduceat(seg, edges[:-1], axis=1)
    hi = np.maximum.reduceat(seg, edges[:-1], axis=1)
    return np.stack([lo, hi], -1)


class Store:
    def __init__(self):
        self.jobs, self.lock = {}, threading.Lock()
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._reaper, daemon=True).start()

    def new(self):
        job = Job()
        with self.lock:
            self.jobs[job.id] = job
        return job

    def get(self, job_id):
        job = self.jobs.get(job_id)
        if job is not None:
            job.touched = time.time()
        return job

    def delete(self, job_id):
        with self.lock:
            job = self.jobs.pop(job_id, None)
        if job is not None:
            shutil.rmtree(job.dir, ignore_errors=True)

    def _reaper(self):
        while True:
            time.sleep(60)
            now = time.time()
            for job_id, job in list(self.jobs.items()):
                if job.state != "processing" and now - job.touched > TTL_S:
                    self.delete(job_id)
