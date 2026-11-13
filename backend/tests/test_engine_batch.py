"""Batched evaluation in UMLIPEngine: forces_batch / energy_forces_stress_batch / payload_from_results."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from ase import Atoms

from app.presets import load_preset

F_TOL = 1e-8      # eV/Å, batched vs sequential (float64)
E_TOL = 1e-8      # eV
S_TOL = 1e-10     # eV/Å^3


def rattle(atoms: Atoms, sigma: float, seed: int) -> Atoms:
    atoms = atoms.copy()
    atoms.positions += np.random.default_rng(seed).normal(0.0, sigma, atoms.positions.shape)
    return atoms


def mixed_structures():
    """Different sizes and species in one call, incl. a 40-atom perovskite supercell."""
    return [
        rattle(load_preset("srtio3"), 0.03, 0),
        rattle(load_preset("si"), 0.03, 1),
        rattle(load_preset("li3ps4"), 0.02, 2),
        rattle(load_preset("batio3").repeat((2, 1, 1)), 0.03, 3),
        rattle(load_preset("srtio3").repeat(2), 0.01, 4),
        rattle(load_preset("si").repeat((2, 1, 1)), 0.02, 5),
    ]


def assert_matches_sequential(engine, structures, batched, stress=True):
    assert len(batched) == len(structures)
    for atoms, (e, f, s) in zip(structures, batched):
        e_ref, f_ref, s_ref = engine.energy_forces_stress(atoms)
        assert f.shape == (len(atoms), 3)
        assert abs(e - e_ref) <= E_TOL
        np.testing.assert_allclose(f, f_ref, atol=F_TOL, rtol=0)
        np.testing.assert_allclose(s, s_ref if stress else np.zeros(6), atol=S_TOL, rtol=0)


# ------------------------------------------------------------- agreement
def test_batch_matches_sequential_mixed(engine):
    structures = mixed_structures()
    before = [(a.positions.copy(), a.cell.array.copy()) for a in structures]
    assert_matches_sequential(engine, structures, engine.energy_forces_stress_batch(structures))
    for a, (p, c) in zip(structures, before):  # inputs untouched
        np.testing.assert_array_equal(a.positions, p)
        np.testing.assert_array_equal(a.cell.array, c)
        assert a.calc is None


def test_forces_batch_matches_sequential(engine):
    structures = mixed_structures()
    forces = engine.forces_batch(structures)
    assert [f.shape for f in forces] == [(len(a), 3) for a in structures]
    for a, f in zip(structures, forces):
        np.testing.assert_allclose(f, engine.energy_forces_stress(a)[1], atol=F_TOL, rtol=0)


def test_compute_stress_false_returns_zero_stress(engine):
    structures = mixed_structures()[:3]
    assert_matches_sequential(
        engine, structures, engine.energy_forces_stress_batch(structures, compute_stress=False), stress=False
    )


def test_empty_and_single(engine):
    assert engine.forces_batch([]) == []
    assert engine.energy_forces_stress_batch([]) == []
    one = [rattle(load_preset("srtio3"), 0.05, 7)]
    assert_matches_sequential(engine, one, engine.energy_forces_stress_batch(one))
    assert len(engine.forces_batch(one)) == 1


def test_non_periodic_items_use_sequential_path(engine):
    molecule = Atoms("H2O", positions=[[0, 0, 0], [0.96, 0, 0], [-0.24, 0.93, 0]], pbc=False)
    structures = [rattle(load_preset("srtio3"), 0.03, 0), molecule, rattle(load_preset("si"), 0.03, 1)]
    out = engine.energy_forces_stress_batch(structures)
    assert_matches_sequential(engine, structures, out)
    np.testing.assert_array_equal(out[1][2], np.zeros(6))


def test_displaced_supercells_like_phonopy(engine):
    """The phonon use case: many copies of one supercell, each with one atom displaced by 0.01 Å."""
    base = load_preset("srtio3").repeat(2)
    displaced = []
    for k in range(6):
        a = base.copy()
        a.positions[k % len(a), k % 3] += 0.01
        displaced.append(a)
    for a, f in zip(displaced, engine.forces_batch(displaced)):
        np.testing.assert_allclose(f, engine.energy_forces_stress(a)[1], atol=F_TOL, rtol=0)


# ---------------------------------------------------------------- chunking
@pytest.mark.requires_mace
def test_chunking_by_edge_budget(mace_engine, monkeypatch):
    structures = mixed_structures()
    chunks = []
    original = mace_engine._mace_run_graphs

    def spy(graphs, compute_stress):
        chunks.append([int(g["edge_index"].shape[1]) for g in graphs])
        return original(graphs, compute_stress)

    monkeypatch.setattr(mace_engine, "_mace_run_graphs", spy)

    # Budget of one edge: every structure becomes its own chunk.
    assert_matches_sequential(mace_engine, structures, mace_engine.energy_forces_stress_batch(structures, max_edges=1))
    assert [len(c) for c in chunks] == [1] * len(structures)

    # Moderate budget: chunks respect it unless a single graph alone exceeds it.
    chunks.clear()
    budget = 3000
    assert_matches_sequential(mace_engine, structures, mace_engine.energy_forces_stress_batch(structures, max_edges=budget))
    assert sum(len(c) for c in chunks) == len(structures)
    assert len(chunks) < len(structures)
    assert all(sum(c) <= budget or len(c) == 1 for c in chunks)

    # Huge budget: one forward pass for everything.
    chunks.clear()
    mace_engine.forces_batch(structures, max_edges=10**9)
    assert len(chunks) == 1 and len(chunks[0]) == len(structures)


@pytest.mark.requires_mace
def test_graph_cap_per_chunk(mace_engine, monkeypatch):
    sizes = []
    original = mace_engine._mace_run_graphs

    def spy(graphs, compute_stress):
        sizes.append(len(graphs))
        return original(graphs, compute_stress)

    monkeypatch.setattr(mace_engine, "_mace_run_graphs", spy)
    monkeypatch.setattr(mace_engine, "BATCH_MAX_GRAPHS", 2)
    structures = [rattle(load_preset("srtio3"), 0.02, k) for k in range(5)]
    mace_engine.forces_batch(structures, max_edges=10**9)
    assert sizes == [2, 2, 1]


@pytest.mark.requires_mace
def test_batched_failure_falls_back_to_sequential(mace_engine, monkeypatch):
    def broken(graphs, compute_stress):
        raise RuntimeError("CUDA out of memory (simulated)")

    monkeypatch.setattr(mace_engine, "_mace_run_graphs", broken)
    structures = mixed_structures()[:4]
    assert_matches_sequential(mace_engine, structures, mace_engine.energy_forces_stress_batch(structures))


@pytest.mark.requires_mace
def test_mace_engine_supports_batching(mace_engine):
    assert mace_engine.supports_batching


def test_fallback_backend_is_sequential(fallback_engine, monkeypatch):
    assert not fallback_engine.supports_batching
    calls = []
    original = fallback_engine.energy_forces_stress
    monkeypatch.setattr(fallback_engine, "energy_forces_stress", lambda a: calls.append(1) or original(a))
    structures = mixed_structures()
    out = fallback_engine.energy_forces_stress_batch(structures)
    assert len(calls) == len(structures)
    for a, (e, f, s) in zip(structures, out):
        e_ref, f_ref, s_ref = original(a)
        assert e == e_ref
        np.testing.assert_array_equal(f, f_ref)
        np.testing.assert_array_equal(s, s_ref)


# ------------------------------------------------------------ concurrency
def test_concurrent_batch_and_single_point_callers(engine):
    structures = mixed_structures()
    reference = [engine.energy_forces_stress(a) for a in structures]
    barrier = threading.Barrier(6)

    def batch_worker(_):
        barrier.wait()
        return ("batch", engine.forces_batch(structures))

    def single_worker(k):
        barrier.wait()
        return ("single", k, engine.energy_forces_stress(structures[k]))

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(batch_worker, i) for i in range(3)]
        futures += [pool.submit(single_worker, k) for k in range(3)]
        results = [f.result() for f in futures]

    for r in results:
        if r[0] == "batch":
            for f, (_, f_ref, _) in zip(r[1], reference):
                np.testing.assert_allclose(f, f_ref, atol=F_TOL, rtol=0)
        else:
            _, k, (e, f, s) = r
            assert abs(e - reference[k][0]) <= E_TOL
            np.testing.assert_allclose(f, reference[k][1], atol=F_TOL, rtol=0)


# ------------------------------------------------------- payload from results
def test_payload_from_results_equals_evaluate(engine):
    atoms = rattle(load_preset("srtio3"), 0.05, 9)
    atoms.positions[1] += atoms.cell[2]  # outside the cell
    e, f, s = engine.energy_forces_stress(atoms)
    a = engine.payload_from_results(atoms, e, f, s).model_dump()
    b = engine.evaluate(atoms).model_dump()
    assert atoms.calc is None

    def compare(x, y, path="payload"):
        if isinstance(x, dict):
            assert x.keys() == y.keys(), path
            for k in x:
                compare(x[k], y[k], f"{path}.{k}")
        elif isinstance(x, list):
            assert len(x) == len(y), path
            for k, (u, v) in enumerate(zip(x, y)):
                compare(u, v, f"{path}[{k}]")
        elif isinstance(x, float):
            assert abs(x - y) <= 1e-9, (path, x, y)
        else:
            assert x == y, (path, x, y)

    compare(a, b)


def test_payload_from_results_rejects_wrong_shapes(fallback_engine):
    atoms = load_preset("srtio3")
    e, f, s = fallback_engine.energy_forces_stress(atoms)
    with pytest.raises(ValueError):
        fallback_engine.payload_from_results(atoms, e, f[:3], s)
    with pytest.raises(ValueError):
        fallback_engine.payload_from_results(atoms, e, f, s[:3])
