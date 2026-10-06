"""EEG file formats: group uploaded files into recordings, read them, classify channels,
and write processed data back in the format they arrived in.

Supported: EDF/EDF+ (.edf), BDF/BDF+ (.bdf), BrainVision (.vhdr + .vmrk + .eeg),
EEGLAB (.set, optionally + .fdt), and MNE/Neuromag FIF (.fif, .fif.gz).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import mne
import numpy as np

mne.set_log_level("ERROR")

HEADER_EXT = {".edf": "edf", ".bdf": "bdf", ".vhdr": "brainvision", ".set": "eeglab",
              ".fif": "fif", ".gz": "fif"}
COMPANION_EXT = {".vmrk", ".eeg", ".fdt"}
FORMAT_LABEL = {"edf": "EDF", "bdf": "BDF", "brainvision": "BrainVision",
                "eeglab": "EEGLAB", "fif": "FIF"}

# Channels that are typed as EEG in many files but are not scalp EEG.
NON_EEG_NAME = re.compile(
    r"(EOG|ECG|EKG|EMG|STATUS|TRIG|^STI|STIM|RESP|GSR|EDA|PULSE|PLETH|SPO2|SAO2|ACC|GYRO|"
    r"PHOTIC|MARKER|EVENT|AUX|EXG|THERM|BREATH|SNORE|LIGHT|^[XYZ]\d*$|^BIP|^MISC)")


@dataclass
class RecordingSpec:
    rid: str
    fmt: str
    header: Path
    companions: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        name = self.header.name
        for ext in (".fif.gz", ".vhdr", ".edf", ".bdf", ".set", ".fif"):
            if name.lower().endswith(ext):
                return name[: -len(ext)]
        return self.header.stem


def _ext(path: Path) -> str:
    n = path.name.lower()
    return ".gz" if n.endswith(".fif.gz") else path.suffix.lower()


def _vhdr_refs(vhdr: Path):
    """DataFile and MarkerFile names referenced inside a BrainVision header."""
    refs = {}
    for line in vhdr.read_text(errors="ignore").splitlines():
        m = re.match(r"\s*(DataFile|MarkerFile)\s*=\s*(.+?)\s*$", line, re.I)
        if m:
            refs[m.group(1).lower()] = m.group(2)
    return refs


def group_uploads(paths: list[Path]) -> tuple[list[RecordingSpec], list[str]]:
    """Group uploaded files into recordings; report orphan companion files."""
    by_lower = {p.name.lower(): p for p in paths}
    claimed, specs, notes = set(), [], []
    headers = sorted([p for p in paths if _ext(p) in HEADER_EXT
                      and not (_ext(p) == ".gz" and not p.name.lower().endswith(".fif.gz"))],
                     key=lambda p: p.name.lower())
    for i, h in enumerate(headers):
        fmt = HEADER_EXT[_ext(h)]
        spec = RecordingSpec(rid=f"r{i + 1}", fmt=fmt, header=h)
        claimed.add(h.name.lower())
        if fmt == "brainvision":
            refs = _vhdr_refs(h)
            for kind in ("datafile", "markerfile"):
                ref = refs.get(kind)
                if not ref:
                    continue
                comp = by_lower.get(Path(ref).name.lower())
                if comp is None:
                    spec.warnings.append(f"missing {Path(ref).name} referenced by {h.name}")
                else:
                    spec.companions.append(comp)
                    claimed.add(comp.name.lower())
        elif fmt == "eeglab":
            fdt = by_lower.get(f"{spec.base}.fdt".lower())
            if fdt is not None:
                spec.companions.append(fdt)
                claimed.add(fdt.name.lower())
        specs.append(spec)
    for p in paths:
        if p.name.lower() not in claimed:
            if _ext(p) in COMPANION_EXT:
                notes.append(f"{p.name} has no matching header file and was ignored")
            else:
                notes.append(f"{p.name} is not a supported EEG file and was ignored")
    return specs, notes


def read(spec: RecordingSpec, preload: bool = True):
    """Read a recording with MNE. Epoched files raise a clear error."""
    try:
        return mne.io.read_raw(spec.header, preload=preload, verbose="ERROR")
    except Exception as exc:  # noqa: BLE001 - surface MNE's reason to the user
        msg = str(exc)
        if "trials" in msg.lower() or "epoch" in msg.lower():
            raise ValueError("this file holds epoched data; upload the continuous recording") from exc
        if "fdt" in msg.lower() or "no such file" in msg.lower():
            raise ValueError(f"a companion file is missing ({msg})") from exc
        raise ValueError(f"could not read {spec.header.name}: {msg}") from exc


def classify_channels(raw, sample_seconds: float = 60.0):
    """Split channels into SANE-processed scalp EEG and untouched pass-through channels."""
    types = raw.get_channel_types()
    bads = set(raw.info["bads"])
    n = min(raw.n_times, int(sample_seconds * raw.info["sfreq"]))
    eeg, passthrough, reasons = [], [], {}
    for i, (name, kind) in enumerate(zip(raw.ch_names, types)):
        key = "".join(name.split()).upper()
        if kind != "eeg":
            reasons[name] = f"{kind} channel"
        elif name in bads:
            reasons[name] = "marked bad"
        elif NON_EEG_NAME.search(key):
            reasons[name] = "non-EEG signal (by name)"
        else:
            eeg.append(i)
            continue
        passthrough.append(i)
    if eeg and raw.preload:
        sd = raw._data[eeg, :n].std(1)
        flat = [eeg[k] for k in np.nonzero(sd < 1e-10)[0]]
        for i in flat:
            reasons[raw.ch_names[i]] = "flat signal"
        eeg = [i for i in eeg if i not in flat]
        passthrough = sorted(passthrough + flat)
    return eeg, passthrough, reasons


def bad_sample_mask(raw):
    """Boolean mask of samples covered by BAD_* annotations."""
    mask = np.zeros(raw.n_times, bool)
    fs, first = raw.info["sfreq"], raw.first_time
    for ann in raw.annotations:
        if ann["description"].lower().startswith("bad"):
            a = int(round((ann["onset"] - first) * fs))
            b = int(round((ann["onset"] - first + ann["duration"]) * fs))
            mask[max(0, a):max(0, min(raw.n_times, b))] = True
    return mask


# Standard montages tried when a file stores no positions. Proprietary layouts
# (Biosemi A1..D32, EGI E1..E256) reuse labels that mean other places in 10-05
# (A1 is an earlobe there), so they must explain nearly every channel to be used.
_MONTAGES = (["standard_1005"]
             + [f"biosemi{n}" for n in (16, 32, 64, 128, 160, 256)]
             + [f"GSN-HydroCel-{n}" for n in ("32", "64_1", "65_1", "128", "129", "256", "257")])


def _montage_lookup(name):
    try:
        ch_pos = mne.channels.make_standard_montage(name).get_positions()["ch_pos"]
    except Exception:  # noqa: BLE001 - montage not shipped with this MNE version
        return {}
    return {"".join(k.split()).upper(): v for k, v in ch_pos.items()}


def _label_key(name):
    return re.sub(r"^EEG", "", "".join(name.split()).upper()).split("-")[0]


def electrode_positions(raw, picks):
    """3-D positions (metres) for picked channels.

    Positions stored in the file win. Otherwise the standard montage that explains
    the most channel names is used; Biosemi/EGI layouts need a 90% match.
    """
    pos = np.full((len(picks), 3), np.nan)
    for k, i in enumerate(picks):
        loc = raw.info["chs"][i]["loc"][:3]
        if np.all(np.isfinite(loc)) and np.linalg.norm(loc) > 1e-6:
            pos[k] = loc
    missing = np.nonzero(np.isnan(pos[:, 0]))[0]
    if missing.size:
        keys = [_label_key(raw.ch_names[picks[k]]) for k in missing]
        best, best_hits = None, 0
        for name in _MONTAGES:
            lookup = _montage_lookup(name)
            hits = sum(key in lookup for key in keys)
            proprietary = name != "standard_1005"
            if proprietary and hits < 0.9 * len(keys):
                continue
            if hits > best_hits:
                best, best_hits = lookup, hits
        if best:
            for k, key in zip(missing, keys):
                if key in best:
                    pos[k] = best[key]
    return pos


def output_name(spec: RecordingSpec, suffix: str = "_sane") -> str:
    ext = {"edf": ".edf", "bdf": ".bdf", "brainvision": ".vhdr",
           "eeglab": ".set", "fif": ".fif"}[spec.fmt]
    base = spec.base
    if spec.fmt == "fif":
        for tail in ("_raw", "-raw", "_eeg", "-eeg"):
            if base.endswith(tail):
                return f"{base[: -len(tail)]}{suffix}{tail}.fif"
        return f"{base}{suffix}_raw.fif"
    return f"{base}{suffix}{ext}"


def write(raw, spec: RecordingSpec, out_dir: Path, suffix: str = "_sane") -> list[Path]:
    """Write raw in the recording's original format; return every file produced."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / output_name(spec, suffix)
    if spec.fmt == "fif":
        raw.save(path, overwrite=True, verbose="ERROR")
        return [path]
    if spec.fmt in ("edf", "bdf"):
        mne.export.export_raw(path, raw, fmt=spec.fmt, physical_range="channelwise",
                              overwrite=True, verbose="ERROR")
        return [path]
    if spec.fmt == "brainvision":
        mne.export.export_raw(path, raw, fmt="brainvision", overwrite=True, verbose="ERROR")
        return [path, path.with_suffix(".vmrk"), path.with_suffix(".eeg")]
    if spec.fmt == "eeglab":
        mne.export.export_raw(path, raw, fmt="eeglab", overwrite=True, verbose="ERROR")
        return [p for p in (path, path.with_suffix(".fdt")) if p.exists()]
    raise ValueError(f"unsupported format {spec.fmt}")


def suggest_subject(spec: RecordingSpec) -> str:
    m = re.search(r"(sub-[A-Za-z0-9]+)", spec.header.name)
    return m.group(1) if m else spec.base
