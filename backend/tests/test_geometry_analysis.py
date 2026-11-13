"""Tests for app.core.geometry_analysis (structures are built inline on purpose)."""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pytest
from ase import Atoms
from ase.build import bulk
from ase.spacegroup import crystal

from app.core.geometry_analysis import (
    compute_rdf,
    find_bonds,
    get_spacegroup,
    lattice_params,
)
from app.schemas.crystal import BondEdge, LatticeParams

# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------


def srtio3() -> Atoms:
    a = 3.905
    return Atoms(
        "SrTiO3",
        scaled_positions=[[0, 0, 0], [0.5, 0.5, 0.5], [0.5, 0.5, 0], [0.5, 0, 0.5], [0, 0.5, 0.5]],
        cell=[a, a, a],
        pbc=True,
    )


def si_conv() -> Atoms:
    return bulk("Si", "diamond", a=5.431, cubic=True)


def si_prim() -> Atoms:
    return bulk("Si", "diamond", a=5.431)


def nacl() -> Atoms:
    return bulk("NaCl", "rocksalt", a=5.64, cubic=True)


def fe_bcc() -> Atoms:
    return bulk("Fe", "bcc", a=2.87)


def na3ps4() -> Atoms:
    """Cubic Na3PS4 (I-43m), isolated PS4 tetrahedra, P-S ~2.06 A."""
    return crystal(
        ["Na", "P", "S"],
        basis=[(0, 0.5, 0.5), (0, 0, 0), (0.17, 0.17, 0.17)],
        spacegroup=217,
        cellpar=[6.99, 6.99, 6.99, 90, 90, 90],
    )


def random_gas(n: int = 32, seed: int = 0, density: float = 0.05) -> Atoms:
    rng = np.random.default_rng(seed)
    vol = n / density
    cell = np.diag([1.0, 1.1, 1.2]) * (vol / 1.32) ** (1 / 3)
    cell[1, 0] = 0.3 * cell[0, 0]  # triclinic-ish
    syms = (["Li", "P", "S", "S"] * n)[:n]
    return Atoms(syms, scaled_positions=rng.random((n, 3)), cell=cell, pbc=True)


def tri_32() -> Atoms:
    """A 32-atom ordered Li-P-S cell (2x2x2 of a distorted 4-atom cell) for timing."""
    base = Atoms(
        "LiPS2",
        scaled_positions=[[0, 0, 0], [0.5, 0.5, 0.5], [0.25, 0.25, 0.7], [0.75, 0.2, 0.3]],
        cell=[[4.0, 0, 0], [0.4, 4.3, 0], [0.3, 0.2, 4.6]],
        pbc=True,
    )
    return base.repeat((2, 2, 2))


def snapshot(atoms: Atoms):
    return (atoms.positions.copy(), atoms.cell.array.copy(), atoms.numbers.copy(), atoms.pbc.copy())


def assert_unchanged(atoms: Atoms, snap) -> None:
    pos, cell, num, pbc = snap
    assert np.array_equal(atoms.positions, pos)
    assert np.array_equal(atoms.cell.array, cell)
    assert np.array_equal(atoms.numbers, num)
    assert np.array_equal(atoms.pbc, pbc)


def pair_counter(atoms: Atoms, bonds: list[BondEdge]) -> Counter:
    c: Counter = Counter()
    for b in bonds:
        pair = tuple(sorted((atoms[b.source_index].symbol, atoms[b.target_index].symbol)))
        c[pair] += 1
    return c


def degree(atoms: Atoms, bonds: list[BondEdge]) -> np.ndarray:
    deg = np.zeros(len(atoms), dtype=int)
    for b in bonds:
        deg[b.source_index] += 1
        deg[b.target_index] += 1
    return deg


def bond_vector(atoms: Atoms, b: BondEdge) -> np.ndarray:
    return (
        atoms.positions[b.target_index]
        + np.asarray(b.image_offset, dtype=float) @ atoms.cell.array
        - atoms.positions[b.source_index]
    )


def bond_set(atoms: Atoms, bonds: list[BondEdge], decimals: int = 6) -> set:
    """Orientation-independent set of (i, j, vector) for comparing bond graphs."""
    out = set()
    for b in bonds:
        v = np.round(bond_vector(atoms, b), decimals) + 0.0
        fwd = (b.source_index, b.target_index, tuple(v))
        rev = (b.target_index, b.source_index, tuple(np.round(-v, decimals) + 0.0))
        out.add(min(fwd, rev))
    return out


def check_bond_invariants(atoms: Atoms, bonds: list[BondEdge]) -> None:
    keys = set()
    for b in bonds:
        assert isinstance(b, BondEdge)
        assert len(b.image_offset) == 3 and all(isinstance(n, int) for n in b.image_offset)
        assert math.isclose(np.linalg.norm(bond_vector(atoms, b)), b.length, rel_tol=0, abs_tol=1e-8)
        assert b.is_unstable is False
        assert b.source_index <= b.target_index
        key = (b.source_index, b.target_index, tuple(b.image_offset))
        rev = (b.target_index, b.source_index, tuple(-n for n in b.image_offset))
        assert key not in keys and rev not in keys, "bond reported twice"
        keys.add(key)
        if b.source_index == b.target_index:
            assert any(b.image_offset)


# ---------------------------------------------------------------------------
# lattice_params
# ---------------------------------------------------------------------------


def test_lattice_params_cubic_and_triclinic():
    lp = lattice_params(srtio3())
    assert isinstance(lp, LatticeParams)
    assert lp.a == pytest.approx(3.905) and lp.b == pytest.approx(3.905) and lp.c == pytest.approx(3.905)
    assert (lp.alpha, lp.beta, lp.gamma) == pytest.approx((90, 90, 90))
    assert lp.volume == pytest.approx(3.905**3)
    assert np.allclose(lp.matrix, np.diag([3.905] * 3))

    cellpar = [4.1, 5.2, 6.3, 80.0, 95.0, 110.0]
    at = Atoms("H", cell=cellpar, pbc=True)
    lp = lattice_params(at)
    assert [lp.a, lp.b, lp.c, lp.alpha, lp.beta, lp.gamma] == pytest.approx(cellpar)
    assert np.allclose(lp.matrix, at.cell.array)
    assert lp.volume == pytest.approx(at.get_volume())


def test_lattice_params_degenerate_cell_is_finite():
    lp = lattice_params(Atoms("H", positions=[[0, 0, 0]]))
    assert lp.volume == 0.0
    assert all(math.isfinite(v) for v in (lp.alpha, lp.beta, lp.gamma))


# ---------------------------------------------------------------------------
# get_spacegroup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, expected",
    [
        (srtio3, ("Pm-3m", 221)),
        (si_conv, ("Fd-3m", 227)),
        (si_prim, ("Fd-3m", 227)),
        (nacl, ("Fm-3m", 225)),
        (fe_bcc, ("Im-3m", 229)),
        (na3ps4, ("I-43m", 217)),
    ],
)
def test_spacegroup_known(factory, expected):
    at = factory()
    snap = snapshot(at)
    assert get_spacegroup(at) == expected
    assert_unchanged(at, snap)


def test_spacegroup_robust():
    rng = np.random.default_rng(1)
    gas = random_gas()
    assert get_spacegroup(gas) == ("P1", 1)
    # overlapping atoms
    at = Atoms("Si2", positions=[[0, 0, 0], [0, 0, 1e-6]], cell=[3, 3, 3], pbc=True)
    sym, num = get_spacegroup(at)
    assert isinstance(sym, str) and 1 <= num <= 230
    # all atoms on one spot, NaN positions, zero cell, empty
    for bad in (
        Atoms("Si4", positions=np.zeros((4, 3)), cell=[3, 3, 3], pbc=True),
        Atoms("Si", positions=[[np.nan, 0, 0]], cell=[3, 3, 3], pbc=True),
        Atoms("Si", positions=[[0, 0, 0]]),
        Atoms(cell=[3, 3, 3], pbc=True),
    ):
        sym, num = get_spacegroup(bad)
        assert isinstance(sym, str) and isinstance(num, int) and 1 <= num <= 230
    # single atom: primitive cubic
    assert get_spacegroup(Atoms("Po", cell=[3.35] * 3, pbc=True)) == ("Pm-3m", 221)
    # heavily distorted cell does not raise
    at = si_conv()
    at.positions += rng.normal(scale=0.4, size=at.positions.shape)
    get_spacegroup(at)


# ---------------------------------------------------------------------------
# find_bonds (fast)
# ---------------------------------------------------------------------------


def test_bonds_srtio3():
    at = srtio3()
    snap = snapshot(at)
    bonds = find_bonds(at)
    assert_unchanged(at, snap)
    check_bond_invariants(at, bonds)
    c = pair_counter(at, bonds)
    assert c[("O", "Ti")] == 6
    for pair in [("Sr", "Ti"), ("Sr", "Sr"), ("Ti", "Ti"), ("O", "O")]:
        assert c[pair] == 0, pair
    assert set(c) <= {("O", "Ti"), ("O", "Sr")}
    for b in bonds:
        syms = {at[b.source_index].symbol, at[b.target_index].symbol}
        if syms == {"Ti", "O"}:
            assert b.length == pytest.approx(1.9525, abs=1e-6)
            assert b.strain_ratio == pytest.approx((1.9525 - 2.26) / 2.26, abs=1e-6)
        else:
            assert b.length == pytest.approx(3.905 / math.sqrt(2), abs=1e-6)
    # Ti-O bonds: Ti to each of 3 O, both directions -> 6 distinct offsets
    tio = [b for b in bonds if {at[b.source_index].symbol, at[b.target_index].symbol} == {"Ti", "O"}]
    assert len({(b.source_index, b.target_index, tuple(b.image_offset)) for b in tio}) == 6
    assert degree(at, bonds)[1] == 6  # Ti octahedral


def test_bonds_si_conventional():
    at = si_conv()
    bonds = find_bonds(at)
    check_bond_invariants(at, bonds)
    assert len(bonds) == 16
    assert np.all(degree(at, bonds) == 4)
    assert all(b.length == pytest.approx(5.431 * math.sqrt(3) / 4, abs=1e-4) for b in bonds)
    assert all(b.length == pytest.approx(2.3517, abs=1e-4) for b in bonds)


def test_bonds_si_primitive_multi_image():
    at = si_prim()
    bonds = find_bonds(at)
    check_bond_invariants(at, bonds)
    assert len(bonds) == 4
    assert all((b.source_index, b.target_index) == (0, 1) for b in bonds)
    assert len({tuple(b.image_offset) for b in bonds}) == 4
    assert all(b.length == pytest.approx(2.3517, abs=1e-4) for b in bonds)


def test_bonds_nacl():
    at = nacl()
    bonds = find_bonds(at)
    check_bond_invariants(at, bonds)
    assert set(pair_counter(at, bonds)) == {("Cl", "Na")}
    assert np.all(degree(at, bonds) == 6)
    assert all(b.length == pytest.approx(2.82, abs=1e-6) for b in bonds)


def test_bonds_bcc_fe_self_images():
    at = fe_bcc()
    bonds = find_bonds(at)
    check_bond_invariants(at, bonds)
    assert len(bonds) == 4
    assert all(b.source_index == b.target_index == 0 for b in bonds)
    assert all(b.length == pytest.approx(2.87 * math.sqrt(3) / 2, abs=1e-6) for b in bonds)
    offs = {tuple(b.image_offset) for b in bonds}
    assert not any(tuple(-n for n in o) in offs for o in offs)  # only one of +-n


def test_bonds_polyanion_ps4():
    at = na3ps4()
    bonds = find_bonds(at)
    check_bond_invariants(at, bonds)
    c = pair_counter(at, bonds)
    assert c[("P", "S")] == 8  # two PS4 tetrahedra
    assert c[("S", "S")] == 0
    assert c[("Na", "P")] == 0 and c[("Na", "Na")] == 0 and c[("P", "P")] == 0
    for b in bonds:
        if {at[b.source_index].symbol, at[b.target_index].symbol} == {"P", "S"}:
            assert b.length == pytest.approx(math.sqrt(3) * 0.17 * 6.99, abs=1e-6)
    deg = degree(at, [b for b in bonds if {at[b.source_index].symbol, at[b.target_index].symbol} == {"P", "S"}])
    assert all(deg[i] == 4 for i, s in enumerate(at.get_chemical_symbols()) if s == "P")


def _preset(name: str) -> Atoms:
    presets = pytest.importorskip("app.presets")
    return presets.load_preset(name)


def _transmute(atoms: Atoms, mapping: dict[str, str]) -> Atoms:
    out = atoms.copy()
    out.set_chemical_symbols([mapping.get(s, s) for s in out.get_chemical_symbols()])
    return out


def test_bonds_li3ps4_preset_regression():
    """beta-Li3PS4: Li-P contacts (2.69 A) are cation-centre, not bonds."""
    at = _preset("li3ps4")
    assert len(at) == 32
    snap = snapshot(at)
    bonds = find_bonds(at)
    assert_unchanged(at, snap)
    check_bond_invariants(at, bonds)
    c = pair_counter(at, bonds)
    assert set(c) == {("Li", "S"), ("P", "S")}
    syms = np.array(at.get_chemical_symbols())
    ps = [b for b in bonds if {syms[b.source_index], syms[b.target_index]} == {"P", "S"}]
    deg = degree(at, ps)
    assert np.all(deg[syms == "P"] == 4)
    assert np.all(deg[syms == "S"] == 1)
    assert all(1.95 < b.length < 2.15 for b in ps)
    assert np.all(degree(at, bonds)[syms == "Li"] >= 3)


@pytest.mark.parametrize(
    "mapping, centre",
    [({"P": "As"}, "As"), ({"P": "Sb"}, "Sb"), ({"P": "Ge"}, "Ge"), ({"S": "Se"}, "P"), ({"Li": "Na"}, "P")],
)
def test_bonds_transmuted_thio_polyanions(mapping, centre):
    """Polyanion centres with small chi gap to the ligand (As/Sb/Ge-S, P-Se) never bond to the cation."""
    at = _transmute(_preset("li3ps4"), mapping)
    bonds = find_bonds(at)
    check_bond_invariants(at, bonds)
    cation = mapping.get("Li", "Li")
    ligand = mapping.get("S", "S")
    c = pair_counter(at, bonds)
    assert set(c) == {tuple(sorted((cation, ligand))), tuple(sorted((centre, ligand)))}
    syms = np.array(at.get_chemical_symbols())
    centre_bonds = [b for b in bonds if centre in (syms[b.source_index], syms[b.target_index])]
    assert np.all(degree(at, centre_bonds)[syms == centre] == 4)


def calcite() -> Atoms:
    return crystal(["Ca", "C", "O"], basis=[(0, 0, 0), (0, 0, 0.25), (0.257, 0, 0.25)],
                   spacegroup=167, cellpar=[4.99, 4.99, 17.06, 90, 90, 120])


def nano3() -> Atoms:
    return crystal(["Na", "N", "O"], basis=[(0, 0, 0), (0, 0, 0.25), (0.2446, 0, 0.25)],
                   spacegroup=167, cellpar=[5.07, 5.07, 16.82, 90, 90, 120])


@pytest.mark.parametrize("factory, cation, centre", [(calcite, "Ca", "C"), (nano3, "Na", "N")])
def test_bonds_planar_oxyanions(factory, cation, centre):
    at = factory()
    bonds = find_bonds(at)
    check_bond_invariants(at, bonds)
    c = pair_counter(at, bonds)
    assert set(c) == {tuple(sorted((cation, "O"))), tuple(sorted((centre, "O")))}
    syms = np.array(at.get_chemical_symbols())
    deg = degree(at, [b for b in bonds if centre in (syms[b.source_index], syms[b.target_index])])
    assert np.all(deg[syms == centre] == 3)
    assert np.all(degree(at, bonds)[syms == cation] == 6)


def test_bonds_non_ionic_like_contacts_kept():
    """The polyanion rule must not strip like-atom bonds where no strong anion exists."""
    mgzn2 = crystal(["Mg", "Zn", "Zn"], basis=[(1 / 3, 2 / 3, 0.0629), (0, 0, 0), (0.8305, 0.661, 0.25)],
                    spacegroup=194, cellpar=[5.22, 5.22, 8.57, 90, 90, 120])
    c = pair_counter(mgzn2, find_bonds(mgzn2))
    assert c[("Mg", "Mg")] > 0 and c[("Zn", "Zn")] > 0 and c[("Mg", "Zn")] > 0
    pyrite = crystal(["Fe", "S"], basis=[(0, 0, 0), (0.385, 0.385, 0.385)],
                     spacegroup=205, cellpar=[5.417] * 3 + [90] * 3)
    c = pair_counter(pyrite, find_bonds(pyrite))
    assert c == Counter({("Fe", "S"): 24, ("S", "S"): 4})


@pytest.mark.parametrize(
    "name, expected",
    [
        ("srtio3", {("O", "Sr"): 12, ("O", "Ti"): 6}),
        ("batio3", {("Ba", "O"): 12, ("O", "Ti"): 6}),
        ("si", {("Si", "Si"): 16}),
        ("li3ps4", {("Li", "S"): 48, ("P", "S"): 16}),
    ],
)
def test_bonds_presets(name, expected):
    at = _preset(name)
    assert pair_counter(at, find_bonds(at)) == Counter(expected)


def test_bonds_metals_and_other_solids():
    cu = bulk("Cu", "fcc", a=3.61)  # 12 NN -> 6 self-image bonds
    assert len(find_bonds(cu)) == 6
    mg = bulk("Mg", "hcp", a=3.21, c=5.21)  # 12 NN per atom
    assert np.all(degree(mg, find_bonds(mg)) == 12)
    cscl = bulk("CsCl", "cesiumchloride", a=4.12)  # Cs-Cs at 4.12 must not bond
    b = find_bonds(cscl)
    assert set(pair_counter(cscl, b)) == {("Cl", "Cs")} and len(b) == 8
    gaas = bulk("GaAs", "zincblende", a=5.65)
    b = find_bonds(gaas)
    assert set(pair_counter(gaas, b)) == {("As", "Ga")} and len(b) == 4


@pytest.mark.parametrize("factory", [srtio3, si_conv, si_prim, nacl, fe_bcc, na3ps4, random_gas])
def test_bonds_translation_and_wrap_invariance(factory):
    at = factory()
    ref = bond_set(at, find_bonds(at))
    rng = np.random.default_rng(7)
    for _ in range(3):
        moved = at.copy()
        moved.translate(rng.normal(scale=3.0, size=3))
        unwrapped = bond_set(moved, find_bonds(moved))
        moved.wrap()
        wrapped = bond_set(moved, find_bonds(moved))
        assert unwrapped == ref
        assert wrapped == ref
        # jiggle individual atoms by whole lattice vectors
        jig = at.copy()
        jig.positions += rng.integers(-2, 3, size=(len(at), 3)) @ at.cell.array
        assert bond_set(jig, find_bonds(jig)) == ref


def test_bonds_robust_inputs():
    # far apart: nothing
    far = Atoms("Si2", positions=[[0, 0, 0], [10, 10, 10]], cell=[20, 20, 20], pbc=True)
    assert find_bonds(far) == []
    # empty / single isolated atom / NaN / zero cell
    assert find_bonds(Atoms(cell=[3, 3, 3], pbc=True)) == []
    assert find_bonds(Atoms("Ar", cell=[10, 10, 10], pbc=True)) == []
    assert find_bonds(Atoms("Si", positions=[[np.nan, 0, 0]], cell=[3, 3, 3], pbc=True)) == []
    assert isinstance(find_bonds(Atoms("Si2", positions=[[0, 0, 0], [0, 0, 2.3]])), list)
    # coincident atoms do not produce zero-length bonds
    at = Atoms("Si3", positions=[[0, 0, 0], [0, 0, 0], [0, 0, 2.3]], cell=[10, 10, 10], pbc=True)
    bonds = find_bonds(at)
    assert all(b.length > 0.01 for b in bonds) and len(bonds) == 2
    # unknown mode
    with pytest.raises(ValueError):
        find_bonds(si_prim(), mode="nope")


@pytest.mark.parametrize("seed", range(5))
def test_bonds_no_explosion(seed):
    rng = np.random.default_rng(seed)
    # crushed cell: 32 atoms in 3x3x3 A^3 (absurdly dense), and a very thin cell
    for at in (
        Atoms(["Fe"] * 16 + ["O"] * 16, scaled_positions=rng.random((32, 3)), cell=[3, 3, 3], pbc=True),
        Atoms(["Cu"] * 32, scaled_positions=rng.random((32, 3)), cell=[[8, 0, 0], [0, 8, 0], [0, 0, 0.4]], pbc=True),
        random_gas(seed=seed),
        Atoms(["Li"] * 32, scaled_positions=rng.random((32, 3)) * 0.05, cell=[12, 12, 12], pbc=True),
    ):
        snap = snapshot(at)
        bonds = find_bonds(at)
        assert_unchanged(at, snap)
        check_bond_invariants(at, bonds)
        deg = degree(at, bonds)
        assert deg.max() <= 16
        assert len(bonds) <= 8 * len(at)


def test_bonds_unstrained_strain_ratio_sign():
    at = srtio3()
    at.set_cell(at.cell * 0.9, scale_atoms=True)
    for b in find_bonds(at):
        rsum = b.length / (1 + b.strain_ratio)
        assert rsum > 0
    tio = [b for b in find_bonds(at) if 1 in (b.source_index, b.target_index)]
    assert all(b.strain_ratio < 0 for b in tio)


# ---------------------------------------------------------------------------
# find_bonds (crystalnn)
# ---------------------------------------------------------------------------


def test_bonds_crystalnn_srtio3_and_si():
    pytest.importorskip("pymatgen.analysis.local_env")
    at = srtio3()
    snap = snapshot(at)
    bonds = find_bonds(at, mode="crystalnn")
    assert_unchanged(at, snap)
    check_bond_invariants(at, bonds)
    c = pair_counter(at, bonds)
    assert c[("O", "Ti")] == 6
    assert c[("Sr", "Ti")] == 0 and c[("O", "O")] == 0

    si = si_conv()
    bonds = find_bonds(si, mode="crystalnn")
    check_bond_invariants(si, bonds)
    assert len(bonds) == 16 and np.all(degree(si, bonds) == 4)

    sp = si_prim()
    bonds = find_bonds(sp, mode="crystalnn")
    check_bond_invariants(sp, bonds)
    assert bond_set(sp, bonds) == bond_set(sp, find_bonds(sp))

    moved = sp.copy()
    moved.translate([3.3, -1.7, 8.1])  # unwrapped positions still give consistent offsets
    check_bond_invariants(moved, find_bonds(moved, mode="crystalnn"))


# ---------------------------------------------------------------------------
# compute_rdf
# ---------------------------------------------------------------------------

R_GRID = 0.5 + 0.05 * np.arange(151)


def rdf_bruteforce(atoms: Atoms) -> np.ndarray:
    """Direct §4.5 formula with an explicit, generous image loop."""
    sigma, cutoff = 0.08, 8.0 + 4 * 0.08
    cell = atoms.cell.array
    pos = atoms.positions
    nat = len(atoms)
    vol = abs(np.linalg.det(cell))
    heights = [vol / np.linalg.norm(np.cross(cell[(k + 1) % 3], cell[(k + 2) % 3])) for k in range(3)]
    span = [int(math.ceil(cutoff / h)) + 2 for h in heights]
    ds = []
    for i in range(nat):
        for j in range(nat):
            for na in range(-span[0], span[0] + 1):
                for nb in range(-span[1], span[1] + 1):
                    for nc in range(-span[2], span[2] + 1):
                        if i == j and na == nb == nc == 0:
                            continue
                        v = pos[j] + np.array([na, nb, nc]) @ cell - pos[i]
                        d = np.linalg.norm(v)
                        if d <= cutoff:
                            ds.append(d)
    ds = np.array(ds)
    gauss = np.exp(-0.5 * ((R_GRID[:, None] - ds[None, :]) / sigma) ** 2) / math.sqrt(2 * math.pi * sigma**2)
    return vol / (4 * math.pi * R_GRID**2 * nat**2) * gauss.sum(axis=1)


def local_maxima(r: np.ndarray, g: np.ndarray, min_height: float = 0.5) -> list[float]:
    """Parabolically refined positions of local maxima."""
    out = []
    for k in range(1, len(g) - 1):
        if g[k] > g[k - 1] and g[k] >= g[k + 1] and g[k] > min_height:
            y0, y1, y2 = g[k - 1], g[k], g[k + 1]
            denom = y0 - 2 * y1 + y2
            off = 0.5 * (y0 - y2) / denom if denom != 0 else 0.0
            out.append(r[k] + off * (r[1] - r[0]))
    return out


def test_rdf_grid_and_shape():
    for at in (srtio3(), Atoms("Fe", cell=[2.5, 2.5, 2.5], pbc=True), Atoms(cell=[3, 3, 3], pbc=True),
               Atoms("H", positions=[[0, 0, 0]])):
        r, g = compute_rdf(at)
        assert r.shape == (151,) and g.shape == (151,)
        assert np.allclose(r, R_GRID)
        assert np.isfinite(g).all()


@pytest.mark.parametrize("factory", [srtio3, si_prim, fe_bcc, lambda: random_gas(12, seed=3)])
def test_rdf_matches_bruteforce(factory):
    at = factory()
    snap = snapshot(at)
    _, g = compute_rdf(at)
    assert_unchanged(at, snap)
    ref = rdf_bruteforce(at)
    assert np.max(np.abs(g - ref)) <= 1e-3 * np.max(ref)


def test_rdf_peaks_si():
    r, g = compute_rdf(si_conv())
    peaks = local_maxima(r, g)
    for expected, got in zip([2.352, 3.840, 4.503], peaks[:3]):
        assert got == pytest.approx(expected, abs=0.02)
    # primitive and conventional Si agree
    _, g2 = compute_rdf(si_prim())
    assert np.allclose(g, g2, rtol=1e-6, atol=1e-9)


def test_rdf_peak_srtio3():
    r, g = compute_rdf(srtio3())
    peaks = local_maxima(r, g)
    assert peaks[0] == pytest.approx(1.9525, abs=0.02)
    assert np.all(g[r < 1.5] < 1e-6)


def test_rdf_supercell_identical():
    for at in (srtio3(), si_prim()):
        _, g1 = compute_rdf(at)
        _, g8 = compute_rdf(at.repeat((2, 2, 2)))
        assert np.allclose(g1, g8, rtol=1e-6, atol=1e-9)


def test_rdf_gas_tends_to_one():
    vals = []
    for seed in range(4):
        r, g = compute_rdf(random_gas(32, seed=seed, density=0.06))
        vals.append(g[(r >= 5.0) & (r <= 8.0)].mean())
    assert np.mean(vals) == pytest.approx(1.0, abs=0.05)
    assert all(abs(v - 1) < 0.1 for v in vals)


def test_rdf_translation_wrap_invariance():
    at = random_gas(20, seed=5)
    _, g0 = compute_rdf(at)
    moved = at.copy()
    moved.translate([1.3, -7.2, 4.4])
    _, g1 = compute_rdf(moved)
    moved.wrap()
    _, g2 = compute_rdf(moved)
    assert np.allclose(g0, g1, rtol=1e-8, atol=1e-10)
    assert np.allclose(g0, g2, rtol=1e-8, atol=1e-10)


def test_rdf_single_atom_cell():
    r, g = compute_rdf(Atoms("Po", cell=[3.35] * 3, pbc=True))
    assert g.shape == (151,)
    assert local_maxima(r, g)[0] == pytest.approx(3.35, abs=0.02)


# ---------------------------------------------------------------------------
# Timings (smoke-level budgets; real numbers are reported separately)
# ---------------------------------------------------------------------------


def _median_ms(fn, n: int = 50) -> float:
    import time

    fn()
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t)
    return 1e3 * float(np.median(ts))


def test_fast_paths_are_fast():
    big = tri_32()
    assert len(big) == 32
    # generous budgets so slow CI machines do not flake; target is a few ms
    assert _median_ms(lambda: find_bonds(big)) < 20
    assert _median_ms(lambda: compute_rdf(big)) < 20
    li = _preset("li3ps4")
    assert _median_ms(lambda: find_bonds(li)) < 20
