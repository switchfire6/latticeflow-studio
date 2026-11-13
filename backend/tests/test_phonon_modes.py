"""Verification Gate 2: phonon engine (PROJECT_SPEC.md §4.4, §5.2, §7 Phase 2).

Physical assertions (Si stable; cubic BaTiO3 / SrTiO3 soft modes; frozen-phonon
energies) need MACE and carry ``requires_mace``. Under the analytic fallback
calculator only pipeline mechanics are asserted (shapes, Hermiticity, acoustic
sum rule, orthonormality, cancellation, async responsiveness).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import time

import numpy as np
import pytest
from ase import Atoms

import app.core.phonon_engine as pe
from app.core import geometry_analysis as geo
from app.core.phonon_engine import (
    PhononAnalysis,
    PhononCancelled,
    build_band_path,
    commensurate_multiples,
    run_phonon_analysis,
    run_phonon_analysis_async,
    stabilize_mode,
)
from app.presets import load_preset
from app.schemas.messages import F_TOL_THZ, Eigenmode, PhononAnalysisComplete

THZ_PER_SQRT_EV_A2_AMU = 15.633302300230191  # phonopy VaspToTHz: f[THz] = factor * sqrt(w2[eV/A^2/amu])
PRESETS = ["si", "batio3", "srtio3", "li3ps4"]

_CACHE: dict = {}


def _analysis(engine, preset: str) -> PhononAnalysis:
    key = (engine.backend_name, id(engine), preset)
    if key not in _CACHE:
        _CACHE[key] = run_phonon_analysis(load_preset(preset), engine)
    return _CACHE[key]


def _index_of(an: PhononAnalysis, name: str) -> int:
    return min(i for i, n in an.path.point_names.items() if n == name)


@pytest.fixture(params=["fallback", pytest.param("mace", marks=pytest.mark.requires_mace)])
def any_engine(request):
    return request.getfixturevalue(f"{request.param}_engine")


# --------------------------------------------------------------------------- #
# Gate 2 physics (MACE)
# --------------------------------------------------------------------------- #
@pytest.mark.requires_mace
def test_si_is_dynamically_stable(mace_engine):
    r = _analysis(mace_engine, "si").result
    assert r.min_frequency_thz >= -F_TOL_THZ
    assert r.is_dynamically_stable
    assert r.imaginary_mode_count == 0


@pytest.mark.requires_mace
def test_batio3_gamma_soft_mode(mace_engine):
    an = _analysis(mace_engine, "batio3")
    r = an.result
    assert r.min_frequency_thz < -1.0
    assert not r.is_dynamically_stable
    g = np.array(r.frequencies_thz[_index_of(an, "\\Gamma")])
    assert np.all(g[:3] < -1.0), g[:4]
    assert np.ptp(g[:3]) < 1e-3, "Gamma soft mode should be triply degenerate"
    assert g[3] > -F_TOL_THZ  # exactly three imaginary modes at Gamma
    assert r.imaginary_mode_count >= 3


@pytest.mark.requires_mace
def test_srtio3_antiferrodistortive_instability_at_R(mace_engine):
    an = _analysis(mace_engine, "srtio3")
    r = an.result
    f = np.array(r.frequencies_thz)
    assert r.min_frequency_thz < -1.0
    assert not r.is_dynamically_stable
    q_min = int(np.unravel_index(np.argmin(f), f.shape)[0])
    assert np.allclose(an.path.qpoints[q_min], [0.5, 0.5, 0.5])
    assert an.path.point_names.get(q_min) == "R"
    assert f[_index_of(an, "R")].min() < -1.0
    assert f[_index_of(an, "M")].min() < -1.0
    assert f[_index_of(an, "\\Gamma")].min() > -F_TOL_THZ  # only acoustic noise at Gamma


# --------------------------------------------------------------------------- #
# Payload contract (both backends, every preset)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("preset", PRESETS)
def test_payload_contract(any_engine, preset):
    atoms = load_preset(preset)
    n = len(atoms)
    an = _analysis(any_engine, preset)
    r = an.result
    dumped = r.model_dump_json()
    again = PhononAnalysisComplete.model_validate(json.loads(dumped))
    assert again == r

    f = np.array(r.frequencies_thz)
    n_q = len(r.kpath_distances)
    assert f.shape == (n_q, 3 * n)
    assert 100 <= n_q <= 200
    assert np.all(np.diff(f, axis=1) >= -1e-9), "frequencies must ascend within each q-point"
    d = np.array(r.kpath_distances)
    assert d[0] == 0.0 and np.all(np.diff(d) >= 0.0)

    idx = [lab.index for lab in r.kpath_labels]
    assert idx[0] == 0 and idx[-1] == n_q - 1
    assert all(0 <= i < n_q for i in idx) and idx == sorted(set(idx))
    for lab in r.kpath_labels:
        if "|" in lab.label:  # discontinuity: zero-length jump right after the tick
            assert d[lab.index + 1] == d[lab.index]
    # every zero-length jump is a labelled discontinuity
    for i in np.where(np.diff(d) == 0.0)[0]:
        assert any(lab.index == i and "|" in lab.label for lab in r.kpath_labels)

    # scalar summaries follow the field semantics
    n_imag = (f < -F_TOL_THZ).sum(axis=1)
    assert r.min_frequency_thz == f.min()
    assert r.imaginary_mode_count == n_imag.max()
    assert r.is_dynamically_stable == (n_imag.max() == 0)

    # eigenmodes: every band at each distinct labelled point
    labelled = {i for i in an.path.point_names}
    distinct = {tuple(np.round(an.path.qpoints[i], 9)) for i in labelled}
    assert len(r.eigenmodes) == len(distinct) * 3 * n
    for m in r.eigenmodes:
        assert m.q_index in labelled and m.q_label == an.path.point_names[m.q_index]
        assert m.frequency_thz == f[m.q_index][m.band_index]
        assert m.is_imaginary == (m.frequency_thz < -F_TOL_THZ)
        assert np.array(m.eigenvectors_real).shape == (n, 3)
        assert np.array(m.eigenvectors_imag).shape == (n, 3)
        assert np.allclose(m.q_frac, an.path.qpoints[m.q_index])


# --------------------------------------------------------------------------- #
# Linear algebra / conventions (both backends)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("preset", ["si", "batio3", "srtio3"])
def test_hermitian_orthonormal_and_eigen_equation(any_engine, preset):
    an = _analysis(any_engine, preset)
    n = an.n_atoms
    # Gamma, a generic (complex) path point, and the second high-symmetry point
    for q_index in (0, an.n_q // 3 + 1, an.result.kpath_labels[1].index):
        D = an.dynamical_matrix(q_index)
        assert np.abs(D - D.conj().T).max() <= 1e-10 * max(1.0, np.abs(D).max())
        freqs, modes = an.eigenvectors_at(q_index)
        assert np.allclose(freqs, an.frequencies[q_index], atol=1e-8)
        E = modes.reshape(3 * n, 3 * n).T  # columns = bands
        assert np.allclose(E.conj().T @ E, np.eye(3 * n), atol=1e-8)
        # D e = w^2 e with w^2 from the reported signed frequency
        w2 = np.sign(freqs) * (freqs / THZ_PER_SQRT_EV_A2_AMU) ** 2
        assert np.allclose(D @ E, E * w2[None, :], atol=1e-6 * max(1.0, np.abs(w2).max()))
        for band in range(3 * n):  # phase convention on the full-precision vectors
            flat = modes[band].reshape(-1)
            mags = np.abs(flat)
            k = int(np.flatnonzero(mags >= mags.max() * (1 - 1e-6))[0])
            assert flat[k].imag == 0.0 and flat[k].real > 0
        # payload vectors are rounded to 6 decimals: orthonormal to ~1e-5
        P = np.array([_payload_vector(an.eigenmode(q_index, b)) for b in range(3 * n)]).T
        assert np.allclose(P.conj().T @ P, np.eye(3 * n), atol=2e-5)
        for band in (0, 3 * n - 1):
            mode = an.eigenmode(q_index, band)
            assert mode.frequency_thz == an.result.frequencies_thz[q_index][band]
            assert np.allclose(_payload_vector(mode), modes[band].reshape(-1), atol=6e-7)


def _payload_vector(mode: Eigenmode) -> np.ndarray:
    return (np.array(mode.eigenvectors_real) + 1j * np.array(mode.eigenvectors_imag)).reshape(-1)


def _w2(freqs: np.ndarray) -> np.ndarray:
    return np.sign(freqs) * (freqs / THZ_PER_SQRT_EV_A2_AMU) ** 2


def _degenerate_groups(freqs: np.ndarray):
    return [list(range(a, b)) for a, b in pe.degenerate_groups(_w2(freqs))]


def _anchor_margin(v: np.ndarray) -> float:
    """1 - |c|/|c_max| for the largest component outside the phase tie window (inf if none)."""
    mags = np.sort(np.abs(v).ravel())[::-1]
    rest = mags[mags < mags[0] * (1 - pe._PHASE_TIE_RTOL)]
    return float(1 - rest[0] / mags[0]) if rest.size else float("inf")


def _basis_instabilities(base: PhononAnalysis, other: PhononAnalysis, near_tie_exempt: float = 0.0):
    """Every path point: degenerate grouping must be identical, and every vector may move only
    as much as the intrinsic motion of its set allows. For a set (one or more bands) whose
    projector moved by dsub and whose basis was fixed by an operator gap g, the vectors may move
    by at most 10 * dsub * (1 + 2/g) + 1e-9. A discontinuous choice (grouping change, basis jump
    inside a set, phase/sign flip) shows up as an O(0.1-1) change and violates this.

    ``near_tie_exempt`` > 0 exempts pure phase-anchor changes (vector unchanged up to its global
    phase) when the base vector's two largest components genuinely differ by less than that:
    the spec's "largest component real and positive" convention is undefined there.
    """
    n = base.n_atoms
    bad = []
    for i in range(base.n_q):
        f0, m0 = base.eigenvectors_at(i)
        f1, m1 = other.eigenvectors_at(i)
        g0, g1 = _degenerate_groups(f0), _degenerate_groups(f1)
        if g0 != g1:
            bad.append(("grouping", i, [g for g in g0 if len(g) > 1], [g for g in g1 if len(g) > 1]))
            continue
        E0, E1 = m0.reshape(3 * n, -1).T, m1.reshape(3 * n, -1).T
        raw = np.array(base.phonon.run_qpoints([base.path.qpoints[i]], with_eigenvectors=True).eigenvectors[0])
        for g in g0:
            P0 = E0[:, g] @ E0[:, g].conj().T
            P1 = E1[:, g] @ E1[:, g].conj().T
            dsub = np.abs(P1 - P0).max()
            gap = pe.symmetry_adapted_basis_with_conditioning(raw[:, g], n)[1] if len(g) > 1 else np.inf
            allowed = 10 * dsub * (1 + 2 / gap) + 1e-9
            for b in g:
                d = np.abs(E1[:, b] - E0[:, b]).max()
                if d <= allowed:
                    continue
                inv = np.sqrt(max(0.0, 1 - abs(np.vdot(E0[:, b], E1[:, b])) ** 2))
                if inv <= allowed and _anchor_margin(E0[:, b]) < near_tie_exempt:
                    continue
                bad.append(("basis jump" if len(g) > 1 else "phase flip", i, b, float(d), float(dsub), gap))
    return bad


class _SignalNoiseEngine:
    """Forces cached from a real engine, plus Gaussian noise of ``rel`` times the force-constant
    signal max|F(du) - F(0)| of each displaced supercell (F(0) is left exact). This is what
    run-to-run GPU noise does to phonopy's input, scaled up by orders of magnitude."""

    def __init__(self, inner, rel: float, seed: int):
        self.inner, self.rel, self.rng = inner, rel, np.random.default_rng(seed)
        self.f_perfect = None  # run_phonon_analysis evaluates the perfect supercell first

    def forces_batch(self, atoms_list):
        cache = _FORCE_CACHE.setdefault(id(self.inner), {})
        keys = [(a.numbers.tobytes(), a.cell.array.tobytes(), a.positions.tobytes()) for a in atoms_list]
        missing = [a for a, k in zip(atoms_list, keys) if k not in cache]
        if missing:
            for a, f in zip(missing, self.inner.forces_batch(missing)):
                cache[(a.numbers.tobytes(), a.cell.array.tobytes(), a.positions.tobytes())] = np.array(f)
        out = []
        for k in keys:
            f = cache[k].copy()
            if self.f_perfect is None:
                self.f_perfect = f
            elif self.rel:
                f += self.rel * np.abs(f - self.f_perfect).max() * self.rng.standard_normal(f.shape)
            out.append(f)
        return out


_FORCE_CACHE: dict = {}  # exact forces per (engine, structure), shared by all noisy runs


@pytest.mark.parametrize("preset", ["si", "batio3", "srtio3"])
def test_degenerate_subspace_rotation(any_engine, preset):
    """Rotated degenerate sets span the same subspace as phonopy's raw eigenvectors, stay
    orthonormal and satisfy D e = w^2 e; non-degenerate modes are phonopy's up to phase;
    the result is reproducible."""
    an = _analysis(any_engine, preset)
    n = an.n_atoms
    seen_degenerate = 0
    for q_index in sorted(set(an.path.point_names) | {0, an.n_q // 3 + 1, an.n_q // 2}):
        freqs, modes = an.eigenvectors_at(q_index)
        E = modes.reshape(3 * n, 3 * n).T
        raw = np.array(an.phonon.run_qpoints([an.path.qpoints[q_index]], with_eigenvectors=True).eigenvectors[0])
        D = an.dynamical_matrix(q_index)
        w2 = np.sign(freqs) * (freqs / THZ_PER_SQRT_EV_A2_AMU) ** 2
        assert np.allclose(E.conj().T @ E, np.eye(3 * n), atol=1e-10)
        assert np.allclose(D @ E, E * w2[None, :], atol=1e-8 * max(1.0, np.abs(w2).max()))
        for g in _degenerate_groups(freqs):
            if len(g) == 1:
                overlap = abs(np.vdot(raw[:, g[0]], E[:, g[0]]))
                assert np.isclose(overlap, 1.0, atol=1e-10), "non-degenerate mode must be untouched"
            else:
                seen_degenerate += 1
                Pr = raw[:, g] @ raw[:, g].conj().T
                Pe = E[:, g] @ E[:, g].conj().T
                assert np.allclose(Pr, Pe, atol=1e-10), "same degenerate subspace"
        _, again = an.eigenvectors_at(q_index)
        assert np.allclose(again, modes, rtol=0, atol=1e-13)  # reproducible call to call
    assert seen_degenerate > 0
    # run to run: a fresh analysis (real GPU / thread noise) may move vectors only by the
    # intrinsic, conditioning-bounded amount; no discrete choice may flip.
    fresh = run_phonon_analysis(load_preset(preset), any_engine)
    assert _basis_instabilities(an, fresh) == []


NOISE_PRESETS = ["si", "batio3", "srtio3"]


@pytest.mark.parametrize("preset", NOISE_PRESETS)
def test_basis_stable_under_injected_force_noise(any_engine, preset):
    """Regression for the degenerate-basis noise instability: 1e-8 relative noise on the
    displaced-supercell forces (orders of magnitude above real run-to-run noise) must not
    change the degenerate grouping, flip a basis choice or flip a phase anywhere on the path."""
    base = run_phonon_analysis(load_preset(preset), _SignalNoiseEngine(any_engine, 0.0, 0))
    for seed in (1, 2, 3):
        noisy = run_phonon_analysis(load_preset(preset), _SignalNoiseEngine(any_engine, 1e-8, seed))
        drift = np.abs(noisy.frequencies - base.frequencies).max()
        assert drift > 0  # the noise really reached the force constants
        assert _basis_instabilities(base, noisy) == [], f"seed {seed}, frequency drift {drift:.1e} THz"


def test_li3ps4_basis_stable_under_injected_force_noise(any_engine):
    """Low-symmetry (Pnma), unrelaxed 32-atom cell: same check. The only tolerated change is a
    global-phase re-anchoring where the two largest components of a mode genuinely agree to
    < 1e-4 (the spec's phase convention is undefined there; the vector itself is unchanged)."""
    base = run_phonon_analysis(load_preset("li3ps4"), _SignalNoiseEngine(any_engine, 0.0, 0))
    noisy = run_phonon_analysis(load_preset("li3ps4"), _SignalNoiseEngine(any_engine, 1e-8, 7))
    assert _basis_instabilities(base, noisy, near_tie_exempt=1e-4) == []


def _decades_from(values, tol: float) -> float:
    v = np.asarray([x for x in values if x > 0])
    return float(np.min(np.abs(np.log10(v) - np.log10(tol)))) if v.size else float("inf")


# Minimum distance (decades) between every decision quantity and its threshold. The pre-fix
# tolerances violate these on the presets: phase 0.0 (mace li3ps4) / 0.5 (fallback batio3),
# grouping 0.9 (mace li3ps4, absolute 1e-8), axis tie 1.3 (mace si).
#
# The phase window is checked on its two sides separately. "genuine" = component ratios above
# the window (real physics, deterministic). "ties" = ratios below it: symmetry-tied components,
# so the value is pure numerical error. In the unrelaxed Li3PS4 cell (max |F| 1.32 eV/A) that
# error is ~1e-9 and varies between processes (0.8e-9 ... 3.9e-9 observed with MACE), because
# genuinely split pairs only ~1e-6 apart make its eigenvectors sensitive at noise/gap. A fixed
# margin there would test GPU noise rather than the algorithm, so for li3ps4 the tie side is
# printed, not asserted (it sits ~1 decade below the window).
MIN_DECADES = {"grouping": 2.0, "axis tie": 2.0, "tie-break guard": 1.0,
               "phase window (genuine)": 1.0, "phase window (ties)": 1.0}
TIE_SIDE_NOT_ASSERTED = {"li3ps4"}


@pytest.mark.parametrize("preset", PRESETS)
def test_decision_margins(any_engine, preset):
    """Every discrete decision of the degenerate-basis construction is far from its threshold
    on every path point, so small noise cannot flip it (grouping, axis-character ties,
    tie-break-operator conditioning, the phase-anchor tie window)."""
    an = _analysis(any_engine, preset)
    n = an.n_atoms
    q = {k: [] for k in MIN_DECADES}
    for i in range(an.n_q):
        qp = an.phonon.run_qpoints([an.path.qpoints[i]], with_eigenvectors=True)
        f, V = qp.frequencies[0], qp.eigenvectors[0]
        lam = _w2(f)
        q["grouping"] += (np.diff(lam) / np.abs(lam).max()).tolist()
        for a, b in pe.degenerate_groups(lam):
            if b - a < 2:
                continue
            w, B = pe._rotate_by_operator(V[:, a:b], np.tile(pe._AXIS_WEIGHTS, n))
            q["axis tie"] += np.diff(w).tolist()
            for c0, c1 in pe._clusters(w, pe._OPERATOR_TIE_TOL):
                if c1 - c0 > 1:
                    wt, _ = pe._rotate_by_operator(B[:, c0:c1], pe._tie_break_operator(3 * n))
                    q["tie-break guard"] += np.diff(wt).tolist()
        for v in an.eigenvectors_at(i)[1]:
            mags = np.abs(v).ravel()
            r = 1 - mags / mags.max()
            q["phase window (genuine)"] += r[r >= pe._PHASE_TIE_RTOL].tolist()
            q["phase window (ties)"] += r[r < pe._PHASE_TIE_RTOL].tolist()
    tol = {"grouping": pe.DEGENERACY_RTOL_W2, "axis tie": pe._OPERATOR_TIE_TOL,
           "tie-break guard": pe._TIE_BREAK_MIN_GAP,
           "phase window (genuine)": pe._PHASE_TIE_RTOL, "phase window (ties)": pe._PHASE_TIE_RTOL}
    margins = {k: _decades_from(q[k], tol[k]) for k in MIN_DECADES}
    print(f"{preset}: " + ", ".join(f"{k} {m:.1f}" for k, m in margins.items()))
    for k, m in margins.items():
        if k == "phase window (ties)" and preset in TIE_SIDE_NOT_ASSERTED:
            continue
        assert m >= MIN_DECADES[k], (k, margins)


def test_degeneracy_grouping_is_relative():
    w2 = np.array([-0.3, -0.3 * (1 - 1e-15), 0.0, 1e-14, 1.2, 1.2 + 1e-13, 1.2 + 2e-13, 1.2 + 5e-8, 2.0])
    groups = pe.degenerate_groups(w2)
    assert groups == [(0, 2), (2, 4), (4, 7), (7, 8), (8, 9)]
    for scale in (1e-6, 1e6):  # unit / scale invariant
        assert pe.degenerate_groups(w2 * scale) == groups


def test_tie_break_guard_falls_back_to_best_seed(monkeypatch):
    rng = np.random.default_rng(5)
    A = rng.standard_normal((12, 12)) + 1j * rng.standard_normal((12, 12))
    E = np.linalg.qr(A)[0][:, :3]  # random 3-dim subspace of a 4-atom cell
    B_ref, gap_ref = pe.symmetry_adapted_basis_with_conditioning(E.copy(), 4)
    monkeypatch.setattr(pe, "_TIE_BREAK_MIN_GAP", np.inf)  # no seed is "good enough"
    B, gap = pe.symmetry_adapted_basis_with_conditioning(E.copy(), 4)
    assert np.allclose(B.conj().T @ B, np.eye(3), atol=1e-12)
    assert np.allclose(B @ B.conj().T, E @ E.conj().T, atol=1e-12)  # same subspace
    assert gap > 0 and np.isfinite(gap)


def _axis_fractions(v: np.ndarray) -> np.ndarray:
    return (np.abs(v) ** 2).sum(axis=0)


@pytest.mark.requires_mace
def test_batio3_soft_triplet_is_axis_aligned_polar(mace_engine):
    an = _analysis(mace_engine, "batio3")
    freqs, modes = an.eigenvectors_at(_index_of(an, "\\Gamma"))
    axes = []
    for b in range(3):
        v = modes[b]
        frac = _axis_fractions(v)
        assert frac.max() > 1 - 1e-8, frac  # displacement along a single Cartesian axis
        axis = int(np.argmax(frac))
        axes.append(axis)
        assert np.abs(v.imag).max() < 1e-10
        u = v.real[:, axis] / np.sqrt(an.masses)
        assert np.sign(u[1]) == -np.sign(u[2:]).mean()  # polar: Ti against the O cage
        assert abs((an.masses * u).sum()) < 1e-8  # no centre-of-mass motion
    assert axes == [0, 1, 2]  # deterministic order x, y, z


@pytest.mark.requires_mace
def test_srtio3_R_triplet_is_single_axis_rotation(mace_engine):
    an = _analysis(mace_engine, "srtio3")
    freqs, modes = an.eigenvectors_at(_index_of(an, "R"))
    bond_axis = {2: 2, 3: 1, 4: 0}  # O(.5,.5,0): Ti-O along z; O(.5,0,.5): y; O(0,.5,.5): x
    rot_axes = []
    for b in range(3):
        v = modes[b]
        assert np.abs(v[:2]).max() < 1e-8  # Sr, Ti at rest
        frac = _axis_fractions(v)
        axis = int(np.argmin(frac))  # rotation axis: no displacement along it
        rot_axes.append(axis)
        assert frac[axis] < 1e-10 and np.allclose(np.delete(frac, axis), 0.5, atol=1e-8)
        for k in (2, 3, 4):
            assert abs(v[k, bond_axis[k]]) < 1e-8  # O moves perpendicular to its Ti-O bond
            if bond_axis[k] == axis:
                assert np.abs(v[k]).max() < 1e-8  # the apical O on the rotation axis stays put
    assert rot_axes == [2, 1, 0]  # deterministic order: rotation about z, y, x


def test_payload_is_rounded(fallback_engine):
    r = _analysis(fallback_engine, "srtio3").result
    vals = list(r.kpath_distances) + [x for row in r.frequencies_thz for x in row] + [r.min_frequency_thz]
    for m in r.eigenmodes:
        vals.append(m.frequency_thz)
        vals.extend(x for row in m.eigenvectors_real + m.eigenvectors_imag for x in row)
    vals = np.array(vals)
    assert np.array_equal(np.round(vals, 6), vals)
    assert not any(json.dumps(x).startswith("-0.0") and x == 0 for x in vals.tolist())


@pytest.mark.parametrize("preset", PRESETS)
def test_fallback_mechanics_every_preset(fallback_engine, preset):
    an = _analysis(fallback_engine, preset)
    g = np.array(an.result.frequencies_thz[0])  # path starts at Gamma
    assert np.allclose(an.path.qpoints[0], 0.0)
    # acoustic sum rule: three translational modes at Gamma with |f| below tolerance
    assert int((np.abs(g) < F_TOL_THZ).sum()) >= 3, g[:6]
    D = an.dynamical_matrix(0)
    assert np.abs(D - D.conj().T).max() < 1e-10 * max(1.0, np.abs(D).max())
    # uniform translation is a zero mode of D(Gamma): D (sqrt(m) x unit vector) = 0
    sqrt_m = np.sqrt(np.repeat(an.masses, 3))
    for a in range(3):
        t = np.zeros(3 * an.n_atoms)
        t[a::3] = 1.0
        t = sqrt_m * t
        assert np.abs(D @ t).max() < 1e-6 * np.abs(D).max() * np.abs(t).max()


def test_input_atoms_not_mutated(fallback_engine):
    atoms = load_preset("batio3")
    atoms.positions += 0.3  # outside the [0, 1) convention on purpose
    before = (atoms.positions.copy(), atoms.cell.array.copy(), atoms.numbers.copy())
    an = run_phonon_analysis(atoms, fallback_engine)
    assert np.array_equal(atoms.positions, before[0])
    assert np.array_equal(atoms.cell.array, before[1])
    assert np.array_equal(atoms.numbers, before[2])
    assert atoms.calc is None
    assert len(an.result.frequencies_thz[0]) == 15


def test_bad_indices_raise(fallback_engine):
    an = _analysis(fallback_engine, "batio3")
    with pytest.raises(IndexError):
        an.eigenmode(an.n_q, 0)
    with pytest.raises(IndexError):
        an.eigenmode(0, 15)
    with pytest.raises(IndexError):
        an.eigenmode(-1, 0)
    with pytest.raises(ValueError):
        an.eigenmode(1.5, 0)  # type: ignore[arg-type]
    with pytest.raises(IndexError):
        an.instability_structure(0, 99)
    m = an.eigenmode(an.n_q // 2 + 1, 4)
    assert isinstance(m, Eigenmode)
    assert m.q_label is None or m.q_index in an.path.point_names


# --------------------------------------------------------------------------- #
# Band path construction
# --------------------------------------------------------------------------- #
def test_cubic_perovskite_path_points():
    path = build_band_path(load_preset("srtio3"))
    assert path.source == "seekpath"
    pts = {path.point_names[i]: tuple(path.qpoints[i]) for i in path.point_names}
    assert pts["\\Gamma"] == (0.0, 0.0, 0.0)
    assert pts["R"] == (0.5, 0.5, 0.5)
    assert sorted(pts["X"]) == [0.0, 0.0, 0.5]
    assert sorted(pts["M"]) == [0.0, 0.5, 0.5]
    labels = [lab.label for lab in path.labels]
    assert labels[0] == "\\Gamma" and any("|" in s for s in labels)


def test_path_is_in_basis_of_the_given_cell():
    """Tetragonal cell with the unique (long) axis along x: seekpath's standard Z = (0,0,1/2)
    must come back as (1/2,0,0) in the reciprocal basis of the cell we passed."""
    a, c = 3.9, 4.6
    atoms = Atoms("BaTiO3", cell=np.diag([c, a, a]), pbc=True,
                  scaled_positions=[(0, 0, 0), (0.52, 0.5, 0.5), (0.5, 0.5, 0), (0.5, 0, 0.5), (0.0, 0.5, 0.5)])
    path = build_band_path(atoms)
    assert path.source == "seekpath"
    pts = {path.point_names[i]: path.qpoints[i] for i in path.point_names}
    assert np.allclose(pts["Z"], [0.5, 0.0, 0.0]), pts
    assert np.allclose(pts["X"], [0.0, 0.5, 0.0]) or np.allclose(pts["X"], [0.0, 0.0, 0.5])
    # Cartesian length Gamma->Z equals 1/(2c) (crystallographic reciprocal, no 2 pi)
    cart = pts["Z"] @ np.linalg.inv(atoms.cell.array).T
    assert np.isclose(np.linalg.norm(cart), 1 / (2 * c))


def test_low_symmetry_cell_runs(fallback_engine):
    atoms = Atoms("LiF", cell=[[3.1, 0.1, 0.2], [0.3, 3.3, 0.1], [0.2, 0.4, 3.6]], pbc=True,
                  scaled_positions=[(0.02, 0.01, 0.0), (0.47, 0.53, 0.49)])
    an = run_phonon_analysis(atoms, fallback_engine)
    f = np.array(an.result.frequencies_thz)
    assert f.shape[1] == 6 and 100 <= f.shape[0] <= 200
    PhononAnalysisComplete.model_validate_json(an.result.model_dump_json())


def test_seekpath_failure_falls_back(fallback_engine, monkeypatch):
    import app.core.phonon_engine as pe

    def boom(_atoms):
        raise RuntimeError("seekpath exploded")

    monkeypatch.setattr(pe, "_seekpath_segments", boom)
    an = run_phonon_analysis(load_preset("batio3"), fallback_engine)
    assert an.path.source == "fallback"
    labels = [lab.label for lab in an.result.kpath_labels]
    assert labels == ["\\Gamma", "X|Y", "\\Gamma", "Z|R", "\\Gamma"]
    pts = {an.path.point_names[i]: tuple(an.path.qpoints[i]) for i in an.path.point_names}
    assert pts["X"] == (0.5, 0.0, 0.0) and pts["R"] == (0.5, 0.5, 0.5)


# --------------------------------------------------------------------------- #
# Frozen phonon: harmonic energy of a displaced supercell, dE = 1/2 w^2 Q^2
# --------------------------------------------------------------------------- #
FROZEN_TOL = 0.03  # relative; observed deviation <= 0.5% at 0.01 A with MACE float64

FROZEN_CASES = [
    pytest.param("mace", "batio3", "\\Gamma", 0, -1, id="mace-batio3-gamma-soft", marks=pytest.mark.requires_mace),
    pytest.param("mace", "si", "\\Gamma", -1, +1, id="mace-si-gamma-optical", marks=pytest.mark.requires_mace),
    pytest.param("mace", "srtio3", "R", 0, -1, id="mace-srtio3-R-soft", marks=pytest.mark.requires_mace),
    pytest.param("fallback", "batio3", "\\Gamma", -1, +1, id="fallback-batio3-gamma-top"),
]


@pytest.mark.parametrize("backend,preset,point,band,sign", FROZEN_CASES)
def test_frozen_phonon_energy(request, backend, preset, point, band, sign):
    """Displace the real supercell along the mode by +u and -u (max displacement 0.01 A).
    The symmetric energy change [E(+u)+E(-u)]/2 - E0 (odd terms cancel) must equal
    1/2 w^2 Q^2 with Q^2 = sum_i m_i |u_i|^2 over the supercell (mass-weighted normal
    coordinate of a unit-normalised e/sqrt(m) pattern) to within FROZEN_TOL."""
    engine = request.getfixturevalue(f"{backend}_engine")
    an = _analysis(engine, preset)
    q_index = _index_of(an, point)
    b = band % (3 * an.n_atoms)
    f = an.frequencies[q_index, b]
    assert np.sign(f) == sign and abs(f) > 1.0

    amp = 0.01
    plus = an.instability_structure(q_index, b, amplitude=amp)
    ref = an.instability_structure(q_index, b, amplitude=1e-12)  # same supercell, undisplaced
    u = plus.positions - ref.positions
    assert np.isclose(np.linalg.norm(u, axis=1).max(), amp, rtol=1e-6)
    minus = ref.copy()
    minus.positions = ref.positions - u

    e0 = engine.energy_forces_stress(ref)[0]
    ep = engine.energy_forces_stress(plus)[0]
    em = engine.energy_forces_stress(minus)[0]
    measured = 0.5 * (ep + em) - e0
    q2 = float(np.sum(plus.get_masses()[:, None] * u**2))
    w2 = np.sign(f) * (f / THZ_PER_SQRT_EV_A2_AMU) ** 2
    predicted = 0.5 * w2 * q2
    print(f"{request.node.callspec.id}: f={f:.3f} THz predicted={predicted*1e3:.4f} meV "
          f"measured={measured*1e3:.4f} meV ratio={measured/predicted:.4f}")
    assert np.sign(measured) == sign
    assert abs(measured / predicted - 1.0) < FROZEN_TOL


# --------------------------------------------------------------------------- #
# instability_structure
# --------------------------------------------------------------------------- #
@pytest.mark.requires_mace
def test_instability_structure_batio3_gamma(mace_engine):
    an = _analysis(mace_engine, "batio3")
    s = an.instability_structure(_index_of(an, "\\Gamma"), 0, amplitude=0.1)
    assert len(s) == 5
    assert np.allclose(s.cell.array, an.atoms.cell.array)
    disp = s.positions - an.atoms.positions
    assert np.isclose(np.linalg.norm(disp, axis=1).max(), 0.1, rtol=1e-9)
    # optical mode: orthogonal to rigid translation, so the centre of mass does not move
    com_shift = (an.masses[:, None] * disp).sum(axis=0) / an.masses.sum()
    assert np.abs(com_shift).max() < 1e-6
    _, number = geo.get_spacegroup(s)
    assert number < 221


@pytest.mark.requires_mace
def test_instability_structure_srtio3_R_rotation(mace_engine):
    an = _analysis(mace_engine, "srtio3")
    q_index = _index_of(an, "R")
    s = an.instability_structure(q_index, 0, amplitude=0.1)
    assert len(s) == 40
    assert np.allclose(s.cell.array, 2 * an.atoms.cell.array)
    mult, u = an.mode_displacements(q_index, 0)
    assert mult == (2, 2, 2)
    u = u * (0.1 / np.linalg.norm(u, axis=1).max())
    u = u.reshape(2, 2, 2, 5, 3)
    # alternating pattern: neighbouring cells along every axis move in antiphase
    assert np.allclose(u[1], -u[0], atol=1e-9)
    assert np.allclose(u[:, 1], -u[:, 0], atol=1e-9)
    assert np.allclose(u[:, :, 1], -u[:, :, 0], atol=1e-9)
    # pure oxygen octahedral rotation: Sr/Ti stay put, O moves perpendicular to its Ti-O bond
    assert np.abs(u[..., :2, :]).max() < 1e-3 * 0.1
    assert np.abs(u[..., 2:, :]).max() > 0.05
    for k, bond_axis in ((2, 2), (3, 1), (4, 0)):  # O at (.5,.5,0): Ti-O along z, etc.
        assert np.abs(u[..., k, bond_axis]).max() < 1e-3 * 0.1
    assert np.isclose(np.linalg.norm(s.positions - an.instability_structure(q_index, 0, 1e-12).positions,
                                     axis=1).max(), 0.1)


def test_non_commensurate_q_raises(fallback_engine):
    an = _analysis(fallback_engine, "srtio3")
    bad = [i for i, q in enumerate(an.path.qpoints) if not _is_commensurate(q)]
    assert bad
    with pytest.raises(ValueError, match="commensurate"):
        an.instability_structure(bad[0], 0)
    assert commensurate_multiples([0.5, 0.0, 0.25]) == (2, 1, 4)
    assert commensurate_multiples([1.0, -0.5, 1 / 3]) == (1, 2, 3)
    with pytest.raises(ValueError):
        commensurate_multiples([0.2, 0.0, 0.0])
    with pytest.raises(ValueError):
        an.instability_structure(0, 0, amplitude=0.0)


def _is_commensurate(q) -> bool:
    try:
        commensurate_multiples(q)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Cancellation & async
# --------------------------------------------------------------------------- #
class _CountingEngine:
    """Delegates to a real engine, counts forces_batch calls, optionally sleeps per call."""

    def __init__(self, inner, delay: float = 0.0, on_call=None):
        self.inner, self.delay, self.on_call = inner, delay, on_call
        self.calls = 0
        self.structures = 0

    def forces_batch(self, atoms_list):
        self.calls += 1
        self.structures += len(atoms_list)
        if self.on_call:
            self.on_call(self.calls)
        if self.delay:
            time.sleep(self.delay)
        return self.inner.forces_batch(atoms_list)

    def energy_forces_stress(self, atoms):
        return self.inner.energy_forces_stress(atoms)


def test_cancel_before_start(fallback_engine):
    eng = _CountingEngine(fallback_engine)
    with pytest.raises(PhononCancelled):
        run_phonon_analysis(load_preset("srtio3"), eng, should_cancel=lambda: True)
    assert eng.calls == 0


def test_cancel_between_chunks_is_prompt(fallback_engine):
    flag = threading.Event()
    eng = _CountingEngine(fallback_engine, on_call=lambda n: flag.set())
    t0 = time.perf_counter()
    with pytest.raises(PhononCancelled):
        run_phonon_analysis(load_preset("li3ps4"), eng, should_cancel=flag.is_set)  # 28 supercells
    assert eng.calls == 1  # stopped right after the first chunk
    assert time.perf_counter() - t0 < 10.0


async def test_async_wrapper_keeps_loop_responsive(fallback_engine):
    eng = _CountingEngine(fallback_engine, delay=0.15)  # >= 0.3 s of blocking work in the thread
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    t0 = time.perf_counter()
    an = await run_phonon_analysis_async(load_preset("batio3"), eng)
    elapsed = time.perf_counter() - t0
    done.set()
    await task
    assert isinstance(an, PhononAnalysis) and eng.calls >= 1
    assert elapsed >= 0.15
    # a blocked loop would tick ~once; a responsive one ~elapsed/10 ms (allow 4x slack for Windows timers)
    assert ticks >= 0.25 * elapsed / 0.01, (ticks, elapsed)


async def test_async_task_cancellation_stops_worker(fallback_engine):
    eng = _CountingEngine(fallback_engine, delay=0.2)
    task = asyncio.create_task(run_phonon_analysis_async(load_preset("li3ps4"), eng))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    calls_at_cancel = eng.calls
    await asyncio.sleep(0.6)  # worker thread hits its next checkpoint and stops
    assert eng.calls <= calls_at_cancel + 1


async def test_async_should_cancel_raises(fallback_engine):
    with pytest.raises(PhononCancelled):
        await run_phonon_analysis_async(load_preset("srtio3"), fallback_engine, should_cancel=lambda: True)


# --------------------------------------------------------------------------- #
# Follow Instability end to end
# --------------------------------------------------------------------------- #
@pytest.mark.requires_mace
@pytest.mark.parametrize(
    "preset,point,n_atoms,expected_sg",
    [("batio3", "\\Gamma", 5, 99), ("srtio3", "R", 40, 140)],  # P4mm; I4/mcm (a0a0c- rotation)
    ids=["batio3-gamma-P4mm", "srtio3-R-I4mcm"],
)
def test_stabilize_mode_end_to_end(mace_engine, preset, point, n_atoms, expected_sg):
    if importlib.util.find_spec("app.core.relaxer") is None:
        pytest.skip("app/core/relaxer.py not available yet")
    an = _analysis(mace_engine, preset)
    e_cubic = mace_engine.energy_forces_stress(an.atoms)[0] / len(an.atoms)
    atoms, frames = stabilize_mode(an, _index_of(an, point), 0, amplitude=0.1, fmax=0.02, max_steps=200)
    assert len(atoms) == n_atoms
    last = None
    for frame in frames:
        last = frame
    assert last is not None and last.final and last.converged
    e_final = mace_engine.energy_forces_stress(atoms)[0] / len(atoms)
    print(f"{preset} {point}: E/atom {e_cubic:.5f} -> {e_final:.5f} eV ({1e3 * (e_final - e_cubic):.2f} meV/atom), "
          f"{last.step} steps, {last.crystal.spacegroup_symbol} (#{last.crystal.spacegroup_number})")
    assert e_final < e_cubic - 1e-3
    assert abs(last.crystal.energy_per_atom - e_final) < 1e-6
    assert last.crystal.spacegroup_number == expected_sg
    assert geo.get_spacegroup(atoms)[1] == expected_sg
