"""Built-in "Symmetry-Conditioned Riemannian Langevin Flow Sampler" (PROJECT_SPEC.md §4.2, §7 Phase 2).

What this is, and what it is not
---------------------------------
There is **no trained generative network** in this module. ``v_theta`` is not
learned: it is the analytic conditional-flow-matching velocity of §4.2, i.e. the
torus geodesic towards an endpoint that is *constructed* (not predicted) from the
request. In plain words this is a **physics-guided symmetric structure sampler**
with a flow-matching-style trajectory:

1. a bounded, seeded search over Wyckoff site assignments / free parameters of
   the target space group, scored with the force field and briefly
   pre-relaxed under that symmetry before the first frame, picks the endpoint;
2. a stream of ``steps`` frames then carries uniform torus noise along geodesics
   (and the lattice along a straight line in parameter space) onto that endpoint,
   with a force-field Langevin corrector for t > 0.65 that also refines the
   endpoint inside the symmetric subspace.

It finds a low-energy arrangement among a small enumerated candidate set; it does
not learn a distribution over crystals and cannot propose structures outside the
Wyckoff combinations it enumerates. The "generative" part is the seeded
enumeration/sampling of the candidate pool plus the noise prior; the flow itself
is a deterministic interpolation with a physics corrector. A learned
``v_theta`` belongs to the MatterGen / FlowMM adapter (§2.2, §4.2).

Algorithm (all randomness from ``numpy.random.default_rng(seed)`` child streams)
-------------------------------------------------------------------------------
Composition (``_resolve_plan``)
    ``request.composition`` if given (counts are used as is; must be placeable in
    the space group). Otherwise: every element gets one of its pymatgen
    ``common_oxidation_states`` (no mixed valence); count vectors (each >= 1) are
    enumerated by total N in order |N - num_atoms| = 0, 1, 2 ... (smaller N first
    on a tie), and a vector is admissible iff sum n_i q_i = 0 for some choice of
    states. Single elements and all-metal systems (no element with a negative
    common state) are treated as q = 0 (elemental / intermetallic). The first
    admissible (N, space group, composition) in the order (|dN|, group rank,
    atoms per reduced formula unit, counts) whose counts pyxtal can place on
    the group's Wyckoff positions wins.

Space group
    ``target_spacegroup`` if given; else the ranked representative list of
    ``target_crystal_system`` (``REPRESENTATIVE_GROUPS``; "hexagonal" means the
    hexagonal crystal family, i.e. hexagonal + trigonal); else the concatenation
    cubic -> hexagonal -> trigonal -> tetragonal -> orthorhombic -> monoclinic ->
    triclinic, so the highest-symmetry representative that fits the atom count
    is used (P1 always fits). Cells are conventional, in pyxtal's ITA standard
    setting (hexagonal axes for R groups, unique axis b for monoclinic).

Endpoint candidates (``_search_endpoint``)
    up to ``MAX_COMBOS`` distinct Wyckoff assignments are found by seeded,
    node-budgeted random depth-first searches (sites without free parameters
    occupied at most once per cell; feasibility pre-checked with pyxtal's
    ``check_compatible``), and up to ``POOL_SIZE`` structures generated
    (one per combination without free parameters, up to ``SAMPLES_PER_COMBO``
    random free-parameter draws otherwise) on a lattice obeying the crystal-
    system constraints at the atomic-volume estimate (Shannon ionic radii x1.7
    for ions, elemental molar volume for q = 0). Draws with a contact closer
    than X_TRUST = 0.75 x the covalent-radius sum are rejected (real crystals sit
    at >= ~0.85: Ti-O in SrTiO3 0.86, Si-O 0.91, ionic pairs ~1.0); the cell is
    loosened by x1.3 / x1.7 while fewer than N_SCORED structures fit, and every
    later volume (scoring step, pre-relaxed cell, corrector updates) is kept at
    or above the volume where the closest contact reaches 0.75. The pool is pre-screened
    with the analytic LJ + Coulomb calculator (``fallback_calculator``, ~1 ms),
    duplicates (equal pre-screen energy, e.g. origin-shifted copies) dropped,
    and the best ``N_SCORED`` = 6 (2 if a combination has > 6 free parameters,
    where relaxation matters more than more random draws) scored by the engine with two evaluations each
    (one at the estimate, one after a pressure-driven volume step; the
    energy is extrapolated to the zero-pressure volume with the two-point bulk
    modulus K0). The best is pre-relaxed with ASE BFGS + ``FixSymmetry`` +
    ``FrechetCellFilter`` (positions in the symmetric subspace, symmetry-allowed
    cell DOF) with the rest of a fixed budget of ``PRESTREAM_CALLS`` = 21 engine
    calls (at least ``MIN_PRERELAX`` = 8), stopping early at fmax 0.01 eV/Å. A
    fixed call count (not a wall-clock budget) keeps runs deterministic.

Top-k candidates (Phase C2, ``top_candidates``; only when the request has top_k > 1)
    Runs after the last frame, so neither the pre-stream budget nor the per-frame
    cost changes, and top_k = 1 does no extra work. Rank 1 is the streamed
    endpoint; the alternates are the search's other structures (scored ones first
    by extrapolated energy, then the unscored distinct pool by analytic energy),
    each pre-relaxed under its symmetry with ``CANDIDATE_RELAX_CALLS`` = 8 engine
    calls + 1 evaluation, deduplicated with ``StructureMatcher``, sorted by energy;
    at most top_k + 1 alternates are tried (<= 9 (top_k + 1) extra engine calls).

Bulk-modulus steering (heuristic, said plainly)
    With ``target_bulk_modulus_gpa`` = K_t, the endpoint is relaxed to a target
    pressure P* ~ (K_t - K0) / 4, smoothly saturated (P* = c K0 tanh(x / c),
    x = (K_t - K0) / (4 K0), c = 0.2 for compression, 0.1 for tension) instead
    of 0, using the
    Murnaghan relation K(P) ~ K0 + K0' P with K0' = 4: a stiffer target gives a
    compressed, denser cell. The prior volume is also scaled by
    clip((100 GPa / K_t)^0.25, 0.8, 1.25). This is not a property predictor; the
    final cell is simply under hydrostatic pressure P* (reported in the stress).

Prior (t = 0)
    s_0 uniform on the torus conditioned on a hard core (every contact >= 0.6 x
    covalent sum, random sequential addition; DEVIATION from the literal
    U[0,1)^(N x 3) of §7: unconditioned uniform points put atoms on top of each
    other, where MACE returns energies far *below* the crystal, so the t = 0
    frame was not "high energy" and showed nonsense forces). Lattice: volume = atomic-volume estimate x K-factor x
    lognormal(0.08); lengths = endpoint length ratios x lognormal(0.15) (bounded
    to [0.7, 1.45] of the endpoint lengths so the cell change per frame is
    bounded); angles 90 deg +- U(15 deg) (generic triclinic). Prior points are
    assigned to endpoint sites per species by a minimum total squared geodesic
    distance assignment (Hungarian), jointly with the best of 64 global origin
    shifts of the endpoint (any translate of the endpoint is the same crystal),
    so trails do not cross needlessly.

Flow (frame k = 1..K at t_k = (k-1)/(K-1), h = 1/(K-1))
    Frame k shows (s_k, L_k) evaluated once by ``engine.evaluate(bond_mode="fast")``;
    the same evaluation's forces/stress drive the corrector for the next step:
      corrector (t_k > 0.65, u = (t_k - 0.65)/0.35, g = physics_guidance_scale):
        eta(t)  = min(ETA_MAX, ETA0 * g) * min(1, u / 0.3)          [Å]
        tau(t)  = TAU0 * (1 - u)^2                                   [Å] -> 0
        dr_i    = eta F_i / (max_j |F_j| + FORCE_EPS) + sqrt(2 eta tau) xi_i,
                  xi clipped to +-3 (so no atom moves more than eta + 3 sqrt(6 eta tau))
        ds      = dr L_k^-1 (row convention, §4.1); the current state gets ds;
        the endpoint gets w P(ds) with w = u^2 clip(1 - r / 0.15 Å, 0, 1), where
        r is the frame's largest distance from the endpoint (forces of frames
        still far from the endpoint say little about it), P the Reynolds projection
        onto displacement fields invariant under the target group:
        P(ds)_i = 1/|G| sum_g W_g^-1 ds_{perm_g(i)} -- so free Wyckoff
        parameters move, fixed sites do not, symmetry is preserved exactly
        (and positions are re-projected every step to stop float drift);
        cell: eps = -beta (sigma - sigma*) / max(K0, 100 GPa), beta = 0.5 min(1, g/1.5) u^3,
        |eps| <= 1 % per frame, sigma* = -P* I; applied to the current cell, and
        (scaled by the same residual weight) to the endpoint cell, whose
        parameters are then projected back onto the
        crystal-system constraints (e.g. cubic a=b=c, 90 deg), so the stress
        adjusts exactly the symmetry-allowed lattice DOF.
      predictor (§4.2, v = gamma(t) Delta s / (1 - t)):
        s_{k+1} = (s_k' + lam_k * Delta(s_k', S1)) mod 1,
        lam_k = min(1, gamma(t_k) h / (1 - t_k)),
        Delta(x, y) = (y - x + 0.5) mod 1 - 0.5 (torus geodesic), and the same
        update for p = (ln a, ln b, ln c, alpha, beta, gamma). gamma = 1 for
        t <= 0.65, which is exactly §4.2's v = Delta s / (1 - t) (constant-speed
        geodesic, s_t = s_0 + t Delta s_0); inside the corrector window gamma
        ramps linearly to GAMMA_MAX = 3 at t = 1, i.e. the same geodesic is
        traversed faster so the residual (s_t - S1) shrinks like (1 - t)^~3
        instead of (1 - t). DEVIATION from the literal §4.2 speed: without it,
        the late frames sit ~0.1 Å off the endpoint, their forces are dominated
        by that residual, and the corrector made the endpoint *worse* (P1 SiO2:
        fmax 0.65 -> 1.3 eV/Å; with gamma: 0.8 and a lower energy). lam reaches
        1 at the last step (and a step or two earlier), so frame K *is* the
        (refined) symmetric endpoint.
    Trust guard: MACE-MP-0 is not trained on strongly overlapping atoms and
    returns huge, often *attractive* forces there (energies of -1e4 eV/atom were
    seen in in-flight frames). Atoms with a contact closer than X_TRUST = 0.75 x
    the covalent-radius sum therefore get a soft-core repulsion
    REP_K (0.75 - x)/0.75 along the contact instead of the force-field force,
    other forces are capped at 20 eV/Å before normalisation, and an endpoint
    update that would create such a contact is rejected. The same guard rejects
    a pre-relaxation that ends in an overlap. This is the only non-force-field
    term in the corrector.
    The forces in-flight are evaluated at a configuration that is still a
    fraction (1 - t) of the way from the endpoint, so their symmetric part is
    biased early in the corrector window; this is why the endpoint weights
    (u^2, u^3) ramp up late, when s_t ~ S1.

Smoothness
    Linear angle interpolation stays inside the (convex) set of valid angle
    triples and log-lengths stay finite, so the volume is always positive.
    Per frame, an atom's minimum-image displacement is at most
    gamma(t) 0.5 h (|a|+|b|+|c|)  [geodesic: each fractional component of the
    whole path is <= 0.5, traversed at speed gamma/(K-1)]
    + sum_rows |L_{k+1} - L_k|  [cell motion at fixed s, |s| <= 1]
    + 2 (eta + 3 sqrt(6 eta tau))  [this step's corrector + the predictor
    removing earlier non-symmetric corrector residue]
    (``displacement_bound``). It scales like (cell size) / (K - 1): for K = 60 and
    4-7 Å cells it is ~0.2-0.6 Å; for K = 5 it is a quarter of a cell diagonal.
    Lattice parameters move by lam * (p1 - p) per frame (log-lengths, angles)
    plus <= 1 % corrector strain; the prior lengths are kept within
    [0.7, 1.45] x the endpoint lengths and the prior angles within 90 +- 15 deg.

Known weaknesses
    * Candidate pools are tiny (<= 24 structures, <= 6 force-field scored) and
      free parameters are drawn at random, so for low-symmetry groups (P1, P-1,
      monoclinic) with many free parameters the result is a locally relaxed
      random structure, not a global minimum.
    * Composition choice is a heuristic (common oxidation states, smallest
      formula unit) and never consults the force field or a phase diagram.
    * Only 8-19 BFGS steps + ~0.35 K corrector frames of refinement: residual forces
      of ~0.05-0.5 eV/Å are normal for structures with free parameters.
    * The final cell is conventional, never reduced to primitive.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import AsyncIterator, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms
from ase.data import atomic_numbers, covalent_radii
from ase.geometry import cell_to_cellpar, cellpar_to_cell, get_distances
from scipy.optimize import linear_sum_assignment

from app.schemas.messages import (
    FlowTrajectoryFrame,
    GenerationCandidate,
    GenerationCandidates,
    GenerationSearchSummary,
    StartFlowGenerationPayload,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FlowCancelled",
    "RiemannianFlowGenerator",
    "REPRESENTATIVE_GROUPS",
    "displacement_bound",
]

# --------------------------------------------------------------------------
# Constants (see module docstring)
# --------------------------------------------------------------------------
EV_PER_A3_TO_GPA = 160.21766208
T_CORRECTOR = 0.65
ETA0 = 0.04            # Å per unit physics_guidance_scale
ETA_MAX = 0.15         # Å, hard cap on the corrector step length
FORCE_EPS = 3.0        # eV/Å, epsilon of F / (|F|max + eps): eta/eps ~ 0.02 Å^2/eV is a stable
                       # gradient step for bond stiffnesses up to ~50 eV/Å^2
TAU0 = 0.01            # Å, noise temperature at t = 0.65
XI_CLIP = 3.0
R_TRUST = 0.15         # Å: endpoint refinement weight clip(1 - residual / R_TRUST, 0, 1)
K_STEP_MIN_GPA = 100.0 # floor on the modulus used to size cell steps (stability)
GAMMA_MAX = 3.0        # late-time speed-up of the residual contraction (see docstring)
CELL_BETA = 0.5
MAX_STRAIN_STEP = 0.01
K_GUESS_GPA = 80.0
K_PRIME = 4.0
MAX_ATOMS = 64
MAX_COMBOS = 16
SAMPLES_PER_COMBO = 3
POOL_SIZE = 24
N_SCORED = 6
N_SCORED_HIGH_DOF = 2   # when a combination has > 6 free parameters (e.g. P1)
PRESTREAM_CALLS = 21   # total engine calls before the first frame (scoring + pre-relaxation)
MIN_PRERELAX = 8
CLASH_RATIO = 0.75     # candidate contacts must be >= this x covalent-radius sum (= X_TRUST)
PRIOR_HARD_CORE = 0.6  # prior contacts >= this x covalent sum (see _hard_core_uniform)
X_TRUST = 0.75         # below this x covalent sum the force field is not trusted (see docstring)
REP_K = 5.0            # eV/Å, soft-core repulsion used instead of untrusted forces
F_NORM_CAP = 20.0      # eV/Å, per-atom force cap before normalisation
N_ORIGIN_SHIFTS = 64
MAX_Z = 89             # MACE-MP-0 element coverage (H .. Ac)
CANDIDATE_RELAX_CALLS = 8   # top-k: symmetric pre-relaxation budget per extra candidate (+1 evaluate)
CANDIDATE_EXTRA_ATTEMPTS = 2  # top-k: alternates tried beyond top_k - 1 (duplicates after relaxation)

REPRESENTATIVE_GROUPS: Dict[str, List[int]] = {
    "cubic": [221, 225, 229, 227, 215, 216, 223, 200, 195, 198],
    "hexagonal": [191, 194, 187, 186, 189, 176, 168],
    "trigonal": [166, 164, 160, 156, 148, 147, 143],
    "tetragonal": [123, 139, 129, 141, 136, 115, 99, 83, 75],
    "orthorhombic": [47, 65, 62, 63, 25, 16],
    "monoclinic": [10, 12, 14, 6, 3],
    "triclinic": [2, 1],
}
_SYSTEM_ALIASES = {
    "hexagonal": ["hexagonal", "trigonal"],   # crystal family as offered by the UI
    "trigonal": ["trigonal"],
    "rhombohedral": ["trigonal"],
}
_DEFAULT_ORDER = ["cubic", "hexagonal", "trigonal", "tetragonal", "orthorhombic", "monoclinic", "triclinic"]


class FlowCancelled(Exception):
    """Raised by the synchronous core when ``should_cancel()`` returns True."""


# --------------------------------------------------------------------------
# pyxtal helpers (pyxtal import costs ~1.5 s, so do it once at module import)
# --------------------------------------------------------------------------
from pyxtal.symmetry import Group as _PyxtalGroup  # noqa: E402


@lru_cache(maxsize=256)
def _group(number: int) -> "_PyxtalGroup":
    return _PyxtalGroup(int(number))


@lru_cache(maxsize=256)
def _group_ops(number: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(W (G,3,3), t (G,3), W^-1 (G,3,3)) of the general position, fractional, incl. centring."""
    ops = _group(number)[0].ops
    aff = np.array([op.affine_matrix for op in ops], dtype=float)
    w = aff[:, :3, :3]
    tr = aff[:, :3, 3]
    winv = np.linalg.inv(w)
    for arr in (w, tr, winv):
        arr.setflags(write=False)
    return w, tr, winv


def _contact_ratios(positions: np.ndarray, cell: np.ndarray, numbers: np.ndarray):
    """Minimum-image pair vectors D[i, j] = r_j - r_i, distances, x = d / (R_i + R_j)."""
    D, d = get_distances(positions, cell=cell, pbc=True)
    rad = np.maximum(covalent_radii[numbers], 0.3)
    x = d / (rad[:, None] + rad[None, :])
    np.fill_diagonal(x, np.inf)
    return D, d, x


def _min_contact(frac: np.ndarray, cell: np.ndarray, numbers: np.ndarray) -> float:
    if len(numbers) < 2:
        return float("inf")
    return float(_contact_ratios(frac @ cell, cell, numbers)[2].min())


def _trusted_forces(forces: np.ndarray, positions: np.ndarray, cell: np.ndarray,
                    numbers: np.ndarray) -> Tuple[np.ndarray, int]:
    """Replace forces on overlapping atoms by a soft-core repulsion; cap the rest."""
    out = np.array(forces, dtype=float, copy=True)
    norms = np.linalg.norm(out, axis=1)
    big = norms > F_NORM_CAP
    out[big] *= (F_NORM_CAP / norms[big])[:, None]
    if len(numbers) < 2:
        return out, 0
    D, d, x = _contact_ratios(positions, cell, numbers)
    ii, jj = np.nonzero(x < X_TRUST)
    if len(ii) == 0:
        return out, 0
    rep = np.zeros_like(out)
    for i, j in zip(ii, jj):
        v = D[i, j]
        dist = float(d[i, j])
        u = -v / dist if dist > 1e-8 else np.array([1.0, 0.0, 0.0]) * (1.0 if i < j else -1.0)
        rep[i] += REP_K * (X_TRUST - x[i, j]) / X_TRUST * u
    bad = np.unique(ii)
    out[bad] = rep[bad]
    return out, int(len(bad))


def _hard_core_uniform(numbers: np.ndarray, cell: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Uniform points on the torus conditioned on contacts >= PRIOR_HARD_CORE x covalent sum.

    Random sequential addition: each atom takes the first of up to 200 uniform
    draws that respects the hard core w.r.t. the atoms already placed (else the
    draw with the largest clearance). Without it the t = 0 frame contains
    near-coincident atoms for which MACE-MP-0 returns meaningless, often very
    *negative* energies (-15 to -8000 eV/atom observed).
    """
    n = len(numbers)
    rad = np.maximum(covalent_radii[numbers], 0.3)
    out = np.zeros((n, 3))
    for i in range(n):
        best, best_x = None, -1.0
        for _ in range(200):
            cand = rng.random(3)
            if i == 0:
                best = cand
                break
            d = cand[None, :] - out[:i]
            d -= np.round(d)
            dist = np.linalg.norm(d @ cell, axis=1)
            x = float((dist / (rad[i] + rad[:i])).min())
            if x > best_x:
                best, best_x = cand, x
            if x >= PRIOR_HARD_CORE:
                break
        out[i] = best
    return out


def _wyckoff_by_label(number: int) -> Dict[str, object]:
    return {wp.get_label(): wp for wp in _group(number).Wyckoff_positions}


def _lattice_system(number: int) -> str:
    return str(_group(number).lattice_type)


def _compatible(number: int, counts: Sequence[int]) -> bool:
    try:
        ok, _ = _group(number).check_compatible(list(int(c) for c in counts))
        return bool(ok)
    except Exception:
        return False


# --------------------------------------------------------------------------
# Lattice helpers
# --------------------------------------------------------------------------
def _constrain_cellpar(system: str, p: np.ndarray, symbol: str = "") -> np.ndarray:
    """Project (a, b, c, alpha, beta, gamma) onto the crystal-system constraints."""
    a, b, c, al, be, ga = (float(x) for x in p)
    if system == "cubic":
        m = (a * b * c) ** (1.0 / 3.0)
        return np.array([m, m, m, 90.0, 90.0, 90.0])
    if system == "tetragonal":
        m = math.sqrt(a * b)
        return np.array([m, m, c, 90.0, 90.0, 90.0])
    if system in ("hexagonal", "trigonal"):
        m = math.sqrt(a * b)
        return np.array([m, m, c, 90.0, 90.0, 120.0])
    if system == "orthorhombic":
        return np.array([a, b, c, 90.0, 90.0, 90.0])
    if system == "monoclinic":
        return np.array([a, b, c, 90.0, be, 90.0])
    return np.array([a, b, c, al, be, ga])


def _cell(p: np.ndarray) -> np.ndarray:
    return np.array(cellpar_to_cell(np.asarray(p, dtype=float)), dtype=float)


def _cellpar(cell: np.ndarray) -> np.ndarray:
    return np.array(cell_to_cellpar(np.asarray(cell, dtype=float)), dtype=float)


def _lat_vec(p: np.ndarray) -> np.ndarray:
    """Interpolation coordinates (ln a, ln b, ln c, alpha, beta, gamma)."""
    return np.concatenate([np.log(p[:3]), p[3:]])


def _lat_par(v: np.ndarray) -> np.ndarray:
    return np.concatenate([np.exp(v[:3]), v[3:]])


def _valid_angles(al: float, be: float, ga: float) -> bool:
    ca, cb, cg = (math.cos(math.radians(x)) for x in (al, be, ga))
    return 1 - ca * ca - cb * cb - cg * cg + 2 * ca * cb * cg > 1e-3


def _sample_shape(system: str, symbol: str, rng: np.random.Generator) -> np.ndarray:
    """Unit-volume-agnostic cell parameters obeying the crystal-system constraints."""
    def ln(s):
        return float(math.exp(rng.normal(0.0, s)))

    if system == "cubic":
        return np.array([1.0, 1.0, 1.0, 90.0, 90.0, 90.0])
    if system == "tetragonal":
        return np.array([1.0, 1.0, ln(0.1), 90.0, 90.0, 90.0])
    if system in ("hexagonal", "trigonal"):
        c_over_a = 2.6 if symbol.startswith("R") else 1.6 if system == "hexagonal" else 1.2
        return np.array([1.0, 1.0, c_over_a * ln(0.08), 90.0, 90.0, 120.0])
    if system == "orthorhombic":
        return np.array([1.0, ln(0.12), ln(0.12), 90.0, 90.0, 90.0])
    if system == "monoclinic":
        return np.array([1.0, ln(0.12), ln(0.12), 90.0, 90.0 + 5.0 + abs(rng.normal(0.0, 8.0)), 90.0])
    while True:
        ang = 90.0 + rng.uniform(-12.0, 12.0, 3)
        if _valid_angles(*ang):
            return np.array([1.0, ln(0.12), ln(0.12), *ang])


def _scale_to_volume(p: np.ndarray, volume: float) -> np.ndarray:
    q = np.array(p, dtype=float)
    v = abs(np.linalg.det(_cell(q)))
    q[:3] *= (volume / v) ** (1.0 / 3.0)
    return q


def _wrap(s: np.ndarray) -> np.ndarray:
    out = np.mod(s, 1.0)
    out[out >= 1.0] = 0.0
    return out


def _geodesic(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """§4.2: shortest torus vector from x to y, components in [-0.5, 0.5)."""
    return np.mod(y - x + 0.5, 1.0) - 0.5


def _schedules(t: float, guidance: float) -> Tuple[float, float, float]:
    """(eta Å, tau Å, gamma) at flow time t; eta = tau = 0 and gamma = 1 for t <= 0.65."""
    if t <= T_CORRECTOR:
        return 0.0, 0.0, 1.0
    u = (t - T_CORRECTOR) / (1.0 - T_CORRECTOR)
    eta = min(ETA_MAX, ETA0 * guidance) * min(1.0, u / 0.3) if guidance > 0 else 0.0
    tau = TAU0 * (1.0 - u) ** 2
    return eta, tau, 1.0 + (GAMMA_MAX - 1.0) * u


def displacement_bound(cell_k: np.ndarray, cell_k1: np.ndarray, steps: int, t_k: float,
                       guidance: float) -> float:
    """Upper bound (Å) on any atom's minimum-image displacement from frame k to k+1."""
    eta, tau, gamma = _schedules(t_k, guidance)
    h = 1.0 / (steps - 1)
    lengths = np.maximum(np.linalg.norm(cell_k, axis=1), np.linalg.norm(cell_k1, axis=1))
    geo = gamma * 0.5 * h * float(lengths.sum())
    lat = float(np.linalg.norm(np.asarray(cell_k1) - np.asarray(cell_k), axis=1).sum())
    corr = 2.0 * (eta + XI_CLIP * math.sqrt(3.0 * 2.0 * eta * tau))
    return geo + lat + corr


# --------------------------------------------------------------------------
# Symmetry projection with fixed index permutations
# --------------------------------------------------------------------------
def _perm_tables(frac: np.ndarray, numbers: np.ndarray, w: np.ndarray, tr: np.ndarray,
                 tol: float = 1e-3) -> np.ndarray:
    """perm[g, i] = j such that op g maps site i onto site j (same species)."""
    img = np.einsum("gab,nb->gna", w, frac) + tr[:, None, :]          # (G, N, 3)
    d = img[:, :, None, :] - frac[None, None, :, :]                   # (G, N, N, 3)
    d -= np.round(d)
    dist = np.abs(d).max(axis=-1)                                     # (G, N, N)
    dist = np.where(numbers[None, None, :] == numbers[None, :, None], dist, np.inf)
    perm = np.argmin(dist, axis=2)
    best = np.take_along_axis(dist, perm[..., None], axis=2)[..., 0]
    if not np.all(best < tol):
        raise RuntimeError("endpoint is not invariant under the target space group")
    return perm


def _sym_displacement(ds: np.ndarray, perm: np.ndarray, winv: np.ndarray) -> np.ndarray:
    """Reynolds projection onto invariant displacement fields (fractional)."""
    return np.einsum("gab,gnb->na", winv, ds[perm]) / perm.shape[0]


def _sym_positions(s: np.ndarray, perm: np.ndarray, w_inv: np.ndarray, tr: np.ndarray) -> np.ndarray:
    pre = np.einsum("gab,gnb->gna", w_inv, s[perm] - tr[:, None, :])  # preimages of s_perm
    d = pre - s[None, :, :]
    d -= np.round(d)
    return _wrap(s + d.mean(axis=0))


# --------------------------------------------------------------------------
# Composition / chemistry
# --------------------------------------------------------------------------
def _canonical_element(sym: str) -> str:
    if not isinstance(sym, str) or not sym.strip():
        raise ValueError(f"invalid element symbol {sym!r}")
    s = sym.strip()
    s = s[0].upper() + s[1:].lower()
    z = atomic_numbers.get(s)
    if z is None or s == "X":
        raise ValueError(f"unknown element {sym!r}")
    if z > MAX_Z:
        raise ValueError(f"element {s} (Z={z}) is outside the force-field coverage (Z <= {MAX_Z})")
    return s


@lru_cache(maxsize=512)
def _oxidation_options(el: str) -> Tuple[int, ...]:
    from pymatgen.core import Element

    states = tuple(int(q) for q in Element(el).common_oxidation_states)
    return states or (0,)


def _neutral_states(elements: Sequence[str], counts: Sequence[int], metallic: bool) -> Optional[Tuple[int, ...]]:
    if metallic:
        return tuple(0 for _ in elements)
    for qs in itertools.product(*(_oxidation_options(e) for e in elements)):
        if sum(n * q for n, q in zip(counts, qs)) == 0 and any(qs):
            return tuple(qs)
    return None


def _is_metallic_system(elements: Sequence[str]) -> bool:
    if len(elements) == 1:
        return True
    return all(min(_oxidation_options(e)) >= 0 for e in elements)


@lru_cache(maxsize=4096)
def _atom_volume(el: str, q: int) -> float:
    """Heuristic volume per atom (Å^3) for the density estimate."""
    from pymatgen.core import Element, Species

    if q != 0:
        try:
            r = Species(el, q).ionic_radius
            if r is not None and float(r) > 0:
                return 1.7 * 4.0 / 3.0 * math.pi * float(r) ** 3
        except Exception:
            pass
    try:
        mv = Element(el).molar_volume
        if mv is not None and float(mv) > 0:
            return float(mv) * 1.66053907  # cm^3/mol -> Å^3/atom
    except Exception:
        pass
    r = float(covalent_radii[atomic_numbers[el]])
    return 2.5 * 4.0 / 3.0 * math.pi * r ** 3


def _compositions_with_total(n_el: int, total: int) -> Iterator[Tuple[int, ...]]:
    if n_el == 1:
        yield (total,)
        return
    for first in range(1, total - n_el + 2):
        for rest in _compositions_with_total(n_el - 1, total - first):
            yield (first,) + rest


def _fu_size(counts: Sequence[int]) -> int:
    g = 0
    for c in counts:
        g = math.gcd(g, int(c))
    return sum(counts) // max(g, 1)


@dataclass
class _Plan:
    spacegroup: int
    symbol: str
    system: str
    elements: List[str]
    counts: List[int]
    charges: List[int]
    numbers: np.ndarray            # per atom, species blocks in element order

    @property
    def n_atoms(self) -> int:
        return int(sum(self.counts))


def _candidate_groups(request: StartFlowGenerationPayload) -> List[int]:
    if request.target_spacegroup is not None:
        n = int(request.target_spacegroup)
        if not 1 <= n <= 230:
            raise ValueError(f"target_spacegroup must be 1..230, got {n}")
        return [n]
    if request.target_crystal_system:
        key = request.target_crystal_system.strip().lower()
        systems = _SYSTEM_ALIASES.get(key, [key] if key in REPRESENTATIVE_GROUPS else None)
        if systems is None:
            raise ValueError(
                f"unknown target_crystal_system {request.target_crystal_system!r}; expected one of "
                f"{sorted(set(REPRESENTATIVE_GROUPS) | set(_SYSTEM_ALIASES))}")
        return [g for s in systems for g in REPRESENTATIVE_GROUPS[s]]
    return [g for s in _DEFAULT_ORDER for g in REPRESENTATIVE_GROUPS[s]]


def _resolve_plan(request: StartFlowGenerationPayload) -> _Plan:
    if not request.elements:
        raise ValueError("elements must contain at least one element")
    elements: List[str] = []
    for e in request.elements:
        s = _canonical_element(e)
        if s not in elements:
            elements.append(s)
    groups = _candidate_groups(request)

    if request.composition:
        comp: Dict[str, int] = {}
        for k, v in request.composition.items():
            s = _canonical_element(k)
            if int(v) < 0:
                raise ValueError(f"negative count for {s} in composition")
            if int(v) > 0:
                comp[s] = comp.get(s, 0) + int(v)
        if not comp:
            raise ValueError("composition has no atoms")
        extra = sorted(set(comp) - set(elements))
        if extra:
            raise ValueError(f"composition contains elements not listed in elements: {extra}")
        if sum(comp.values()) > MAX_ATOMS:
            raise ValueError(f"composition has {sum(comp.values())} atoms; at most {MAX_ATOMS} per cell are supported")
        els = [e for e in elements if e in comp]
        counts = [comp[e] for e in els]
        for g in groups:
            if _compatible(g, counts):
                q = _neutral_states(els, counts, _is_metallic_system(els)) or tuple(0 for _ in els)
                return _make_plan(g, els, counts, list(q))
        where = f"space group {groups[0]}" if len(groups) == 1 else f"any of space groups {groups}"
        raise ValueError(
            f"composition {comp} cannot be placed on the Wyckoff positions of {where}")

    metallic = _is_metallic_system(elements)
    target = int(request.num_atoms)
    n_el = len(elements)
    if n_el > 6:
        raise ValueError("at most 6 elements are supported without an explicit composition")
    by_total: Dict[int, List[Tuple[Tuple[int, ...], Tuple[int, ...]]]] = {}

    def comps(total: int):
        if total not in by_total:
            found = []
            if n_el <= total <= MAX_ATOMS:
                for c in _compositions_with_total(n_el, total):
                    q = _neutral_states(elements, c, metallic)
                    if q is not None:
                        found.append((c, q))
                found.sort(key=lambda cq: (_fu_size(cq[0]), cq[0]))
            by_total[total] = found
        return by_total[total]

    for delta in range(0, MAX_ATOMS + 1):
        totals = [t for t in (target - delta, target + delta) if n_el <= t <= MAX_ATOMS]
        totals = sorted(set(totals))
        if not totals:
            if target - delta < n_el and target + delta > MAX_ATOMS:
                break
            continue
        for g in groups:
            for total in totals:
                for c, q in comps(total):
                    if _compatible(g, c):
                        return _make_plan(g, elements, list(c), list(q))
    raise ValueError(
        f"no charge-neutral composition of {elements} with <= {MAX_ATOMS} atoms fits space group(s) {groups}")


def _make_plan(g: int, elements: List[str], counts: List[int], charges: List[int]) -> _Plan:
    grp = _group(g)
    numbers = np.concatenate([[atomic_numbers[e]] * n for e, n in zip(elements, counts)]).astype(int)
    return _Plan(spacegroup=g, symbol=str(grp.symbol), system=_lattice_system(g), elements=elements,
                 counts=counts, charges=charges, numbers=numbers)


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------
@dataclass
class _Endpoint:
    frac: np.ndarray               # (N, 3) standard-setting fractional coordinates
    cellpar: np.ndarray            # (6,)
    labels: List[List[str]]        # Wyckoff labels per species
    perm: np.ndarray               # (G, N)
    k0_gpa: float = K_GUESS_GPA
    p_target_gpa: float = 0.0
    info: Dict[str, object] = field(default_factory=dict)
    # top-k (Phase C2): the other searched structures, best first: (combo, frac, cellpar, scored)
    alternates: List[Tuple[List[List[str]], np.ndarray, np.ndarray, bool]] = field(default_factory=list)


def _payload_symbol(number: int, fallback: str) -> str:
    """Hermann-Mauguin symbol in the payloads' (spglib / pymatgen) spelling, e.g. "P6_3mc"."""
    try:
        from pymatgen.symmetry.groups import SpaceGroup

        return str(SpaceGroup.from_int_number(int(number)).symbol)
    except Exception:  # noqa: BLE001
        return fallback


def _wyckoff_text(elements: Sequence[str], combo: List[List[str]]) -> str:
    """e.g. "Sr 1a, Ti 1b, O 3c" (several orbits of one species joined with "+")."""
    return ", ".join(f"{el} {'+'.join(labels)}" for el, labels in zip(elements, combo))


def _orbit(wp, rng: np.random.Generator) -> Optional[np.ndarray]:
    pt = wp.ops[0].operate(rng.random(3))
    pos = _wrap(np.asarray(wp.get_all_positions(pt), dtype=float))
    # orbit must be non-degenerate (special values of free parameters can merge points)
    d = pos[:, None, :] - pos[None, :, :]
    d -= np.round(d)
    dm = np.abs(d).max(axis=-1) + np.eye(len(pos))
    if len(pos) != wp.multiplicity or dm.min() < 1e-3:
        return None
    return pos


def _build_structure(plan: _Plan, combo: List[List[str]], cellpar: np.ndarray,
                     rng: np.random.Generator) -> Optional[np.ndarray]:
    wps = _wyckoff_by_label(plan.spacegroup)
    blocks = []
    for labels in combo:
        for lab in labels:
            orb = _orbit(wps[lab], rng)
            if orb is None:
                return None
            blocks.append(orb)
    frac = _wrap(np.concatenate(blocks, axis=0))
    cell = _cell(cellpar)
    _, d = get_distances(frac @ cell, cell=cell, pbc=True)
    rad = np.maximum(covalent_radii[plan.numbers], 0.3)
    ratio = d / (rad[:, None] + rad[None, :]) + np.eye(len(frac)) * 1e3
    if ratio.min() < CLASH_RATIO:
        return None
    return frac


def _random_combo(sites: List[Tuple[str, int, bool]], counts: Sequence[int],
                  rng: np.random.Generator, budget: int = 4000) -> Optional[List[List[str]]]:
    """One randomised depth-first search for a Wyckoff assignment.

    ``sites`` = (label, multiplicity, has_free_parameters). A site without free
    parameters can be occupied once in the whole cell; a site with free
    parameters any number of times (different parameter values). Returns per-
    species label lists, or None if the node budget is exhausted.
    """
    nodes = [0]
    used: set = set()

    def fill(sp: int, remaining: int, start_pos: int, acc: List[List[str]]):
        nodes[0] += 1
        if nodes[0] > budget:
            return None
        if remaining == 0:
            if sp + 1 == len(counts):
                return [list(a) for a in acc]
            acc.append([])
            out = fill(sp + 1, counts[sp + 1], 0, acc)
            acc.pop()
            return out
        # canonical order within a species (labels non-decreasing in a random permutation)
        order = list(range(start_pos, len(sites)))
        rng.shuffle(order)
        for pos in order:
            label, mult, free = sites[pos]
            if mult > remaining or (not free and label in used):
                continue
            if not free:
                used.add(label)
            acc[-1].append(label)
            out = fill(sp, remaining - mult, pos if free else pos + 1, acc)
            acc[-1].pop()
            if not free:
                used.discard(label)
            if out is not None:
                return out
            if nodes[0] > budget:
                return None
        return None

    return fill(0, counts[0], 0, [[]])


def _combos(plan: _Plan, rng: np.random.Generator) -> List[List[List[str]]]:
    """Up to MAX_COMBOS distinct Wyckoff assignments, found by seeded random searches.

    pyxtal's ``list_wyckoff_combinations`` is exhaustive (thousands of
    combinations / ~1 s for e.g. P2/m with 14 atoms), so it is not used.
    """
    wps = _group(plan.spacegroup).Wyckoff_positions
    sites = [(wp.get_label(), int(wp.multiplicity), int(wp.get_dof()) > 0) for wp in wps]
    found: List[List[List[str]]] = []
    keys = set()
    for _ in range(4 * MAX_COMBOS):
        c = _random_combo(sites, list(plan.counts), rng)
        if c is None:
            continue
        key = tuple(tuple(sorted(x)) for x in c)
        if key not in keys:
            keys.add(key)
            found.append([sorted(x) for x in c])
        if len(found) >= MAX_COMBOS:
            break
    if not found:
        raise ValueError(
            f"composition {dict(zip(plan.elements, plan.counts))} cannot be placed in space group {plan.spacegroup}")
    return found


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------
class RiemannianFlowGenerator:
    """Symmetry-conditioned geodesic flow sampler with force-field Langevin corrector.

    ``iter_frames`` is the synchronous, cancellable core; ``sample`` runs it in a
    worker thread. ``current_atoms`` is the structure of the most recently
    yielded frame (what STOP_STREAM keeps); ``final_atoms`` is set with the
    t == 1.0 frame. ``last_run_info`` holds diagnostics of the latest run.
    """

    def __init__(self, engine=None) -> None:
        if engine is None:
            from app.core.umlip_engine import UMLIPEngine

            engine = UMLIPEngine.get_instance()
        self.engine = engine
        self.current_atoms: Optional[Atoms] = None
        self.final_atoms: Optional[Atoms] = None
        self.last_run_info: Dict[str, object] = {}
        self._last_search: Optional[Tuple[_Plan, _Endpoint]] = None

    # ------------------------------------------------------------ public API
    def iter_frames(self, request: StartFlowGenerationPayload, *,
                    should_cancel: Optional[Callable[[], bool]] = None) -> Iterator[FlowTrajectoryFrame]:
        """Validate ``request`` now (ValueError) and return the frame iterator."""
        self.current_atoms = None
        self.final_atoms = None
        plan = _resolve_plan(request)
        return self._iter_with_state(plan, request, should_cancel)

    def _iter_with_state(self, plan, request, should_cancel):
        for frame, atoms in self._generate(plan, request, should_cancel):
            self.current_atoms = atoms
            if frame.step == frame.total_steps:
                self.final_atoms = atoms.copy()
            yield frame

    async def sample(self, request: StartFlowGenerationPayload) -> AsyncIterator[FlowTrajectoryFrame]:
        """``async for frame in generator.sample(request)``; compute runs in a worker thread.

        The worker computes at most two frames ahead of the consumer. Leaving the
        loop early (break / aclose) or cancelling the consuming task sets a cancel
        flag the worker polls before every engine call, and this generator waits
        (without blocking the loop) for the worker to finish its current call.

        Note: a bare ``break`` out of ``async for`` does not close an async
        generator immediately (Python only finalises it at garbage collection /
        loop shutdown), so consumers should wrap it in
        ``contextlib.aclosing(generator.sample(req))`` to stop the worker at once.
        """
        self.current_atoms = None
        self.final_atoms = None
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        cancel = threading.Event()
        slots = threading.Semaphore(2)

        def put(item) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:  # loop already closed
                cancel.set()

        def worker() -> None:
            try:
                plan = _resolve_plan(request)
                gen = self._generate(plan, request, cancel.is_set)
                while True:
                    while not slots.acquire(timeout=0.05):
                        if cancel.is_set():
                            return
                    if cancel.is_set():
                        return
                    try:
                        item = next(gen)
                    except StopIteration:
                        break
                    put(("frame", item))
                put(("done", None))
            except FlowCancelled:
                put(("done", None))
            except BaseException as exc:  # noqa: BLE001 - re-raised in the consumer
                put(("error", exc))

        thread = threading.Thread(target=worker, name="flow-generator", daemon=True)
        thread.start()
        try:
            while True:
                kind, item = await queue.get()
                if kind == "frame":
                    slots.release()
                    frame, atoms = item
                    self.current_atoms = atoms
                    if frame.step == frame.total_steps:
                        self.final_atoms = atoms.copy()
                    yield frame
                elif kind == "done":
                    return
                else:
                    raise item
        finally:
            cancel.set()
            deadline = time.monotonic() + 30.0
            while thread.is_alive() and time.monotonic() < deadline:
                try:
                    await asyncio.sleep(0.005)
                except asyncio.CancelledError:
                    break

    def top_candidates(self, top_k: int, final_atoms: Atoms, final_crystal, *,
                       should_cancel: Optional[Callable[[], bool]] = None) -> GenerationCandidates:
        """Top-k distinct endpoint candidates of the latest run (call after its last frame).

        Rank 1 is always the streamed endpoint (``final_atoms`` / ``final_crystal``, i.e. what
        CRYSTAL_STATE_FINAL carries). The alternates are the search's other structures in
        search order (engine-scored ones by extrapolated energy, then unscored pre-screened ones
        by analytic energy); each is pre-relaxed under its symmetry like the endpoint
        (``_prerelax`` with ``CANDIDATE_RELAX_CALLS`` = 8 engine calls, same target pressure),
        evaluated once (CrystalNN bonds) and kept if pymatgen's ``StructureMatcher`` (default
        tolerances) finds it distinct from every structure kept so far. At most
        ``top_k - 1 + CANDIDATE_EXTRA_ATTEMPTS`` alternates are tried, i.e. <= 9 engine calls per
        attempt. Ranks 2.. are sorted by energy; since rank 1 is pinned to the streamed
        structure, an alternate can (rarely) end lower in energy than rank 1, which then shows as
        a negative ``relative_energy_mev_per_atom``. Deterministic for a seeded run.
        """
        from pymatgen.analysis.structure_matcher import StructureMatcher
        from pymatgen.io.ase import AseAtomsAdaptor

        if self._last_search is None:
            raise RuntimeError("top_candidates needs a completed run")
        plan, ep = self._last_search
        matcher = StructureMatcher()
        w, tr, winv = _group_ops(plan.spacegroup)
        kept = [(final_crystal, ep.labels, AseAtomsAdaptor.get_structure(final_atoms))]
        extra = []
        n_new_scored = 0
        attempts = 0
        for combo, frac0, par0, scored in ep.alternates:
            if len(kept) >= top_k or attempts >= top_k - 1 + CANDIDATE_EXTRA_ATTEMPTS:
                break
            attempts += 1
            self._check(should_cancel)
            frac, par, _ = self._prerelax(plan, frac0, par0, ep.p_target_gpa, CANDIDATE_RELAX_CALLS, should_cancel)
            try:
                frac = _sym_positions(frac, _perm_tables(frac, plan.numbers, w, tr), winv, tr)
            except RuntimeError:          # relaxation drifted off the group: keep the relaxed positions
                pass
            par = _constrain_cellpar(plan.system, par)
            atoms = Atoms(numbers=plan.numbers, scaled_positions=_wrap(frac), cell=_cell(par), pbc=True)
            self._check(should_cancel)
            payload = self.engine.evaluate(atoms, bond_mode="crystalnn")
            if not scored:
                n_new_scored += 1
            if not math.isfinite(float(payload.energy_per_atom)):
                continue
            struct = AseAtomsAdaptor.get_structure(atoms)
            if any(matcher.fit(struct, k[2]) for k in kept):
                continue
            kept.append((payload, combo, struct))
            extra.append((payload, combo))

        e1 = float(final_crystal.energy_per_atom)
        ordered = [(final_crystal, ep.labels)] + sorted(extra, key=lambda x: float(x[0].energy_per_atom))
        candidates = [
            GenerationCandidate(
                rank=i + 1,
                energy_per_atom=float(c.energy_per_atom),
                relative_energy_mev_per_atom=(float(c.energy_per_atom) - e1) * 1000.0,
                spacegroup_symbol=c.spacegroup_symbol,
                spacegroup_number=int(c.spacegroup_number),
                wyckoff_assignment=_wyckoff_text(plan.elements, combo),
                crystal=c,
            )
            for i, (c, combo) in enumerate(ordered)
        ]
        summary = GenerationSearchSummary(
            spacegroup_number=plan.spacegroup,
            spacegroup_symbol=_payload_symbol(plan.spacegroup, plan.symbol),
            composition=dict(zip(plan.elements, (int(c) for c in plan.counts))),
            n_wyckoff_combinations=int(ep.info["n_combinations"]),
            n_structures_screened=int(ep.info["pool_size"]),
            n_structures_scored=int(ep.info["n_scored"]) + n_new_scored,
        )
        self.last_run_info["top_k_attempts"] = attempts
        return GenerationCandidates(summary=summary, candidates=candidates)

    # ------------------------------------------------------------ internals
    def _check(self, should_cancel) -> None:
        if should_cancel is not None and should_cancel():
            raise FlowCancelled("flow generation cancelled")

    def _efs(self, frac: np.ndarray, cellpar: np.ndarray, numbers: np.ndarray):
        cell = _cell(cellpar)
        atoms = Atoms(numbers=numbers, scaled_positions=frac, cell=cell, pbc=True)
        e, f, s = self.engine.energy_forces_stress(atoms)
        n = len(numbers)
        pressure = -float(np.mean(s[:3])) * EV_PER_A3_TO_GPA
        return e / n, f, s, pressure

    def _search_endpoint(self, plan: _Plan, request: StartFlowGenerationPayload,
                         rng: np.random.Generator, should_cancel) -> _Endpoint:
        from app.core.fallback_calculator import AnalyticFallbackCalculator

        t0 = time.perf_counter()
        numbers = plan.numbers
        n = plan.n_atoms
        v_est = sum(c * _atom_volume(e, q) for e, c, q in zip(plan.elements, plan.counts, plan.charges))
        shape = _constrain_cellpar(plan.system, _sample_shape(plan.system, plan.symbol, rng))
        combos = _combos(plan, rng)
        wps = _wyckoff_by_label(plan.spacegroup)

        pool: List[Tuple[List[List[str]], np.ndarray, np.ndarray]] = []
        # loosen the cell (x1.3, x1.7) while fewer than N_SCORED structures fit without overlaps
        for vol_factor in (1.0, 1.3, 1.7):
            cellpar_v = _scale_to_volume(shape, v_est * vol_factor)
            rounds = 0
            while len(pool) < POOL_SIZE and rounds < SAMPLES_PER_COMBO:
                for combo in combos:
                    if len(pool) >= POOL_SIZE:
                        break
                    has_dof = any(int(wps[l].get_dof()) > 0 for labs in combo for l in labs)
                    if (rounds > 0 or vol_factor > 1.0) and not has_dof and                             any(c is combo for c, _, _ in pool):
                        continue
                    for _ in range(30):
                        frac = _build_structure(plan, combo, cellpar_v, rng)
                        if frac is not None:
                            pool.append((combo, frac, cellpar_v))
                            break
                rounds += 1
            if len(pool) >= N_SCORED:
                break
        if not pool:
            raise ValueError(
                f"could not place {dict(zip(plan.elements, plan.counts))} in space group {plan.spacegroup} "
                f"without atomic overlaps")

        def contact_safe(frac_c, par, volume):
            """Volume >= the smallest one keeping every contact >= X_TRUST x covalent sum."""
            x = _min_contact(frac_c, _cell(par), numbers)
            v_par = abs(float(np.linalg.det(_cell(par))))
            v_min = v_par * (X_TRUST / x) ** 3 if x > 0 else volume
            return max(volume, v_min * (1.0 + 1e-9))

        # cheap analytic pre-screen, dedupe equal energies (origin-shifted copies)
        cheap = AnalyticFallbackCalculator()
        scored = []
        for combo, frac, par in pool:
            e, _, _ = cheap.compute(Atoms(numbers=numbers, scaled_positions=frac, cell=_cell(par), pbc=True))
            scored.append((e / n, combo, frac, par))
        scored.sort(key=lambda x: x[0])
        distinct = []
        for item in scored:
            if all(abs(item[0] - d[0]) > 1e-7 * max(1.0, abs(item[0])) for d in distinct):
                distinct.append(item)
        # many free parameters: relaxation is worth more than scoring more random draws
        dof = max(sum(int(wps[l].get_dof()) for labs in c for l in labs) for c in combos)
        shortlist = distinct[:N_SCORED if dof <= 6 else N_SCORED_HIGH_DOF]

        # force-field scoring with a two-point volume step
        best = None
        n_evals = 0
        best_idx = -1
        scored_alts = []      # (e_min, index in shortlist, combo, frac, cellpar at the extrapolated volume)
        for idx, (_, combo, frac, par) in enumerate(shortlist):
            v1 = abs(float(np.linalg.det(_cell(par))))
            self._check(should_cancel)
            e1, _, _, p1 = self._efs(frac, par, numbers)
            n_evals += 1
            if not (math.isfinite(e1) and math.isfinite(p1)):
                continue
            dlnv = float(np.clip(p1 / K_GUESS_GPA, -0.3, 0.3))
            v2 = contact_safe(frac, par, v1 * math.exp(dlnv))
            dlnv = math.log(v2 / v1)
            if abs(dlnv) < 1e-3:
                dlnv = 0.02
                v2 = v1 * math.exp(dlnv)
            cp2 = _scale_to_volume(par, v2)
            self._check(should_cancel)
            e2, _, _, p2 = self._efs(frac, cp2, numbers)
            n_evals += 1
            if not (math.isfinite(e2) and math.isfinite(p2)):
                continue
            k0 = -(p2 - p1) / dlnv
            k0 = float(np.clip(k0, 10.0, 500.0)) if math.isfinite(k0) else K_GUESS_GPA
            # E(lnV) ~ quadratic with curvature K0 V: E_min ~ E2 - P2^2 V2 / (2 K0)
            v_opt = contact_safe(frac, par, v2 * math.exp(float(np.clip(p2 / k0, -0.3, 0.3))))
            e_min = e2 - (p2 ** 2) * v2 / (2.0 * k0) / EV_PER_A3_TO_GPA / n
            if v_opt > v2 * math.exp(p2 / k0) * 1.0001:      # contact-limited: no full extrapolation
                e_min = min(e1, e2)
            e_min = max(e_min, min(e1, e2) - 2.0)
            scored_alts.append((e_min, idx, combo, frac, _scale_to_volume(par, v_opt)))
            if best is None or e_min < best[0]:
                best = (e_min, combo, frac, k0, v2, p2, par)
                best_idx = idx
        if best is None:
            raise ValueError("force field returned non-finite energies for every candidate")
        _, combo, frac, k0, v2, p2, cellpar0 = best
        # top-k alternates (used only after the stream, see ``top_candidates``): the other scored
        # structures by extrapolated energy, then the unscored pre-screened ones by analytic energy
        alternates = [(c, f, cp, True) for _, i, c, f, cp in sorted(scored_alts, key=lambda x: (x[0], x[1]))
                      if i != best_idx]
        for _, c, f, par in distinct[len(shortlist):]:
            v = abs(float(np.linalg.det(_cell(par))))
            alternates.append((c, f, _scale_to_volume(par, contact_safe(f, par, v)), False))

        p_target = 0.0
        if request.target_bulk_modulus_gpa:
            kt = float(request.target_bulk_modulus_gpa)
            x = (kt - k0) / (K_PRIME * k0)
            cap = 0.2 if x > 0 else 0.1          # smooth, strictly monotone saturation
            p_target = float(cap * k0 * math.tanh(x / cap))
        lnv = math.log(v2) + float(np.clip((p2 - p_target) / k0, -0.3, 0.3))
        cellpar = _scale_to_volume(cellpar0, contact_safe(frac, cellpar0, math.exp(lnv)))

        w, tr, winv = _group_ops(plan.spacegroup)
        perm = _perm_tables(frac, numbers, w, tr)

        # bounded symmetric pre-relaxation (positions in the symmetric subspace + cell)
        budget = max(MIN_PRERELAX, PRESTREAM_CALLS - n_evals)
        frac, cellpar, n_relax = self._prerelax(plan, frac, cellpar, p_target, budget, should_cancel)
        n_evals += n_relax
        frac = _sym_positions(frac, perm, winv, tr)
        cellpar = _constrain_cellpar(plan.system, cellpar)

        ep = _Endpoint(frac=frac, cellpar=cellpar, labels=combo, perm=perm, k0_gpa=k0, p_target_gpa=p_target,
                       alternates=alternates)
        ep.info = {
            "n_combinations": len(combos), "pool_size": len(pool), "n_distinct": len(distinct),
            "n_scored": len(shortlist), "engine_calls_before_first_frame": n_evals,
            "k0_estimate_gpa": k0, "target_pressure_gpa": p_target, "volume_estimate": v_est,
            "search_seconds": time.perf_counter() - t0,
        }
        return ep

    def _prerelax(self, plan: _Plan, frac, cellpar, p_target_gpa, budget, should_cancel):
        from ase.constraints import FixSymmetry
        from ase.filters import FrechetCellFilter
        from ase.optimize import BFGS

        if budget <= 0:
            return frac, cellpar, 0
        atoms = Atoms(numbers=plan.numbers, scaled_positions=frac, cell=_cell(cellpar), pbc=True)
        atoms.calc = self.engine.get_calculator()
        calls = 0
        try:
            atoms.set_constraint(FixSymmetry(atoms, symprec=1e-3))
            filt = FrechetCellFilter(atoms, scalar_pressure=p_target_gpa / EV_PER_A3_TO_GPA)
            opt = BFGS(filt, logfile=None, maxstep=0.15)
            for _ in opt.irun(fmax=0.01, steps=budget - 1):
                calls += 1
                self._check(should_cancel)
            new_frac = _wrap(atoms.get_scaled_positions(wrap=False))
            new_par = _cellpar(atoms.cell.array)
            ok = np.all(np.isfinite(new_frac)) and np.all(np.isfinite(new_par)) and _valid_angles(*new_par[3:])
            if ok:
                x = _min_contact(new_frac, _cell(new_par), plan.numbers)
                if x >= X_TRUST:
                    return new_frac, new_par, calls
                if x >= X_TRUST / 1.1:          # small dip: open the cell isotropically by <= 10 %
                    new_par = new_par.copy()
                    new_par[:3] *= X_TRUST / x * (1.0 + 1e-9)
                    return new_frac, new_par, calls
            logger.warning("symmetric pre-relaxation ended in an overlap; using the unrelaxed endpoint")
        except FlowCancelled:
            raise
        except Exception as exc:  # optimiser failure is not fatal: keep the scored candidate
            logger.warning("symmetric pre-relaxation failed (%s); using unrelaxed endpoint", exc)
        return frac, cellpar, calls

    def _prior(self, plan: _Plan, ep: _Endpoint, request, rng: np.random.Generator):
        n = plan.n_atoms
        v_est = float(ep.info["volume_estimate"])
        kfac = 1.0
        if request.target_bulk_modulus_gpa:
            kfac = float(np.clip((100.0 / float(request.target_bulk_modulus_gpa)) ** 0.25, 0.8, 1.25))
        v0 = v_est * kfac * math.exp(rng.normal(0.0, 0.08))
        lengths = ep.cellpar[:3] * np.exp(rng.normal(0.0, 0.15, 3))
        while True:
            ang = 90.0 + rng.uniform(-15.0, 15.0, 3)
            if _valid_angles(*ang):
                break
        p0 = _scale_to_volume(np.concatenate([lengths, ang]), v0)
        ratio = np.clip(p0[:3] / ep.cellpar[:3], 0.7, 1.45)
        p0[:3] = ep.cellpar[:3] * ratio
        s0 = _hard_core_uniform(plan.numbers, _cell(p0), rng)

        # assignment of prior points to endpoint sites, jointly with an origin shift
        cell1 = _cell(ep.cellpar)
        shifts = rng.random((N_ORIGIN_SHIFTS, 3))
        shifts[0] = 0.0
        blocks = []
        start = 0
        for c in plan.counts:
            blocks.append(np.arange(start, start + c))
            start += c

        def cost_for(shift):
            total = 0.0
            assign = np.empty(n, dtype=int)
            for idx in blocks:
                d = _geodesic(s0[idx][:, None, :], (ep.frac[idx] + shift)[None, :, :])
                cart = d @ cell1
                cmat = np.einsum("ijk,ijk->ij", cart, cart)       # rows: prior point, cols: site
                r, c = linear_sum_assignment(cmat)
                total += float(cmat[r, c].sum())
                assign[idx[c]] = idx[r]                            # site -> prior point
            return total, assign

        best = None
        for sh in shifts:
            cst, assign = cost_for(sh)
            if best is None or cst < best[0]:
                best = (cst, sh.copy(), assign)
        _, shift, assign = best
        for _ in range(2):   # refine the shift by the mean residual (Procrustes on the torus)
            resid = _geodesic(ep.frac + shift, s0[assign])
            cand = shift + resid.mean(axis=0)
            cst, a2 = cost_for(cand)
            if cst < best[0]:
                best = (cst, cand, a2)
                _, shift, assign = best
        return s0[assign], p0, _wrap(shift)

    def _generate(self, plan: _Plan, request: StartFlowGenerationPayload,
                  should_cancel) -> Iterator[Tuple[FlowTrajectoryFrame, Atoms]]:
        t_start = time.perf_counter()
        seed = request.seed
        if seed is None:
            seed = int(np.random.SeedSequence().entropy % (2 ** 63))
        ss = np.random.SeedSequence(int(seed))
        rng_cand, rng_prior, rng_noise = (np.random.default_rng(s) for s in ss.spawn(3))
        self._check(should_cancel)

        ep = self._search_endpoint(plan, request, rng_cand, should_cancel)
        self._last_search = (plan, ep)
        s, p_cur, shift = self._prior(plan, ep, request, rng_prior)
        w, tr, winv = _group_ops(plan.spacegroup)
        perm = ep.perm
        numbers = plan.numbers
        n = plan.n_atoms
        steps = int(request.steps)
        h = 1.0 / (steps - 1)
        g = float(request.physics_guidance_scale)
        eta_base = min(ETA_MAX, ETA0 * g)
        beta_base = CELL_BETA * min(1.0, g / 1.5)
        s1 = ep.frac.copy()            # standard frame; actual endpoint = s1 + shift
        p1 = ep.cellpar.copy()
        sigma_target = -ep.p_target_gpa * np.eye(3)
        n_guarded = 0
        trace: List[Tuple[float, float, float]] = []   # (t, residual Å, endpoint weight)
        n_rejected = 0

        self.last_run_info = {
            "seed": int(seed), "spacegroup": plan.spacegroup, "symbol": plan.symbol,
            "crystal_system": plan.system, "composition": dict(zip(plan.elements, plan.counts)),
            "oxidation_states": dict(zip(plan.elements, plan.charges)), "wyckoff": ep.labels,
            "origin_shift": shift.tolist(), "backend": getattr(self.engine, "backend_name", "?"),
            **ep.info, "seconds_before_first_frame": None, "eta_max": eta_base, "tau0": TAU0,
        }

        for k in range(1, steps + 1):
            self._check(should_cancel)
            t = 1.0 if k == steps else (k - 1) * h
            if k == steps:
                s_frame = _wrap(s1 + shift)
                p_frame = _constrain_cellpar(plan.system, p1)
            else:
                s_frame = _wrap(s)
                p_frame = p_cur
            cell = _cell(p_frame)
            atoms = Atoms(numbers=numbers, scaled_positions=s_frame, cell=cell, pbc=True)
            payload = self.engine.evaluate(atoms, bond_mode="fast")
            if k == 1:
                self.last_run_info["seconds_before_first_frame"] = time.perf_counter() - t_start
            frame = FlowTrajectoryFrame(step=k, total_steps=steps, t=float(t), crystal=payload)
            yield frame, atoms
            if k == steps:
                break

            # ---------------- corrector (t > 0.65) ----------------
            s_cur = s_frame.copy()
            cell_cur = cell
            if t > T_CORRECTOR and g > 0.0:
                u = (t - T_CORRECTOR) / (1.0 - T_CORRECTOR)
                eta, tau, _ = _schedules(t, g)
                forces, n_clash = _trusted_forces(
                    np.array([a.force for a in payload.atoms], dtype=float), atoms.positions, cell, numbers)
                n_guarded += n_clash
                fmax = float(np.linalg.norm(forces, axis=1).max()) if n else 0.0
                dr = eta * forces / (fmax + FORCE_EPS)
                if tau > 0.0 and eta > 0.0:
                    xi = np.clip(rng_noise.standard_normal((n, 3)), -XI_CLIP, XI_CLIP)
                    dr = dr + math.sqrt(2.0 * eta * tau) * xi
                ds = dr @ np.linalg.inv(cell_cur)
                s_cur = s_cur + ds
                # endpoint refinement only trusts frames that are already close to it
                resid = float(np.linalg.norm(_geodesic(s_frame, s1 + shift) @ cell, axis=1).max())
                w_res = float(np.clip(1.0 - resid / R_TRUST, 0.0, 1.0))
                trace.append((round(t, 4), round(resid, 4), round(w_res, 4)))
                s1_new = s1
                if w_res > 0.0:
                    s1_new = _sym_positions(
                        _wrap(s1 + w_res * (u ** 2) * _sym_displacement(ds, perm, winv)), perm, winv, tr)
                p1_new = p1

                beta = beta_base * u ** 3
                if beta > 0.0:
                    sv = np.asarray(payload.stress_gpa, dtype=float)
                    sigma = np.array([[sv[0], sv[5], sv[4]], [sv[5], sv[1], sv[3]], [sv[4], sv[3], sv[2]]])
                    eps = -beta * (sigma - sigma_target) / max(ep.k0_gpa, K_STEP_MIN_GPA)
                    m = float(np.abs(eps).max())
                    if m > MAX_STRAIN_STEP:
                        eps *= MAX_STRAIN_STEP / m
                    deform = np.eye(3) + eps
                    cell_cur = cell_cur @ deform
                    if w_res > 0.0:
                        p1_new = _constrain_cellpar(plan.system, _cellpar(_cell(p1) @ (np.eye(3) + w_res * eps)))
                if _min_contact(s1_new, _cell(p1_new), numbers) >= X_TRUST:
                    s1, p1 = s1_new, p1_new
                else:
                    n_rejected += 1

            # ---------------- predictor: geodesic step, v = Delta / (1 - t) ----------------
            lam = min(1.0, _schedules(t, g)[2] * h / (1.0 - t))
            if k == steps - 1:
                lam = 1.0
            target = s1 + shift
            s = _wrap(s_cur + lam * _geodesic(s_cur, target))
            v_cur = _lat_vec(_cellpar(cell_cur))
            v_new = v_cur + lam * (_lat_vec(p1) - v_cur)
            p_cur = _lat_par(v_new)
            if not _valid_angles(*p_cur[3:]):   # cannot happen for convex combos; keep safe
                p_cur = _lat_par(_lat_vec(p_frame) + lam * (_lat_vec(p1) - _lat_vec(p_frame)))

        self.last_run_info["total_seconds"] = time.perf_counter() - t_start
        self.last_run_info["guarded_atom_steps"] = n_guarded
        self.last_run_info["rejected_endpoint_updates"] = n_rejected
        self.last_run_info["endpoint_refinement_trace"] = trace
