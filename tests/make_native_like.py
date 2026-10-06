"""Files written the way recording software writes them, to stress the upload path.

bv_space/     BrainVision Recorder-style INT_16, header and data named with spaces
bv_renamed/   header + data renamed after recording; the header still points at the old name
bv_accent/    data file named "Messung ü.eeg" (sanitized on upload)
fif_meg/      Neuromag-style FIF: 60 EEG + 102 mag + 204 grad + EOG + ECG + STI101
edf_clinical/ EDF+ via pyedflib: "EEG Fp1-REF" labels, EKG, 1 Hz pulse channel, annotations
set_renamed/  EEGLAB .set/.fdt pair renamed after saving
"""
import glob
import shutil
from pathlib import Path

import mne
import numpy as np
import pybv
import pyedflib

ROOT = Path(__file__).resolve().parent / "native_like"
mne.set_log_level("ERROR")


def src():
    raw = mne.io.read_raw("D:/MDD_Mumtaz/MDD S1 EC.edf", preload=True).crop(0, 90)
    raw.pick([c for c in raw.ch_names if raw.get_channel_types([c])[0] == "eeg"])
    return raw


def brainvision():
    raw = src()
    data, fs, names = raw.get_data(), raw.info["sfreq"], raw.ch_names
    events = np.array([[int(t * fs), 1] for t in np.arange(5, 85, 5)])
    for case in ("bv_space", "bv_renamed", "bv_accent"):
        out = ROOT / case
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        pybv.write_brainvision(data=data, sfreq=fs, ch_names=names, fname_base="Messung Proband 07",
                               folder_out=out, events=events, unit="µV", resolution=0.1, fmt="binary_int16")
    # renamed after recording: files get a new stem, references inside stay stale
    d = ROOT / "bv_renamed"
    for ext in (".vhdr", ".vmrk", ".eeg"):
        (d / f"Messung Proband 07{ext}").rename(d / f"renamed_session{ext}")
    # accented data file name, referenced correctly inside the header
    d = ROOT / "bv_accent"
    (d / "Messung Proband 07.eeg").rename(d / "Messung ü.eeg")
    vhdr = d / "Messung Proband 07.vhdr"
    vhdr.write_text(vhdr.read_text(encoding="utf-8").replace("DataFile=Messung Proband 07.eeg",
                                                            "DataFile=Messung ü.eeg"), encoding="utf-8")


def fif_meg(fs=250.0, dur=60.0):
    out = ROOT / "fif_meg"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    rng = np.random.default_rng(3)
    eeg = [n for n in mne.channels.make_standard_montage("standard_1020").ch_names
           if not n.startswith(("A", "M", "T9", "T10", "P9", "P10", "F9", "F10", "Nz", "LPA", "RPA"))][:60]
    mag = [f"MEG{i:04d}" for i in range(1, 103)]
    grad = [f"MEG{i:04d}" for i in range(1001, 1205)]
    names = eeg + mag + grad + ["EOG061", "EOG062", "ECG063", "STI101"]
    types = ["eeg"] * len(eeg) + ["mag"] * len(mag) + ["grad"] * len(grad) + ["eog", "eog", "ecg", "stim"]
    n = int(fs * dur)
    raw_eeg = np.cumsum(rng.standard_normal((len(eeg), n)), 1)
    raw_eeg = 15e-6 * (raw_eeg - raw_eeg.mean(1, keepdims=True)) / raw_eeg.std()
    raw_eeg += 10e-6 * np.sin(2 * np.pi * 10 * np.arange(n) / fs) * rng.uniform(.5, 1.5, (len(eeg), 1))
    data = np.vstack([raw_eeg, 1e-13 * rng.standard_normal((len(mag), n)),
                      1e-11 * rng.standard_normal((len(grad), n)), 50e-6 * rng.standard_normal((3, n)), np.zeros((1, n))])
    for t in np.arange(2, dur - 1, 2.5):
        data[-1, int(t * fs):int(t * fs) + 5] = 5
    raw = mne.io.RawArray(data, mne.create_info(names, fs, types))
    raw.set_montage("standard_1020", on_missing="ignore")
    raw.save(out / "sub-07_task-auditory_meg.fif", overwrite=True)


def edf_clinical():
    out = ROOT / "edf_clinical"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    raw = src()
    fs = int(raw.info["sfreq"])
    sigs, heads = [], []
    for name in raw.ch_names:
        x = raw.get_data(picks=[name])[0] * 1e6
        sigs.append(x)
        heads.append(dict(label=f"EEG {name}-REF"[:16], dimension="uV", sample_frequency=fs,
                          physical_min=-3200.0, physical_max=3200.0, digital_min=-32768, digital_max=32767,
                          transducer="AgAgCl electrode", prefilter="HP:0.1Hz LP:70Hz"))
    n = len(sigs[0])
    sigs.append(300 * np.sin(2 * np.pi * 1.2 * np.arange(n) / fs))
    heads.append(dict(label="EKG", dimension="uV", sample_frequency=fs, physical_min=-3200.0, physical_max=3200.0,
                      digital_min=-32768, digital_max=32767, transducer="", prefilter=""))
    sigs.append(70 + np.zeros(n // fs))
    heads.append(dict(label="Pulse Rate", dimension="bpm", sample_frequency=1, physical_min=0.0, physical_max=250.0,
                      digital_min=-32768, digital_max=32767, transducer="", prefilter=""))
    w = pyedflib.EdfWriter(str(out / "PATIENT_0042 routine EEG.edf"), len(sigs), file_type=pyedflib.FILETYPE_EDFPLUS)
    w.setSignalHeaders(heads)
    w.setPatientName("Doe^Jane")
    w.setBirthdate("04.05.1971")
    w.writeSamples(sigs)
    for t, label in [(10, "Eyes closed"), (40, "Photic 10 Hz"), (70, "Hyperventilation")]:
        w.writeAnnotation(t, -1, label)
    w.close()


def set_renamed():
    out = ROOT / "set_renamed"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    s = sorted(glob.glob("D:/MDD2_ds003478/sub-*/eeg/*.set"))[2]
    shutil.copy(s, out / "patient-A.set")
    shutil.copy(s[:-4] + ".fdt", out / "patient-A.fdt")


if __name__ == "__main__":
    for fn in (brainvision, fif_meg, edf_clinical, set_renamed):
        fn()
    for p in sorted(ROOT.rglob("*.*")):
        print(f"{p.stat().st_size / 1e6:7.2f} MB  {p.relative_to(ROOT)}")
