---
title: SANE Studio
emoji: 🧠
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Neutralize disease biomarkers in EEG, same file format back
---

# SANE Studio

SANE Studio removes subject-level neurological biomarkers from EEG recordings and gives the data back in the format it arrived in. Upload EDF, BDF, BrainVision, EEGLAB or FIF files. SANE sets each subject's spectral profile and spatial covariance to a shared reference, so a marker such as an elevated theta/beta ratio no longer distinguishes that person. Trial-level brain dynamics, events, EOG/ECG/trigger channels, sampling rate and channel layout are kept, so the dataset still serves the purpose it was collected for.

## Supported formats

| Format | Upload | Returned as |
|---|---|---|
| EDF / EDF+ | `.edf` | `.edf` |
| BDF / BDF+ (Biosemi) | `.bdf` | `.bdf` |
| BrainVision | `.vhdr` + `.vmrk` + `.eeg` | `.vhdr` + `.vmrk` + `.eeg` (zip) |
| EEGLAB | `.set`, with `.fdt` if present | `.set` |
| MNE / Neuromag FIF | `.fif`, `.fif.gz` | `.fif` |

Drop several files, or a whole folder, at once. Companion files are matched to their header automatically, and recordings in different formats can share one session.

## What SANE does

SANE separates each recording into a **trait** (what stays constant for a subject, where disease biomarkers live) and a **state** (what changes from trial to trial, which a BCI or an event-related analysis needs). It then changes only the trait.

1. **Spectral standardization.** For every channel and band (δ, θ, α, β, γ), the subject's mean power is mapped onto the reference power with a subject-constant gain. Trial-to-trial fluctuations pass through unchanged.
2. **Spatial alignment.** Each subject's per-band channel covariance is mapped onto the reference with the Gaussian optimal-transport map. Of all the linear maps that reach the reference covariance, this one changes the signal the least.
3. **Exact band power.** Neighbouring bands share smooth transitions. SANE predicts the output's band power analytically from the input cross-spectra and corrects it until every channel lands on target.

Both operators are constant per subject, so they are applied to the whole continuous recording as a frequency-dependent spatial filter. The band weights add up to exactly one: at strength 0 the output is bit-identical to the input, and content below 1 Hz and above 45 Hz is preserved.

**Reference.** With two or more subjects, SANE aligns everyone to the cohort average, pooled channel by channel so slightly different montages still match. A recording whose channel names no other subject shares falls back to the normative reference, and the report says so. A single subject is aligned to a built-in normative profile: each channel keeps its own overall power and takes on the normative spectral shape.

**Spatial alignment** is automatic by default. It is on at full strength against a cohort, which is a measured reference. Against the normative profile it is off, because the only spatial target there is a generic head model, and alignment would rebuild each channel largely from its neighbours. On real recordings, that cost 15-40% of the within-band brain dynamics without improving biomarker removal. The slider sets it by hand, for example to also remove a single subject's spatial fingerprint.

**Robustness.** Fitting skips windows with amplifier saturation, spikes or flat signal, and anything annotated `BAD_*`. Channels typed as EEG but named like EOG, ECG/EKG, EMG or triggers are passed through untouched, as are flat and bad channels. Rank-deficient data (average reference, ICA-cleaned) is handled without amplifying null directions.

## Privacy report

Each session produces before/after measurements:

- **θ/β biomarker deviation** from the reference, per recording.
- **Spatial fingerprint**: distance between the subject's band correlations and the reference.
- **Between-subject θ/β variance**: with variance at zero, any threshold classifier on the biomarker is at chance (balanced accuracy Φ(d/2) with d → 0).
- **Trait Leakage Index Λ**: mean subject identifiability (intraclass correlation) of band-power features across the cohort.
- **Adaptive attacker**: a leave-one-subject-out ridge model trying to recover each subject's θ/β (5+ subjects).
- **Signal fidelity**: per-channel correlation between input and output on clean (artifact-free) samples, broadband and within each band. The within-band score, shown as **Brain dynamics kept**, ignores the per-band rescaling that is the biomarker change itself.

## Supported files

EDF/EDF+, BDF (Biosemi), BrainVision (`.vhdr` + `.vmrk` + `.eeg`), EEGLAB (`.set`, with or without `.fdt`) and FIF. You get back the same format. EDF, BDF and BrainVision outputs are made by editing your uploaded file, so only the EEG samples change. Header fields (labels, units, filters, sampling rates) are kept, apart from the personal fields you choose to strip and the physical range of processed channels. Every marker is kept, and every non-EEG channel stays bit-identical. EEGLAB and FIF are rewritten with MNE. Each format was tested end to end on public OpenNeuro recordings (`tests/test_downloaded.py`).

On a real depression dataset (OpenNeuro ds003478, three subjects), SANE cut between-subject θ/β variance by more than 99.99% and lowered Λ from 0.51 to 0.008, while each output channel kept a 0.98 correlation with its input.

## Data handling

Every session gets its own directory and is deleted automatically 60 minutes after its last use, or right away with **New session**. Files are never used for anything except producing your output. **Strip personal metadata** (on by default) removes name, birth date, IDs and the recording date from the returned file headers.

## Scope

SANE neutralizes subject-level spectral and spatial biomarkers. It is not a complete anonymization method: individual traits outside these statistics, such as the alpha peak frequency, can remain. Epoched files are not supported yet; upload the continuous recording.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Then open http://localhost:8000. The API is documented at `/api/docs`.

## Deploy

The repository builds as a container on any host. On Hugging Face Spaces, create a Space with the **Docker** SDK and push this repository; the front matter above configures it. Settings, via environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `SANE_TTL_MINUTES` | 60 | delete sessions after this much inactivity |
| `SANE_MAX_UPLOAD_MB` | 1024 | upload limit per session |
| `SANE_WORKERS` | 2 | sessions processed in parallel |
| `SANE_DATA_DIR` | `/tmp/sane-studio` | working storage |

## Tests

```bash
python tests/make_samples.py     # builds a multi-format test corpus from local data
python tests/test_formats.py     # round-trips every format
```

## Sample data

- `samples/depression-rest`: three subjects from OpenNeuro ds003478 (CC0), 60 s each, resampled to 250 Hz.
- `samples/hd128-motor`: synthetic 128-channel Biosemi recordings with a motor task; subject 3 carries an elevated theta biomarker.
