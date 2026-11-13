"""Finite-temperature molecular dynamics with a uMLIP engine (IMPROVEMENT_SUGGESTIONS.md §5.8).

API
---
``iter_md(atoms, engine, ...)`` returns a generator of :class:`MDFrame`. It
integrates ``atoms`` **in place** (like ``relaxer.iter_relaxation``): whenever
the generator is suspended at a yield, ``atoms`` is exactly the structure of
the frame just yielded (positions are *not* wrapped during the run; the frame's
``crystal`` payload is). The generator's **return value** (``StopIteration.value``,
or ``summary = yield from iter_md(...)``) is the :class:`MDComplete` summary.
Frames are yielded at step 0 (initial structure + velocities), every
``frame_interval`` steps, and at the last step (``n_steps``) whether or not it is
a multiple of ``frame_interval``; the last frame is the one the summary's
``final_crystal`` describes.

Cancellation: ``should_cancel()`` is polled before every step; when it returns
True the generator returns ``None`` without another frame (``atoms`` may then
be one partial step past the last frame - callers keep their own snapshot of
the last frame, as the WebSocket layer does).

Divergence: non-finite energy / forces / stress / positions, a kinetic
temperature above ``max(5 T, T + 3000 K)``, forces above ``MAX_FORCE_EV_A`` or
(NPT) a volume change by more than a factor ``MAX_VOLUME_RATIO`` raise
:class:`MDDivergedError` (``step`` = the bad step, ``last_frame`` = the last
yielded frame). Nothing is yielded for the bad step.

Integrators
-----------
* NVE: ``VelocityVerlet``.
* NVT: ``Langevin`` (ASE, friction ``friction_per_fs`` / fs, seeded noise) with a
  ``FixCom`` constraint during the run (removed afterwards; frames yielded
  mid-run carry it, so callers copying ``atoms`` should drop it).
* NPT: ``LangevinBAOAB`` with ``hydrostatic=True`` and ``externalstress = -P``:
  Langevin thermostat on the atoms plus a Langevin-Hoover isotropic barostat
  on the volume (Leimkuhler & Matthews 2013; Quigley & Probert 2004). Chosen
  over ``NPTBerendsen`` (Berendsen thermostat, not Langevin, and the wrong
  ensemble: it suppresses volume fluctuations) and over ``IsotropicMTKNPT``
  (Nose-Hoover chains, deterministic, more sensitive to the barostat mass on
  the small cells this studio uses). BAOAB is a stochastic integrator - it
  cannot develop the barostat/thermostat resonances of extended-system
  methods, samples the correct NPT ensemble, uses the same seeded noise as
  NVT and needs one force + stress evaluation per step. Barostat time constant
  ``P_tau = 20 T_tau`` with ``T_tau = 1 / friction`` (ASE default heuristic).

Each integrator step is one model evaluation (energy, forces and stress are
computed together by the engine calculator and cached for that geometry).

Initial velocities: Maxwell-Boltzmann at ``temperature_k`` (``numpy.random
.default_rng(seed)``), zero total momentum, rescaled to exactly ``temperature_k``.

MSD and diffusion
-----------------
Displacements are accumulated from minimum-image fractional increments
between consecutive steps (so they are wrap-free whatever the coordinates
do) and converted with the current cell, which removes the affine part of
an NPT volume change. ``msd_by_element`` is relative to step 0. The summary's
MSD series is sampled every ``max(1, n_steps // MAX_MSD_SAMPLES)`` steps (plus
step 0 and the last step). ``D = slope / 6`` of a least-squares line through
the samples of the second half of the run, in cm^2/s (1 A^2/ps = 1e-4 cm^2/s);
``None`` when the final MSD is below ``MIN_DIFFUSIVE_MSD_A2`` (vibration only),
the slope is not positive, fewer than 4 samples, or R^2 < ``MIN_FIT_R2``.
"""

from __future__ import annotations

import logging
import math
from typing import Callable, Dict, Generator, List, Optional

import numpy as np
from ase import Atoms, units
from ase.md.langevin import Langevin
from ase.constraints import FixCom
from ase.md.velocitydistribution import Stationary, thermalize_momenta
from ase.md.verlet import VelocityVerlet

from app.core.umlip_engine import EV_PER_A3_TO_GPA, UMLIPEngine
from app.schemas.messages import MDComplete, MDFrame

logger = logging.getLogger(__name__)

__all__ = ["iter_md", "MDDivergedError", "ENSEMBLES"]

ENSEMBLES = ("NVE", "NVT", "NPT")
MAX_FORCE_EV_A = 1.0e3
MAX_VOLUME_RATIO = 3.0
MAX_MSD_SAMPLES = 1000
MIN_DIFFUSIVE_MSD_A2 = 1.0
MIN_FIT_R2 = 0.9
_A2_PER_PS_TO_CM2_PER_S = 1.0e-4


class MDDivergedError(RuntimeError):
    """The trajectory blew up; ``last_frame`` is the last frame that was yielded (if any)."""

    def __init__(self, reason: str, step: int, last_frame: Optional[MDFrame] = None) -> None:
        super().__init__(f"MD diverged at step {step}: {reason}")
        self.reason = reason
        self.step = step
        self.last_frame = last_frame


def _is_periodic(atoms: Atoms) -> bool:
    return bool(np.all(atoms.pbc)) and atoms.cell.rank == 3


def diffusion_coefficient(times_ps: np.ndarray, msd: np.ndarray) -> Optional[float]:
    """D in cm^2/s from MSD(t) over the second half of the samples (None: no linear regime)."""
    times_ps = np.asarray(times_ps, dtype=float)
    msd = np.asarray(msd, dtype=float)
    if times_ps.size < 2 or not np.all(np.isfinite(msd)) or msd[-1] < MIN_DIFFUSIVE_MSD_A2:
        return None
    mask = times_ps >= 0.5 * times_ps[-1]
    t, y = times_ps[mask], msd[mask]
    if t.size < 4 or np.ptp(t) <= 0:
        return None
    slope, intercept = np.polyfit(t, y, 1)
    resid = y - (slope * t + intercept)
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - float(np.sum(resid ** 2)) / ss_tot if ss_tot > 0 else 0.0
    if not slope > 0 or r2 < MIN_FIT_R2:
        return None
    return float(slope / 6.0 * _A2_PER_PS_TO_CM2_PER_S)


def iter_md(
    atoms: Atoms,
    engine: UMLIPEngine,
    *,
    temperature_k: float = 300.0,
    ensemble: str = "NVT",
    timestep_fs: float = 1.0,
    n_steps: int = 2000,
    friction_per_fs: float = 0.01,
    pressure_gpa: float = 0.0,
    frame_interval: int = 10,
    seed: Optional[int] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    bond_mode: str = "fast",
) -> Generator[MDFrame, None, Optional[MDComplete]]:
    """Run MD on ``atoms`` in place; yield frames, return the :class:`MDComplete` summary.

    Validation errors (``ValueError``) are raised immediately, not on the first ``next``.
    """
    ensemble = str(ensemble).upper()
    if ensemble not in ENSEMBLES:
        raise ValueError(f"unknown ensemble {ensemble!r}; choose one of {list(ENSEMBLES)}")
    if len(atoms) == 0:
        raise ValueError("cannot run MD on an empty structure")
    if not (temperature_k > 0 and timestep_fs > 0 and friction_per_fs > 0):
        raise ValueError("temperature, timestep and friction must be > 0")
    if int(n_steps) < 1 or int(frame_interval) < 1:
        raise ValueError("n_steps and frame_interval must be >= 1")
    if not np.isfinite(pressure_gpa):
        raise ValueError("pressure_gpa must be finite")
    if ensemble == "NPT" and not _is_periodic(atoms):
        raise ValueError("NPT needs a fully periodic 3D cell")
    return _md_generator(
        atoms, engine, float(temperature_k), ensemble, float(timestep_fs), int(n_steps),
        float(friction_per_fs), float(pressure_gpa), int(frame_interval), seed, should_cancel, bond_mode,
    )


def _md_generator(atoms, engine, temperature_k, ensemble, timestep_fs, n_steps, friction_per_fs,
                  pressure_gpa, frame_interval, seed, should_cancel, bond_mode):
    def cancelled() -> bool:
        return should_cancel is not None and bool(should_cancel())

    if cancelled():
        return None
    periodic = _is_periodic(atoms)
    rng = np.random.default_rng(seed)

    # ---- initial velocities: MB, zero momentum, exact T
    thermalize_momenta(atoms, temperature_k, rng=rng)
    Stationary(atoms)
    original_constraints = list(atoms.constraints)
    if ensemble == "NVT":
        # Langevin noise would make the centre of mass drift; ASE >= 3.28 wants FixCom, not fixcm=True.
        atoms.set_constraint(original_constraints + [FixCom()])
    t_now = atoms.get_temperature()
    if t_now > 0:
        atoms.set_momenta(atoms.get_momenta() * math.sqrt(temperature_k / t_now))

    symbols = atoms.get_chemical_symbols()
    elements = sorted(set(symbols))
    masks = {el: np.array([s == el for s in symbols]) for el in elements}
    frac0 = atoms.cell.scaled_positions(atoms.positions) if periodic else None
    frac_prev = frac0.copy() if periodic else None
    unwrapped = frac0.copy() if periodic else None
    pos0 = atoms.positions.copy()
    v_start = abs(atoms.cell.volume) if periodic else None
    t_ceiling = max(5.0 * temperature_k, temperature_k + 3000.0)
    sample_every = max(1, n_steps // MAX_MSD_SAMPLES)

    times: List[float] = []
    msd_series: Dict[str, List[float]] = {el: [] for el in elements}
    temps: List[float] = []
    pressures: List[float] = []
    volumes: List[float] = []
    last_frame: Optional[MDFrame] = None

    atoms.calc = engine.get_calculator()  # before the integrator: LangevinBAOAB reads the stress
    try:
        dyn = _make_dynamics(atoms, ensemble, timestep_fs, temperature_k, friction_per_fs, pressure_gpa, rng)

        def measure(step: int):
            """One model evaluation of the current geometry -> observables, or raise on divergence."""
            nonlocal frac_prev
            energy = float(atoms.get_potential_energy())
            forces = np.array(atoms.get_forces(), dtype=float)
            stress = np.array(atoms.get_stress(voigt=True), dtype=float) if periodic else np.zeros(6)
            ekin = float(atoms.get_kinetic_energy())
            temp = float(atoms.get_temperature())
            reason = None
            if not (np.isfinite(energy) and np.all(np.isfinite(forces)) and np.all(np.isfinite(stress))
                    and np.isfinite(ekin)):
                reason = "non-finite energy, forces or stress"
            elif not (np.all(np.isfinite(atoms.positions)) and np.all(np.isfinite(atoms.cell.array))):
                reason = "non-finite positions or cell"
            elif temp > t_ceiling:
                reason = f"temperature {temp:.0f} K exceeds {t_ceiling:.0f} K"
            elif float(np.linalg.norm(forces, axis=1).max()) > MAX_FORCE_EV_A:
                reason = f"max force exceeds {MAX_FORCE_EV_A:.0f} eV/A"
            elif v_start is not None:
                ratio = abs(atoms.cell.volume) / v_start
                if not (1.0 / MAX_VOLUME_RATIO <= ratio <= MAX_VOLUME_RATIO):
                    reason = f"cell volume changed by a factor {ratio:.3g}"
            if reason is not None:
                logger.warning("MD diverged at step %d: %s", step, reason)
                raise MDDivergedError(reason, step, last_frame)
            # wrap-free displacements
            if periodic:
                frac = atoms.cell.scaled_positions(atoms.positions)
                d = frac - frac_prev
                d -= np.round(d)
                unwrapped[:] += d
                frac_prev = frac
                disp = (unwrapped - frac0) @ atoms.cell.array
                volume = float(abs(atoms.cell.volume))
                total_stress = stress - _kinetic_stress(atoms, volume)
                pressure = float(-np.mean(total_stress[:3]) * EV_PER_A3_TO_GPA)
            else:
                disp = atoms.positions - pos0
                volume = float(abs(atoms.cell.volume)) if atoms.cell.rank == 3 else 0.0
                pressure = None
            sq = np.sum(disp * disp, axis=1)
            msd = {el: float(sq[m].mean()) for el, m in masks.items()}
            return energy, forces, stress, ekin, temp, pressure, volume, msd

        def record(step: int, temp, pressure, volume, msd) -> None:
            times.append(step * timestep_fs * 1e-3)
            for el in elements:
                msd_series[el].append(msd[el])
            temps.append(temp)
            volumes.append(volume)
            if pressure is not None:
                pressures.append(pressure)

        def frame(step, energy, forces, stress, ekin, temp, pressure, volume, msd) -> MDFrame:
            crystal = engine.payload_from_results(atoms, energy, forces, stress, bond_mode=bond_mode)
            return MDFrame(
                step=step, time_ps=step * timestep_fs * 1e-3, temperature_k=temp,
                potential_energy_ev=energy, kinetic_energy_ev=ekin, pressure_gpa=pressure,
                volume=volume, msd_by_element=msd, crystal=crystal,
            )

        obs = measure(0)
        record(0, *obs[4:])
        last_frame = frame(0, *obs)
        yield last_frame

        for step in range(1, n_steps + 1):
            if cancelled():
                return None
            dyn.run(1)  # one integrator step = one model evaluation (forces cached per geometry)
            obs = measure(step)
            if step % sample_every == 0 or step == n_steps:
                record(step, *obs[4:])
            if step % frame_interval == 0 or step == n_steps:
                last_frame = frame(step, *obs)
                yield last_frame

        times_arr = np.asarray(times)
        method = (
            f"{_integrator_text(ensemble, friction_per_fs, pressure_gpa)}; dt = {timestep_fs:g} fs, "
            f"{n_steps} steps ({n_steps * timestep_fs * 1e-3:.4g} ps), T = {temperature_k:g} K; "
            f"Maxwell-Boltzmann start (seed {seed}), zero momentum; MSD from unwrapped positions vs step 0, "
            f"D = slope/6 of a linear fit over the second half (None below {MIN_DIFFUSIVE_MSD_A2:g} A^2 "
            f"or R^2 < {MIN_FIT_R2:g}); model {engine.backend_name}"
        )
        return MDComplete(
            n_steps=n_steps,
            time_ps=n_steps * timestep_fs * 1e-3,
            mean_temperature_k=float(np.mean(temps)),
            mean_pressure_gpa=float(np.mean(pressures)) if pressures else None,
            mean_volume=float(np.mean(volumes)),
            times_ps=[float(t) for t in times],
            msd_by_element={el: [float(x) for x in v] for el, v in msd_series.items()},
            diffusion_coefficients_cm2_s={
                el: diffusion_coefficient(times_arr, np.asarray(v)) for el, v in msd_series.items()
            },
            final_crystal=last_frame.crystal,
            method=method,
        )
    finally:
        atoms.calc = None
        atoms.set_constraint(original_constraints)


def _make_dynamics(atoms, ensemble, timestep_fs, temperature_k, friction_per_fs, pressure_gpa, rng):
    dt = timestep_fs * units.fs
    if ensemble == "NVE":
        return VelocityVerlet(atoms, dt, logfile=None)
    if ensemble == "NVT":
        return Langevin(atoms, dt, temperature_K=temperature_k, friction=friction_per_fs / units.fs,
                        fixcm=False, rng=rng, logfile=None)
    from ase.md.langevinbaoab import LangevinBAOAB

    return LangevinBAOAB(
        atoms, dt, temperature_K=temperature_k, externalstress=-pressure_gpa / EV_PER_A3_TO_GPA,
        hydrostatic=True, T_tau=(1.0 / friction_per_fs) * units.fs, rng=rng, logfile=None,
    )


def _kinetic_stress(atoms: Atoms, volume: float) -> np.ndarray:
    """Ideal-gas (kinetic) stress, Voigt eV/A^3, ASE sign (negative = pushes outwards)."""
    p = atoms.get_momenta()
    m = atoms.get_masses()[:, None]
    tensor = (p / m).T @ p / volume  # sum_i p_i p_i^T / (m_i V)
    return np.array([tensor[0, 0], tensor[1, 1], tensor[2, 2], tensor[1, 2], tensor[0, 2], tensor[0, 1]])


def _integrator_text(ensemble: str, friction_per_fs: float, pressure_gpa: float) -> str:
    if ensemble == "NVE":
        return "NVE velocity Verlet"
    if ensemble == "NVT":
        return f"NVT Langevin (friction {friction_per_fs:g} /fs)"
    return (f"NPT Langevin BAOAB + isotropic Langevin-Hoover barostat (friction {friction_per_fs:g} /fs, "
            f"P = {pressure_gpa:g} GPa incl. kinetic term)")
