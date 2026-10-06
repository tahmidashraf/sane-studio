"""EEG file formats: group uploaded files into recordings, read them, classify channels,
and write processed data back in the format they arrived in.

Supported: EDF/EDF+ (.edf), BDF/BDF+ (.bdf), BrainVision (.vhdr + .vmrk + .eeg),
EEGLAB (.set, optionally + .fdt), and MNE/Neuromag FIF (.fif, .fif.gz).
"""
from __future__ import annotations

import re
import threading
import warnings
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

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
    r"(EOG|ECG|EKG|EMG|STATUS|TRIG|^STI|STIM|RESP|GSR|EDA|PULSE|PLET|SPO2|SAO2|ACC|GYRO|"
    r"PHOTIC|MARKER|EVENT|AUX|EXG|THERM|TEMP|BREATH|SNORE|LIGHT|^ERG|^ANA\d|^[XYZ]\d*$|^BIP|^MISC)")
# Electrodes that record EEG but are missing from MNE's 10-05 list: cerebellar (Neuroscan
# 64-channel caps), anterior temporal, inferior occipital, nasion, sphenoidal, nasopharyngeal.
EXTRA_ELECTRODES = {"CB1", "CB2", "CBZ", "T1", "T2", "O9", "O10", "NZ", "SP1", "SP2", "PG1", "PG2"}
# Units a scalp EEG channel can be stored in; anything else (nS, degC, bpm, Boolean...) is a sensor.
VOLT_SCALE = {"v": 1.0, "mv": 1e3, "uv": 1e6, "nv": 1e9}


def _volt_scale(unit):
    """Factor from volts to a file's voltage unit, or None if the unit is not a voltage."""
    u = str(unit or "").strip().lower().replace("µ", "u").replace("μ", "u")
    return VOLT_SCALE.get(u)


@dataclass
class RecordingSpec:
    rid: str
    fmt: str
    header: Path
    companions: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    writer: str = ""

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


def safe_name(name: str) -> str:
    """File name as stored on the server: no directories, conservative characters."""
    name = Path(str(name).replace("\\", "/")).name
    return re.sub(r"[^A-Za-z0-9._ \-()+]", "_", name).strip()


def _text_encoding(path: Path) -> str:
    """UTF-8 if the file decodes as UTF-8, otherwise Latin-1 (older BrainVision 'ANSI' files)."""
    try:
        path.read_bytes().decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def _read_text(path: Path) -> str:
    return path.read_bytes().decode(_text_encoding(path))


def _vhdr_refs(vhdr: Path):
    """DataFile and MarkerFile names referenced inside a BrainVision header."""
    refs = {}
    for line in _read_text(vhdr).splitlines():
        m = re.match(r"\s*(DataFile|MarkerFile)\s*=\s*(.+?)\s*$", line, re.I)
        if m:
            refs[m.group(1).lower()] = m.group(2)
    return refs


def _repoint_vhdr(vhdr: Path, kind: str, new_name: str):
    """Rewrite a DataFile/MarkerFile reference so MNE finds the uploaded companion."""
    key = "DataFile" if kind == "datafile" else "MarkerFile"
    text = _read_text(vhdr)
    text = re.sub(rf"(?im)^(\s*{key}\s*=)[^\r\n]*", lambda m: m.group(1) + new_name, text)
    vhdr.write_bytes(text.encode(_text_encoding(vhdr), errors="replace"))


def group_uploads(paths: list[Path]) -> tuple[list[RecordingSpec], list[str]]:
    """Group uploaded files into recordings; report orphan companion files."""
    by_lower = {p.name.lower(): p for p in paths}
    claimed, specs, notes = set(), [], []
    headers = sorted([p for p in paths if _ext(p) in HEADER_EXT
                      and not (_ext(p) == ".gz" and not p.name.lower().endswith(".fif.gz"))],
                     key=lambda p: p.name.lower())
    # later parts of a split FIF recording belong to its first part (MNE reads them through it)
    split_groups = {}
    for p in headers:
        part = _split_part(p.name) if _ext(p) == ".fif" else None
        if part:
            split_groups.setdefault(part[0], []).append((part[1], p))
    later = {}
    for parts in split_groups.values():
        parts.sort(key=lambda x: x[0])
        if len(parts) > 1 and parts[0][0] == 1:
            later[parts[0][1]] = [p for _, p in parts[1:]]
    continuation = {p for ps in later.values() for p in ps}
    headers = [h for h in headers if h not in continuation]
    for i, h in enumerate(headers):
        fmt = HEADER_EXT[_ext(h)]
        spec = RecordingSpec(rid=f"r{i + 1}", fmt=fmt, header=h)
        claimed.add(h.name.lower())
        for p in later.get(h, []):
            spec.companions.append(p)
            claimed.add(p.name.lower())
        if fmt == "brainvision":
            refs = _vhdr_refs(h)
            for kind, ext in (("datafile", ".eeg"), ("markerfile", ".vmrk")):
                ref = refs.get(kind)
                ref_name = Path(ref.replace("\\", "/")).name if ref else ""
                # exact name, then the name as sanitized on upload, then a file
                # renamed together with the header (same stem)
                comp = (by_lower.get(ref_name.lower()) or by_lower.get(safe_name(ref_name).lower())
                        or by_lower.get(f"{spec.base}{ext}".lower()))
                if comp is None:
                    if kind == "datafile":
                        spec.warnings.append(f"missing the data file ({ref_name or spec.base + ext}) for {h.name}")
                    continue
                if comp.name != ref_name:
                    _repoint_vhdr(h, kind, comp.name)
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


def _split_part(name):
    """(group key, part number) for split FIF files, else None.

    BIDS names parts `..._split-01_meg.fif`, `..._split-02_meg.fif`; MNE names them
    `x_raw.fif`, `x_raw-1.fif`, `x_raw-2.fif`.
    """
    low = name.lower()
    if not low.endswith(".fif"):
        return None
    m = re.match(r"^(.*_split-)(\d+)(_.*\.fif)$", low)
    if m:
        return (m.group(1), m.group(3)), int(m.group(2))
    m = re.match(r"^(.*(?:raw|meg|eeg|sss))-(\d+)\.fif$", low)
    if m:
        return (m.group(1), ".fif"), int(m.group(2)) + 1
    return (low[:-4], ".fif"), 1


def _check_integrity(spec):
    """Problems MNE would read straight past: other formats sharing an extension,
    lone parts of split recordings, cut-off data files."""
    head = spec.header.read_bytes()[:256]
    if spec.header.stat().st_size < 2048 and spec.fmt in ("edf", "bdf", "fif"):
        if b"annex/objects" in head or b"git-lfs" in head or b"version https://" in head:
            raise ValueError("this is a placeholder from DataLad/git-annex or Git LFS, not the recording; "
                             "fetch the real file first (e.g. `datalad get`)")
    if spec.fmt == "edf" and head.startswith(b"SR_RESEARCH"):
        raise ValueError("this is an EyeLink eye-tracker file (SR Research), which also uses the .edf "
                         "extension; it holds eye movements, not EEG")
    if spec.fmt == "fif":
        part = _split_part(spec.header.name)
        if part and part[1] > 1 and not spec.companions:
            raise ValueError(f"this is part {part[1]} of a recording split across several .fif files; "
                             "upload all parts together, including the first")
    if spec.fmt == "brainvision":
        text = _read_text(spec.header)
        eeg_file = next((c for c in spec.companions if c.suffix.lower() == ".eeg"), None)
        fmt = (_vhdr_value(text, "Binary Infos", "BinaryFormat") or "INT_16").upper()
        width = {"INT_16": 2, "UINT_16": 2, "INT_32": 4, "IEEE_FLOAT_32": 4}.get(fmt)
        try:
            nch = int(_vhdr_value(text, "Common Infos", "NumberOfChannels"))
        except (TypeError, ValueError):
            nch = 0
        if eeg_file is not None and width and nch and eeg_file.stat().st_size % (width * nch):
            raise ValueError(f"the data file {eeg_file.name} is incomplete: its size is not a whole number of "
                             f"samples for {nch} channels in {fmt}, so it was probably cut off while it was "
                             "being copied or downloaded")


def read(spec: RecordingSpec, preload: bool = True):
    """Read a recording with MNE. Epoched files raise a clear error."""
    _check_integrity(spec)
    try:
        return mne.io.read_raw(spec.header, preload=preload, verbose="ERROR")
    except Exception as exc:  # noqa: BLE001 - surface MNE's reason to the user
        msg = str(exc)
        low = msg.lower()
        if "trials" in low or "epoch" in low:
            raise ValueError("this file holds epoched data; upload the continuous recording") from exc
        if "fdt" in low or "no such file" in low or "does not exist" in low:
            raise ValueError(f"a companion file is missing: {msg}") from exc
        if "bad bdf" in low or "bad edf" in low or "header" in low:
            raise ValueError(f"{spec.header.name} is not a valid {FORMAT_LABEL[spec.fmt]} file "
                             "(the header could not be parsed; the file may be truncated or corrupted)") from exc
        raise ValueError(f"could not read {spec.header.name}: {msg}") from exc


def channel_summary(raw):
    """'306 MEG, 9 stim' style count of a recording's channel types."""
    from collections import Counter

    label = {"mag": "MEG", "grad": "MEG", "eeg": "EEG", "eog": "EOG", "ecg": "ECG", "emg": "EMG", "stim": "stim",
             "misc": "misc", "bio": "bio", "resp": "respiration"}
    counts = Counter(label.get(t, t) for t in raw.get_channel_types())
    return ", ".join(f"{n} {k}" for k, n in counts.most_common())


def classify_channels(raw, sample_seconds: float = 60.0):
    """Split channels into SANE-processed scalp EEG and untouched pass-through channels."""
    types = raw.get_channel_types()
    bads = set(raw.info["bads"])
    orig_units = getattr(raw, "_orig_units", None) or {}
    n = min(raw.n_times, int(sample_seconds * raw.info["sfreq"]))
    eeg, passthrough, reasons = [], [], {}
    for i, (name, kind) in enumerate(zip(raw.ch_names, types)):
        key = "".join(name.split()).upper()
        unit = orig_units.get(name)
        if kind != "eeg":
            reasons[name] = f"{kind} channel"
        elif name in bads:
            reasons[name] = "marked bad"
        elif unit and _volt_scale(unit) is None:
            reasons[name] = f"sensor, recorded in {unit}"
        elif NON_EEG_NAME.search(key):
            reasons[name] = "non-EEG signal (by name)"
        else:
            eeg.append(i)
            continue
        passthrough.append(i)
    # When the montage is named in 10-05 terms, an "EEG" channel whose name is no electrode
    # is a sensor recorded alongside it (lane position, steering, speed, ...), not scalp EEG.
    if len(eeg) >= 4:
        std = _montage_lookup("standard_1005")
        known = [_label_key(raw.ch_names[i]) in std or _label_key(raw.ch_names[i]) in EXTRA_ELECTRODES
                 for i in eeg]
        if sum(known) >= 0.8 * len(eeg):
            odd = [i for i, k in zip(eeg, known) if not k]
            for i in odd:
                reasons[raw.ch_names[i]] = "not a scalp electrode name (the other EEG channels use 10-05 names)"
            eeg = [i for i in eeg if i not in odd]
            passthrough = sorted(passthrough + odd)
    if eeg and raw.preload:
        # flat only if flat everywhere: six stretches spread over the whole recording, so
        # a silent start (amplifier settling) does not count; missing samples are ignored
        N, seg = raw.n_times, max(1, n // 6)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)          # all-missing stretches
            sds = np.vstack([np.nanstd(raw._data[eeg, a:a + seg], axis=1)
                             for a in np.linspace(0, max(0, N - seg), 6).astype(int)])
            sd = np.nan_to_num(np.nanmax(sds, axis=0), nan=0.0)
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


@lru_cache(maxsize=None)
def _montage_lookup(name):
    try:
        ch_pos = mne.channels.make_standard_montage(name).get_positions()["ch_pos"]
    except Exception:  # noqa: BLE001 - montage not shipped with this MNE version
        return {}
    return {"".join(k.split()).upper(): v for k, v in ch_pos.items()}


def _label_key(name):
    """'EEG Fp1-REF' -> 'FP1'; PhysioNet's padded 'Fc5.' / 'Cz..' -> 'FC5' / 'CZ'."""
    key = re.sub(r"^EEG", "", "".join(name.split()).upper())
    key = re.split(r"[-/:]", key)[0]
    return re.sub(r"[^A-Z0-9]", "", key)


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


def _write_edf_inplace(raw, spec, path, processed, anonymize):
    """Edit the original EDF/BDF: replace only the processed signals.

    Every header field (labels, units, transducers, prefiltering, per-signal sampling
    rates, record layout) and every untouched signal stays exactly as recorded.
    """
    import edfio

    edf = (edfio.read_bdf if spec.fmt == "bdf" else edfio.read_edf)(spec.header)
    sigs = list(edf.signals)
    if len(sigs) != len(raw.ch_names):
        raise ValueError("signal count differs from MNE's channel list")
    for i in processed:
        s, name = sigs[i], raw.ch_names[i]
        label = s.label.strip()
        if name != label and not name.startswith(label):
            raise ValueError(f"signal {label!r} does not match channel {name!r}")
        scale = _volt_scale(s.physical_dimension)
        if scale is None or s.sampling_frequency != raw.info["sfreq"]:
            raise ValueError(f"{label}: unit {s.physical_dimension!r} or rate not writable in place")
        new = s.data.copy()
        n = min(len(new), raw.n_times)
        new[:n] = raw._data[i, :n] * scale
        s.update_data(new)
    if anonymize:
        edf.anonymize()
    edf.write(path)
    return [path]


def _vhdr_value(text, section, key):
    current = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and "]" in s:
            current = s[1:s.index("]")].strip().lower()
        elif current == section.lower() and "=" in s and not s.startswith(";"):
            k, v = s.split("=", 1)
            if k.strip().lower() == key.lower():
                return v.strip()
    return None


def _write_bv_inplace(raw, spec, path, processed, anonymize):
    """Rewrite a BrainVision recording from its own files.

    The header and marker files are copied verbatim (only file references change), so
    marker types, channel properties and comments survive. The data file keeps its
    binary format, orientation and per-channel resolution; integer files are promoted
    to IEEE_FLOAT_32 only if the processed values would not fit.
    """
    text = _read_text(spec.header)
    if (_vhdr_value(text, "Common Infos", "DataFormat") or "BINARY").upper() != "BINARY":
        raise ValueError("only binary BrainVision data can be written in place")
    orient = (_vhdr_value(text, "Common Infos", "DataOrientation") or "MULTIPLEXED").upper()
    nch = int(_vhdr_value(text, "Common Infos", "NumberOfChannels"))
    fmt = (_vhdr_value(text, "Binary Infos", "BinaryFormat") or "INT_16").upper()
    big = (_vhdr_value(text, "Common Infos", "UseBigEndianOrder") or "NO").upper() == "YES"
    base = {"INT_16": "i2", "UINT_16": "u2", "INT_32": "i4", "IEEE_FLOAT_32": "f4"}[fmt]
    dtype = np.dtype((">" if big else "<") + base)
    if nch != len(raw.ch_names):
        raise ValueError("channel count differs from MNE's channel list")
    res, scales = [], []
    for k in range(1, nch + 1):
        parts = (_vhdr_value(text, "Channel Infos", f"Ch{k}") or "").split(",")
        res.append(float(parts[2]) if len(parts) > 2 and parts[2].strip() else 1.0)
        scales.append(_volt_scale(parts[3] if len(parts) > 3 and parts[3].strip() else "µV"))
    eeg_file = next(c for c in spec.companions if c.suffix.lower() == ".eeg")
    vmrk_file = next((c for c in spec.companions if c.suffix.lower() == ".vmrk"), None)
    flat = np.fromfile(eeg_file, dtype=dtype)
    nsamp = flat.size // nch
    data = (flat[: nsamp * nch].reshape(nsamp, nch) if orient == "MULTIPLEXED"
            else flat[: nsamp * nch].reshape(nch, nsamp).T)
    n = min(nsamp, raw.n_times)
    new = {}
    for i in processed:
        if scales[i] is None:
            raise ValueError(f"channel {raw.ch_names[i]} is not stored in a voltage unit")
        new[i] = raw._data[i, :n] * scales[i] / res[i]
    out = data.copy()
    if dtype.kind in "iu":
        info = np.iinfo(dtype)
        if any(np.nanmax(v) > info.max or np.nanmin(v) < info.min for v in new.values()):
            out, fmt = data.astype((">" if big else "<") + "f4"), "IEEE_FLOAT_32"
    for i, v in new.items():
        out[:n, i] = np.round(v) if out.dtype.kind in "iu" else v
    eeg_out, vmrk_out = path.with_suffix(".eeg"), path.with_suffix(".vmrk")
    (out if orient == "MULTIPLEXED" else out.T).astype(out.dtype, copy=False).tofile(eeg_out)
    text = re.sub(r"(?im)^(\s*DataFile\s*=)[^\r\n]*", lambda m: m.group(1) + eeg_out.name, text)
    text = re.sub(r"(?im)^(\s*MarkerFile\s*=)[^\r\n]*", lambda m: m.group(1) + vmrk_out.name, text)
    text = re.sub(r"(?im)^(\s*BinaryFormat\s*=)[^\r\n]*", lambda m: m.group(1) + fmt, text)
    path.write_bytes(text.encode(_text_encoding(spec.header), errors="replace"))   # line endings as uploaded
    files = [path, eeg_out]
    if vmrk_file is not None:
        marks = _read_text(vmrk_file)
        marks = re.sub(r"(?im)^(\s*DataFile\s*=)[^\r\n]*", lambda m: m.group(1) + eeg_out.name, marks)
        if anonymize:   # drop the recording timestamp carried by "New Segment" markers
            marks = re.sub(r"(?im)^(Mk\d+=New Segment(?:,[^,\r\n]*){4}),\d+[ \t]*(?=\r?$)", r"\1", marks)
        vmrk_out.write_bytes(marks.encode(_text_encoding(vmrk_file), errors="replace"))
        files.insert(1, vmrk_out)
    return files


def write(raw, spec: RecordingSpec, out_dir: Path, suffix: str = "_sane",
          processed=None, anonymize: bool = False) -> list[Path]:
    """Write raw in the recording's original format; return every file produced.

    With `processed` (indices of channels that changed), EDF/BDF and BrainVision are
    written by editing the uploaded files, keeping every header detail and untouched
    channel exact. spec.writer records which path was used.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / output_name(spec, suffix)
    if processed is not None and spec.fmt in ("edf", "bdf", "brainvision"):
        try:
            fn = _write_bv_inplace if spec.fmt == "brainvision" else _write_edf_inplace
            files = fn(raw, spec, path, processed, anonymize)
            spec.writer = "original file, EEG samples replaced"
            return files
        except Exception as exc:  # noqa: BLE001 - fall back to a full export
            spec.warnings.append(f"wrote a fresh {FORMAT_LABEL[spec.fmt]} file ({exc})")
    if anonymize:
        raw.anonymize(verbose="ERROR")
    spec.writer = "MNE export"
    if spec.fmt == "fif":
        raw.save(path, overwrite=True, verbose="ERROR")
        # MNE splits files over 2 GB into path-1.fif, path-2.fif, ...
        return [path] + sorted(out_dir.glob(f"{path.stem}-*.fif"))
    if spec.fmt in ("edf", "bdf"):
        mne.export.export_raw(path, raw, fmt=spec.fmt, physical_range="channelwise",
                              overwrite=True, verbose="ERROR")
        return [path]
    if spec.fmt == "brainvision":
        mne.export.export_raw(path, raw, fmt="brainvision", overwrite=True, verbose="ERROR")
        return [path, path.with_suffix(".vmrk"), path.with_suffix(".eeg")]
    if spec.fmt == "eeglab":
        return _write_eeglab(raw, spec, path)
    raise ValueError(f"unsupported format {spec.fmt}")


_EEGLAB_LOCK = threading.Lock()
V5_LIMIT_BYTES = 1_900_000_000          # MATLAB v5 files hold at most 2 GB per variable


def _write_eeglab(raw, spec, path):
    """EEGLAB output in the uploaded layout, sized like the upload.

    MNE builds the EEGLAB structure (channels, locations, events) through eeglabio; the
    saving is done here. A .set + .fdt upload gets .set + .fdt back (raw float32 samples
    in the .fdt, no size limit). A single .set is written compressed, like MATLAB's own
    -v7 files, unless the data exceed the 2 GB per-variable limit of that format; then
    it is split into .set + .fdt as well and a note says so.
    """
    import eeglabio.raw
    from scipy.io import savemat

    two_files = any(c.suffix.lower() == ".fdt" for c in spec.companions)
    written = []

    def save(fname, eeg):
        data = np.asarray(eeg["data"], dtype=np.float32)
        nonlocal two_files
        if not two_files and data.nbytes > V5_LIMIT_BYTES:
            two_files = True
            spec.warnings.append("The data exceed the 2 GB limit of a single MATLAB file, so they were "
                                 "written as .set + .fdt, which EEGLAB opens the same way.")
        if two_files:
            fdt = Path(fname).with_suffix(".fdt")
            data.T.tofile(fdt)               # EEGLAB .fdt: float32, channels vary fastest
            eeg["data"] = fdt.name
            eeg["datfile"] = fdt.name
            written.append(fdt)
        savemat(fname, eeg, appendmat=False, do_compression=True)

    with _EEGLAB_LOCK, patch.object(eeglabio.raw, "_get_savemat", lambda fmt: save):
        mne.export.export_raw(path, raw, fmt="eeglab", overwrite=True, verbose="ERROR")
    return [path, *written]


def suggest_subject(spec: RecordingSpec) -> str:
    m = re.search(r"(sub-[A-Za-z0-9]+)", spec.header.name)
    return m.group(1) if m else spec.base
