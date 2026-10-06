"""Privacy and fidelity measurements, plus data the viewer needs (projections, envelopes)."""
from __future__ import annotations

import numpy as np
from scipy.stats import spearmanr

from .engine import corr_from_cov, norm_name


# ---------------------------------------------------------------- biomarkers
def biomarkers(P, bands, sel=None):
    """Scalp-level spectral biomarkers from band power P (C, nb), over channels sel."""
    names = [b[0] for b in bands]
    m = P if sel is None else P[sel]
    bp = m.mean(0)
    out = {}
    if "theta" in names and "beta" in names:
        out["theta_beta"] = float(np.log(bp[names.index("theta")] / bp[names.index("beta")]))
    if "theta" in names and "alpha" in names:
        out["theta_alpha"] = float(np.log(bp[names.index("theta")] / bp[names.index("alpha")]))
    if all(n in names for n in ("delta", "theta", "alpha", "beta")):
        slow = bp[names.index("delta")] + bp[names.index("theta")]
        fast = bp[names.index("alpha")] + bp[names.index("beta")]
        out["slowing"] = float(np.log(slow / fast))
    rel = bp / bp.sum()
    out["relative_power"] = {n: float(r) for n, r in zip(names, rel)}
    return out


def fingerprint_distance(R, Rref):
    """Mean over bands of the relative distance between correlation matrices."""
    d = []
    for b in range(R.shape[0]):
        Cr = corr_from_cov(Rref[b])
        off = np.linalg.norm(Cr - np.eye(len(Cr)))
        d.append(np.linalg.norm(corr_from_cov(R[b]) - Cr) / max(off, 1e-12))
    return float(np.mean(d))


def power_deviation(P, mu):
    """Median absolute log-ratio of channel band power to the reference."""
    return float(np.median(np.abs(np.log(np.clip(P, 1e-30, None) / np.clip(mu, 1e-30, None)))))


# ---------------------------------------------------------------- cohort measures
def _common(units):
    sets = [set(norm_name(n) for n in u["ch_names"]) for u in units]
    return sorted(set.intersection(*sets)) if sets else []


def _select(u, common):
    pos = {norm_name(n): i for i, n in enumerate(u["ch_names"])}
    return [pos[c] for c in common]


def trait_leakage_index(units, key):
    """Lambda with tau = 0: mean subject identifiability of log band-power features.

    units: list with subject, ch_names and feats under `key` (n_win, C, nb).
    Subjects are the groups; a subject's windows are pooled across its recordings.
    """
    common = _common(units)
    if len(common) < 2:
        return None
    by_subj = {}
    for u in units:
        f = np.log(np.clip(u[key][:, _select(u, common), :], 1e-30, None))
        by_subj.setdefault(u["subject"], []).append(f.reshape(len(f), -1))
    if len(by_subj) < 2:
        return None
    groups = [np.concatenate(v) for v in by_subj.values()]
    means = np.stack([g.mean(0) for g in groups])
    sb = means.var(0)
    sw = np.mean([g.var(0) for g in groups], axis=0)
    iota = sb / np.clip(sb + sw, 1e-30, None)
    return float(iota.mean())


def adaptive_attack(units, key, bands):
    """Leave-one-subject-out ridge recovering each subject's theta/beta (paper's attacker)."""
    names = [b[0] for b in bands]
    if "theta" not in names or "beta" not in names:
        return None
    common = _common(units)
    subj = {}
    for u in units:
        sel = _select(u, common)
        subj.setdefault(u["subject"], []).append((u[key][sel], u["P_in"][sel]))
    if len(subj) < 5:
        return None
    X, y = [], []
    th, be = names.index("theta"), names.index("beta")
    for recs in subj.values():
        feat = np.mean([np.log(np.clip(p, 1e-30, None)).ravel() for p, _ in recs], 0)
        pin = np.mean([p for _, p in recs], 0)
        X.append(feat)
        y.append(np.log(pin[:, th].mean() / pin[:, be].mean()))
    X, y = np.array(X), np.array(y)
    pred = np.zeros(len(y))
    for i in range(len(y)):
        tr = np.arange(len(y)) != i
        mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
        A = (X[tr] - mu) / sd
        w = np.linalg.solve(A.T @ A + 10.0 * np.eye(A.shape[1]), A.T @ (y[tr] - y[tr].mean()))
        pred[i] = ((X[i] - mu) / sd) @ w + y[tr].mean()
    if np.std(pred) < 1e-12:
        return {"rho": 0.0, "p": 1.0, "n": len(y)}
    rho, p = spearmanr(pred, y)
    return {"rho": float(rho), "p": float(p), "n": len(y)}


def cohort_report(units, bands):
    """Before/after measures across subjects (needs at least two)."""
    subjects = sorted({u["subject"] for u in units})
    out = {"n_subjects": len(subjects), "n_recordings": len(units)}
    if len(subjects) < 2:
        return out
    common = _common(units)
    out["common_channels"] = len(common)
    for stage in ("in", "out"):
        tb = {}
        for u in units:
            v = biomarkers(u[f"P_{stage}"], bands, _select(u, common)).get("theta_beta")
            tb.setdefault(u["subject"], []).append(v)
        out[f"theta_beta_{stage}"] = {s: float(np.mean(v)) for s, v in tb.items()}
        out[f"theta_beta_var_{stage}"] = float(np.var([np.mean(v) for v in tb.values()]))
        out[f"lambda_{stage}"] = trait_leakage_index(units, f"feats_{stage}")
        out[f"attack_{stage}"] = adaptive_attack(units, f"P_{stage}", bands)
    vin, vout = out["theta_beta_var_in"], out["theta_beta_var_out"]
    out["theta_beta_var_reduction"] = float(1 - vout / vin) if vin > 0 else None
    return out


# ---------------------------------------------------------------- viewer helpers
def project_2d(pos):
    """Azimuthal-equidistant projection of 3-D electrode positions onto a unit head disc."""
    out = np.full((len(pos), 2), np.nan)
    ok = np.all(np.isfinite(pos), axis=1)
    if ok.sum() < 4:
        return out
    q = pos[ok]
    # least-squares sphere fit: |p|^2 = 2 p.c + k
    A = np.hstack([2 * q, np.ones((len(q), 1))])
    sol = np.linalg.lstsq(A, (q ** 2).sum(1), rcond=None)[0]
    p = q - sol[:3]
    r = np.linalg.norm(p, axis=1)
    theta = np.arccos(np.clip(p[:, 2] / np.clip(r, 1e-12, None), -1, 1))
    phi = np.arctan2(p[:, 1], p[:, 0])
    xy = np.stack([theta * np.cos(phi), theta * np.sin(phi)], 1)
    scale = np.percentile(np.linalg.norm(xy, axis=1), 98)
    out[ok] = xy / max(scale, 1e-12) * 0.92
    return out


def envelope(data, n_bins=1200):
    """Per-bin RMS across channels, for the timeline overview."""
    C, N = data.shape
    edges = np.linspace(0, N, n_bins + 1).astype(int)
    env = np.empty(n_bins, dtype=np.float32)
    for i in range(n_bins):
        seg = np.asarray(data[:, edges[i]:max(edges[i + 1], edges[i] + 1)], dtype=np.float64)
        env[i] = np.sqrt(np.mean(seg ** 2))
    return env


def channel_fidelity(a, b, chunk=200_000):
    """Per-channel Pearson correlation between two (C, N) arrays, computed in chunks."""
    C, N = a.shape
    s = np.zeros((5, C))
    for k in range(0, N, chunk):
        x = np.asarray(a[:, k:k + chunk], np.float64)
        y = np.asarray(b[:, k:k + chunk], np.float64)
        s += np.stack([x.sum(1), y.sum(1), (x * x).sum(1), (y * y).sum(1), (x * y).sum(1)])
    sx, sy, sxx, syy, sxy = s
    cov = sxy / N - sx * sy / N ** 2
    vx, vy = sxx / N - (sx / N) ** 2, syy / N - (sy / N) ** 2
    return cov / np.sqrt(np.clip(vx * vy, 1e-60, None))
