"""End-to-end test on freshly downloaded public recordings (OpenNeuro), every format.

Each case is uploaded over HTTP exactly as the website does, processed, downloaded, and
the output is checked independently against the original file:
  same format, channel names, sampling rate, length and events; non-EEG channels
  unchanged; theta/beta recomputed from the downloaded EEG moves to the reference.

Usage: python tests/test_downloaded.py [data_root] [base_url]
"""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import mne
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import engine as E, formats as F  # noqa: E402

mne.set_log_level("ERROR")
DATA = Path(sys.argv[1] if len(sys.argv) > 1 else "D:/sane_testdata")
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"
HEADER_EXT = (".edf", ".bdf", ".vhdr", ".set", ".fif")
OUT = DATA / "_out"          # extraction on the data drive, removed after each check

CASES = [
    ("EDF alone (BCI, ds002034)", ["edf"], {}),
    ("BDF alone (Biosemi, ds001787)", ["bdf"], {}),
    ("BrainVision alone (ds004018)", ["brainvision"], {}),
    ("FIF alone, MEG + EEG (ds000248)", ["fif"], {}),
    ("EEGLAB .set alone, Alzheimer's sub-001", ["eeglab_set/sub-001_task-eyesclosed_eeg.set"], {}),
    ("EEGLAB .set+.fdt alone, Parkinson's sub-001", ["eeglab_set_fdt/sub-001_task-Rest_eeg.set",
                                                      "eeglab_set_fdt/sub-001_task-Rest_eeg.fdt"], {}),
    ("Cohort: Alzheimer's + healthy + FTD (.set)", ["eeglab_set"], {}),
    ("Cohort: Parkinson's + control (.set+.fdt)", ["eeglab_set_fdt"], {}),
    ("Single file, spatial alignment off", ["eeglab_set/sub-037_task-eyesclosed_eeg.set"], {"spatial_alpha": 0.0}),
    ("Everything in one upload (5 formats, 9 recordings)",
     ["edf", "bdf", "brainvision", "fif", "eeglab_set", "eeglab_set_fdt"], {}),
]


def files_for(items):
    out = []
    for it in items:
        p = DATA / it
        out += sorted(x for x in p.iterdir() if x.is_file()) if p.is_dir() else [p]
    return out


def curl_json(*args):
    return json.loads(subprocess.run(["curl", "-s", *args], capture_output=True, check=True).stdout)


def theta_beta(raw):
    eeg, _, _ = F.classify_channels(raw)
    X = raw.get_data(picks=eeg).astype(np.float32)
    fs = raw.info["sfreq"]
    bands = E.active_bands(fs)
    P = E.welch_stats(X, fs, bands, keep=E.artifact_mask(X, fs))["P"]
    return float(np.log(P[:, 1].mean() / P[:, 3].mean()))


def header_checks(src_path, out_path, passthrough):
    """Format-level preservation: EDF/BDF signal headers and digital samples, BrainVision markers."""
    issues = []
    ext = src_path.suffix.lower()
    if ext in (".edf", ".bdf"):
        import edfio
        rd = edfio.read_bdf if ext == ".bdf" else edfio.read_edf
        a, b = rd(src_path), rd(out_path)
        for k, (sa, sb) in enumerate(zip(a.signals, b.signals)):
            for attr in ("label", "physical_dimension", "transducer_type", "sampling_frequency", "prefiltering"):
                if getattr(sa, attr) != getattr(sb, attr):
                    issues.append(f"signal {sa.label}: {attr} {getattr(sa, attr)!r} -> {getattr(sb, attr)!r}")
                    break
        same = sum(np.array_equal(a.signals[i].digital, b.signals[i].digital) for i in passthrough)
        if same != len(passthrough):
            issues.append(f"only {same}/{len(passthrough)} pass-through signals bit-identical")
        if len(a.annotations) != len(b.annotations):
            issues.append(f"EDF annotations {len(a.annotations)} -> {len(b.annotations)}")
    elif ext == ".vhdr":
        def marks(p):
            vm = next(p.parent.glob("*.vmrk")) if p.parent != src_path.parent else src_path.with_name(
                next(l.split("=", 1)[1] for l in p.read_text(encoding="utf-8", errors="ignore").splitlines()
                     if l.lower().startswith("markerfile=")).strip())
            return [l for l in vm.read_text(encoding="utf-8", errors="ignore").splitlines()
                    if l.startswith("Mk") and "New Segment" not in l]
        if marks(src_path) != marks(out_path):
            issues.append("BrainVision marker lines changed")
    return issues


def check(original_header, out_dir, rec, report_row):
    src = mne.io.read_raw(original_header, preload=True)
    header = next(p for p in out_dir.iterdir() if p.suffix.lower() in HEADER_EXT)
    out = mne.io.read_raw(header, preload=True)
    eeg, passthrough, _ = F.classify_channels(src)
    issues = []
    if header.suffix.lower() != original_header.suffix.lower():
        issues.append(f"format {original_header.suffix}->{header.suffix}")
    trunc = 16 if header.suffix.lower() in (".edf", ".bdf") else 999
    if [c[:trunc] for c in out.ch_names] != [c[:trunc] for c in src.ch_names]:
        issues.append("channel names differ")
    if out.info["sfreq"] != src.info["sfreq"]:
        issues.append(f"fs {src.info['sfreq']}->{out.info['sfreq']}")
    if abs(out.n_times - src.n_times) > 1:
        issues.append(f"samples {src.n_times}->{out.n_times}")
    a, b = src.annotations, out.annotations
    if len(a) != len(b):
        issues.append(f"events {len(a)}->{len(b)}")
    elif len(a) and (np.max(np.abs((a.onset - src.first_time) - (b.onset - out.first_time))) > 2 / src.info["sfreq"]
                     or list(a.description) != list(b.description)):
        issues.append("event onsets/labels differ")
    n = min(src.n_times, out.n_times)
    worst = 0.0
    for i in passthrough:
        x, y = src.get_data(picks=[i])[0, :n], out.get_data(picks=[i])[0, :n]
        rng = max(np.ptp(x), 1e-30)
        worst = max(worst, float(np.max(np.abs(x - y)) / rng))
    if worst > 1e-3:
        issues.append(f"pass-through channel changed ({worst:.1e} of range)")
    issues += header_checks(original_header, header, passthrough)
    tb_in, tb_out = theta_beta(src), theta_beta(out)
    target = report_row["biomarkers_ref"]["theta_beta"]
    if report_row["reference"] != "cohort" or report_row["reference_subjects"] > 1:
        if abs(tb_out - target) > 0.1 and abs(tb_out - target) > 0.25 * abs(tb_in - target):
            issues.append(f"theta/beta not neutralized ({tb_out:+.2f} vs target {target:+.2f})")
    return issues, {
        "fmt": f"{original_header.suffix}->{header.suffix}", "ch": f"{len(src.ch_names)}/{len(eeg)} EEG",
        "pass": len(passthrough), "fs": src.info["sfreq"], "dur": round(src.n_times / src.info["sfreq"]),
        "events": len(a), "tb": f"{tb_in:+.2f}->{tb_out:+.2f} (ref {target:+.2f})",
        "dyn": report_row.get("dynamics_mean"), "wave": report_row.get("fidelity_mean"),
        "ref": report_row["reference"], "pt_err": worst, "writer": report_row.get("writer")}


def run_case(label, items, params):
    files = files_for(items)
    t0 = time.time()
    job = curl_json(*sum((["-F", f"files=@{p.as_posix()}"] for p in files), []), f"{BASE}/api/jobs")
    ready = [r for r in job["recordings"] if r["status"] != "error"]
    print(f"\n### {label}  ({len(files)} files, {sum(p.stat().st_size for p in files) / 1e6:.0f} MB) "
          f"-> {len(job['recordings'])} recordings detected, {len(ready)} ready  [upload {time.time() - t0:.0f}s]")
    for r in job["recordings"]:
        if r["status"] == "error":
            print(f"    REJECTED {r['file']}: {r['message']}")
    if not ready:
        return [(label, "REJECTED")]
    curl_json("-X", "POST", "-H", "Content-Type: application/json", "-d", json.dumps(params),
              f"{BASE}/api/jobs/{job['id']}/run")
    t1 = time.time()
    while (job := curl_json(f"{BASE}/api/jobs/{job['id']}"))["state"] not in ("done", "error"):
        time.sleep(2)
    if job["state"] == "error":
        print(f"    PROCESSING FAILED: {job['error']}")
        return [(label, "FAILED")]
    rep = job["report"]
    c = rep["cohort"]
    lam = (f"Lambda {c['lambda_in']:.3f}->{c['lambda_out']:.4f}" if c.get("lambda_in") is not None
           else f"Lambda n/a ({c.get('common_channels', 0)} channels shared by every recording)")
    print(f"    processed in {time.time() - t1:.0f}s"
          + (f" | cohort of {c['n_subjects']}: theta/beta variance -{100 * c['theta_beta_var_reduction']:.2f}%, {lam}"
             if c.get("theta_beta_var_reduction") is not None else ""))
    results = []
    by_name = {p.name: p for p in files}
    for row in rep["recordings"]:
        blob = subprocess.run(["curl", "-s", f"{BASE}/api/jobs/{job['id']}/download/{row['rid']}"],
                              capture_output=True, check=True).stdout
        OUT.mkdir(exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=OUT))
        if blob[:2] == b"PK":
            zipfile.ZipFile(io.BytesIO(blob)).extractall(tmp)
        else:
            (tmp / row["outputs"][0]).write_bytes(blob)
        try:
            issues, m = check(by_name[row["file"]], tmp, row, row)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        status = "PASS" if not issues else "FAIL"
        results.append((f"{label} :: {row['file']}", status))
        print(f"    [{status}] {row['file'][:46]:46s} {m['fmt']:12s} {m['ch']:14s} fs={m['fs']:.0f} {m['dur']}s "
              f"events={m['events']} pass-through={m['pass']} (max err {m['pt_err']:.0e}) | {m['ref']} | "
              f"theta/beta {m['tb']} | dynamics kept {m['dyn']} | waveform {m['wave']} | writer: {m['writer']}")
        for i in issues:
            print(f"           ! {i}")
    curl_json("-X", "DELETE", f"{BASE}/api/jobs/{job['id']}")
    return results


if __name__ == "__main__":
    only = sys.argv[3].lower() if len(sys.argv) > 3 else ""      # optional: run cases whose label contains this
    all_results = []
    for label, items, params in CASES:
        if only in label.lower():
            all_results += run_case(label, items, params)
    passed = sum(s == "PASS" for _, s in all_results)
    print(f"\n==== {passed}/{len(all_results)} recordings passed every check ====")
    for name, s in all_results:
        if s != "PASS":
            print(f"  {s}: {name}")
