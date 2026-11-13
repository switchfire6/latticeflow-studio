"""Streaming relaxation and bulk-modulus estimation (PROJECT_SPEC.md §4.3, §5.2, §7 Phase 2).

Relaxation
----------
:func:`iter_relaxation` is the synchronous, cancellable core; :func:`relax_stream`
is a thin async wrapper that runs every optimizer step off the event loop.

* ASE ``FIRE`` (default parameters, ``maxstep = 0.2 Å``; or ``BFGS`` / ``LBFGS``
  with their ASE defaults when ``optimizer`` says so) on
  ``FrechetCellFilter(atoms, scalar_pressure=pressure_gpa / 160.21766208)``
  when ``relax_cell`` (positions + cell shape and volume; ``pressure_gpa`` is an
  external hydrostatic pressure, + = compressive, so the run minimises the
  enthalpy E + PV), on ``atoms`` itself otherwise (``pressure_gpa`` is then
  ignored). Forces come from ``engine.get_calculator()``. Non-periodic
  structures are always relaxed with positions only.
* ``fix_symmetry``: an ASE ``FixSymmetry(atoms, symprec=1e-2)`` constraint is
  added for the run. ``FixSymmetry`` first *refines* the structure to its
  detected space group (removing noise below ``symprec``), so frame 0 is the
  symmetrised start; position steps, cell steps, forces and stress are then
  symmetrised, so the space group cannot drop below the detected one.
* ``atoms`` is relaxed **in place**. After every evaluation the atoms are wrapped
  back into the cell (``[0, 1)`` fractional, same rule as
  ``UMLIPEngine.evaluate``), so whenever a frame has been yielded and the
  generator is suspended, ``atoms`` *is* the frame's structure
  (``atoms.positions == frame.crystal.atoms[*].cart_coords``, same cell).
  If the generator finishes, is closed early, is cancelled or raises, ``atoms``
  is left at the structure of the last yielded frame (a step that was
  computed but never yielded is rolled back; with no frame yielded at all it
  is the input structure) with **no calculator attached** and exactly the
  caller's constraints (pre-existing ones are honoured and kept; the
  ``FixSymmetry`` of ``fix_symmetry`` is removed again).
* Frames: ``step = 0`` is the starting structure before any move, then one
  frame per FIRE iteration (``step = 1, 2, ...``). Exactly one frame has
  ``final=True``: the last one, whether it converged or hit ``max_steps``. A
  converged start gives the single frame ``(step=0, converged=True, final=True)``.
  A cancelled run simply stops: no extra frame, the last frame is not changed
  (the caller knows it cancelled).
* ``converged`` (precisely): ``max_i |F_i| < fmax`` on the Cartesian forces, AND,
  when relaxing the cell, ASE's own ``FrechetCellFilter`` criterion
  ``max_row |G| < fmax`` over all N+3 rows of the filter's generalised gradient
  (atom rows ``F_i · D`` with D the deformation gradient; cell rows
  ``dE/d(log D) / N``, i.e. per-atom virial ``|V σ| / N`` in eV). For a cubic
  cell this means ``|σ| < fmax · N / V`` (e.g. 0.27 GPa for 5-atom SrTiO3,
  0.16 GPa for 8-atom Si at fmax = 0.02). With ``stress_tol_gpa`` (and
  ``relax_cell``) it additionally requires
  ``max_ij |σ_ij + P δ_ij| < stress_tol_gpa``, σ the ASE stress in GPa
  (+ = tensile) and P = ``pressure_gpa``: the residual stress relative to the
  target pressure.
* The force field is evaluated once per geometry: the frame payload is built
  with ``engine.payload_from_results`` from the energy / forces / stress the
  optimizer already requested (fast bond finder). FIRE, BFGS and LBFGS each
  call the optimizable's ``get_gradient`` once per step, and it is memoised
  per geometry. Wrapping atoms into the cell is a lattice translation; BFGS /
  LBFGS remember the previous point, so that memory is shifted by the same
  translation (their step differences stay physical).
* Divergence guard: if an evaluation returns non-finite energy / forces /
  stress, the cell volume leaves ``[V_start / MAX_VOLUME_RATIO, V_start * MAX_VOLUME_RATIO]``
  (8x either way) or drops below ``MIN_VOLUME_PER_ATOM`` (0.5 Å^3/atom),
  or ``max |F|`` exceeds ``max(MAX_FORCE_EV_A, 10 * max |F| at step 0)``, the
  atoms are restored to the last good (last yielded) structure, a final frame
  ``(step=<attempted step>, converged=False, final=True)`` carrying that last
  good structure is yielded, and on the next ``next()`` the generator raises
  :class:`RelaxationDivergedError` (``.reason``, ``.step``, ``.frame``). If
  the *starting* structure already evaluates to non-finite values the error is
  raised before any frame. Errors from the engine itself propagate unchanged
  (after cleanup and rollback).

Bulk modulus
------------
:func:`estimate_bulk_modulus` / :func:`fit_bulk_modulus`: volumetric strains
``ε = ΔV/V0 ∈ {-0.02, -0.01, 0, +0.01, +0.02}`` by default (cell scaled by
``(1+ε)^(1/3)`` at fixed fractional coordinates), all evaluated in one batched
engine call. :func:`bulk_modulus_result` builds the full ``BulkModulusResult``
event payload for ``n_points`` strains evenly spaced over ``±strain_window``
(including B0' and the raw E(V), P(V) data, also when the fit is rejected).
The energies are fitted *linearly* with the 3rd-order Birch–Murnaghan form
``E = c0 + c1 u + c2 u^2 + c3 u^3`` with ``u = (V/V0)^(-2/3) - 1`` (exactly the
BM-3 functional family, but with no equilibrium-volume parameter to guess), so
the fit is well defined even when the presets are off the model's equilibrium
and the energy minimum lies outside the ±2 % window.

Definition: ``K = V0 · d²E/dV² |_{V0}`` evaluated at the **input volume V0**
(this is the spec's formula with V0 the unstrained volume; at a non-zero
pressure it equals the isothermal ``-V dP/dV`` at that volume, *not* the BM
``B0`` at the model's equilibrium volume). It is cross-checked against
``K_P = -V0 · dP/dV`` from the stresses (``P = -tr σ / 3``) at the same five
strains; ``None`` is returned if the structure is not 3-D periodic, any value
is non-finite, either estimate is ≤ 0 (non-convex), or the two disagree by
more than ``K_CONSISTENCY_RTOL`` (the energy surface is then not smooth enough
over the window for a meaningful fit). Note this is the clamped-ion,
fixed-shape bulk modulus (no internal relaxation, no shape relaxation), which
is exact for cubic cells whose sites are fixed by symmetry (Si, SrTiO3) and an
upper bound otherwise. :func:`fit_bulk_modulus` also reports the fit's
equilibrium volume and the BM ``B0`` and ``B0'`` there: with
``g(u) = dE/du = c1 + 2 c2 u + 3 c3 u^2``, ``u_eq`` its root and ``t = 1 + u_eq``,
``B0 = (4/9) t^(7/2) g'(u_eq) / V0`` and ``B0' = 4 + (2/3) t g''(u_eq) / g'(u_eq)``
(exactly 4 for a second-order fit, c3 = 0).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator, Callable, Iterator, List, Optional, Tuple

import numpy as np
from ase import Atoms
from ase.filters import FrechetCellFilter
from ase.optimize import BFGS, FIRE, LBFGS

from app.schemas.messages import BulkModulusResult, RelaxationFrame

if TYPE_CHECKING:  # pragma: no cover
    from app.core.umlip_engine import UMLIPEngine

logger = logging.getLogger(__name__)

EV_PER_A3_TO_GPA = 160.21766208

#: Divergence guard: allowed range of V / V_start during a cell relaxation. Wide on
#: purpose: legitimate relaxations of transmuted cells can change the volume by
#: 2x (the fallback LJ model compresses Li3PS4 to 47 %); this only catches
#: run-away cells.
MAX_VOLUME_RATIO = 8.0
#: Divergence guard: collapse floor in Å^3 per atom.
MIN_VOLUME_PER_ATOM = 0.5
#: Divergence guard: absolute force ceiling (eV/Å); the effective ceiling is
#: max(MAX_FORCE_EV_A, 10 * max|F| of the starting structure).
MAX_FORCE_EV_A = 1.0e3

#: Optimizers selectable with ``iter_relaxation(optimizer=...)``.
OPTIMIZERS = {"FIRE": FIRE, "BFGS": BFGS, "LBFGS": LBFGS}
#: ``FixSymmetry`` tolerance used by ``fix_symmetry=True``.
FIX_SYMMETRY_SYMPREC = 1e-2

BULK_STRAINS = (-0.02, -0.01, 0.0, 0.01, 0.02)
#: Max relative disagreement between the energy-fit and stress-derived K.
K_CONSISTENCY_RTOL = 0.10


class RelaxationError(RuntimeError):
    """Base class for relaxation failures raised by this module."""


class RelaxationDivergedError(RelaxationError):
    """The relaxation produced non-finite values or an exploding cell / forces.

    ``frame`` is the final frame that was yielded just before (``None`` if the
    starting structure itself was bad); ``atoms`` was restored to its structure.
    """

    def __init__(self, reason: str, step: int, frame: Optional[RelaxationFrame] = None) -> None:
        super().__init__(f"relaxation diverged at step {step}: {reason}")
        self.reason = reason
        self.step = step
        self.frame = frame


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _resolve_engine(engine: Optional["UMLIPEngine"]) -> "UMLIPEngine":
    if engine is not None:
        return engine
    from app.core.umlip_engine import UMLIPEngine

    return UMLIPEngine.get_instance()


def _is_periodic(atoms: Atoms) -> bool:
    return bool(np.all(atoms.pbc)) and atoms.cell.rank == 3


def _wrap_atoms_in_place(atoms: Atoms) -> None:
    """Same wrapping rule as ``UMLIPEngine.evaluate`` (periodic axes only)."""
    if atoms.cell.rank != 3:
        return
    pbc = np.asarray(atoms.pbc, dtype=bool)
    if not pbc.any():
        return
    from app.core.umlip_engine import wrap_in_place

    # The engine's own rule (it snaps the last 1e-12 below 1 to 0). A second, slightly
    # different rule here once left atoms on a cell face one lattice vector away from
    # the payload. The positions setter bypasses constraints (lattice translation).
    wrap_in_place(atoms)


def _snapshot(atoms: Atoms) -> Tuple[np.ndarray, np.ndarray]:
    return atoms.positions.copy(), atoms.cell.array.copy()


def _restore(atoms: Atoms, snap: Tuple[np.ndarray, np.ndarray]) -> None:
    # Bypass constraints: FixSymmetry.adjust_cell would symmetrise (or reject) the jump.
    atoms.set_cell(snap[1], scale_atoms=False, apply_constraint=False)
    atoms.positions = snap[0]


def _shift_optimizer_memory(opt, dx: np.ndarray) -> None:
    """Shift the remembered previous point of a quasi-Newton optimizer by a lattice translation ``dx``."""
    for name in ("pos0", "r0"):  # BFGS / LBFGS
        prev = getattr(opt, name, None)
        if isinstance(prev, np.ndarray) and prev.shape == dx.shape:
            setattr(opt, name, prev + dx)


# --------------------------------------------------------------------------
# synchronous core
# --------------------------------------------------------------------------
def iter_relaxation(
    atoms: Atoms,
    engine: Optional["UMLIPEngine"] = None,
    *,
    fmax: float = 0.02,
    max_steps: int = 100,
    relax_cell: bool = True,
    should_cancel: Optional[Callable[[], bool]] = None,
    bond_mode: str = "fast",
    fix_symmetry: bool = False,
    pressure_gpa: float = 0.0,
    stress_tol_gpa: Optional[float] = None,
    optimizer: str = "FIRE",
) -> Iterator[RelaxationFrame]:
    """Relax ``atoms`` in place, yielding one ``RelaxationFrame`` per optimizer step.

    See the module docstring for the exact frame, convergence, cleanup and
    divergence semantics. ``should_cancel()`` is polled before step 0 and before
    every optimizer step; when it returns True the generator stops without another frame.
    """
    if not fmax > 0:
        raise ValueError("fmax must be > 0")
    max_steps = int(max_steps)
    if max_steps < 0:
        raise ValueError("max_steps must be >= 0")
    if len(atoms) == 0:
        raise ValueError("cannot relax an empty structure")
    opt_cls = OPTIMIZERS.get(str(optimizer).upper())
    if opt_cls is None:
        raise ValueError(f"unknown optimizer {optimizer!r}; choose one of {sorted(OPTIMIZERS)}")
    pressure_gpa = float(pressure_gpa)
    if not np.isfinite(pressure_gpa):
        raise ValueError("pressure_gpa must be finite")
    if stress_tol_gpa is not None and not stress_tol_gpa > 0:
        raise ValueError("stress_tol_gpa must be > 0")
    return _relaxation_generator(
        atoms, engine, fmax, max_steps, relax_cell, should_cancel, bond_mode,
        bool(fix_symmetry), pressure_gpa, stress_tol_gpa, opt_cls,
    )


def _relaxation_generator(atoms, engine, fmax, max_steps, relax_cell, should_cancel, bond_mode,
                          fix_symmetry=False, pressure_gpa=0.0, stress_tol_gpa=None, opt_cls=FIRE):
    def cancelled() -> bool:
        return should_cancel is not None and bool(should_cancel())

    if cancelled():
        return
    engine = _resolve_engine(engine)
    periodic = _is_periodic(atoms)
    if relax_cell and not periodic:
        logger.info("structure is not 3-D periodic; relaxing positions only")
        relax_cell = False

    good: Optional[Tuple[np.ndarray, np.ndarray]] = None  # structure of the last yielded frame
    original = _snapshot(atoms)  # FixSymmetry refines atoms before the first frame
    original_constraints = list(atoms.constraints)
    calc = engine.get_calculator()
    atoms.calc = calc
    try:
        if fix_symmetry:
            from ase.constraints import FixSymmetry

            atoms.set_constraint(original_constraints + [FixSymmetry(atoms, symprec=FIX_SYMMETRY_SYMPREC)])
        if relax_cell:
            target = FrechetCellFilter(atoms, scalar_pressure=pressure_gpa / EV_PER_A3_TO_GPA)
        else:
            target = atoms
        check_stress = relax_cell and stress_tol_gpa is not None
        opt = opt_cls(target, logfile=None)
        opt.fmax = fmax
        optimizable = opt.optimizable
        v_start = abs(atoms.cell.volume) if periodic else None
        force_ceiling = MAX_FORCE_EV_A

        # Memoise the optimizable's generalised gradient per geometry. For the
        # FrechetCellFilter each call costs ~2 ms (nine 3x3 expm_frechet), and
        # FIRE.step() would otherwise recompute what the convergence test just did.
        raw_get_gradient = optimizable.get_gradient
        grad_cache: dict = {}

        def geometry_key():
            return atoms.positions.tobytes() + atoms.cell.array.tobytes()

        def cached_get_gradient():
            key = geometry_key()
            if grad_cache.get("key") != key:
                grad_cache["grad"] = raw_get_gradient()
                grad_cache["key"] = key
            return grad_cache["grad"].copy()

        optimizable.get_gradient = cached_get_gradient

        def evaluate_current(step: int):
            """Evaluate the current geometry (once), wrap it, return (payload, converged) or a reason."""
            energy = float(atoms.get_potential_energy())  # the single model evaluation for this geometry
            forces = np.array(atoms.get_forces(), dtype=float)
            stress = np.array(atoms.get_stress(voigt=True), dtype=float) if periodic else np.zeros(6)
            fnorm = float(np.linalg.norm(forces, axis=1).max())
            reason = None
            if not (np.isfinite(energy) and np.all(np.isfinite(forces)) and np.all(np.isfinite(stress))):
                reason = "non-finite energy, forces or stress"
            elif not (np.all(np.isfinite(atoms.positions)) and np.all(np.isfinite(atoms.cell.array))):
                reason = "non-finite positions or cell"
            elif v_start is not None:
                vol = abs(atoms.cell.volume)
                ratio = vol / v_start if v_start > 0 else np.inf
                if not (1.0 / MAX_VOLUME_RATIO <= ratio <= MAX_VOLUME_RATIO):
                    reason = f"cell volume changed by a factor {ratio:.3g}"
                elif step > 0 and vol / len(atoms) < MIN_VOLUME_PER_ATOM:
                    reason = f"cell collapsed to {vol / len(atoms):.3g} A^3/atom"
            if reason is None and step > 0 and fnorm > force_ceiling:
                reason = f"max force {fnorm:.3g} eV/A exceeds {force_ceiling:.3g} eV/A"
            if reason is not None:
                return None, False, fnorm, reason
            gradient = cached_get_gradient()  # cached E/F/sigma -> no model call
            converged = fnorm < fmax and bool(optimizable.converged(gradient, fmax))
            if converged and check_stress:
                residual = stress * EV_PER_A3_TO_GPA
                residual[:3] += pressure_gpa
                converged = float(np.abs(residual).max()) < stress_tol_gpa
            # Wrap into the cell and tell the calculator (and the gradient cache)
            # that nothing changed: a lattice translation leaves E, F, sigma and
            # the filter gradient invariant, so the next step reuses them.
            x_before = optimizable.get_x() if opt_cls is not FIRE else None
            _wrap_atoms_in_place(atoms)
            calc.atoms = atoms.copy()
            grad_cache["key"] = geometry_key()
            if x_before is not None:
                dx = optimizable.get_x() - x_before
                if np.any(dx):
                    _shift_optimizer_memory(opt, dx)
            payload = engine.payload_from_results(atoms, energy, forces, stress, bond_mode=bond_mode)
            return payload, converged, fnorm, None

        payload, converged, f0, reason = evaluate_current(0)
        if reason is not None:
            raise RelaxationDivergedError(reason, 0)
        force_ceiling = max(MAX_FORCE_EV_A, 10.0 * f0)
        final = converged or max_steps == 0
        good = _snapshot(atoms)
        yield RelaxationFrame(step=0, converged=converged, final=final, crystal=payload)
        if final:
            return

        last_payload = payload
        for step in range(1, max_steps + 1):
            if cancelled():
                return
            opt.step()
            opt.nsteps += 1
            payload, converged, _, reason = evaluate_current(step)
            if reason is not None:
                _restore(atoms, good)
                calc.reset()
                frame = RelaxationFrame(step=step, converged=False, final=True, crystal=last_payload)
                logger.warning("relaxation diverged at step %d: %s", step, reason)
                yield frame
                raise RelaxationDivergedError(reason, step, frame)
            final = converged or step == max_steps
            good = _snapshot(atoms)
            last_payload = payload
            yield RelaxationFrame(step=step, converged=converged, final=final, crystal=payload)
            if final:
                return
    finally:
        atoms.calc = None
        if fix_symmetry:
            atoms.set_constraint(original_constraints)
        # Roll back a computed-but-unyielded step (no-op otherwise); with no frame
        # yielded at all, back to the input (undoes FixSymmetry's refinement).
        _restore(atoms, good if good is not None else original)


# --------------------------------------------------------------------------
# async wrapper
# --------------------------------------------------------------------------
_DONE = object()


async def relax_stream(
    atoms: Atoms,
    engine: Optional["UMLIPEngine"] = None,
    *,
    fmax: float = 0.02,
    max_steps: int = 100,
    relax_cell: bool = True,
    should_cancel: Optional[Callable[[], bool]] = None,
    **options,
) -> AsyncIterator[RelaxationFrame]:
    """Async wrapper around :func:`iter_relaxation` (same frames, same in-place semantics).

    Every optimizer step (including the engine lookup / model load and payload
    construction) runs in a dedicated single worker thread, so the event loop
    stays responsive. When the consumer stops iterating (``break`` /
    ``aclose()``) or the task is cancelled, no further steps start; an in-flight
    step is allowed to finish, then the sync generator is closed in the worker
    and ``atoms`` is rolled back to the last frame actually delivered to the
    consumer. Cleanup is awaited (shielded), so when ``aclose()`` / the
    cancellation returns, ``atoms`` is consistent and has no calculator.
    ``options`` (``fix_symmetry``, ``pressure_gpa``, ``stress_tol_gpa``,
    ``optimizer``, ``bond_mode``) are passed through to :func:`iter_relaxation`.
    """
    stop = threading.Event()

    def cancelled() -> bool:
        return stop.is_set() or (should_cancel is not None and bool(should_cancel()))

    gen = iter_relaxation(atoms, engine, fmax=fmax, max_steps=max_steps, relax_cell=relax_cell,
                          should_cancel=cancelled, **options)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relax-stream")
    loop = asyncio.get_running_loop()
    delivered: List[Optional[Tuple[np.ndarray, np.ndarray]]] = [None]

    def advance():
        frame = next(gen, _DONE)
        return frame, (_snapshot(atoms) if frame is not _DONE else None)

    def finish() -> None:
        gen.close()  # runs the core's cleanup (calculator detached)
        if delivered[0] is not None:
            _restore(atoms, delivered[0])
        atoms.calc = None

    try:
        while True:
            frame, snap = await loop.run_in_executor(executor, advance)
            if frame is _DONE:
                break
            delivered[0] = snap
            yield frame
    finally:
        stop.set()
        cleanup = executor.submit(finish)  # queued behind any in-flight step
        executor.shutdown(wait=False)
        try:
            await asyncio.shield(asyncio.wrap_future(cleanup))
        except asyncio.CancelledError:
            # Cancelled again while waiting: the worker still finishes the cleanup.
            raise
        except Exception:  # noqa: BLE001 - cleanup must not mask the original error
            logger.exception("relaxation cleanup failed")


# --------------------------------------------------------------------------
# bulk modulus
# --------------------------------------------------------------------------
@dataclass
class BulkModulusFit:
    """Result of :func:`fit_bulk_modulus` (volumes Å^3, energies eV, pressures / moduli GPa)."""

    k_gpa: float                 # V0 d2E/dV2 at the input volume (Birch-Murnaghan energy fit)
    k_stress_gpa: float          # -V0 dP/dV at the input volume, from the stresses
    v0: float                    # input (unstrained) volume
    pressure_gpa: float          # P = -tr(sigma)/3 at V0 (positive = compressed)
    v_eq: Optional[float]        # stationary point of the BM fit (extrapolated if outside the window)
    k_eq_gpa: Optional[float]    # BM B0 = V d2E/dV2 at v_eq
    rms_residual_ev: float       # RMS residual of the energy fit
    k_eq_prime: Optional[float] = None  # BM B0' = dB/dP at v_eq (dimensionless)
    volumes: List[float] = field(default_factory=list)
    energies: List[float] = field(default_factory=list)
    pressures_gpa: List[float] = field(default_factory=list)


class _BulkUnavailable(ValueError):
    """The structure / energies do not allow an equation-of-state fit at all."""


@dataclass
class _BulkData:
    strains: np.ndarray
    volumes: np.ndarray
    energies: np.ndarray
    pressures_gpa: np.ndarray
    v0: float
    pressure_gpa: float
    k_e: float
    k_p: float
    rms: float
    v_eq: Optional[float]
    k_eq: Optional[float]
    k_prime: Optional[float]
    rejected: Optional[str]      # why the fit is not meaningful (None = accepted)


def _strained_copy(atoms: Atoms, eps: float) -> Atoms:
    frac = atoms.cell.scaled_positions(atoms.positions)
    cell = atoms.cell.array * (1.0 + eps) ** (1.0 / 3.0)
    return Atoms(numbers=atoms.get_atomic_numbers().copy(), cell=cell, scaled_positions=frac, pbc=True)


def _bulk_data(atoms: Atoms, engine: Optional["UMLIPEngine"], strains) -> _BulkData:
    """Evaluate the strained cells and fit them (raises :class:`_BulkUnavailable`)."""
    if len(atoms) == 0 or not _is_periodic(atoms):
        raise _BulkUnavailable("the bulk modulus needs a non-empty, 3-D periodic structure")
    v0 = abs(float(atoms.cell.volume))
    if not np.isfinite(v0) or v0 <= 0:
        raise _BulkUnavailable("the cell volume is not finite and positive")
    eps = np.array(BULK_STRAINS if strains is None else strains, dtype=float).ravel()
    if eps.size < 4 or np.unique(eps).size != eps.size or not np.all(np.isfinite(eps)) or np.any(eps <= -0.5):
        raise ValueError("strains must be at least 4 distinct finite values > -0.5")
    engine = _resolve_engine(engine)
    structures = [_strained_copy(atoms, float(e)) for e in eps]
    results = engine.energy_forces_stress_batch(structures, compute_stress=True)
    vols = np.array([abs(s.cell.volume) for s in structures])
    energies = np.array([r[0] for r in results], dtype=float)
    pressures = np.array([-np.mean(r[2][:3]) for r in results], dtype=float)  # eV/Å^3
    if not (np.all(np.isfinite(energies)) and np.all(np.isfinite(pressures))):
        raise _BulkUnavailable("non-finite energies or stresses on the strained cells")

    # BM-3 as a linear model in u = (V/V0)^(-2/3) - 1 (energies relative to the
    # point closest to the unstrained one, for conditioning).
    i_ref = int(np.argmin(np.abs(eps)))
    u = (vols / v0) ** (-2.0 / 3.0) - 1.0
    e_rel = energies - energies[i_ref]
    design = np.vander(u, 4, increasing=True)
    coef, *_ = np.linalg.lstsq(design, e_rel, rcond=None)
    c0, c1, c2, c3 = coef
    rms = float(np.sqrt(np.mean((design @ coef - e_rel) ** 2)))
    k_e = float((8.0 * c2 + 10.0 * c1) / (9.0 * v0) * EV_PER_A3_TO_GPA)

    # -V dP/dV from the stresses (polynomial in eps; dV = V0 d eps).
    pcoef = np.polyfit(eps, pressures, 2 if eps.size <= 5 else 3)
    k_p = float(-pcoef[-2] * EV_PER_A3_TO_GPA)  # -V0 dP/dV = -dP/d(eps) at eps = 0
    zero = np.flatnonzero(eps == 0.0)
    p0_ev = pressures[zero[0]] if zero.size else np.polyval(pcoef, 0.0)
    p0 = float(p0_ev * EV_PER_A3_TO_GPA)

    rejected = None
    if not (np.isfinite(k_e) and np.isfinite(k_p)) or k_e <= 0 or k_p <= 0:
        rejected = f"non-convex or non-finite: K(E) = {k_e:.4g} GPa, K(P) = {k_p:.4g} GPa"
    elif abs(k_e - k_p) > K_CONSISTENCY_RTOL * max(k_e, k_p):
        rejected = f"inconsistent: K(E) = {k_e:.1f} GPa vs K(P) = {k_p:.1f} GPa"
        logger.info("bulk modulus fit %s", rejected)

    # Stationary point of the fit: g(u) = c1 + 2 c2 u + 3 c3 u^2 = 0 (root closest to V0).
    v_eq = k_eq = k_prime = None
    if rejected is None:
        roots = np.roots([3.0 * c3, 2.0 * c2, c1]) if abs(c3) > 0 else np.array([-c1 / (2.0 * c2)])
        roots = [r.real for r in np.atleast_1d(roots) if abs(np.imag(r)) < 1e-12 and r.real > -1.0]
        if roots:
            ue = min(roots, key=abs)
            t = 1.0 + ue
            e_uu = 2.0 * c2 + 6.0 * c3 * ue          # g'(u_eq)
            if e_uu > 0 and abs(ue) < 0.2:
                v_eq = float(v0 * t ** (-1.5))
                k_eq = float(e_uu * (4.0 / 9.0) * t * t / v_eq * EV_PER_A3_TO_GPA)
                k_prime_value = 4.0 + (2.0 / 3.0) * t * (6.0 * c3) / e_uu
                k_prime = float(k_prime_value) if np.isfinite(k_prime_value) else None

    return _BulkData(
        strains=eps, volumes=vols, energies=energies, pressures_gpa=pressures * EV_PER_A3_TO_GPA,
        v0=v0, pressure_gpa=p0, k_e=k_e, k_p=k_p, rms=rms, v_eq=v_eq, k_eq=k_eq, k_prime=k_prime,
        rejected=rejected,
    )


def fit_bulk_modulus(
    atoms: Atoms, engine: Optional["UMLIPEngine"] = None, *, strains: Optional[List[float]] = None,
) -> Optional[BulkModulusFit]:
    """Birch-Murnaghan fit over volumetric ``strains`` (default ±2 %, 5 points); ``None`` if not meaningful.

    ``atoms`` is not mutated.
    """
    try:
        data = _bulk_data(atoms, engine, strains)
    except _BulkUnavailable:
        return None
    if data.rejected is not None:
        return None
    return BulkModulusFit(
        k_gpa=data.k_e,
        k_stress_gpa=data.k_p,
        v0=data.v0,
        pressure_gpa=data.pressure_gpa,
        v_eq=data.v_eq,
        k_eq_gpa=data.k_eq,
        rms_residual_ev=data.rms,
        k_eq_prime=data.k_prime,
        volumes=[float(v) for v in data.volumes],
        energies=[float(e) for e in data.energies],
        pressures_gpa=[float(p) for p in data.pressures_gpa],
    )


def bulk_modulus_strains(strain_window: float = 0.02, n_points: int = 5) -> List[float]:
    """``n_points`` volumetric strains evenly spaced over ``[-strain_window, +strain_window]``."""
    if not (np.isfinite(strain_window) and 0 < strain_window < 0.5):
        raise ValueError("strain_window must be in (0, 0.5)")
    if int(n_points) < 4:
        raise ValueError("n_points must be >= 4")
    eps = np.round(np.linspace(-strain_window, strain_window, int(n_points)), 12)  # exact 0, ±0.01, ...
    eps[eps == 0.0] = 0.0  # no -0.0
    return [float(e) for e in eps]


def bulk_modulus_result(
    atoms: Atoms, engine: Optional["UMLIPEngine"] = None, *, strain_window: float = 0.02, n_points: int = 5,
) -> BulkModulusResult:
    """Full equation-of-state result (the ``BULK_MODULUS_RESULT`` payload) for ``atoms``.

    ``k_gpa`` (and ``v_eq`` / ``k_eq_gpa`` / ``k_eq_prime``) are ``None`` when the
    consistency checks reject the fit; the raw volumes / energies / pressures are
    always returned. Raises ``ValueError`` when no fit is possible at all
    (non-periodic or empty structure, non-finite energies). ``atoms`` is not mutated.
    """
    strains = bulk_modulus_strains(strain_window, n_points)
    try:
        data = _bulk_data(atoms, engine, strains)
    except _BulkUnavailable as exc:
        raise ValueError(str(exc)) from None
    ok = data.rejected is None
    extrapolated = bool(
        data.v_eq is not None and not (data.volumes.min() <= data.v_eq <= data.volumes.max())
    )
    method = (
        f"clamped-ion, fixed cell shape (fractional coordinates held); 3rd-order Birch-Murnaghan "
        f"fit (linear in u = (V/V0)^(-2/3) - 1) of {len(strains)} energies over "
        f"dV/V = ±{100.0 * strain_window:.3g} %; K at the given volume, cross-checked with -V dP/dV from the stresses"
    )
    if not ok:
        method += f"; fit rejected ({data.rejected})"
    k_p = data.k_p if np.isfinite(data.k_p) else None
    return BulkModulusResult(
        k_gpa=data.k_e if ok else None,
        k_stress_gpa=k_p,
        v0=data.v0,
        pressure_gpa=data.pressure_gpa,
        v_eq=data.v_eq,
        k_eq_gpa=data.k_eq,
        k_eq_prime=data.k_prime,
        extrapolated=extrapolated,
        volumes=[float(v) for v in data.volumes],
        energies_ev=[float(e) for e in data.energies],
        pressures_gpa=[float(p) for p in data.pressures_gpa],
        method=method,
    )


def estimate_bulk_modulus(atoms: Atoms, engine: Optional["UMLIPEngine"] = None) -> Optional[float]:
    """Bulk modulus K = V0 d²E/dV² at the input volume, in GPa (see module docstring), or ``None``."""
    try:
        fit = fit_bulk_modulus(atoms, engine)
    except Exception:  # noqa: BLE001 - never let the estimator take down a stream
        logger.exception("bulk modulus estimate failed")
        return None
    return None if fit is None else fit.k_gpa
