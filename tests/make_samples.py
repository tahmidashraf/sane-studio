"""Build a test corpus in every supported format.

real/   one real depression recording (ds003478, EEGLAB source, 120 s) written to
        EDF, BDF, BrainVision, EEGLAB and FIF through the product's own writer.
cohort/ three real subjects (EDF) for cohort-mode tests.
hd128/  three synthetic 128-channel Biosemi BDF subjects with a Status trigger
        channel; subject 3 carries a theta boost that mimics a disease biomarker.
"""
import glob
import shutil
import sys
from pathlib import Path

import mne
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import formats  # noqa: E402

OUT = ROOT / "tests" / "samples"
SRC = sorted(glob.glob("D:/MDD2_ds003478/sub-*/eeg/*.set"))


def real_set():
    if (OUT / "real").exists():
        shutil.rmtree(OUT / "real")
    raw = mne.io.read_raw(SRC[1], preload=True).crop(0, 120)
    for fmt in ("edf", "bdf", "brainvision", "eeglab", "fif"):
        spec = formats.RecordingSpec("x", fmt, Path(f"sub-002_task-Rest_eeg.{fmt}"))
        files = formats.write(raw, spec, OUT / "real", suffix="")
        print(f"  real {fmt:12s} -> {[f.name for f in files]}")


def cohort():
    if (OUT / "cohort").exists():
        shutil.rmtree(OUT / "cohort")
    for f in SRC[2:5]:
        raw = mne.io.read_raw(f, preload=True).crop(0, 90)
        name = Path(f).name.split("_")[0]
        spec = formats.RecordingSpec("x", "edf", Path(f"{name}_task-Rest_eeg.edf"))
        files = formats.write(raw, spec, OUT / "cohort", suffix="")
        print(f"  cohort {files[0].name}")


def hd128(n_subjects=3, fs=512.0, dur=60.0, seed=7):
    if (OUT / "hd128").exists():
        shutil.rmtree(OUT / "hd128")
    rng = np.random.default_rng(seed)
    mont = mne.channels.make_standard_montage("biosemi128")
    names = mont.ch_names
    pos = np.array([mont.get_positions()["ch_pos"][n] for n in names])
    d = np.linalg.norm(pos[:, None] - pos[None], axis=-1)
    L = np.linalg.cholesky(np.exp(-d / 0.05) + 1e-6 * np.eye(len(names)))
    n = int(fs * dur)
    t = np.arange(n) / fs
    freqs = np.fft.rfftfreq(n, 1 / fs)
    for s in range(n_subjects):
        spec_shape = 1.0 / np.maximum(freqs, 0.5)                       # 1/f background
        spec_shape += 6.0 * np.exp(-0.5 * ((freqs - (9.5 + s)) / 1.0) ** 2)   # alpha peak
        if s == n_subjects - 1:
            spec_shape += 5.0 * np.exp(-0.5 * ((freqs - 6.0) / 1.2) ** 2)     # "disease" theta
        white = rng.standard_normal((len(names), n))
        coloured = np.fft.irfft(np.fft.rfft(white, axis=1) * np.sqrt(spec_shape), n=n, axis=1)
        gains = 1.0 + 0.4 * rng.standard_normal(len(names))[:, None]      # subject montage gains
        eeg = gains * (L @ coloured)
        eeg = 20e-6 * eeg / eeg.std()
        status = np.zeros(n)
        for k in np.arange(2.0, dur - 1, 4.0):
            status[int(k * fs):int(k * fs) + 10] = 1 + (int(k) // 4) % 2
        info = mne.create_info(names + ["Status"], fs, ["eeg"] * len(names) + ["stim"])
        raw = mne.io.RawArray(np.vstack([eeg, status]), info)
        raw.set_montage(mont)
        onsets = np.arange(2.0, dur - 1, 4.0)
        raw.set_annotations(mne.Annotations(onsets, 0.0, [f"stim/{1 + (int(k) // 4) % 2}" for k in onsets]))
        spec = formats.RecordingSpec("x", "bdf", Path(f"sub-{s + 1:02d}_task-motor_eeg.bdf"))
        files = formats.write(raw, spec, OUT / "hd128", suffix="")
        print(f"  hd128 {files[0].name}  ({len(names)} EEG + Status, {fs:.0f} Hz)")


if __name__ == "__main__":
    mne.set_log_level("ERROR")
    real_set()
    cohort()
    hd128()
