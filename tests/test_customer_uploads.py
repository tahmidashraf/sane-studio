"""The customer scenario, file by file: upload one recording over HTTP, process it with
the defaults, download the result, and re-open it independently with MNE.

Usage: python tests/test_customer_uploads.py [base_url]   (server must be running)
"""
import io
import json
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import mne
import numpy as np

mne.set_log_level("ERROR")
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parent
NL = ROOT / "native_like"


def case(label, *paths):
    return label, [Path(p) for p in paths]


def first(pattern):
    hits = sorted(Path("/").glob(pattern)) if not pattern.startswith(("D:", "E:")) else sorted(
        Path(pattern[:3]).glob(pattern[3:]))
    return hits[0] if hits else None


PD = first("D:/PD2_ds002778/sub-hc1/ses-hc/eeg/*.bdf")
HBN = first("D:/HBN_EEG/ds005512/sub-NDARAA306NT2/eeg/*contrastChangeDetection_run-1_eeg.set")
MDD = first("D:/MDD2_ds003478/sub-001/eeg/*.set")
CASES = [
    case("native Biosemi BDF (Parkinson's study)", PD),
    case("placeholder BDF (DataLad pointer)", "E:/Brain signal work/nm000213/sub-1/ses-0/eeg/sub-1_ses-0_task-p300_run-0_eeg.bdf"),
    case("native EEGLAB, 129-ch EGI (HBN, 235 MB)", HBN),
    case("native clinical EDF (Mumtaz)", "D:/MDD_Mumtaz/MDD S1 EC.edf"),
    case("native PhysioNet EDF (dotted labels)", "D:/MI_PhysioNet/S010R04.edf"),
    case("native EEGLAB .set + .fdt", MDD, MDD.with_suffix(".fdt")),
    case("BrainVision, names with spaces", *sorted((NL / "bv_space").iterdir())),
    case("BrainVision renamed (stale refs)", *sorted((NL / "bv_renamed").iterdir())),
    case("BrainVision accented data file", *sorted((NL / "bv_accent").iterdir())),
    case("FIF with MEG + EEG + EOG + ECG", *sorted((NL / "fif_meg").iterdir())),
    case("clinical EDF+, mixed sampling rates", *sorted((NL / "edf_clinical").iterdir())),
    case("EEGLAB pair renamed after saving", *sorted((NL / "set_renamed").iterdir())),
]


def curl_json(*args):
    out = subprocess.run(["curl", "-s", *args], capture_output=True, check=True).stdout
    return json.loads(out)


def run(label, files):
    t0 = time.time()
    job = curl_json(*sum((["-F", f"files=@{p.as_posix()}"] for p in files), []), f"{BASE}/api/jobs")
    rec = job["recordings"][0] if job["recordings"] else None
    if rec is None or rec["status"] == "error":
        return {"label": label, "result": "rejected", "detail": (rec or {}).get("message") or "; ".join(job["notes"])}
    curl_json("-X", "POST", "-H", "Content-Type: application/json", "-d", "{}", f"{BASE}/api/jobs/{job['id']}/run")
    while True:
        job = curl_json(f"{BASE}/api/jobs/{job['id']}")
        if job["state"] in ("done", "error"):
            break
        time.sleep(1)
    if job["state"] == "error":
        return {"label": label, "result": "FAILED", "detail": job["error"]}
    r = job["report"]["recordings"][0]
    blob = subprocess.run(["curl", "-s", f"{BASE}/api/jobs/{job['id']}/download/{r['rid']}"],
                          capture_output=True, check=True).stdout
    tmp = Path(tempfile.mkdtemp())
    if blob[:2] == b"PK":
        zipfile.ZipFile(io.BytesIO(blob)).extractall(tmp)
    else:
        (tmp / r["outputs"][0]).write_bytes(blob)
    header = next(p for p in tmp.iterdir() if p.suffix.lower() in (".edf", ".bdf", ".vhdr", ".set", ".fif"))
    out = mne.io.read_raw(header, preload=True)
    src_meta = rec
    same_fmt = header.suffix.lower() == Path(rec["file"]).suffix.lower()
    ok = (same_fmt and len(out.ch_names) == src_meta["n_channels"] and out.info["sfreq"] == src_meta["fs"]
          and abs(out.n_times / out.info["sfreq"] - src_meta["duration_s"]) < 0.05
          and len(out.annotations) == src_meta["n_events"])
    return {"label": label, "result": "OK" if ok else "MISMATCH",
            "detail": (f"{rec['format']} -> {header.name} | ch {src_meta['n_channels']}->{len(out.ch_names)} "
                       f"(EEG {r['eeg_channels']}) | fs {src_meta['fs']:.0f}->{out.info['sfreq']:.0f} | "
                       f"events {src_meta['n_events']}->{len(out.annotations)} | ref {r['reference']} | "
                       f"theta/beta {r['biomarkers_in']['theta_beta']:+.2f}->{r['biomarkers_out']['theta_beta']:+.2f} "
                       f"(target {r['biomarkers_ref']['theta_beta']:+.2f}) | fidelity {r['fidelity_mean']:.3f} | "
                       f"{time.time() - t0:.0f}s")}


if __name__ == "__main__":
    results = []
    for label, files in CASES:
        res = run(label, files)
        results.append(res)
        print(f"[{res['result']:8s}] {res['label']}\n           {res['detail']}", flush=True)
    bad = [r for r in results if r["result"] in ("FAILED", "MISMATCH")]
    print(f"\n{len(results) - len(bad)}/{len(results)} behaved correctly" + (f"; problems: {[r['label'] for r in bad]}" if bad else ""))
