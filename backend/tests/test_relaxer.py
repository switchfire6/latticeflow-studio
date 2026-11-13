"""Streaming FIRE relaxer and bulk-modulus estimator (PROJECT_SPEC.md §4.3, §5.2 RELAXATION_FRAME)."""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
import pytest
from ase import Atoms

from app.core.relaxer import (
    BulkModulusFit,
    RelaxationDivergedError,
    estimate_bulk_modulus,
    fit_bulk_modulus,
    iter_relaxation,
    relax_stream,
)
from app.core.umlip_engine import EV_PER_A3_TO_GPA, EngineCalculator, UMLIPEngine
from app.presets import load_preset
from app.schemas.messages import RelaxationFrame

FMAX = 0.02


def rattled(name: str, sigma: float = 0.05, seed: int = 1) -> Atoms:
    atoms = load_preset(name)
    atoms.positions += np.random.default_rng(seed).normal(0.0, sigma, atoms.positions.shape)
    return atoms


def frame_positions(frame: RelaxationFrame) -> np.ndarray:
    return np.array([s.cart_coords for s in frame.crystal.atoms])


def assert_atoms_match_frame(atoms: Atoms, frame: RelaxationFrame, atol: float = 1e-9) -> None:
    np.testing.assert_allclose(atoms.cell.array, np.array(frame.crystal.lattice.matrix), atol=atol, rtol=0)
    np.testing.assert_allclose(atoms.positions, frame_positions(frame), atol=atol, rtol=0)
    assert [s.atomic_number for s in frame.crystal.atoms] == atoms.numbers.tolist()


def assert_frame_contract(frames, max_steps: int) -> None:
    assert frames, "no frames"
    assert [f.step for f in frames] == list(range(len(frames)))
    assert [f.final for f in frames].count(True) == 1 and frames[-1].final
    assert not any(f.converged for f in frames[:-1])  # we stop at the first converged frame
    assert frames[-1].converged or frames[-1].step == max_steps
    for f in frames:
        assert isinstance(f, RelaxationFrame)
        RelaxationFrame.model_validate(f.model_dump())


def cell_criterion_gpa(frame: RelaxationFrame) -> float:
    """|sigma| bound implied by the FrechetCellFilter criterion: fmax * N / V (GPa)."""
    return FMAX * len(frame.crystal.atoms) / frame.crystal.lattice.volume * EV_PER_A3_TO_GPA


class StubEngine:
    """Delegates to a real engine; ``hook(call_no, e, f, s)`` may tamper with results; ``delay`` slows calls."""

    def __init__(self, base: UMLIPEngine, hook=None, delay: float = 0.0):
        self.base = base
        self.hook = hook
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def energy_forces_stress(self, atoms):
        with self._lock:
            self.calls += 1
            n = self.calls
        if self.delay:
            time.sleep(self.delay)
        e, f, s = self.base.energy_forces_stress(atoms)
        return self.hook(n, e, f, s) if self.hook else (e, f, s)

    def get_calculator(self):
        return EngineCalculator(self)

    def payload_from_results(self, *args, **kwargs):
        return self.base.payload_from_results(*args, **kwargs)


# --------------------------------------------------------------- convergence
@pytest.mark.parametrize("preset", ["si", "srtio3"])
@pytest.mark.parametrize("relax_cell", [True, False], ids=["cell", "fixed-cell"])
def test_rattled_structure_relaxes(engine, preset, relax_cell):
    atoms = rattled(preset)
    cell0 = atoms.cell.array.copy()
    frames = list(iter_relaxation(atoms, engine, fmax=FMAX, max_steps=600, relax_cell=relax_cell))
    assert_frame_contract(frames, 600)
    last = frames[-1]
    assert last.converged and last.crystal.max_force < FMAX
    energies = np.array([f.crystal.energy_per_atom for f in frames])
    assert energies[-1] < energies[0]
    assert energies[-1] <= energies.min() + 1e-3  # FIRE is inertial: not monotone, but ends at the bottom
    assert_atoms_match_frame(atoms, last)
    assert atoms.calc is None and not atoms.constraints
    if relax_cell:
        assert not np.allclose(atoms.cell.array, cell0)
        assert np.abs(last.crystal.stress_gpa).max() < 2.0 * cell_criterion_gpa(last)
    else:
        np.testing.assert_array_equal(atoms.cell.array, cell0)


@pytest.mark.requires_mace
def test_li3ps4_relaxes_with_mace(mace_engine):
    for relax_cell in (True, False):
        atoms = load_preset("li3ps4")
        frames = list(iter_relaxation(atoms, mace_engine, fmax=FMAX, max_steps=300, relax_cell=relax_cell))
        assert frames[0].crystal.max_force > 1.0
        assert frames[-1].converged and frames[-1].crystal.max_force < FMAX
        assert frames[-1].crystal.energy_per_atom < frames[0].crystal.energy_per_atom


def test_converged_definition_includes_cell(fallback_engine):
    """Ideal SrTiO3 has zero forces but non-zero stress: converged only without cell relaxation."""
    atoms = load_preset("srtio3")
    stress0 = fallback_engine.energy_forces_stress(atoms)[2]
    assert np.abs(stress0[:3]).max() * EV_PER_A3_TO_GPA > 1.0
    fixed = list(iter_relaxation(atoms.copy(), fallback_engine, relax_cell=False))
    assert len(fixed) == 1 and fixed[0].converged and fixed[0].final and fixed[0].step == 0
    first = next(iter_relaxation(atoms.copy(), fallback_engine, relax_cell=True))
    assert first.crystal.max_force < FMAX and not first.converged and not first.final


# ------------------------------------------------------------ frame contract
def test_max_steps_cutoff(engine):
    atoms = rattled("srtio3", 0.1)
    frames = list(iter_relaxation(atoms, engine, max_steps=3))
    assert [f.step for f in frames] == [0, 1, 2, 3]
    assert [f.final for f in frames] == [False, False, False, True]
    assert not any(f.converged for f in frames)
    assert_atoms_match_frame(atoms, frames[-1])
    assert atoms.calc is None


def test_atoms_on_a_cell_face_stay_on_the_payload_image(fallback_engine):
    """Round-off on a cell face must not leave the atoms one lattice vector away from the payload."""
    atoms = load_preset("srtio3")
    frac = atoms.get_scaled_positions(wrap=False)
    frac[0] = [-1e-16, 1.0 - 1e-13, 0.0]
    atoms.set_scaled_positions(frac)
    frame = next(iter_relaxation(atoms, fallback_engine, max_steps=0))
    np.testing.assert_allclose(atoms.positions, [a.cart_coords for a in frame.crystal.atoms], atol=1e-9)


def test_max_steps_zero_gives_single_final_frame(fallback_engine):
    frames = list(iter_relaxation(rattled("srtio3"), fallback_engine, max_steps=0))
    assert len(frames) == 1 and frames[0].final and not frames[0].converged and frames[0].step == 0


def test_already_converged_input(engine):
    atoms = rattled("srtio3")
    # Pre-relax well below FMAX: a fresh FrechetCellFilter measures the cell
    # gradient relative to a new reference cell, so exactly-at-threshold could flip.
    list(iter_relaxation(atoms, engine, fmax=FMAX / 4, max_steps=1000))
    again = atoms.copy()
    frames = list(iter_relaxation(again, engine, fmax=FMAX))
    assert len(frames) == 1
    assert frames[0].step == 0 and frames[0].converged and frames[0].final
    np.testing.assert_allclose(again.positions, atoms.positions, atol=1e-12)


def test_step_zero_is_the_unmoved_start(fallback_engine):
    atoms = rattled("srtio3")
    reference = fallback_engine.evaluate(atoms)
    first = next(iter_relaxation(atoms.copy(), fallback_engine))
    assert first.step == 0
    assert first.crystal.energy_per_atom == pytest.approx(reference.energy_per_atom, abs=1e-10)
    np.testing.assert_allclose(frame_positions(first), [s.cart_coords for s in reference.atoms], atol=1e-10)


def test_invalid_arguments(fallback_engine):
    with pytest.raises(ValueError):
        iter_relaxation(rattled("si"), fallback_engine, fmax=0.0)
    with pytest.raises(ValueError):
        iter_relaxation(rattled("si"), fallback_engine, max_steps=-1)
    with pytest.raises(ValueError):
        iter_relaxation(Atoms(), fallback_engine)


def test_single_evaluation_per_step(fallback_engine):
    stub = StubEngine(fallback_engine)
    frames = list(iter_relaxation(rattled("srtio3"), stub, max_steps=10))
    assert stub.calls == len(frames)  # FIRE's results are reused for the payload


# ---------------------------------------------------------- in-place / cleanup
def test_in_place_semantics_every_frame(engine):
    atoms = rattled("srtio3", 0.08)
    atoms.positions[0] -= atoms.cell[0]  # start outside the cell
    for frame in iter_relaxation(atoms, engine, max_steps=15):
        assert_atoms_match_frame(atoms, frame)
        assert isinstance(atoms.calc, EngineCalculator)
        frac = atoms.get_scaled_positions(wrap=False)
        assert np.all(frac >= -1e-12) and np.all(frac < 1.0 + 1e-12)
    assert atoms.calc is None and not atoms.constraints


def test_early_close_cleans_up(fallback_engine):
    atoms = rattled("srtio3", 0.1)
    gen = iter_relaxation(atoms, fallback_engine, max_steps=50)
    frames = [next(gen) for _ in range(4)]
    gen.close()
    assert atoms.calc is None and not atoms.constraints
    assert_atoms_match_frame(atoms, frames[-1])


def test_cancel_stops_cleanly(engine):
    atoms = rattled("srtio3", 0.1)
    seen = []

    def should_cancel():
        return len(seen) >= 3

    for frame in iter_relaxation(atoms, engine, max_steps=50, should_cancel=should_cancel):
        seen.append(frame)
    assert [f.step for f in seen] == [0, 1, 2]
    assert not any(f.final for f in seen)
    assert atoms.calc is None
    assert_atoms_match_frame(atoms, seen[-1])


def test_cancel_before_start_yields_nothing(fallback_engine):
    atoms = rattled("srtio3")
    before = atoms.positions.copy()
    assert list(iter_relaxation(atoms, fallback_engine, should_cancel=lambda: True)) == []
    np.testing.assert_array_equal(atoms.positions, before)
    assert atoms.calc is None


def test_engine_error_rolls_back_and_cleans_up(fallback_engine):
    def hook(n, e, f, s):
        if n == 4:
            raise RuntimeError("boom")
        return e, f, s

    atoms = rattled("srtio3", 0.1)
    frames = []
    with pytest.raises(RuntimeError, match="boom"):
        for frame in iter_relaxation(atoms, StubEngine(fallback_engine, hook), max_steps=50):
            frames.append(frame)
    assert len(frames) == 3
    assert atoms.calc is None
    assert_atoms_match_frame(atoms, frames[-1])


# ----------------------------------------------------------------- divergence
@pytest.mark.parametrize(
    "tamper",
    [
        lambda e, f, s: (e, np.full_like(f, np.nan), s),
        lambda e, f, s: (float("inf"), f, s),
        lambda e, f, s: (e, f, np.full_like(s, np.nan)),
        lambda e, f, s: (e, f * 0 + 1e6, s),
    ],
    ids=["nan-forces", "inf-energy", "nan-stress", "exploding-forces"],
)
def test_divergence_guard(fallback_engine, tamper):
    def hook(n, e, f, s):
        return tamper(e, f, s) if n == 3 else (e, f, s)

    atoms = rattled("srtio3", 0.1)
    frames = []
    with pytest.raises(RelaxationDivergedError) as info:
        for frame in iter_relaxation(atoms, StubEngine(fallback_engine, hook), max_steps=50):
            frames.append(frame)
    assert [f.step for f in frames] == [0, 1, 2]
    last = frames[-1]
    assert last.final and not last.converged
    assert last.crystal == frames[1].crystal  # final frame carries the last good structure
    assert info.value.step == 2 and info.value.frame is last and info.value.reason
    assert atoms.calc is None
    assert_atoms_match_frame(atoms, frames[1])


def test_divergence_guard_exploding_cell(fallback_engine):
    """A stress that keeps pulling the cell open must be stopped by the volume guard, not run away."""

    def hook(n, e, f, s):
        return e, f, s + np.array([-0.5, -0.5, -0.5, 0, 0, 0])  # huge tensile stress (~80 GPa)

    atoms = rattled("srtio3")
    v0 = atoms.cell.volume
    frames = []
    with pytest.raises(RelaxationDivergedError, match="volume"):
        for frame in iter_relaxation(atoms, StubEngine(fallback_engine, hook), max_steps=2000):
            frames.append(frame)
    assert frames[-1].final
    assert atoms.cell.volume < 8.0 * v0 and np.all(np.isfinite(atoms.positions))
    assert atoms.calc is None


def test_nan_at_start_raises_without_frames(fallback_engine):
    stub = StubEngine(fallback_engine, lambda n, e, f, s: (float("nan"), f, s))
    atoms = rattled("srtio3")
    before = atoms.positions.copy()
    with pytest.raises(RelaxationDivergedError):
        list(iter_relaxation(atoms, stub))
    assert atoms.calc is None
    np.testing.assert_array_equal(atoms.positions, before)


# ---------------------------------------------------------------------- async
def _frames_equal(a, b, tol):
    assert [(f.step, f.converged, f.final) for f in a] == [(f.step, f.converged, f.final) for f in b]
    for x, y in zip(a, b):
        assert abs(x.crystal.energy_per_atom - y.crystal.energy_per_atom) <= tol
        np.testing.assert_allclose(frame_positions(x), frame_positions(y), atol=tol * 100, rtol=0)


async def test_async_stream_matches_sync(engine):
    sync_atoms = rattled("srtio3", 0.08)
    async_atoms = sync_atoms.copy()
    sync_frames = list(iter_relaxation(sync_atoms, engine, max_steps=40))
    async_frames = [f async for f in relax_stream(async_atoms, engine, max_steps=40)]
    # CUDA scatter-add is not bitwise deterministic, so allow fp noise on MACE.
    _frames_equal(sync_frames, async_frames, 0.0 if not engine.is_mace else 1e-8)
    assert async_atoms.calc is None
    assert_atoms_match_frame(async_atoms, async_frames[-1])


async def test_async_stream_keeps_loop_responsive(fallback_engine):
    stub = StubEngine(fallback_engine, delay=0.15)  # every step blocks its thread for 150 ms
    gaps = []
    done = asyncio.Event()

    async def ticker():
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    frames = [f async for f in relax_stream(rattled("srtio3", 0.1), stub, max_steps=4)]
    done.set()
    await tick
    assert len(frames) == 5
    assert len(gaps) > 50  # the loop kept ticking through ~0.75 s of compute
    assert max(gaps) < 0.1


async def test_async_break_stops_further_steps(fallback_engine):
    stub = StubEngine(fallback_engine)
    atoms = rattled("srtio3", 0.1)
    received = []
    stream = relax_stream(atoms, stub, max_steps=100)
    async for frame in stream:
        received.append(frame)
        if len(received) == 3:
            break
    await stream.aclose()
    calls = stub.calls
    await asyncio.sleep(0.2)
    assert stub.calls == calls == 3  # no step beyond the last delivered frame
    assert atoms.calc is None
    assert_atoms_match_frame(atoms, received[-1])


async def test_async_task_cancel_mid_step(fallback_engine):
    stub = StubEngine(fallback_engine, delay=0.2)
    atoms = rattled("srtio3", 0.1)
    received = []

    async def consume():
        async for frame in relax_stream(atoms, stub, max_steps=100):
            received.append(frame)

    task = asyncio.create_task(consume())
    while len(received) < 2:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # step 2 is now in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    calls = stub.calls
    await asyncio.sleep(0.5)
    assert stub.calls == calls <= 3  # the in-flight step may finish; nothing after it
    assert atoms.calc is None
    assert_atoms_match_frame(atoms, received[-1])  # rolled back to the last delivered frame


async def test_async_stream_propagates_divergence(fallback_engine):
    stub = StubEngine(fallback_engine, lambda n, e, f, s: (e, np.full_like(f, np.nan), s) if n == 3 else (e, f, s))
    atoms = rattled("srtio3", 0.1)
    frames = []
    with pytest.raises(RelaxationDivergedError):
        async for frame in relax_stream(atoms, stub, max_steps=50):
            frames.append(frame)
    assert frames[-1].final and atoms.calc is None
    assert_atoms_match_frame(atoms, frames[1])


# --------------------------------------------------------------- bulk modulus
def test_bulk_modulus_does_not_mutate_input(engine):
    atoms = rattled("srtio3", 0.01)
    before = (atoms.positions.copy(), atoms.cell.array.copy(), atoms.numbers.copy())
    estimate_bulk_modulus(atoms, engine)
    np.testing.assert_array_equal(atoms.positions, before[0])
    np.testing.assert_array_equal(atoms.cell.array, before[1])
    np.testing.assert_array_equal(atoms.numbers, before[2])
    assert atoms.calc is None


def test_bulk_modulus_none_when_not_meaningful(fallback_engine):
    molecule = Atoms("H2O", positions=[[0, 0, 0], [0.96, 0, 0], [-0.24, 0.93, 0]], pbc=False)
    assert estimate_bulk_modulus(molecule, fallback_engine) is None
    assert estimate_bulk_modulus(Atoms(), fallback_engine) is None
    nan_engine = StubEngine(fallback_engine, lambda n, e, f, s: (float("nan"), f, s))
    nan_engine.energy_forces_stress_batch = lambda lst, **kw: [nan_engine.energy_forces_stress(a) for a in lst]
    assert estimate_bulk_modulus(load_preset("srtio3"), nan_engine) is None


def test_bulk_modulus_fallback_is_consistent(fallback_engine):
    fit = fit_bulk_modulus(load_preset("srtio3"), fallback_engine)
    assert isinstance(fit, BulkModulusFit)
    assert fit.k_gpa > 0 and abs(fit.k_gpa - fit.k_stress_gpa) < 0.02 * fit.k_gpa
    assert len(fit.volumes) == len(fit.energies) == len(fit.pressures_gpa) == 5
    assert fit.volumes[2] == pytest.approx(load_preset("srtio3").cell.volume, rel=1e-12)


@pytest.mark.requires_mace
@pytest.mark.parametrize("preset, lo, hi", [("si", 65.0, 100.0), ("srtio3", 160.0, 200.0)])
def test_bulk_modulus_mace_values(mace_engine, preset, lo, hi):
    fit = fit_bulk_modulus(load_preset(preset), mace_engine)
    assert fit is not None
    assert lo < fit.k_gpa < hi
    assert abs(fit.k_gpa - fit.k_stress_gpa) < 0.01 * fit.k_gpa  # energy fit agrees with -V dP/dV
    assert fit.pressure_gpa > 0  # presets sit compressed relative to the MACE minimum
    assert fit.v_eq is not None and fit.v_eq > fit.v0
    assert estimate_bulk_modulus(load_preset(preset), mace_engine) == pytest.approx(fit.k_gpa, rel=1e-9)


@pytest.mark.requires_mace
def test_bulk_modulus_at_relaxed_volume_matches_bm_b0(mace_engine):
    """At the relaxed volume P ~ 0, so K(V) must agree with the fit's extrapolated B0."""
    atoms = load_preset("srtio3")
    fit0 = fit_bulk_modulus(atoms, mace_engine)
    list(iter_relaxation(atoms, mace_engine, fmax=0.002, max_steps=500))
    fit1 = fit_bulk_modulus(atoms, mace_engine)
    assert abs(fit1.pressure_gpa) < 0.2
    assert fit1.k_gpa == pytest.approx(fit0.k_eq_gpa, rel=0.03)
    assert fit1.v0 == pytest.approx(fit0.v_eq, rel=0.005)
