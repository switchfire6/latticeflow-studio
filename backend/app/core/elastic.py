"""Elastic tensor from finite strains (IMPROVEMENT_SUGGESTIONS.md §5.5).

Method (``compute_elastic_tensor``)
-----------------------------------
1. **Reference.** A copy of the structure is evaluated (``relax_ions=True``: its positions are
   first FIRE-relaxed at the fixed cell) giving the reference stress σ⁰.
2. **Deformations.** For each of the 6 Voigt components j (xx, yy, zz, yz, xz, xy) and each
   magnitude ε ∈ ±max_strain·k/m, k = 1..m, m = ceil(n_strains / 2) (n_strains = 4 → ±0.5 %, ±1 %),
   the cell is deformed with F = I + ε̂ where ε̂ is the symmetric strain tensor of the Voigt
   vector e_j·ε (engineering shear: ε̂_yz = ε̂_zy = ε/2), atoms scaled with the cell
   (new cell rows a_i F^T).
3. **Stress.** ``relax_ions=False`` (clamped ion): one batched
   ``engine.energy_forces_stress_batch`` call per Voigt component. ``relax_ions=True``
   (relaxed ion): positions FIRE-relaxed at the fixed strained cell to ``fmax`` (at most
   ``RELAX_MAX_STEPS`` steps) before the stress is taken.
4. **Fit.** Δσ = σ(ε) − σ⁰ (ASE sign: + = tensile, so stable solids give C > 0); for every j the
   column C_ij is the least-squares slope of Δσ_i against ε (with intercept, the reference point
   included); C is symmetrised (C + Cᵀ)/2. ``fit_residual_gpa`` is the rms residual of all fits.
   No finite-pressure correction is applied (``reference_pressure_gpa`` reports the pressure).
5. **Derived** (standard formulae, GPa): Voigt / Reuss / Hill K and G, E = 9KG/(3K+G),
   ν = (3K−2G)/(2(3K+G)), Pugh G/K, A^U = 5G_V/G_R + K_V/K_R − 6, eigenvalues of C (Born
   stability: all > 0), Debye temperature from the VRH sound velocities
   θ_D = (h/k_B)(3n/(4πV))^{1/3} v_m, v_m = [(2/v_t³ + 1/v_l³)/3]^{-1/3} (None if not stable).

``progress(done, total, message)`` is called after the reference (done = 1) and after every
deformation, total = 1 + 6 · 2m. ``should_cancel()`` is polled before every evaluation and every
FIRE step; :class:`ElasticCancelled` is raised when it returns True. The input is not mutated.
"""

from __future__ import annotations

import math
import warnings
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms

from app.schemas.messages import ElasticTensorResult

EV_A3_TO_GPA = 160.21766208
RELAX_MAX_STEPS = 300
_VOIGT = ((0, 0), (1, 1), (2, 2), (1, 2), (0, 2), (0, 1))
_VOIGT_NAMES = ("xx", "yy", "zz", "yz", "xz", "xy")
_H = 6.62607015e-34
_KB = 1.380649e-23
_AMU = 1.66053906660e-27

ShouldCancel = Optional[Callable[[], bool]]
Progress = Optional[Callable[[int, int, str], None]]


class ElasticCancelled(Exception):
    """Raised when ``should_cancel()`` returned True during an elastic-tensor calculation."""


def strain_values(max_strain: float, n_strains: int) -> List[float]:
    """Symmetric strain magnitudes: ±max_strain·k/m, k = 1..m, m = ceil(n_strains/2), ascending."""
    m = max(1, int(math.ceil(int(n_strains) / 2)))
    mags = [float(max_strain) * k / m for k in range(1, m + 1)]
    return sorted([-x for x in mags] + mags)


def voigt_strain_tensor(j: int, eps: float) -> np.ndarray:
    """Symmetric strain tensor of the Voigt vector eps * e_j (engineering shear)."""
    a, b = _VOIGT[j]
    e = np.zeros((3, 3))
    if a == b:
        e[a, a] = eps
    else:
        e[a, b] = e[b, a] = 0.5 * eps
    return e


def strained_copy(atoms: Atoms, strain: np.ndarray) -> Atoms:
    out = Atoms(numbers=atoms.get_atomic_numbers().copy(), positions=np.array(atoms.positions, dtype=float),
                cell=np.array(atoms.cell.array, dtype=float), pbc=True)
    F = np.eye(3) + strain
    out.set_cell(out.cell.array @ F.T, scale_atoms=True)
    return out


def _check_cancel(should_cancel: ShouldCancel) -> None:
    if should_cancel is not None and should_cancel():
        raise ElasticCancelled("elastic tensor calculation cancelled")


def _relaxed_stress(atoms: Atoms, engine, fmax: float, should_cancel: ShouldCancel) -> np.ndarray:
    """FIRE position relaxation at a fixed cell (in place); returns the final stress (eV/A^3, Voigt)."""
    from ase.optimize import FIRE

    from app.core.umlip_engine import EngineCalculator

    atoms.calc = EngineCalculator(engine)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        opt = FIRE(atoms, logfile=None)
        for _ in opt.irun(fmax=fmax, steps=RELAX_MAX_STEPS):
            _check_cancel(should_cancel)
    stress = np.array(atoms.get_stress(voigt=True), dtype=float)
    atoms.calc = None
    return stress


def _derived(C: np.ndarray, atoms: Atoms) -> dict:
    """VRH moduli, Pugh ratio, universal anisotropy, Debye temperature (GPa / K)."""
    c = C
    kv = (c[0, 0] + c[1, 1] + c[2, 2] + 2.0 * (c[0, 1] + c[0, 2] + c[1, 2])) / 9.0
    gv = (c[0, 0] + c[1, 1] + c[2, 2] - (c[0, 1] + c[0, 2] + c[1, 2]) + 3.0 * (c[3, 3] + c[4, 4] + c[5, 5])) / 15.0
    try:
        s = np.linalg.inv(c)
        kr = 1.0 / (s[0, 0] + s[1, 1] + s[2, 2] + 2.0 * (s[0, 1] + s[0, 2] + s[1, 2]))
        gr = 15.0 / (4.0 * (s[0, 0] + s[1, 1] + s[2, 2]) - 4.0 * (s[0, 1] + s[0, 2] + s[1, 2])
                     + 3.0 * (s[3, 3] + s[4, 4] + s[5, 5]))
    except np.linalg.LinAlgError:
        kr = gr = float("nan")
    k = 0.5 * (kv + kr)
    g = 0.5 * (gv + gr)
    e = 9.0 * k * g / (3.0 * k + g) if (3.0 * k + g) != 0 else float("nan")
    nu = (3.0 * k - 2.0 * g) / (2.0 * (3.0 * k + g)) if (3.0 * k + g) != 0 else float("nan")
    pugh = g / k if k != 0 else float("nan")
    au = 5.0 * gv / gr + kv / kr - 6.0 if (gr and kr) else float("nan")
    debye = None
    if np.isfinite(k) and np.isfinite(g) and k > 0 and g > 0:
        vol = atoms.get_volume() * 1e-30
        n = len(atoms)
        rho = float(np.sum(atoms.get_masses())) * _AMU / vol
        vl = math.sqrt((k + 4.0 * g / 3.0) * 1e9 / rho)
        vt = math.sqrt(g * 1e9 / rho)
        vm = ((2.0 / vt ** 3 + 1.0 / vl ** 3) / 3.0) ** (-1.0 / 3.0)
        debye = float(_H / _KB * (3.0 * n / (4.0 * math.pi * vol)) ** (1.0 / 3.0) * vm)
    return dict(kv=kv, kr=kr, k=k, gv=gv, gr=gr, g=g, e=e, nu=nu, pugh=pugh, au=au, debye=debye)


def _finite(x: float) -> float:
    x = float(x)
    return x if math.isfinite(x) else 0.0


def compute_elastic_tensor(
    atoms: Atoms,
    engine=None,
    *,
    max_strain: float = 0.01,
    n_strains: int = 4,
    relax_ions: bool = True,
    fmax: float = 0.01,
    should_cancel: ShouldCancel = None,
    progress: Progress = None,
) -> ElasticTensorResult:
    """6x6 elastic tensor (GPa) of ``atoms`` with derived moduli; see the module docstring."""
    if engine is None:
        from app.core.umlip_engine import UMLIPEngine

        engine = UMLIPEngine.get_instance()
    if len(atoms) == 0:
        raise ValueError("cannot compute an elastic tensor of an empty structure")
    if not all(bool(x) for x in np.asarray(atoms.pbc).reshape(-1)) or atoms.cell.rank != 3:
        raise ValueError("the elastic tensor needs a fully periodic 3D cell")
    if not (math.isfinite(max_strain) and 0 < max_strain <= 0.1):
        raise ValueError("max_strain must be in (0, 0.1]")
    if int(n_strains) < 2:
        raise ValueError("n_strains must be >= 2")
    if not fmax > 0:
        raise ValueError("fmax must be positive")

    strains = strain_values(max_strain, n_strains)
    total = 1 + 6 * len(strains)
    done = 0

    def report(msg: str) -> None:
        if progress is not None:
            progress(done, total, msg)

    ref = Atoms(numbers=atoms.get_atomic_numbers().copy(), positions=np.array(atoms.positions, dtype=float),
                cell=np.array(atoms.cell.array, dtype=float), pbc=True)
    _check_cancel(should_cancel)
    if relax_ions:
        sigma0 = _relaxed_stress(ref, engine, fmax, should_cancel)
    else:
        _, _, sigma0 = engine.energy_forces_stress(ref)
        sigma0 = np.asarray(sigma0, dtype=float)
    done += 1
    report("reference structure")

    C = np.zeros((6, 6))
    residuals: List[np.ndarray] = []
    for j in range(6):
        deformed = [strained_copy(ref, voigt_strain_tensor(j, e)) for e in strains]
        stresses: List[np.ndarray] = []
        if relax_ions:
            for e, d in zip(strains, deformed):
                _check_cancel(should_cancel)
                stresses.append(_relaxed_stress(d, engine, fmax, should_cancel))
                done += 1
                report(f"strain {_VOIGT_NAMES[j]} {e * 100:+.2f}% (relaxed ions)")
        else:
            _check_cancel(should_cancel)
            for _, _, s in engine.energy_forces_stress_batch(deformed):
                stresses.append(np.asarray(s, dtype=float))
            for e in strains:
                done += 1
                report(f"strain {_VOIGT_NAMES[j]} {e * 100:+.2f}% (clamped ions)")
        x = np.array([0.0] + list(strains))
        y = np.vstack([np.zeros(6)] + [s - sigma0 for s in stresses]) * EV_A3_TO_GPA   # (n+1, 6)
        A = np.vstack([x, np.ones_like(x)]).T
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)          # (2, 6): slope, intercept per stress i
        C[:, j] = coef[0]
        residuals.append((y - A @ coef).reshape(-1))
    _check_cancel(should_cancel)

    C = 0.5 * (C + C.T)
    eig = np.linalg.eigvalsh(C)
    d = _derived(C, ref)
    born = bool(np.all(eig > 0))
    p0 = float(-np.mean(sigma0[:3]) * EV_A3_TO_GPA)
    resid = float(np.sqrt(np.mean(np.concatenate(residuals) ** 2)))
    pct = ", ".join(f"{e * 100:+.2g}%" for e in strains)
    method = (
        f"finite strain-stress: 6 Voigt strains x {len(strains)} magnitudes ({pct}), "
        + ("relaxed ions (FIRE at fixed cell, fmax " + f"{fmax:g} eV/A)" if relax_ions else "clamped ions")
        + f", stress from {getattr(engine, 'backend_name', 'uMLIP')}; linear least-squares fit per strain "
        "component, C symmetrised; Voigt-Reuss-Hill averages; no finite-pressure correction"
    )
    return ElasticTensorResult(
        c_ij_gpa=[[float(np.round(v, 4)) + 0.0 for v in row] for row in C],
        eigenvalues_gpa=[float(np.round(v, 4)) for v in eig],
        born_stable=born,
        k_voigt_gpa=_finite(d["kv"]),
        k_reuss_gpa=_finite(d["kr"]),
        k_vrh_gpa=_finite(d["k"]),
        g_voigt_gpa=_finite(d["gv"]),
        g_reuss_gpa=_finite(d["gr"]),
        g_vrh_gpa=_finite(d["g"]),
        youngs_modulus_vrh_gpa=_finite(d["e"]),
        poisson_ratio_vrh=_finite(d["nu"]),
        pugh_ratio=_finite(d["pugh"]),
        universal_anisotropy=_finite(d["au"]),
        debye_temperature_k=d["debye"] if born else None,
        relaxed_ions=bool(relax_ions),
        max_strain=float(max_strain),
        reference_pressure_gpa=p0,
        fit_residual_gpa=resid,
        method=method,
    )
