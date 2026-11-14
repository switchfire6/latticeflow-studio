"""Finite-displacement phonon engine (PROJECT_SPEC.md §4.4, §5.2, §7 Phase 2 step 3).

Pipeline (``run_phonon_analysis``)
----------------------------------
1. The active cell is handed to ``phonopy`` *exactly as given* (``primitive_matrix``
   = identity, fractional coordinates wrapped into [0, 1) like
   ``CrystalStatePayload.frac_coords``), so there are 3N branches and eigenvector
   rows follow the caller's atom order.
2. Symmetry-reduced displacements (default 0.01 Å) in a diagonal supercell
   (default 2×2×2). Forces come from ``engine.forces_batch`` in chunks;
   ``should_cancel()`` is polled between chunks. The perfect-supercell forces are
   evaluated too and subtracted (Φ ≈ -[F(Δu) - F(0)]/Δu, §4.4 step 2), which
   removes the residual-force bias for unrelaxed structures.
3. ``produce_force_constants`` followed by ``symmetrize_force_constants``
   (translational invariance / index permutation; phonopy's own default when it
   loads a calculation), so the acoustic sum rule holds and D(q) is Hermitian.
4. Band path from ``seekpath.get_path_orig_cell`` – the HPKOT path expressed in
   the reciprocal basis of the cell as given (conventional/super cells show
   folded bands). If seekpath fails, a Γ–X | Y–Γ–Z | R–Γ path through the zone
   boundary points of the active cell is used instead.
5. Frequencies (signed THz, as phonopy returns them) on the path, eigenvectors
   for every band at each distinct labelled high-symmetry point.

``PhononAnalysis`` keeps the ``Phonopy`` object so ``REQUEST_EIGENMODE`` and
``FOLLOW_INSTABILITY`` can be served later without recomputing forces.

DOS and thermodynamics (``PhononAnalysis.dos_and_thermal``, Phase A section 5.1)
-------------------------------------------------------------------------------
From the cached force constants only (no new force evaluations):

* Gamma-centred mesh, all q explicit (``is_mesh_symmetry=False``, needed for the
  projected DOS). ``mesh=None`` picks n_i ~ |b_i| for ~``DOS_TARGET_QPOINTS``
  q-points, capped so the mesh eigenvectors stay under ``DOS_EIGVEC_BUDGET_BYTES``
  (e.g. 16x16x16 for 5-atom SrTiO3, 12x12x12 for 8-atom Si, ~1.4k q for 32 atoms).
* Total and per-atom projected DOS (|e|^2 weights from the mesh eigenvectors),
  Gaussian-broadened on one shared, signed frequency grid of ``DOS_GRID_POINTS``
  points (see ``_gaussian_dos``; states / THz / cell, integral of total = 3N);
  the per-atom DOS are summed per element (sum over elements = total).
* ``run_thermal_properties`` with ``cutoff_frequency = F_TOL_THZ``: imaginary
  (and Gamma-acoustic) modes are excluded rather than giving NaN; the share of
  mesh modes with f < -F_TOL_THZ is reported as ``imaginary_fraction``.
  phonopy's kJ/mol and J/K/mol per cell are converted to eV/atom and k_B/atom.
  F includes the zero-point energy. Phonopy's mesh state is released afterwards.

Conventions
-----------
* Eigenvectors: mass-weighted, unit-normalised over the cell, phonopy phase
  convention D(q) = Σ Φ exp(iq·(r_l'κ' − r_0κ))/√(m m') (Bloch phase from atomic
  positions), global phase chosen so the largest-|.| component is real > 0 (ties within
  1e-6 relative go to the first component in atom/axis order).
* Degenerate sets (|Δω²| < ``DEGENERACY_RTOL_W2`` · max|ω²| at that q) are reported in a deterministic,
  axis-aligned basis (``symmetry_adapted_basis``) instead of LAPACK's arbitrary one, so
  e.g. SrTiO3's R triplet is three single-axis octahedral rotations and following one
  gives I4/mcm. Non-degenerate modes are phonopy's vectors up to the global phase.
* Payload floats (frequencies, distances, eigenvector components) are rounded to
  ``PAYLOAD_DECIMALS``; the internal arrays (``frequencies``, ``eigenvectors_at``,
  ``instability_structure``) keep full precision.
* Real-space displacement of atom κ in image l: Re[e_κ/√m_κ · exp(2πi q·(n_l + s_κ))]
  with q and s in fractional (reduced) coordinates.
* ``kpath_distances`` are cumulative Cartesian lengths in Å⁻¹ using the
  crystallographic reciprocal lattice (no 2π factor), as phonopy's band plots do.

Phase B (IMPROVEMENT_SUGGESTIONS.md §5.2-5.3, §4.2)
--------------------------------------------------
* ``cell_mode="primitive"``: the *phonon cell* is the spglib primitive cell expressed
  in the displayed cell's orientation: ``P = phonopy.structure.cells.guess_primitive_matrix``
  (the rational matrix phonopy itself uses for ``primitive_matrix="auto"``, relative to the
  given cell, no rotation), primitive lattice ``L_p = Pᵀ L`` (rows), every displayed atom j
  mapped to a primitive atom κ(j) (same element, positions equal modulo L_p; atom κ is
  represented by the first displayed atom that maps to it, so an already primitive cell is
  reproduced exactly, same atom order). The phonopy calculation (supercell, displacements,
  force constants, D(q)) runs on that cell. Band path: ``seekpath.get_path_orig_cell`` of the
  primitive cell, i.e. the standard HPKOT path (labels / segments of the primitive standard
  cell), with every q converted to the displayed reciprocal basis, ``q_disp = q_p · P⁻¹``, for
  ``path.qpoints`` / ``q_frac``. ``kpath_distances`` (Cartesian) are basis independent.
* Eigenvector mapping onto the displayed atoms: in phonopy's convention the displacement of
  any atom is u = e_κ exp(i q·r)/√m with r the atom's *full* position, so a displayed atom j
  sitting at r_j = r_κ + R (R a primitive lattice vector) moves by e_κ(j) exp(i q·r_j)/√m.
  The frontend animates Re[e_j exp(2πi q_disp·s_j)]/√m with s_j the displayed fractional
  position (§4.4 step 6), so the displayed eigenvector is simply e_j = e_κ(j) / √(N/N_p)
  (unit-normalised over the displayed cell); the phase exp(i q·(r_j - r_κ)) is supplied by the
  frontend's own Bloch factor and must not be applied twice. The degenerate basis is built on
  the primitive vectors; the global phase is fixed on the mapped vector.
* ``band_order``: branch connection by maximum eigenvector overlap |⟨v_k|e_m⟩|² between
  consecutive q (Hungarian assignment). Inside a degenerate set the carried vector of a
  branch is its projection onto the set's subspace, so branches pass through degeneracies.
  Reset to the identity at the start of every continuous path block.
* ``gamma_modes``: phonopy ``run_irreps([0, 0, 0])`` on the phonon cell (labels, degenerate
  sets, characters χ(R)). IR activity: the set's irrep occurs in the vector representation
  (χ_V(R) = tr R); Raman: in its symmetric square (χ(R)² + χ(R²))/2; reduction formula
  n = (1/|G|) Σ_R χ*(R) χ_V(R). Acoustic sets (overlap with rigid translations, ~3 modes)
  get ``is_acoustic`` and ir/raman False. None for non-primitive phonon cells (as-displayed
  conventional cells) or when phonopy's irreps fail.
* ``min_supercell_length`` → diagonal n_i = ceil(L/|a_i|) of the phonon cell, reduced
  (largest n first) until ≤ ``PHONON_SUPERCELL_MAX_ATOMS``; ``min_image_distance`` is the
  shortest lattice vector of the supercell (Minkowski-reduced basis).
* DOS / thermodynamics run on the phonon cell (primitive mode: per primitive cell).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms

from app.schemas.messages import (
    F_TOL_THZ,
    Eigenmode,
    GammaModeIrrep,
    KPathLabel,
    PhononAnalysisComplete,
    PhononConvergenceResult,
    PhononDosData,
    RelaxationFrame,
    ThermalProperties,
)

CELL_MODES = ("as_displayed", "primitive")
# Largest supercell (atoms) chosen automatically (min_supercell_length, next_supercell_dim).
PHONON_SUPERCELL_MAX_ATOMS = 600
# spglib tolerance for the primitive cell (same as the seekpath path).
PRIMITIVE_SYMPREC = 1e-3
# Displayed atoms mapping to one primitive atom must agree to this distance (Angstrom).
_PRIMITIVE_MAP_TOL = 2e-2
# q-points per run_qpoints call with eigenvectors (band connection; memory bound).
_EIGVEC_CHUNK = 24
# Frequencies closer than this (THz) form one subspace in the band connection.
_CONNECT_DEGENERACY_THZ = 1e-3
# phonopy irreps degeneracy tolerance (THz).
_IRREPS_DEGENERACY_THZ = 1e-4

# Target number of q-points on the whole path (actual count is within ~±10%).
TARGET_QPOINTS = 150
MIN_STEPS_PER_SEGMENT = 4
# Number of displaced supercells per forces_batch call (cancellation granularity).
FORCE_CHUNK = 8
# Largest supercell multiplier per axis accepted by instability_structure.
MAX_COMMENSURATE_MULTIPLE = 4
_COMMENSURATE_TOL = 1e-6
# Every discrete decision in the degenerate-basis construction compares a quantity whose
# "exact" (symmetry-fixed) and "genuine" values form two populations separated by empty decades;
# each tolerance sits in the middle of that gap, so noise of ~1e-8 relative cannot move a value
# across it. Measured over all presets, both backends, every path point, with and without
# 1e-8 relative force noise:
#   * w² splitting / max|w²| at that q: symmetry degeneracies <= 1e-14, genuine >= 1e-8.
#   * axis-operator eigenvalue gaps inside a set: exact ties <= 1e-12, genuine >= 1e-5.
#   * tie-break-operator gaps: >= 1e-2 (guarded anyway, see _TIE_BREAK_MIN_GAP).
#   * 1 - |c|/|c_max| over components: computed ties <= 2.5e-9 (MACE Li3PS4 varies per process),
#     genuine >= 9.6e-7 (Li3PS4); window at the geometric middle.
DEGENERACY_RTOL_W2 = 1e-11
_AXIS_WEIGHTS = np.array([1.0, np.sqrt(2.0), np.sqrt(3.0)])
_OPERATOR_TIE_TOL = 1e-9
_TIE_BREAK_MIN_GAP = 1e-3
_TIE_BREAK_SEEDS = (20240611, 31415926, 27182818, 16180339, 14142135, 17320508, 22360679, 26457513)
_PHASE_TIE_RTOL = 4e-8
# Decimal places used in the JSON payload (internal arrays keep full precision).
PAYLOAD_DECIMALS = 6

# Phonon DOS / thermodynamics (dos_and_thermal).
DOS_TARGET_QPOINTS = 4096             # automatic mesh: ~this many q-points in the whole zone
DOS_MIN_QPOINTS = 216
DOS_EIGVEC_BUDGET_BYTES = 96 * 2**20  # mesh eigenvectors: n_q * (3N)^2 * 16 bytes
DOS_GRID_POINTS = 301                 # shared frequency grid of total / projected DOS
_KJ_PER_MOL_PER_EV = 96.48533212331002
_R_J_PER_K_MOL = 8.314462618
_H_EV_THZ = 4.135667696e-3            # Planck constant, eV / THz

ShouldCancel = Optional[Callable[[], bool]]


class PhononCancelled(Exception):
    """Raised when ``should_cancel()`` returned True during a phonon calculation."""


# --------------------------------------------------------------------------- #
# Band path
# --------------------------------------------------------------------------- #
@dataclass
class BandPath:
    qpoints: np.ndarray               # (n_q, 3) fractional, reciprocal basis of the active cell
    distances: np.ndarray             # (n_q,) cumulative, Å^-1 (no 2π)
    labels: List[KPathLabel]          # chart ticks ("X|K" at discontinuities)
    point_names: dict                 # q_index -> high-symmetry point name (individual, never "X|K")
    source: str                       # "seekpath" or "fallback"


def _pretty_label(name: str) -> str:
    return "\\Gamma" if name.upper() == "GAMMA" else name


def _snap_fraction(x: float, max_den: int = 48, tol: float = 1e-8) -> float:
    """seekpath's basis change leaves ~1e-16 noise (0.5000000000000001); snap to p/q."""
    for den in range(1, max_den + 1):
        num = round(x * den)
        if abs(x * den - num) < tol * den:
            return num / den + 0.0  # + 0.0 removes negative zero
    return float(x)


def _seekpath_segments(atoms: Atoms) -> Tuple[dict, list]:
    import seekpath

    structure = (atoms.cell.array, atoms.get_scaled_positions(wrap=True), atoms.get_atomic_numbers())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # SupercellWarning, EdgeCaseWarning, spglib deprecations
        res = seekpath.get_path_orig_cell(structure, with_time_reversal=True, symprec=1e-3)
    coords = {k: np.array([_snap_fraction(x) for x in v]) for k, v in res["point_coords"].items()}
    path = [(a, b) for a, b in res["path"]]
    if not path:
        raise ValueError("seekpath returned an empty path")
    return coords, path


def _fallback_segments() -> Tuple[dict, list]:
    coords = {
        "GAMMA": np.zeros(3),
        "X": np.array([0.5, 0.0, 0.0]),
        "Y": np.array([0.0, 0.5, 0.0]),
        "Z": np.array([0.0, 0.0, 0.5]),
        "R": np.array([0.5, 0.5, 0.5]),
    }
    path = [("GAMMA", "X"), ("Y", "GAMMA"), ("GAMMA", "Z"), ("R", "GAMMA")]
    return coords, path


def build_band_path(atoms: Atoms, n_target: int = TARGET_QPOINTS) -> BandPath:
    """Explicit q-point path with labelled points falling exactly on grid points."""
    try:
        coords, path = _seekpath_segments(atoms)
        source = "seekpath"
    except Exception:  # noqa: BLE001 - any seekpath/spglib failure -> simple path
        coords, path = _fallback_segments()
        source = "fallback"

    rec = np.linalg.inv(atoms.cell.array).T  # rows b_i, a_i . b_j = delta_ij
    segs = []
    for a, b in path:
        qa, qb = coords[a], coords[b]
        length = float(np.linalg.norm((qb - qa) @ rec))
        if length > 1e-12:
            segs.append((a, b, qa, qb, length))
    if not segs:
        raise ValueError("band path has zero length")

    total = sum(s[4] for s in segs)
    # Starting points of continuous blocks are extra points; reserve them.
    n_blocks = 1 + sum(1 for i in range(1, len(segs)) if segs[i][0] != segs[i - 1][1])
    budget = max(n_target - n_blocks, MIN_STEPS_PER_SEGMENT * len(segs))
    steps = [max(MIN_STEPS_PER_SEGMENT, int(round(budget * s[4] / total))) for s in segs]

    qpts: List[np.ndarray] = []
    dists: List[float] = []
    labels: List[KPathLabel] = []
    names: dict = {}
    d = 0.0
    prev_end = None
    for (a, b, qa, qb, length), n in zip(segs, steps):
        if prev_end != a:  # start of a new continuous block
            qpts.append(qa.copy())
            dists.append(d)  # zero-length jump: same distance as the previous point
            idx = len(qpts) - 1
            names[idx] = _pretty_label(a)
            if labels and prev_end is not None:
                # discontinuity: one tick "X|K" at the last point of the previous block
                labels[-1] = KPathLabel(index=labels[-1].index,
                                        label=f"{_pretty_label(prev_end)}|{_pretty_label(a)}")
            else:
                labels.append(KPathLabel(index=idx, label=_pretty_label(a)))
        for k in range(1, n + 1):
            t = k / n
            qpts.append(qa + (qb - qa) * t)
            dists.append(d + length * t)
        d += length
        dists[-1] = d
        qpts[-1] = qb.copy()  # exact high-symmetry coordinates, no rounding drift
        idx = len(qpts) - 1
        names[idx] = _pretty_label(b)
        labels.append(KPathLabel(index=idx, label=_pretty_label(b)))
        prev_end = b

    return BandPath(np.array(qpts), np.array(dists), labels, names, source)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _wrapped_scaled(atoms: Atoms) -> np.ndarray:
    frac = atoms.cell.scaled_positions(np.asarray(atoms.positions, dtype=float))
    frac = np.mod(frac, 1.0)
    frac[frac >= 1.0 - 1e-12] = 0.0
    return frac


def _to_ase(ph_atoms) -> Atoms:
    return Atoms(numbers=ph_atoms.numbers, positions=ph_atoms.positions, cell=ph_atoms.cell, pbc=True)


def fix_global_phase(vec: np.ndarray) -> np.ndarray:
    """Multiply by a unit phase so the largest-magnitude component is real and positive.

    Components within ``_PHASE_TIE_RTOL`` of the maximum count as tied (symmetric modes
    often have several equal-magnitude components); the first in (atom, axis) order wins,
    so the sign does not depend on 1e-15 noise.
    """
    flat = vec.reshape(-1)
    mags = np.abs(flat)
    top = mags.max()
    if top == 0.0:
        return vec
    k = int(np.flatnonzero(mags >= top * (1.0 - _PHASE_TIE_RTOL))[0])
    c = flat[k]
    out = vec * (np.conj(c) / abs(c))
    out.reshape(-1)[k] = abs(c)  # exactly real (the product leaves ~1e-17 imaginary residue)
    return out


def _rotate_by_operator(E: np.ndarray, op: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Basis of span(E) diagonalising operator ``op`` (1-D = diagonal weights); eigenvalues ascending."""
    M = E.conj().T @ ((op[:, None] * E) if op.ndim == 1 else (op @ E))
    M = 0.5 * (M + M.conj().T)
    w, U = np.linalg.eigh(M)
    return w, E @ U


def _clusters(values: np.ndarray, tol: float) -> List[Tuple[int, int]]:
    """(start, stop) runs of a sorted array whose consecutive differences are < tol."""
    out, i = [], 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[j + 1] - values[j] < tol:
            j += 1
        out.append((i, j + 1))
        i = j + 1
    return out


def symmetry_adapted_basis(E: np.ndarray, n_atoms: int) -> np.ndarray:
    """Deterministic axis-aligned basis of a degenerate subspace (columns of E, 3N x k).

    Diagonalises the projection onto span(E) of the Cartesian-axis weight operator
    W = diag(w_x, w_y, w_z) on every atom (incommensurate weights 1, √2, √3), i.e. the
    basis in which every vector has a definite "axis character" Σ_κ w_α |e_κα|². In cubic
    degenerate sets (polar Γ triplet, R rotation triplet, acoustic translations) this is the
    single-axis basis: x/y/z polar modes, rotations about z/y/x. Remaining ties (equal axis
    character, e.g. pairs inside the 6-fold sets of the 8-atom Si cell, which no diagonal
    operator can separate) are broken by a fixed, dense, symmetry-free real symmetric
    operator (fixed-seed pseudo-random), whose projection onto a subspace does not depend on the
    basis chosen inside it. Vectors come out ascending in axis character (then in the
    tie-break operator); the phase is fixed afterwards by ``fix_global_phase``.
    """
    return symmetry_adapted_basis_with_conditioning(E, n_atoms)[0]


def symmetry_adapted_basis_with_conditioning(E: np.ndarray, n_atoms: int) -> Tuple[np.ndarray, float]:
    """``symmetry_adapted_basis`` plus its conditioning: the smallest operator eigenvalue gap
    that fixed a basis direction (inf for k < 2). A perturbation that tilts span(E) by δ
    changes the returned vectors by O(δ / gap); no decision inside is discontinuous unless a
    gap crosses one of the tolerances above, which the measured distributions rule out.

    A tie-break operator whose projected eigenvalues come closer than ``_TIE_BREAK_MIN_GAP``
    is replaced by the next fixed seed (never needed on the presets; tie gaps are >= 1e-2).
    """
    if E.shape[1] < 2:
        return E, float("inf")
    w, B = _rotate_by_operator(E, np.tile(_AXIS_WEIGHTS, n_atoms))
    gaps = [g for g in np.diff(w) if g >= _OPERATOR_TIE_TOL]
    for a, b in _clusters(w, _OPERATOR_TIE_TOL):
        if b - a > 1:
            best = None
            for seed in _TIE_BREAK_SEEDS:
                wt, Bt = _rotate_by_operator(B[:, a:b], _tie_break_operator(3 * n_atoms, seed))
                g = float(np.diff(wt).min())
                if best is None or g > best[0]:
                    best = (g, Bt)
                if g >= _TIE_BREAK_MIN_GAP:
                    break
            gaps.append(best[0])
            B[:, a:b] = best[1]
    return B, float(min(gaps)) if gaps else float("inf")


@lru_cache(maxsize=64)
def _tie_break_operator(dim: int, seed: int = _TIE_BREAK_SEEDS[0]) -> np.ndarray:
    # Fixed-seed PCG64 uniforms (NumPy keeps this stream stable). Quasi-random sequences such as
    # frac(k*phi) are too regular: they cancel against the +/- sign patterns of symmetric modes.
    R = np.random.Generator(np.random.PCG64(seed)).random((dim, dim)) - 0.5
    R = R + R.T
    R.setflags(write=False)
    return R


def degenerate_groups(w2: np.ndarray) -> List[Tuple[int, int]]:
    """(start, stop) band ranges of degenerate sets at one q-point (ascending w², any units).

    Relative criterion: consecutive w² closer than ``DEGENERACY_RTOL_W2 * max|w²|``.
    """
    w2 = np.asarray(w2, dtype=float)
    scale = float(np.abs(w2).max()) if w2.size else 0.0
    return _clusters(w2, DEGENERACY_RTOL_W2 * scale if scale > 0 else np.finfo(float).tiny)


def commensurate_multiples(q_frac: Sequence[float], max_multiple: int = MAX_COMMENSURATE_MULTIPLE) -> Tuple[int, int, int]:
    """Smallest diagonal supercell (n1, n2, n3) with n_i * q_i integer, or ValueError."""
    out = []
    for i, qi in enumerate(q_frac):
        for n in range(1, max_multiple + 1):
            if abs(n * qi - round(n * qi)) < _COMMENSURATE_TOL:
                out.append(n)
                break
        else:
            raise ValueError(
                f"q = {list(np.round(q_frac, 6))} is not commensurate with a supercell of at most "
                f"{max_multiple} cells along axis {i} (q_{i} = {qi:.6f}); pick a high-symmetry point"
            )
    return tuple(out)  # type: ignore[return-value]


def _round(x: np.ndarray) -> np.ndarray:
    """Payload rounding; ``+ 0.0`` turns -0.0 into 0.0."""
    return np.round(np.asarray(x, dtype=float), PAYLOAD_DECIMALS) + 0.0


def _check_cancel(should_cancel: ShouldCancel) -> None:
    if should_cancel is not None and should_cancel():
        raise PhononCancelled("phonon calculation cancelled")


def _gaussian_dos(freqs: np.ndarray, weights: np.ndarray, atom_w: np.ndarray,
                  should_cancel: ShouldCancel = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Gaussian-broadened total and per-atom DOS on a shared grid.

    ``freqs`` (n_q, 3N) THz, ``weights`` (n_q,), ``atom_w`` (n_q, N, 3N) with
    sum over atoms = 1 per mode. Grid: ``DOS_GRID_POINTS`` points from
    min(f) - 5 sigma to max(f) + 5 sigma, sigma = 2.5 grid steps (about 1 % of the
    spectrum width, floored at 0.02 THz). The Gaussian tails beyond the grid are
    < 3e-7, so the total integrates to 3N and the per-atom DOS sum to the total to
    rounding (unlike tetrahedron DOS sampled on a grid).
    Returns (grid (n_f,), total (n_f,), per_atom (N, n_f)) in states / THz / cell.
    """
    f_lo, f_hi = float(freqs.min()), float(freqs.max())
    width = max(f_hi - f_lo, 1e-3)
    n_steps = DOS_GRID_POINTS - 1
    # grid span = width + 10 sigma with sigma = 2.5 * span / n_steps  =>  span = width / (1 - 25 / n_steps)
    span = width / (1.0 - 25.0 / n_steps)
    sigma = max(2.5 * span / n_steps, 0.02)
    span = max(span, width + 10.0 * sigma)
    grid = np.linspace(f_lo - 0.5 * (span - width), f_hi + 0.5 * (span - width), DOS_GRID_POINTS)
    wq = weights / float(weights.sum())
    n_atoms = atom_w.shape[1]
    total = np.zeros(len(grid))
    per_atom = np.zeros((n_atoms, len(grid)))
    norm = 1.0 / (np.sqrt(2.0 * np.pi) * sigma)
    chunk = max(1, int(4_000_000 // max(1, freqs.shape[1] * len(grid))))
    for start in range(0, len(freqs), chunk):
        _check_cancel(should_cancel)
        sl = slice(start, start + chunk)
        g = norm * np.exp(-0.5 * ((grid[None, None, :] - freqs[sl, :, None]) / sigma) ** 2)  # (c, 3N, n_f)
        g *= wq[sl, None, None]
        total += g.sum(axis=(0, 1))
        per_atom += np.einsum("qam,qmf->af", atom_w[sl], g, optimize=True)
    return grid, total, per_atom


# --------------------------------------------------------------------------- #
# Phase B helpers: primitive cell, supercell size, band connection, irreps
# --------------------------------------------------------------------------- #
@dataclass
class PrimitiveMap:
    """Phonon cell of a displayed cell: ``L_p = P^T L`` and displayed atom j -> phonon atom kappa[j]."""

    atoms: Atoms                 # phonon cell (positions wrapped into [0, 1) of its own basis)
    kappa: np.ndarray            # (N_displayed,) int
    matrix: np.ndarray           # P (3x3): primitive lattice rows = P^T @ displayed lattice rows

    @property
    def fold(self) -> int:
        return len(self.kappa) // len(self.atoms)


def _identity_map(work: Atoms) -> PrimitiveMap:
    return PrimitiveMap(work, np.arange(len(work)), np.eye(3))


def _try_primitive_map(work: Atoms, symprec: float) -> Optional[PrimitiveMap]:
    from phonopy.structure.atoms import PhonopyAtoms
    from phonopy.structure.cells import guess_primitive_matrix

    unit = PhonopyAtoms(numbers=work.get_atomic_numbers(), cell=work.cell.array,
                        scaled_positions=_wrapped_scaled(work))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        P = np.array(guess_primitive_matrix(unit, symprec=symprec), dtype=float)
    det = float(np.linalg.det(P))
    if not np.isfinite(det) or abs(det) < 1e-8:
        return None
    if det < 0:
        P = -P
        det = -det
    n = len(work)
    n_prim = n * det
    if abs(n_prim - round(n_prim)) > 1e-6 or round(n_prim) < 1:
        return None
    n_prim = int(round(n_prim))
    if n_prim == n:
        return _identity_map(work)
    lat = P.T @ work.cell.array
    inv = np.linalg.inv(lat)
    frac = np.asarray(work.positions, dtype=float) @ inv
    numbers = work.get_atomic_numbers()
    masses = work.get_masses()
    reps: List[int] = []
    kappa = np.full(n, -1, dtype=int)
    for j in range(n):
        for k, r in enumerate(reps):
            if numbers[r] != numbers[j]:
                continue
            d = frac[j] - frac[r]
            d -= np.round(d)
            if np.linalg.norm(d @ lat) < _PRIMITIVE_MAP_TOL:
                kappa[j] = k
                break
        else:
            reps.append(j)
            kappa[j] = len(reps) - 1
    if len(reps) != n_prim or np.any(np.bincount(kappa, minlength=n_prim) != n // n_prim):
        return None
    pfrac = np.mod(frac[reps], 1.0)
    pfrac[pfrac >= 1.0 - 1e-12] = 0.0
    prim = Atoms(numbers=numbers[reps], cell=lat, pbc=True)
    prim.set_scaled_positions(pfrac)
    prim.set_masses(masses[reps])
    return PrimitiveMap(prim, kappa, P)


def primitive_map(work: Atoms, symprec: float = PRIMITIVE_SYMPREC) -> PrimitiveMap:
    """spglib primitive cell of ``work`` in its own orientation (identity map if none is found)."""
    for tol in (symprec, 1e-5):
        try:
            pm = _try_primitive_map(work, tol)
        except Exception:  # noqa: BLE001 - spglib failure -> no reduction
            pm = None
        if pm is not None:
            return pm
    return _identity_map(work)


def auto_supercell_dim(cell: np.ndarray, n_atoms: int, min_length: float,
                       max_atoms: int = PHONON_SUPERCELL_MAX_ATOMS) -> List[int]:
    """Diagonal n_i = ceil(L / |a_i|), the largest n reduced first until n1 n2 n3 N <= max_atoms."""
    lengths = np.linalg.norm(np.asarray(cell, dtype=float), axis=1)
    dim = [max(1, int(np.ceil(float(min_length) / float(x) - 1e-9))) for x in lengths]
    while int(np.prod(dim)) * n_atoms > max_atoms and max(dim) > 1:
        dim[int(np.argmax(dim))] -= 1
    return dim


def min_image_distance(cell: np.ndarray, dim: Sequence[int]) -> float:
    """Shortest non-zero lattice vector (Angstrom) of the supercell diag(dim) @ cell."""
    from ase.geometry import minkowski_reduce

    sc = np.diag([float(x) for x in dim]) @ np.asarray(cell, dtype=float)
    try:
        red, _ = minkowski_reduce(sc)
    except Exception:  # noqa: BLE001
        red = sc
    best = float("inf")
    rng = (-1, 0, 1)
    for i in rng:
        for j in rng:
            for k in rng:
                if i == j == k == 0:
                    continue
                best = min(best, float(np.linalg.norm(i * red[0] + j * red[1] + k * red[2])))
    return best


def next_supercell_dim(current: Sequence[int], n_cell_atoms: int,
                       max_atoms: int = PHONON_SUPERCELL_MAX_ATOMS) -> List[int]:
    """``current`` + 1 along every axis, the largest entry reduced first until <= ``max_atoms``.

    Entries never drop below ``current``; raises ValueError if no larger supercell fits.
    """
    cur = [int(x) for x in current]
    if len(cur) != 3 or any(x < 1 for x in cur):
        raise ValueError(f"supercell_dim must be three positive integers, got {current!r}")
    if n_cell_atoms < 1:
        raise ValueError("n_cell_atoms must be positive")
    trial = [x + 1 for x in cur]
    while int(np.prod(trial)) * n_cell_atoms > max_atoms:
        grown = [i for i in range(3) if trial[i] > cur[i]]
        if not grown:
            break
        i = max(grown, key=lambda a: (trial[a], -a))
        trial[i] -= 1
    if trial == cur or int(np.prod(trial)) * n_cell_atoms > max_atoms:
        raise ValueError(
            f"no supercell larger than {cur} fits the {max_atoms}-atom budget "
            f"({n_cell_atoms} atoms per cell)"
        )
    return trial


def _block_starts(path: "BandPath") -> np.ndarray:
    """Boolean (n_q,): True where a continuous block of the path begins."""
    n = len(path.qpoints)
    starts = np.zeros(n, dtype=bool)
    if n:
        starts[0] = True
    for i in range(1, n):
        if path.distances[i] == path.distances[i - 1] and not np.allclose(path.qpoints[i], path.qpoints[i - 1]):
            starts[i] = True
    return starts


class _BandConnector:
    """Incremental max-overlap band connection (see module docstring)."""

    def __init__(self) -> None:
        self.carried: Optional[np.ndarray] = None   # (3N, n_branches) columns = branch vectors
        self.orders: List[List[int]] = []

    def add(self, freqs: np.ndarray, vecs: np.ndarray, reset: bool) -> None:
        from scipy.optimize import linear_sum_assignment

        nb = vecs.shape[1]
        if reset or self.carried is None or self.carried.shape != vecs.shape:
            perm = np.arange(nb)
        else:
            overlap = np.abs(self.carried.conj().T @ vecs) ** 2     # (branch k, band m)
            _, perm = linear_sum_assignment(-overlap)
        groups = _clusters(np.asarray(freqs, dtype=float), _CONNECT_DEGENERACY_THZ)
        group_of = np.empty(nb, dtype=int)
        for g, (a, b) in enumerate(groups):
            group_of[a:b] = g
        new = np.empty_like(vecs)
        for k in range(nb):
            m = int(perm[k])
            a, b = groups[group_of[m]]
            v = vecs[:, m]
            if b - a > 1 and self.carried is not None and not reset and self.carried.shape == vecs.shape:
                E = vecs[:, a:b]
                proj = E @ (E.conj().T @ self.carried[:, k])
                norm = float(np.linalg.norm(proj))
                if norm > 0.1:
                    v = proj / norm
            new[:, k] = v
        self.carried = new
        self.orders.append([int(x) for x in perm])


def _gamma_irreps(phonon, masses: np.ndarray) -> Optional[List[GammaModeIrrep]]:
    """Irreps at Gamma with IR / Raman activity (None if phonopy cannot assign them)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ir = phonon.run_irreps([0.0, 0.0, 0.0], degeneracy_tolerance=_IRREPS_DEGENERACY_THZ)
        freqs = np.asarray(ir.frequencies, dtype=float)
        sets = [list(map(int, s)) for s in ir.band_indices]
        chars = np.asarray(ir.characters, dtype=complex)            # (n_sets, n_ops)
        rots = np.asarray(ir.conventional_rotations, dtype=float)   # traces are basis invariant
        labels = getattr(ir, "_ir_labels", None)
        vecs = np.asarray(ir.eigenvectors, dtype=complex)           # (3N, 3N) columns = bands
        if len(sets) != chars.shape[0] or chars.shape[1] != len(rots) or len(rots) == 0:
            return None
        n_ops = len(rots)
        chi_v = np.array([np.trace(r) for r in rots])
        chi_v2 = np.array([np.trace(r @ r) for r in rots])
        chi_sym = 0.5 * (chi_v ** 2 + chi_v2)
        n = len(masses)
        trans = np.zeros((3 * n, 3))
        for a in range(3):
            trans[a::3, a] = np.sqrt(masses)
        trans /= np.linalg.norm(trans, axis=0)
        acoustic_w = (np.abs(trans.T @ vecs) ** 2).sum(axis=0)     # per band, 0..1
        out: List[GammaModeIrrep] = []
        n_acoustic_sets = 0
        for s_idx, bands in enumerate(sets):
            ch = chars[s_idx]
            n_v = float(np.real(np.sum(np.conj(ch) * chi_v)) / n_ops)
            n_r = float(np.real(np.sum(np.conj(ch) * chi_sym)) / n_ops)
            acoustic = bool(np.mean(acoustic_w[bands]) > 0.5)
            n_acoustic_sets += int(acoustic)
            label = labels[s_idx] if labels is not None and s_idx < len(labels) else None
            out.append(GammaModeIrrep(
                band_indices=bands,
                frequency_thz=float(_round(np.mean(freqs[bands]))),
                label=str(label) if label else None,
                is_acoustic=acoustic,
                ir_active=False if acoustic else bool(n_v > 0.5),
                raman_active=False if acoustic else bool(n_r > 0.5),
            ))
        return out
    except Exception:  # noqa: BLE001 - irreps are optional decoration, never fatal
        return None


# --------------------------------------------------------------------------- #
# Analysis object
# --------------------------------------------------------------------------- #
class PhononAnalysis:
    """Server-side cache for one analysis; keeps the Phonopy object so more modes can be fetched later."""

    def __init__(
        self,
        phonon,
        atoms: Atoms,
        path: BandPath,
        frequencies: np.ndarray,
        engine,
        *,
        cell_mode: str = "as_displayed",
        prim: Optional[PrimitiveMap] = None,
        phonon_qpoints: Optional[np.ndarray] = None,
        supercell_dim: Sequence[int] = (2, 2, 2),
        displacement_distance: float = 0.01,
        band_order: Optional[List[List[int]]] = None,
        gamma_modes: Optional[List[GammaModeIrrep]] = None,
    ) -> None:
        self.phonon = phonon
        self.atoms = atoms                    # private copy of the displayed cell, positions wrapped
        self.path = path                      # q in the displayed cell's reciprocal basis
        self.frequencies = frequencies        # (n_q, n_branches)
        self.engine = engine
        self.cell_mode = cell_mode
        self.prim = prim if prim is not None else _identity_map(atoms)
        self.phonon_atoms = self.prim.atoms   # the cell phonopy works on (== atoms in as_displayed)
        self.phonon_qpoints = path.qpoints if phonon_qpoints is None else phonon_qpoints  # phonon-cell basis
        self.supercell_dim = [int(x) for x in supercell_dim]
        self.displacement_distance = float(displacement_distance)
        self.band_order = band_order
        self.gamma_modes = gamma_modes
        # displayed-cell arrays (mode_displacements / instability_structure work on the displayed cell)
        self.masses = np.asarray(atoms.get_masses(), dtype=float)
        self.scaled_positions = _wrapped_scaled(atoms)
        self._lock = threading.Lock()
        self._dos_cache: Optional[Tuple[tuple, PhononDosData]] = None
        self.result: PhononAnalysisComplete = self._build_result()

    # -------------------------------------------------------------- basics
    @property
    def n_atoms(self) -> int:
        """Atoms of the displayed cell (rows of every eigenvector)."""
        return len(self.atoms)

    @property
    def n_phonon_atoms(self) -> int:
        return len(self.phonon_atoms)

    @property
    def n_branches(self) -> int:
        """3 x atoms of the phonon cell (columns of ``frequencies``, valid band indices)."""
        return 3 * self.n_phonon_atoms

    @property
    def min_image_distance(self) -> float:
        return min_image_distance(self.phonon_atoms.cell.array, self.supercell_dim)

    @property
    def n_q(self) -> int:
        return len(self.path.qpoints)

    def _check_indices(self, q_index: int, band_index: int) -> None:
        for name, val in (("q_index", q_index), ("band_index", band_index)):
            if isinstance(val, bool) or not isinstance(val, (int, np.integer)):
                raise ValueError(f"{name} must be an integer, got {val!r}")
        if not 0 <= q_index < self.n_q:
            raise IndexError(f"q_index {q_index} out of range [0, {self.n_q})")
        if not 0 <= band_index < self.n_branches:
            raise IndexError(f"band_index {band_index} out of range [0, {self.n_branches})")

    def eigenvectors_at(self, q_index: int) -> Tuple[np.ndarray, np.ndarray]:
        """(frequencies (n_branches,), eigenvectors (n_branches, N_displayed, 3) complex) at a path point.

        Degenerate sets are rotated to ``symmetry_adapted_basis`` (on the phonon cell); in primitive
        mode every vector is mapped onto the displayed atoms (e_j = e_kappa(j) / sqrt(N / N_p), see
        the module docstring); every vector is phase-fixed.
        """
        q = self.phonon_qpoints[q_index]
        with self._lock:
            qp = self.phonon.run_qpoints([q], with_eigenvectors=True)
            freqs = np.array(qp.frequencies[0], dtype=float)
            vecs = np.array(qp.eigenvectors[0], dtype=complex)  # columns are bands
            factor = float(self.phonon.unit_conversion_factor)
        n_p = self.n_phonon_atoms
        w2 = np.sign(freqs) * (freqs / factor) ** 2
        for a, b in degenerate_groups(w2):
            if b - a > 1:
                vecs[:, a:b] = symmetry_adapted_basis(vecs[:, a:b], n_p)
        nb = 3 * n_p
        prim_modes = vecs.T.reshape(nb, n_p, 3)
        if self.prim.fold > 1:
            prim_modes = prim_modes[:, self.prim.kappa, :] / np.sqrt(float(self.prim.fold))
        modes = np.empty((nb, self.n_atoms, 3), dtype=complex)
        for j in range(nb):
            modes[j] = fix_global_phase(prim_modes[j])
        return freqs, modes

    def dynamical_matrix(self, q_index: int) -> np.ndarray:
        """D(q) (n_branches², eV/(Å² amu)) of the phonon cell from phonopy at a path point."""
        with self._lock:
            dm = self.phonon.dynamical_matrix
            dm.run(self.phonon_qpoints[q_index])
            return np.array(dm.dynamical_matrix, dtype=complex)

    @property
    def payload_frequencies(self) -> np.ndarray:
        """Frequencies exactly as they appear in the payload (rounded); flags derive from these."""
        return _round(self.frequencies)

    def _make_mode(self, q_index: int, band_index: int, vec: np.ndarray) -> Eigenmode:
        f = float(self.payload_frequencies[q_index, band_index])
        return Eigenmode(
            q_index=int(q_index),
            q_label=self.path.point_names.get(int(q_index)),
            q_frac=[float(x) for x in self.path.qpoints[q_index]],
            band_index=int(band_index),
            frequency_thz=f,
            is_imaginary=bool(f < -F_TOL_THZ),
            eigenvectors_real=_round(vec.real).tolist(),
            eigenvectors_imag=_round(vec.imag).tolist(),
        )

    def eigenmode(self, q_index: int, band_index: int) -> Eigenmode:
        """Eigenmode at any path point (REQUEST_EIGENMODE)."""
        self._check_indices(q_index, band_index)
        _, modes = self.eigenvectors_at(q_index)
        return self._make_mode(q_index, band_index, modes[band_index])

    # -------------------------------------------------------------- payload
    def _build_result(self) -> PhononAnalysisComplete:
        f = self.payload_frequencies
        n_imag = (f < -F_TOL_THZ).sum(axis=1)
        eigenmodes: List[Eigenmode] = []
        seen: List[np.ndarray] = []
        for q_index in sorted(self.path.point_names):
            q = self.path.qpoints[q_index]
            if any(np.allclose(q, s, atol=1e-9) for s in seen):
                continue  # each distinct high-symmetry point once (first occurrence)
            seen.append(q)
            _, modes = self.eigenvectors_at(q_index)
            eigenmodes.extend(self._make_mode(q_index, j, modes[j]) for j in range(f.shape[1]))
        return PhononAnalysisComplete(
            is_dynamically_stable=bool(n_imag.max() == 0),
            min_frequency_thz=float(f.min()),
            imaginary_mode_count=int(n_imag.max()),
            kpath_distances=_round(self.path.distances).tolist(),
            kpath_labels=list(self.path.labels),
            frequencies_thz=f.tolist(),
            eigenmodes=eigenmodes,
            cell_mode=self.cell_mode,
            n_primitive_atoms=self.n_phonon_atoms if self.cell_mode == "primitive" else None,
            supercell_dim=list(self.supercell_dim),
            min_image_distance=float(_round(self.min_image_distance)),
            band_order=self.band_order,
            gamma_modes=self.gamma_modes,
            band_path_source=self._band_path_source(),
        )

    def _band_path_source(self) -> str:
        if self.path.source != "seekpath":
            return "fallback"
        return "seekpath-standard" if self.cell_mode == "primitive" else "seekpath-orig-cell"

    # ------------------------------------------------------------- export
    def export_phonopy_yaml(self) -> str:
        """``phonopy_params.yaml`` text (unit cell = phonon cell, supercell matrix, displacement
        dataset with the forces used, and the symmetrised force constants); ``phonopy.load`` reads it."""
        with self._lock:
            ph_yaml = self.phonon.to_phonopy_yaml(settings={"force_constants": True})
        return str(ph_yaml)

    # ------------------------------------------------- DOS / thermodynamics
    def auto_dos_mesh(self) -> Tuple[int, int, int]:
        """Gamma-centred mesh with n_i ~ |b_i| (~DOS_TARGET_QPOINTS q-points, memory-capped)."""
        n_modes = self.n_branches
        cap = max(1, int(DOS_EIGVEC_BUDGET_BYTES // (16 * n_modes * n_modes)))
        target = max(1, min(DOS_TARGET_QPOINTS, cap))
        b = np.linalg.norm(np.linalg.inv(self.phonon_atoms.cell.array).T, axis=1)   # |b_i|, 1/A (no 2 pi)
        k = (target / float(np.prod(b))) ** (1.0 / 3.0)
        mesh = np.maximum(1, np.round(k * b)).astype(int)
        while int(np.prod(mesh)) > cap and mesh.max() > 1:                   # the memory cap is hard
            mesh[int(np.argmax(mesh / b))] -= 1
        while int(np.prod(mesh)) < min(DOS_MIN_QPOINTS, cap):
            trial = mesh.copy()
            trial[int(np.argmin(mesh / b))] += 1
            if int(np.prod(trial)) > cap:
                break
            mesh = trial
        return tuple(int(x) for x in mesh)  # type: ignore[return-value]

    def dos_and_thermal(
        self,
        *,
        mesh: Optional[Sequence[int]] = None,
        t_min: float = 0.0,
        t_max: float = 1000.0,
        t_step: float = 10.0,
        should_cancel: ShouldCancel = None,
    ) -> PhononDosData:
        """Phonon DOS, element-projected DOS and harmonic thermodynamics (see module docstring).

        Uses the cached force constants (no force evaluations). Raises
        :class:`PhononCancelled` if ``should_cancel()`` turns True between steps and
        ``ValueError`` for an invalid mesh or temperature range. The last result is
        cached per (mesh, t_min, t_max, t_step).
        """
        if mesh is None:
            mesh_t = self.auto_dos_mesh()
        else:
            mesh_t = tuple(int(x) for x in mesh)
            if len(mesh_t) != 3 or any(x < 1 for x in mesh_t):
                raise ValueError(f"mesh must be three positive integers, got {mesh!r}")
            n_modes = self.n_branches
            if int(np.prod(mesh_t)) * 16 * n_modes * n_modes > 4 * DOS_EIGVEC_BUDGET_BYTES:
                raise ValueError(f"mesh {list(mesh_t)} is too dense for {self.n_phonon_atoms} atoms")
        if not (np.isfinite(t_min) and np.isfinite(t_max) and np.isfinite(t_step)):
            raise ValueError("temperatures must be finite")
        if t_min < 0 or t_max <= t_min or t_step <= 0:
            raise ValueError(f"invalid temperature range t_min={t_min}, t_max={t_max}, t_step={t_step}")
        if (t_max - t_min) / t_step > 5000:
            raise ValueError("too many temperature points (max 5000)")
        key = (mesh_t, float(t_min), float(t_max), float(t_step))
        cached = self._dos_cache
        if cached is not None and cached[0] == key:
            return cached[1]

        _check_cancel(should_cancel)
        n = self.n_phonon_atoms
        symbols = list(self.phonon_atoms.get_chemical_symbols())
        try:
            with self._lock:
                self.phonon.run_mesh(list(mesh_t), is_mesh_symmetry=False, with_eigenvectors=True,
                                     is_gamma_center=True)
                m = self.phonon.mesh
                mesh_freqs = np.array(m.frequencies, dtype=float)                  # (n_q, 3N)
                weights = np.array(m.weights, dtype=float)                         # (n_q,)
                eigvecs = m.eigenvectors                                           # (n_q, 3N comp, 3N bands)
                # |e|^2 summed over x, y, z: weight of every atom in every mode, (n_q, N, 3N)
                atom_w = (np.abs(np.asarray(eigvecs)) ** 2).reshape(len(mesh_freqs), n, 3, 3 * n).sum(axis=2)
            _check_cancel(should_cancel)

            grid, total, per_atom = _gaussian_dos(mesh_freqs, weights, atom_w, should_cancel)
            projected: dict = {}
            for k, sym in enumerate(symbols):
                projected[sym] = projected.get(sym, 0.0) + per_atom[k]
            _check_cancel(should_cancel)

            temps = np.arange(float(t_min), float(t_max) + 0.5 * float(t_step), float(t_step))
            with self._lock, warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.phonon.run_thermal_properties(temperatures=temps, cutoff_frequency=F_TOL_THZ)
                tp = self.phonon.thermal_properties
                temps_out = np.array(tp.temperatures, dtype=float)
                free_kj = np.array(tp.free_energy, dtype=float)
                entropy_j = np.array(tp.entropy, dtype=float)
                cv_j = np.array(tp.heat_capacity, dtype=float)
        finally:
            eigvecs = None
            with self._lock:
                self._release_mesh()
        free = free_kj / _KJ_PER_MOL_PER_EV / n
        entropy = entropy_j / _R_J_PER_K_MOL / n
        cv = cv_j / _R_J_PER_K_MOL / n
        real = mesh_freqs > F_TOL_THZ
        wsum = float(weights.sum())
        zpe = float(0.5 * _H_EV_THZ * np.sum(weights[:, None] * np.where(real, mesh_freqs, 0.0)) / wsum / n)
        imaginary_fraction = float(np.sum(weights[:, None] * (mesh_freqs < -F_TOL_THZ))
                                   / (wsum * mesh_freqs.shape[1]))
        _check_cancel(should_cancel)

        def clean(x) -> List[float]:
            arr = np.nan_to_num(np.asarray(x, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
            return [float(v) for v in arr]

        result = PhononDosData(
            mesh=list(mesh_t),
            frequencies_thz=clean(grid),
            total_dos=clean(total),
            projected_dos={sym: clean(v) for sym, v in projected.items()},
            thermal=ThermalProperties(
                temperatures_k=clean(temps_out),
                free_energy_ev_per_atom=clean(free),
                entropy_kb_per_atom=clean(entropy),
                heat_capacity_kb_per_atom=clean(cv),
                zero_point_energy_ev_per_atom=zpe,
            ),
            imaginary_fraction=imaginary_fraction,
        )
        try:  # superconductivity Tier 0 proxies; a failure never breaks the DOS
            from app.core.superconductivity import descriptors_from_dos

            result.eph_proxy = descriptors_from_dos(result)
        except Exception:  # noqa: BLE001
            logging.getLogger("latticeflow.phonons").exception("superconductivity proxy descriptors failed")
        self._dos_cache = (key, result)
        return result

    def _release_mesh(self) -> None:
        """Drop phonopy's mesh / DOS state (eigenvectors up to the budget); caller holds the lock."""
        for attr in ("_mesh", "_pdos", "_total_dos", "_thermal_properties"):
            if hasattr(self.phonon, attr):
                try:
                    setattr(self.phonon, attr, None)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------- frozen phonon
    def mode_displacements(self, q_index: int, band_index: int) -> Tuple[Tuple[int, int, int], np.ndarray]:
        """Unscaled real displacement field on the commensurate supercell.

        Returns ((n1, n2, n3), u) with u of shape (n1*n2*n3*N, 3), atoms ordered image-major
        (n1 slowest, then n2, n3) and the unit-cell atoms inner, exactly as
        ``instability_structure`` builds the supercell. Global phase maximises |Re u|.
        """
        self._check_indices(q_index, band_index)
        q = self.path.qpoints[q_index]
        mult = commensurate_multiples(q)
        _, modes = self.eigenvectors_at(q_index)
        e = modes[band_index] / np.sqrt(self.masses)[:, None]       # (N, 3)
        images = np.array([(i, j, k) for i in range(mult[0]) for j in range(mult[1]) for k in range(mult[2])])
        frac = images[:, None, :] + self.scaled_positions[None, :, :]  # (n_img, N, 3)
        phase = np.exp(2j * np.pi * (frac @ q))                       # (n_img, N)
        U = (phase[:, :, None] * e[None, :, :]).reshape(-1, 3)
        s = np.sum(U * U)
        if abs(s) > 0:
            U = U * np.exp(-0.5j * np.angle(s))  # maximises ||Re(U e^{i phi})||
        return mult, U.real

    def instability_structure(self, q_index: int, band_index: int, amplitude: float = 0.1) -> Atoms:
        """Commensurate supercell displaced along mode (q, band); max atomic displacement = amplitude Å."""
        if not np.isfinite(amplitude) or amplitude <= 0:
            raise ValueError(f"amplitude must be positive, got {amplitude!r}")
        mult, u = self.mode_displacements(q_index, band_index)
        umax = float(np.linalg.norm(u, axis=1).max())
        if umax < 1e-12:
            raise ValueError("mode has no real-space displacement")
        u = u * (amplitude / umax)
        n_img = mult[0] * mult[1] * mult[2]
        images = np.array([(i, j, k) for i in range(mult[0]) for j in range(mult[1]) for k in range(mult[2])])
        frac = (images[:, None, :] + self.scaled_positions[None, :, :]).reshape(-1, 3)
        cell = self.atoms.cell.array
        positions = frac @ cell + u
        sc = Atoms(
            numbers=np.tile(self.atoms.get_atomic_numbers(), n_img),
            positions=positions,
            cell=np.diag(mult) @ cell,
            pbc=True,
        )
        sc.set_masses(np.tile(self.masses, n_img))
        return sc


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def run_phonon_analysis(
    atoms: Atoms,
    engine=None,
    *,
    supercell_dim: Sequence[int] = (2, 2, 2),
    displacement_distance: float = 0.01,
    cell_mode: str = "as_displayed",
    min_supercell_length: Optional[float] = None,
    should_cancel: ShouldCancel = None,
) -> PhononAnalysis:
    """Synchronous, cancellable phonon analysis of ``atoms`` (not mutated).

    ``cell_mode`` "as_displayed" (the cell as given, 3N branches) or "primitive" (spglib primitive
    cell, standard seekpath path, eigenvectors mapped back onto the displayed atoms; see the module
    docstring). ``min_supercell_length`` (Angstrom), when given, overrides ``supercell_dim`` with
    :func:`auto_supercell_dim` of the phonon cell.
    """
    from phonopy import Phonopy
    from phonopy.structure.atoms import PhonopyAtoms

    if engine is None:
        from app.core.umlip_engine import UMLIPEngine

        engine = UMLIPEngine.get_instance()
    if len(atoms) == 0:
        raise ValueError("cannot run phonons on an empty structure")
    if atoms.cell.rank != 3:
        raise ValueError("phonons need a 3D periodic cell")
    if cell_mode not in CELL_MODES:
        raise ValueError(f"cell_mode must be one of {CELL_MODES}, got {cell_mode!r}")
    if not displacement_distance > 0:
        raise ValueError("displacement_distance must be positive")
    if min_supercell_length is not None and not (np.isfinite(min_supercell_length) and min_supercell_length > 0):
        raise ValueError("min_supercell_length must be positive")
    if min_supercell_length is None:
        dim = [int(x) for x in supercell_dim]
        if len(dim) != 3 or any(x < 1 for x in dim):
            raise ValueError(f"supercell_dim must be three positive integers, got {supercell_dim!r}")

    _check_cancel(should_cancel)
    work = Atoms(
        numbers=atoms.get_atomic_numbers().copy(),
        cell=np.array(atoms.cell.array, dtype=float),
        pbc=True,
    )
    frac = _wrapped_scaled(atoms)
    work.set_scaled_positions(frac)
    masses = np.array(atoms.get_masses(), dtype=float)
    work.set_masses(masses)

    prim_full = primitive_map(work)
    prim = prim_full if cell_mode == "primitive" else _identity_map(work)
    cell_atoms = prim.atoms
    if min_supercell_length is not None:
        dim = auto_supercell_dim(cell_atoms.cell.array, len(cell_atoms), float(min_supercell_length))

    unit = PhonopyAtoms(
        symbols=cell_atoms.get_chemical_symbols(),
        cell=cell_atoms.cell.array,
        scaled_positions=frac if cell_atoms is work else _wrapped_scaled(cell_atoms),
        masses=np.array(cell_atoms.get_masses(), dtype=float),
    )
    phonon = Phonopy(unit, supercell_matrix=np.diag(dim), primitive_matrix=np.eye(3), log_level=0)
    phonon.generate_displacements(distance=float(displacement_distance))
    displaced = [_to_ase(sc) for sc in phonon.supercells_with_displacements]

    _check_cancel(should_cancel)
    batch = [_to_ase(phonon.supercell)] + displaced
    forces: List[np.ndarray] = []
    for start in range(0, len(batch), FORCE_CHUNK):
        _check_cancel(should_cancel)
        forces.extend(np.asarray(f, dtype=float) for f in engine.forces_batch(batch[start:start + FORCE_CHUNK]))
    _check_cancel(should_cancel)

    f0 = forces[0]
    phonon.forces = np.array([f - f0 for f in forces[1:]])
    phonon.produce_force_constants(show_drift=False)
    phonon.symmetrize_force_constants(show_drift=False)

    ph_path = build_band_path(cell_atoms)
    if prim.fold > 1:
        # L_p = P^T L  =>  a phonon-cell q has displayed-basis coordinates q_disp = q_p . P^-1
        qd = ph_path.qpoints @ np.linalg.inv(prim.matrix)
        qd = np.array([[_snap_fraction(x) for x in row] for row in qd])
        path = BandPath(qd, ph_path.distances, ph_path.labels, ph_path.point_names, ph_path.source)
    else:
        path = ph_path
    _check_cancel(should_cancel)

    # frequencies and band connection along the path (eigenvectors in chunks)
    starts = _block_starts(path)
    connector = _BandConnector()
    freq_rows: List[np.ndarray] = []
    for c0 in range(0, len(ph_path.qpoints), _EIGVEC_CHUNK):
        _check_cancel(should_cancel)
        qp = phonon.run_qpoints(ph_path.qpoints[c0:c0 + _EIGVEC_CHUNK], with_eigenvectors=True)
        fr = np.array(qp.frequencies, dtype=float)
        ev = qp.eigenvectors
        for i in range(len(fr)):
            freq_rows.append(fr[i])
            connector.add(fr[i], np.asarray(ev[i], dtype=complex), bool(starts[c0 + i]))
        qp = ev = None
    frequencies = np.array(freq_rows, dtype=float)
    _check_cancel(should_cancel)

    gamma = None
    if prim.fold > 1 or len(prim_full.atoms) == len(work):   # irreps need a primitive phonon cell
        gamma = _gamma_irreps(phonon, np.array(cell_atoms.get_masses(), dtype=float))
    _check_cancel(should_cancel)
    return PhononAnalysis(
        phonon, work, path, frequencies, engine,
        cell_mode=cell_mode,
        prim=prim,
        phonon_qpoints=ph_path.qpoints,
        supercell_dim=dim,
        displacement_distance=float(displacement_distance),
        band_order=connector.orders,
        gamma_modes=gamma,
    )


async def run_phonon_analysis_async(
    atoms: Atoms,
    engine=None,
    *,
    supercell_dim: Sequence[int] = (2, 2, 2),
    displacement_distance: float = 0.01,
    cell_mode: str = "as_displayed",
    min_supercell_length: Optional[float] = None,
    should_cancel: ShouldCancel = None,
) -> PhononAnalysis:
    """``asyncio.to_thread`` wrapper. Cancelling the awaiting task also stops the worker thread
    at its next cancellation checkpoint."""
    stop = threading.Event()

    def _cancel() -> bool:
        return stop.is_set() or (should_cancel is not None and bool(should_cancel()))

    snapshot = atoms.copy()
    snapshot.calc = None
    try:
        return await asyncio.to_thread(
            run_phonon_analysis,
            snapshot,
            engine,
            supercell_dim=supercell_dim,
            displacement_distance=displacement_distance,
            cell_mode=cell_mode,
            min_supercell_length=min_supercell_length,
            should_cancel=_cancel,
        )
    except asyncio.CancelledError:
        stop.set()
        raise


def compare_phonon_analyses(a: PhononAnalysis, b: PhononAnalysis, tolerance_thz: float = 0.1) -> PhononConvergenceResult:
    """Frequency change between two analyses of the same structure (e.g. two supercell sizes).

    Requires the same ``cell_mode``, branch count and band path (same q-points); compares the
    full-precision ascending frequencies point by point. ``converged``: max |delta f| < tolerance
    and both analyses agree on dynamical stability.
    """
    if a.cell_mode != b.cell_mode:
        raise ValueError(f"cell modes differ ({a.cell_mode!r} vs {b.cell_mode!r})")
    if a.frequencies.shape != b.frequencies.shape or a.path.qpoints.shape != b.path.qpoints.shape \
            or not np.allclose(a.path.qpoints, b.path.qpoints, atol=1e-9):
        raise ValueError("the analyses do not share the same band path / branch count")
    if not tolerance_thz > 0:
        raise ValueError("tolerance_thz must be positive")
    delta = np.abs(b.frequencies - a.frequencies)
    per_label: dict = {}
    for q_index, name in a.path.point_names.items():
        per_label[name] = max(per_label.get(name, 0.0), float(delta[q_index].max()))
    max_abs = float(delta.max())
    same_verdict = a.result.is_dynamically_stable == b.result.is_dynamically_stable
    return PhononConvergenceResult(
        supercell_dim_a=list(a.supercell_dim),
        supercell_dim_b=list(b.supercell_dim),
        min_image_distance_a=float(_round(a.min_image_distance)),
        min_image_distance_b=float(_round(b.min_image_distance)),
        min_frequency_thz_a=float(a.result.min_frequency_thz),
        min_frequency_thz_b=float(b.result.min_frequency_thz),
        max_abs_delta_thz=float(_round(max_abs)),
        rms_delta_thz=float(_round(np.sqrt(np.mean(delta ** 2)))),
        per_label_max_delta_thz={k: float(_round(v)) for k, v in per_label.items()},
        tolerance_thz=float(tolerance_thz),
        converged=bool(max_abs < tolerance_thz and same_verdict),
    )


def stabilize_mode(
    analysis: PhononAnalysis,
    q_index: int,
    band_index: int,
    *,
    amplitude: float = 0.1,
    fmax: float = 0.02,
    max_steps: int = 200,
    engine=None,
    should_cancel: ShouldCancel = None,
) -> Tuple[Atoms, Iterator[RelaxationFrame]]:
    """"Follow Instability": displaced commensurate supercell + FIRE relaxation frames.

    The returned ``Atoms`` is the new active structure; it is relaxed in place while the
    iterator is consumed. ``engine`` defaults to the engine the analysis was run with.
    """
    displaced = analysis.instability_structure(q_index, band_index, amplitude=amplitude)
    from app.core.relaxer import iter_relaxation  # lazy: written concurrently by a colleague

    frames = iter_relaxation(
        displaced,
        engine if engine is not None else analysis.engine,
        fmax=fmax,
        max_steps=max_steps,
        relax_cell=True,
        should_cancel=should_cancel,
    )
    return displaced, frames
