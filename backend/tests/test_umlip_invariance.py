"""Verification Gate 1 (PROJECT_SPEC.md §7 Phase 1).

Every test runs against both backends (``engine`` fixture): the analytic
fallback always, MACE-MP-0 (float64) when its checkpoint can be loaded.
Structures are deliberately low-symmetry (rattled + sheared) so forces and
stress are non-zero and the equivariance checks are not vacuous.
"""

from __future__ import annotations

import numpy as np
import pytest
from ase import Atoms
from ase.geometry import cellpar_to_cell

E_TOL = 1e-5          # eV, total energy (spec)
F_TOL = 1e-6          # eV/Å, exact-symmetry force comparisons
S_TOL = 1e-7          # eV/Å^3, exact-symmetry stress comparisons (~1.6e-5 GPa)
FD_F_TOL = 5e-5       # eV/Å, central finite differences, h = 1e-4 Å
FD_S_TOL = 5e-6       # eV/Å^3, central finite strain, h = 1e-4
NET_F_TOL = 1e-6      # eV/Å


# ----------------------------------------------------------------- structures
def rattled_srtio3() -> Atoms:
    a = 3.905
    cell = np.array([[a, 0.0, 0.0], [0.04, a * 1.01, 0.0], [-0.03, 0.05, a * 0.985]])
    atoms = Atoms(
        "SrTiO3",
        scaled_positions=[[0, 0, 0], [0.5, 0.5, 0.5], [0.5, 0.5, 0], [0.5, 0, 0.5], [0, 0.5, 0.5]],
        cell=cell,
        pbc=True,
    )
    atoms.positions += np.random.default_rng(11).normal(0.0, 0.06, (5, 3))
    return atoms


def rattled_triclinic() -> Atoms:
    """Charge-neutral Li2MgTiO4 (8 atoms) in a triclinic cell, P1 after rattling."""
    cell = cellpar_to_cell([5.35, 5.70, 6.05, 81.0, 97.0, 104.0])
    grid = np.array([[x, y, z] for x in (0.25, 0.75) for y in (0.25, 0.75) for z in (0.25, 0.75)])
    rng = np.random.default_rng(23)
    frac = grid + rng.uniform(-0.04, 0.04, grid.shape)
    return Atoms(["Li", "O", "Mg", "O", "Ti", "O", "Li", "O"], scaled_positions=frac, cell=cell, pbc=True)


def rattled_vacancy_transmuted() -> Atoms:
    """SrTiO3 2x1x1 with Ti->Fe and one O vacancy: not charge neutral (LJ-only fallback path)."""
    atoms = rattled_srtio3().repeat((2, 1, 1))
    atoms.symbols[1] = "Fe"
    del atoms[7]
    atoms.positions += np.random.default_rng(5).normal(0.0, 0.03, atoms.positions.shape)
    return atoms


STRUCTURES = {
    "srtio3": rattled_srtio3,
    "triclinic": rattled_triclinic,
    "vacancy": rattled_vacancy_transmuted,
}


@pytest.fixture(params=list(STRUCTURES))
def structure(request) -> Atoms:
    return STRUCTURES[request.param]()


def random_rotation(seed: int) -> np.ndarray:
    q, r = np.linalg.qr(np.random.default_rng(seed).normal(size=(3, 3)))
    q = q @ np.diag(np.sign(np.diag(r)))
    if np.linalg.det(q) < 0:
        q[:, 0] *= -1
    return q


def voigt_to_matrix(v: np.ndarray) -> np.ndarray:
    xx, yy, zz, yz, xz, xy = v
    return np.array([[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]])


# ----------------------------------------------------------------------- tests
def test_backend_identity(engine):
    assert engine.backend_name in ("lj-fallback",) or engine.backend_name.startswith("mace-")
    if engine.is_mace:
        assert engine.dtype == "float64"


def test_outputs_shapes_and_finite(engine, structure):
    e, f, s = engine.energy_forces_stress(structure)
    assert isinstance(e, float) and np.isfinite(e)
    assert f.shape == (len(structure), 3) and np.all(np.isfinite(f))
    assert s.shape == (6,) and np.all(np.isfinite(s))
    # non-vacuous: low-symmetry structures carry real forces
    assert np.abs(f).max() > 1e-3


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_rigid_rotation_equivariance(engine, structure, seed):
    e0, f0, s0 = engine.energy_forces_stress(structure)
    R = random_rotation(seed)
    rot = structure.copy()
    rot.set_cell(structure.cell.array @ R.T, scale_atoms=False)
    rot.positions = structure.positions @ R.T
    e1, f1, s1 = engine.energy_forces_stress(rot)
    assert abs(e1 - e0) <= E_TOL
    np.testing.assert_allclose(f1, f0 @ R.T, atol=F_TOL, rtol=0)
    np.testing.assert_allclose(
        voigt_to_matrix(s1), R @ voigt_to_matrix(s0) @ R.T, atol=S_TOL, rtol=0
    )


def test_uniform_translation_invariance(engine, structure):
    e0, f0, s0 = engine.energy_forces_stress(structure)
    for t in ([0.37, -1.21, 2.05], [13.1, 7.7, -9.4]):
        moved = structure.copy()
        moved.positions += np.array(t)
        e1, f1, s1 = engine.energy_forces_stress(moved)
        assert abs(e1 - e0) <= E_TOL
        np.testing.assert_allclose(f1, f0, atol=F_TOL, rtol=0)
        np.testing.assert_allclose(s1, s0, atol=S_TOL, rtol=0)


def test_lattice_vector_shift_and_rewrap_invariance(engine, structure):
    e0, f0, s0 = engine.energy_forces_stress(structure)
    rng = np.random.default_rng(7)

    shifted = structure.copy()
    n = rng.integers(-2, 3, size=(len(structure), 3))
    shifted.positions += n @ structure.cell.array
    e1, f1, s1 = engine.energy_forces_stress(shifted)
    assert abs(e1 - e0) <= E_TOL
    np.testing.assert_allclose(f1, f0, atol=F_TOL, rtol=0)
    np.testing.assert_allclose(s1, s0, atol=S_TOL, rtol=0)

    # push atoms across the boundary, then wrap back into the cell
    crossed = structure.copy()
    crossed.positions += 0.5 * structure.cell.array.sum(axis=0)
    crossed.wrap()
    assert np.all(crossed.get_scaled_positions(wrap=False) >= -1e-12)
    e2, f2, s2 = engine.energy_forces_stress(crossed)
    assert abs(e2 - e0) <= E_TOL
    np.testing.assert_allclose(f2, f0, atol=F_TOL, rtol=0)
    np.testing.assert_allclose(s2, s0, atol=S_TOL, rtol=0)


def test_permutation_invariance(engine, structure):
    e0, f0, s0 = engine.energy_forces_stress(structure)
    perm = np.random.default_rng(3).permutation(len(structure))
    permuted = structure[perm]
    e1, f1, s1 = engine.energy_forces_stress(permuted)
    assert abs(e1 - e0) <= E_TOL
    np.testing.assert_allclose(f1, f0[perm], atol=F_TOL, rtol=0)
    np.testing.assert_allclose(s1, s0, atol=S_TOL, rtol=0)


def test_supercell_extensivity(engine):
    unit = rattled_srtio3()
    e0, f0, s0 = engine.energy_forces_stress(unit)
    sc = unit.repeat((2, 2, 1))
    e1, f1, s1 = engine.energy_forces_stress(sc)
    assert abs(e1 - 4 * e0) <= 4 * E_TOL
    np.testing.assert_allclose(f1, np.tile(f0, (4, 1)), atol=F_TOL, rtol=0)
    np.testing.assert_allclose(s1, s0, atol=S_TOL, rtol=0)


def test_net_force_vanishes(engine, structure):
    _, f, _ = engine.energy_forces_stress(structure)
    assert np.linalg.norm(f.sum(axis=0)) <= NET_F_TOL


def test_forces_are_negative_energy_gradient(engine, structure):
    _, f, _ = engine.energy_forces_stress(structure)
    h = 1e-4
    fd = np.zeros_like(f)
    for i in range(len(structure)):
        for k in range(3):
            plus, minus = structure.copy(), structure.copy()
            plus.positions[i, k] += h
            minus.positions[i, k] -= h
            fd[i, k] = -(
                engine.energy_forces_stress(plus)[0] - engine.energy_forces_stress(minus)[0]
            ) / (2 * h)
    np.testing.assert_allclose(f, fd, atol=FD_F_TOL, rtol=0)


def test_stress_is_strain_derivative(engine, structure):
    _, _, s = engine.energy_forces_stress(structure)
    h = 1e-4
    volume = structure.get_volume()
    fd = np.zeros((3, 3))
    for a in range(3):
        for b in range(a, 3):
            eps = np.zeros((3, 3))
            eps[a, b] += h / 2
            eps[b, a] += h / 2
            energies = []
            for sign in (1, -1):
                strained = structure.copy()
                strained.set_cell(structure.cell.array @ (np.eye(3) + sign * eps), scale_atoms=True)
                energies.append(engine.energy_forces_stress(strained)[0])
            fd[a, b] = fd[b, a] = (energies[0] - energies[1]) / (2 * h) / volume
    np.testing.assert_allclose(voigt_to_matrix(s), fd, atol=FD_S_TOL, rtol=0)
