"""SANE engine: subject-level spectral and spatial trait neutralization for continuous EEG.

Stage 1 (spectral) rescales each channel's band power so the subject mean matches the
reference. Stage 2 (spatial) maps each subject's per-band covariance onto the reference
covariance with the Gaussian optimal-transport map, the alignment that changes the signal
least. Both operators are constant per subject, so they are applied to the continuous
recording as a frequency-dependent spatial filter built on a smooth band partition of
unity: at alpha = 0 the output equals the input, and content outside the bands is kept.
"""
from __future__ import annotations

import numpy as np
from numpy.fft import rfft, irfft, rfftfreq
from scipy.fft import next_fast_len

BAND_DEFS = (
    ("delta", 1.0, 4.0),
    ("theta", 4.0, 8.0),
    ("alpha", 8.0, 13.0),
    ("beta", 13.0, 30.0),
    ("gamma", 30.0, 45.0),
)

# Normative relative band-power profile (delta..gamma), the population mean over
# 9 healthy BCI IV-2a subjects; used when a montage has no cohort to align to.
NORMATIVE_REL = {"delta": 0.3383, "theta": 0.1804, "alpha": 0.2296,
                 "beta": 0.1941, "gamma": 0.0568}
KERNEL_LENGTH_M = 0.06          # spatial correlation length of the normative kernel
MIN_COHORT_COVERAGE = 0.5       # share of a unit's channels another subject must also have
WIN_S = 4.0                     # Welch window for statistics (0.25 Hz bins)
MIN_DURATION_S = 10.0


# ---------------------------------------------------------------- band partition
def edge_halfwidth(edge_hz: float) -> float:
    return max(0.5, 0.1 * edge_hz)


def _step(f, edge):
    h = edge_halfwidth(edge)
    x = np.clip((f - (edge - h)) / (2.0 * h), 0.0, 1.0)
    return 0.5 - 0.5 * np.cos(np.pi * x)


def active_bands(fs: float):
    """Bands whose upper transition fits below Nyquist."""
    nyq = fs / 2.0
    return [b for b in BAND_DEFS if b[2] + edge_halfwidth(b[2]) < 0.98 * nyq]


def band_weights(freqs, bands):
    """Smooth weights per band plus a residual; they sum to exactly one."""
    W = np.stack([_step(freqs, lo) - _step(freqs, hi) for _, lo, hi in bands])
    return W, 1.0 - W.sum(0)


# ---------------------------------------------------------------- statistics
def window_starts(N, fs):
    nper = int(round(WIN_S * fs))
    return np.arange(0, N - nper + 1, nper // 2), nper


def artifact_mask(data, fs, bad_samples=None):
    """Windows usable for fitting: not saturated, spiking, flat, or annotated bad.

    Each channel's per-window log-amplitude is scored against that channel's
    median/MAD. Thresholds relax if too little data would survive.
    """
    C, N = data.shape
    starts, nper = window_starts(N, fs)
    L = np.empty((len(starts), C))
    for i, s in enumerate(starts):
        seg = np.asarray(data[:, s:s + nper], dtype=np.float64)
        L[i] = np.log(seg.std(1) + 1e-30)
    med = np.median(L, 0)
    mad = 1.4826 * np.median(np.abs(L - med), 0) + 1e-3
    Z = (L - med) / mad
    annotated = np.zeros(len(starts), bool)
    if bad_samples is not None and np.any(bad_samples):
        cs = np.concatenate([[0], np.cumsum(bad_samples)])
        annotated = (cs[starts + nper] - cs[starts]) > 0
    for z_hi, z_med in ((6.0, 3.0), (10.0, 5.0), (np.inf, np.inf)):
        keep = (Z.max(1) < z_hi) & (Z.min(1) > -z_hi) & (np.abs(np.median(Z, 1)) < z_med) & ~annotated
        if keep.mean() >= 0.5 or (keep.sum() >= 8 and z_hi == np.inf):
            break
    if keep.sum() < 2:
        keep = ~annotated if (~annotated).sum() >= 2 else np.ones(len(starts), bool)
    return keep


def welch_stats(data, fs, bands, fmax=70.0, keep=None, max_batch_elems=6_000_000):
    """Band powers, band covariances, per-window band powers and PSD (Welch, Hann, 50%).

    data: (C, N) array-like in volts; keep: boolean mask over windows (artifact_mask).
    Returns dict with P (C, nb) mean band power, R (nb, C, C) band covariance,
    feats (n_win, C, nb) band power of kept windows, psd (C, F), freqs (F,),
    csd (F', C, C) per-bin cross-spectra, keep (mask used).
    """
    C, N = data.shape
    nper = int(round(WIN_S * fs))
    if N < max(nper, int(MIN_DURATION_S * fs)):
        raise ValueError(f"recording shorter than {MIN_DURATION_S:.0f} s")
    all_starts, _ = window_starts(N, fs)
    if keep is None:
        keep = np.ones(len(all_starts), bool)
    win = np.hanning(nper)
    U = float((win ** 2).sum())
    freqs = rfftfreq(nper, 1.0 / fs)
    W, _ = band_weights(freqs, bands)
    nb = len(bands)
    idx = [np.nonzero(W[b] > 1e-9)[0] for b in range(nb)]
    wb = [W[b][idx[b]] for b in range(nb)]
    band_scale = 2.0 / (U * nper)
    psd_keep = freqs <= min(fmax, fs / 2.0)

    # Cross-spectral matrices are kept per bin over the band range; they let us
    # predict the output statistics of any operator exactly (refine_operator).
    top = bands[-1][2] + edge_halfwidth(bands[-1][2])
    csd_sel = np.nonzero(freqs <= top)[0]
    starts = all_starts[keep]
    batch = max(1, int(max_batch_elems // (C * nper)))
    csd = np.zeros((len(csd_sel), C, C), dtype=np.complex128)
    psd = np.zeros((C, int(psd_keep.sum())))
    feats = np.empty((len(starts), C, nb), dtype=np.float32)
    for i0 in range(0, len(starts), batch):
        s = starts[i0:i0 + batch]
        seg = np.stack([np.asarray(data[:, k:k + nper], dtype=np.float64) for k in s])
        seg -= seg.mean(-1, keepdims=True)
        X = rfft(seg * win, axis=-1)                       # (B, C, F)
        mag2 = X.real ** 2 + X.imag ** 2
        psd += mag2[:, :, psd_keep].sum(0)
        Xs = X[:, :, csd_sel]
        csd += np.einsum("bcf,bdf->fcd", Xs, Xs.conj())
        for b in range(nb):
            Xb = X[:, :, idx[b]] * wb[b]
            feats[i0:i0 + len(s), :, b] = (Xb.real ** 2 + Xb.imag ** 2).sum(-1) * band_scale
    n = len(starts)
    csd /= n
    Wc = W[:, csd_sel] ** 2
    R = np.einsum("bf,fcd->bcd", Wc, csd.real) * band_scale
    psd *= 2.0 / (fs * U * n)
    P = np.stack([np.diag(R[b]) for b in range(nb)], axis=1)
    return {"P": P, "R": R, "feats": feats, "psd": psd, "freqs": freqs[psd_keep],
            "csd": csd, "csd_freqs": freqs[csd_sel], "scale": band_scale,
            "keep": keep, "starts": starts}


def predicted_band_power(M, csd, csd_freqs, bands, scale):
    """Exact Welch band power (C, nb) of the output of operator M, from the input CSD."""
    W, wres = band_weights(csd_freqs, bands)
    C = M.shape[1]
    Mf = np.einsum("bf,bij->fij", W, M) + wres[:, None, None] * np.eye(C)[None]
    diag = np.einsum("fij,fij->fi", Mf @ csd, Mf.conj()).real
    return ((W ** 2) @ diag * scale).T


def refine_operator(M, csd, csd_freqs, bands, scale, target, iters=8, tol=1e-3):
    """Rescale operator rows until each channel's output band power equals target (C, nb).

    Adjacent bands share smooth transitions, so the band operators blend there;
    this fixed-point correction removes the resulting bias in band power.
    """
    for _ in range(iters):
        P = predicted_band_power(M, csd, csd_freqs, bands, scale)
        corr = np.sqrt(np.clip(target, 1e-30, None) / np.clip(P, 1e-30, None))
        if np.max(np.abs(corr - 1.0)) < tol:
            break
        M = corr.T[:, :, None] * M
    return M


# ---------------------------------------------------------------- linear algebra
def _sym(A):
    return 0.5 * (A + A.T)


def sqrtm_psd(A):
    w, V = np.linalg.eigh(_sym(A))
    return (V * np.sqrt(np.clip(w, 0.0, None))) @ V.T


def corr_from_cov(R):
    d = np.sqrt(np.clip(np.diag(R), 1e-30, None))
    return R / np.outer(d, d)


def is_average_referenced(R_sum):
    """True when the covariance has a near-null direction along the all-ones vector."""
    C = R_sum.shape[0]
    w, V = np.linalg.eigh(_sym(R_sum))
    if w[-1] <= 0:
        return False
    return bool(w[0] / w[-1] < 1e-8 and abs(V[:, 0] @ (np.ones(C) / np.sqrt(C))) > 0.95)


def ot_map(S, Rref, rtol=1e-6, max_gain=10.0):
    """Gaussian optimal-transport map T with T S T = Rref, restricted to S's support.

    Directions where S has (numerically) no variance, e.g. the common mode of an
    average-referenced montage, are left untouched; gains are capped for stability.
    """
    C = S.shape[0]
    w, V = np.linalg.eigh(_sym(S))
    keep = w > rtol * w.max()
    if w.max() <= 0 or not keep.any():          # no signal in this band: nothing to align
        return np.eye(C)
    U, d = V[:, keep], w[keep]
    Rk = _sym(U.T @ Rref @ U)
    wr, Vr = np.linalg.eigh(Rk)
    Rk = (Vr * np.clip(wr, 1e-9 * max(wr.max(), 1e-30), None)) @ Vr.T
    sd = np.sqrt(d)
    mid = sqrtm_psd((sd[:, None] * Rk) * sd[None, :])
    Tk = (mid / sd[:, None]) / sd[None, :]
    wt, Vt = np.linalg.eigh(_sym(Tk))
    Tk = (Vt * np.clip(wt, 1.0 / max_gain, max_gain)) @ Vt.T
    return U @ Tk @ U.T + (np.eye(C) - U @ U.T)


def normative_kernel(pos, avg_ref):
    """Exponential spatial-correlation kernel from electrode positions (metres)."""
    C = pos.shape[0]
    d = np.linalg.norm(pos[:, None, :] - pos[None, :, :], axis=-1)
    K = np.exp(-d / KERNEL_LENGTH_M)
    if avg_ref:
        H = np.eye(C) - np.ones((C, C)) / C
        K = H @ K @ H
        K /= np.mean(np.diag(K))
    return K


# ---------------------------------------------------------------- operators
def normative_spatial(S_b, pos, avg_ref, min_located=0.6):
    """Normative correlation for one band, or None to keep the subject's own structure.

    Located channels follow the head-geometry kernel; channels without a position
    keep their own correlations with the rest (no position, no model to impose).
    """
    C = S_b.shape[0]
    ok = np.all(np.isfinite(pos), axis=1) if pos is not None else np.zeros(C, bool)
    if ok.sum() < max(4, min_located * C):
        return None
    K = corr_from_cov(S_b)
    K[np.ix_(ok, ok)] = normative_kernel(pos[ok], False)
    if avg_ref:
        H = np.eye(C) - np.ones((C, C)) / C
        K = H @ K @ H
        K /= np.mean(np.diag(K))
    return nearest_corr(K)


def build_operators(units, bands, alpha, reference="auto", spatial_model="kernel", spatial_alpha=None):
    """Per-unit band operators M (nb, C, C) and the reference used.

    units: list of dicts with keys key, subject, ch_names, P, R, weight, avg_ref, pos.
    reference: 'auto' | 'cohort' | 'normative'.
    spatial_model (normative only): 'kernel' (head geometry where positions are
      known, the subject's own structure elsewhere) | 'identity' (full whitening).
    alpha sets spectral neutralization; spatial_alpha sets how far the spatial covariance
    is moved, so channel-specific analyses can keep more of it. None means automatic:
    alpha for units aligned to a cohort (a measured reference) and 0 for normative units,
    where the only spatial target is a generic head model and alignment rebuilds each
    channel largely from its neighbours.
    """
    nb = len(bands)
    names = [b[0] for b in bands]
    rel = np.array([NORMATIVE_REL[n] for n in names])
    rel = rel / rel.sum()
    subjects = sorted({u["subject"] for u in units})
    use_cohort = reference in ("auto", "cohort") and len(subjects) >= 2
    if use_cohort:
        ix, mu_all, Cbar_all, cnt = cohort_reference(units, nb)

    out = {}
    for u in units:
        C = len(u["ch_names"])
        coverage, in_cohort = 0.0, False
        if use_cohort:
            idx = np.array([ix[norm_name(n)] for n in u["ch_names"]])
            shared = cnt[idx] >= 2
            coverage = float(np.mean(shared))
            # A montage no other subject shares has only itself as reference, which would
            # leave the recording unchanged; such units use the normative reference instead.
            in_cohort = coverage >= MIN_COHORT_COVERAGE
        if in_cohort:
            mu_u = mu_all[idx].copy()
            if not shared.all():
                # channels only this subject has: the cohort's spectral shape at their own power
                shape = (mu_u[shared] / mu_u[shared].sum(1, keepdims=True)).mean(0)
                mu_u[~shared] = u["P"][~shared].sum(1, keepdims=True) * shape[None, :]
            Rref_u = np.stack([np.sqrt(mu_u[:, b])[:, None]
                               * nearest_corr(Cbar_all[b][np.ix_(idx, idx)])
                               * np.sqrt(mu_u[:, b])[None, :] for b in range(nb)])
            mode = "cohort"
        else:
            # The normative reference fixes the spectral shape only. Each channel keeps its
            # own total power: amplitude across the scalp mostly reflects the reference
            # electrode and impedances, and forcing channels next to the reference up to the
            # scalp mean would pull their signal in from neighbours.
            mu_u = u["P"].sum(1, keepdims=True) * rel[None, :]                        # (C, nb)
            pos = u.get("pos")
            Ks = []
            for b in range(nb):
                if spatial_model == "identity":
                    K = np.eye(C)
                    if u["avg_ref"]:
                        K = np.eye(C) - np.ones((C, C)) / C
                        K /= np.mean(np.diag(K))
                else:
                    K = normative_spatial(u["R"][b], pos, u["avg_ref"])
                    if K is None:                       # no positions: keep own structure
                        K = corr_from_cov(u["R"][b])
                Ks.append(K)
            smodel = ("identity" if spatial_model == "identity"
                      else "kernel" if normative_spatial(u["R"][0], pos, u["avg_ref"]) is not None else "own-spatial")
            Rref_u = np.stack([np.sqrt(mu_u[:, b])[:, None] * Ks[b] * np.sqrt(mu_u[:, b])[None, :]
                               for b in range(nb)])
            mode = f"normative-{smodel}"
        sa = (alpha if in_cohort else 0.0) if spatial_alpha is None else float(spatial_alpha)
        M = np.empty((nb, C, C))
        for b in range(nb):
            S, Pb = u["R"][b], np.clip(u["P"][:, b], 1e-30, None)
            mub = np.clip(mu_u[:, b], 1e-30, None)
            # Stage 1: per-channel spectral gain toward the reference power.
            g1 = (mub / Pb) ** (alpha / 2.0)
            # Stage 2: OT alignment of the spatial structure (within the data's support).
            T = ot_map((g1[:, None] * S) * g1[None, :], Rref_u[b])
            A = ((1.0 - sa) * np.eye(C) + sa * T) * g1[None, :]
            # Renormalize so each channel's band power lands exactly on
            # P^(1-alpha) * mu^alpha, even when the data are rank-deficient.
            diag = np.clip(np.einsum("ij,jk,ik->i", A, S, A), 1e-30, None)
            g2 = np.sqrt(Pb ** (1.0 - alpha) * mub ** alpha / diag)
            M[b] = g2[:, None] * A
        if u.get("csd") is not None and alpha > 0:
            csd = u["csd"]() if callable(u["csd"]) else u["csd"]
            target = np.clip(u["P"], 1e-30, None) ** (1.0 - alpha) * np.clip(mu_u, 1e-30, None) ** alpha
            M = refine_operator(M, csd, u["csd_freqs"], bands, u["scale"], target)
        out[u["key"]] = {"M": M, "mode": mode, "Rref": Rref_u, "mu": mu_u, "coverage": coverage,
                         "n_subjects_in_reference": len(subjects) if in_cohort else 0,
                         "cohort_fallback": use_cohort and not in_cohort, "spatial_alpha": sa}
    return out


def norm_name(name):
    """Channel-name key for matching across files (case- and space-insensitive)."""
    return "".join(str(name).split()).upper()


def nearest_corr(Cm, floor=1e-6):
    """Closest valid correlation matrix: clip eigenvalues, restore the unit diagonal."""
    w, V = np.linalg.eigh(_sym(Cm))
    Cm = (V * np.clip(w, floor, None)) @ V.T
    d = np.sqrt(np.diag(Cm))
    return Cm / np.outer(d, d)


def cohort_reference(units, nb):
    """Pool band power and correlation by channel name, each subject weighted equally.

    Subjects may have different montages: a channel's reference power averages every
    subject that has it, and a pair's correlation every subject that has both.
    Returns (name -> index, mu (K, nb), Cbar (nb, K, K), subjects per channel (K,)).
    """
    names = sorted({norm_name(n) for u in units for n in u["ch_names"]})
    ix = {n: i for i, n in enumerate(names)}
    K = len(names)
    by_subject = {}
    for u in units:
        by_subject.setdefault(u["subject"], []).append(u)
    sumP, cntP = np.zeros((K, nb)), np.zeros(K)
    sumC, cntC = np.zeros((nb, K, K)), np.zeros((K, K))
    for us in by_subject.values():
        sp, cp = np.zeros((K, nb)), np.zeros(K)
        sc, cc = np.zeros((nb, K, K)), np.zeros((K, K))
        for u in us:
            idx = np.array([ix[norm_name(n)] for n in u["ch_names"]])
            sp[idx] += u["P"]
            cp[idx] += 1
            sc[:, idx[:, None], idx[None, :]] += np.stack([corr_from_cov(u["R"][b]) for b in range(nb)])
            cc[np.ix_(idx, idx)] += 1
        has, pair = cp > 0, cc > 0
        sumP[has] += sp[has] / cp[has, None]
        cntP[has] += 1
        sumC[:, pair] += sc[:, pair] / cc[pair]
        cntC[pair] += 1
    mu = sumP / np.clip(cntP, 1, None)[:, None]
    Cbar = np.where(cntC[None] > 0, sumC / np.clip(cntC, 1, None)[None], 0.0)
    for b in range(nb):
        np.fill_diagonal(Cbar[b], 1.0)
    return ix, mu, Cbar, cntP


# ---------------------------------------------------------------- application
def apply_operator(data_in, data_out, fs, bands, M, preserve_oob=True, avg_ref=False,
                   chunk_s=30.0, pad_s=5.0, progress=None):
    """Filter (C, N) data_in into data_out with Y(f) = sum_b w_b(f) M_b X(f) + w_res(f) X(f)."""
    C, N = data_in.shape
    core = max(1, int(round(chunk_s * fs)))
    pad = int(round(pad_s * fs))
    nb = len(bands)
    for a in range(0, N, core):
        b = min(N, a + core)
        lo, hi = max(0, a - pad), min(N, b + pad)
        seg = np.asarray(data_in[:, lo:hi], dtype=np.float64)
        left, right = pad - (a - lo), pad - (hi - b)
        if left > 0 or right > 0:
            mode = "reflect" if seg.shape[1] > max(left, right) else "edge"
            seg = np.pad(seg, ((0, 0), (left, right)), mode=mode)
        L = seg.shape[1]
        nfft = next_fast_len(L)
        X = rfft(seg, n=nfft, axis=1)
        freqs = rfftfreq(nfft, 1.0 / fs)
        W, wres = band_weights(freqs, bands)
        Y = X * wres if preserve_oob else np.zeros_like(X)
        for k in range(nb):
            sel = np.nonzero(W[k] > 0)[0]
            if sel.size:
                Y[:, sel] += W[k][sel] * (M[k] @ X[:, sel])
        y = irfft(Y, n=nfft, axis=1)[:, pad:pad + (b - a)]
        if avg_ref:
            y -= y.mean(0, keepdims=True)
        data_out[:, a:b] = y
        if progress:
            progress(b / N)
