"""Round-trip every format: group -> read -> classify -> write -> read back."""
import sys
import tempfile
from pathlib import Path

import mne
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import formats  # noqa: E402

mne.set_log_level("ERROR")
SAMPLES = ROOT / "tests" / "samples"


def check_dir(folder):
    specs, notes = formats.group_uploads(sorted((SAMPLES / folder).iterdir()))
    assert not notes, notes
    ok = True
    for spec in specs:
        raw = formats.read(spec)
        eeg, passthrough, why = formats.classify_channels(raw)
        n_ann = len(raw.annotations)
        with tempfile.TemporaryDirectory() as tmp:
            files = formats.write(raw, spec, Path(tmp))
            back_spec = formats.group_uploads(files)[0][0]
            back = formats.read(back_spec)
            same_shape = back.get_data().shape == raw.get_data().shape
            same_fs = back.info["sfreq"] == raw.info["sfreq"]
            r = np.corrcoef(back.get_data(picks=eeg).ravel(), raw.get_data(picks=eeg).ravel())[0, 1]
            ann_ok = len(back.annotations) == n_ann
        status = "OK " if (same_shape and same_fs and r > 0.9999 and ann_ok) else "FAIL"
        ok &= status == "OK "
        skipped = ", ".join(f"{n} ({w})" for n, w in why.items())
        print(f"  {status} {formats.FORMAT_LABEL[spec.fmt]:11s} {spec.header.name:30s} "
              f"files={len(files)} ch={len(raw.ch_names)} eeg={len(eeg)} fs={raw.info['sfreq']:.0f} "
              f"events={n_ann}->{len(back.annotations)} r={r:.6f}")
        if skipped:
            print(f"       pass-through: {skipped}")
    return ok


if __name__ == "__main__":
    results = [check_dir(d) for d in ("real", "cohort", "hd128")]
    print("ALL PASSED" if all(results) else "FAILURES")
