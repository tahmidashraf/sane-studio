"""Build the compact sample datasets shipped with SANE Studio (samples/).

depression-rest/  OpenNeuro ds003478 (CC0): 3 subjects, 60 s, resampled to 250 Hz, EDF.
hd128-motor/      synthetic Biosemi-128 + Status, 3 subjects, 30 s, 256 Hz, BDF;
                  subject 3 carries an elevated theta biomarker.
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

OUT = ROOT / "samples"
mne.set_log_level("ERROR")


def depression():
    folder = OUT / "depression-rest"
    shutil.rmtree(folder, ignore_errors=True)
    for f in sorted(glob.glob("D:/MDD2_ds003478/sub-*/eeg/*.set"))[1:4]:
        raw = mne.io.read_raw(f, preload=True).crop(30, 90).resample(250)
        raw.set_annotations(raw.annotations[[i for i, d in enumerate(raw.annotations.description)
                                             if d.lower() != "boundary"]])
        name = Path(f).name.split("_")[0]
        spec = formats.RecordingSpec("x", "edf", Path(f"{name}_task-rest_eeg.edf"))
        print("  ", formats.write(raw, spec, folder, suffix="")[0].name)


def hd128(n_subjects=3, fs=256.0, dur=30.0, seed=11):
    folder = OUT / "hd128-motor"
    shutil.rmtree(folder, ignore_errors=True)
    rng = np.random.default_rng(seed)
    mont = mne.channels.make_standard_montage("biosemi128")
    names = mont.ch_names
    pos = np.array([mont.get_positions()["ch_pos"][n] for n in names])
    d = np.linalg.norm(pos[:, None] - pos[None], axis=-1)
    L = np.linalg.cholesky(np.exp(-d / 0.05) + 1e-6 * np.eye(len(names)))
    n = int(fs * dur)
    freqs = np.fft.rfftfreq(n, 1 / fs)
    onsets = np.arange(2.0, dur - 1, 3.0)
    for s in range(n_subjects):
        shape = 1.0 / np.maximum(freqs, 0.5) + 6.0 * np.exp(-0.5 * ((freqs - (9.5 + s)) / 1.0) ** 2)
        shape += 1.5 * np.exp(-0.5 * ((freqs - 20.0) / 3.0) ** 2)
        if s == n_subjects - 1:
            shape += 5.0 * np.exp(-0.5 * ((freqs - 6.0) / 1.2) ** 2)
        white = rng.standard_normal((len(names), n))
        eeg = (1.0 + 0.4 * rng.standard_normal(len(names))[:, None]) * (
            L @ np.fft.irfft(np.fft.rfft(white, axis=1) * np.sqrt(shape), n=n, axis=1))
        # event-related desynchronization over motor areas after each cue
        t = np.arange(n) / fs
        motor = np.array([nm in ("A1", "B1", "C1", "D1") or nm.startswith(("A", "B")) for nm in names])
        for k, on in enumerate(onsets):
            win = (t > on) & (t < on + 1.5)
            eeg[np.ix_(motor, win)] *= 0.6 if k % 2 == 0 else 0.8
        eeg = 20e-6 * eeg / eeg.std()
        status = np.zeros(n)
        for k, on in enumerate(onsets):
            status[int(on * fs):int(on * fs) + 8] = 1 + k % 2
        info = mne.create_info(names + ["Status"], fs, ["eeg"] * len(names) + ["stim"])
        raw = mne.io.RawArray(np.vstack([eeg, status]), info)
        raw.set_montage(mont)
        raw.set_annotations(mne.Annotations(onsets, 1.5, ["left hand" if k % 2 == 0 else "right hand"
                                                          for k in range(len(onsets))]))
        spec = formats.RecordingSpec("x", "bdf", Path(f"sub-{s + 1:02d}_task-motor_eeg.bdf"))
        print("  ", formats.write(raw, spec, folder, suffix="")[0].name)


if __name__ == "__main__":
    depression()
    hd128()
    for p in sorted(OUT.rglob("*.*")):
        print(f"{p.stat().st_size / 1e6:6.2f} MB  {p.relative_to(OUT)}")
