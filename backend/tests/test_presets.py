"""Tests for the curated preset library (spec §5.3, §7 Phase 1 step 3)."""

from __future__ import annotations

import math

import numpy as np
import pytest
import spglib
from ase.neighborlist import neighbor_list
from pymatgen.core import Composition

from app.presets import DEFAULT_PRESET_ID, PresetInfo, list_presets, load_preset

SPEC_ORDER = ["srtio3", "li3ps4", "si", "batio3"]
CUBIC_A = {"srtio3": 3.905, "si": 5.431, "batio3": 4.00}
ALL_IDS = pytest.mark.parametrize("preset_id", SPEC_ORDER)


def _info(preset_id: str) -> PresetInfo:
    return {p.id: p for p in list_presets()}[preset_id]


def _spglib_dataset(atoms, symprec=1e-2):
    cell = (atoms.cell[:], atoms.get_scaled_positions(), atoms.numbers)
    return spglib.get_symmetry_dataset(cell, symprec=symprec)


def _pairs(atoms, cutoff):
    i, j, d = neighbor_list("ijd", atoms, cutoff)
    return i, j, d, np.array(atoms.get_chemical_symbols())


# --------------------------------------------------------------------------- #
# API behaviour
# --------------------------------------------------------------------------- #


def test_order_and_default():
    infos = list_presets()
    assert [p.id for p in infos] == SPEC_ORDER
    assert all(isinstance(p, PresetInfo) for p in infos)
    assert DEFAULT_PRESET_ID == "srtio3"
    assert DEFAULT_PRESET_ID in SPEC_ORDER


def test_list_presets_is_json_serialisable():
    for p in list_presets():
        dumped = p.model_dump(mode="json")
        assert PresetInfo.model_validate(dumped) == p
        assert dumped["expected_stability"] in {"stable", "unstable", "unknown"}
        assert dumped["description"] and dumped["provenance"] and dumped["name"]


def test_unknown_id_raises_keyerror():
    with pytest.raises(KeyError):
        load_preset("unobtainium")
    with pytest.raises(KeyError):
        load_preset("SrTiO3")  # ids are case-sensitive


@ALL_IDS
def test_load_returns_independent_copies(preset_id):
    first = load_preset(preset_id)
    reference = first.copy()
    first.positions += 0.37
    first.cell[:] = first.cell[:] * 1.1
    first.numbers[0] = 1
    del first[-1]
    second = load_preset(preset_id)
    assert second is not first
    assert len(second) == len(reference)
    np.testing.assert_array_equal(second.numbers, reference.numbers)
    np.testing.assert_allclose(second.positions, reference.positions, atol=0)
    np.testing.assert_allclose(second.cell[:], reference.cell[:], atol=0)


@ALL_IDS
def test_pbc_calculator_and_wrapping(preset_id):
    atoms = load_preset(preset_id)
    assert atoms.pbc.tolist() == [True, True, True]
    assert atoms.calc is None
    frac = atoms.get_scaled_positions(wrap=False)
    assert np.all(frac >= 0.0)
    assert np.all(frac < 1.0)


# --------------------------------------------------------------------------- #
# Metadata matches structure
# --------------------------------------------------------------------------- #


@ALL_IDS
def test_metadata_matches_structure(preset_id):
    info = _info(preset_id)
    atoms = load_preset(preset_id)
    assert info.num_atoms == len(atoms)
    assert info.formula == Composition(atoms.get_chemical_formula()).reduced_formula
    ds = _spglib_dataset(atoms, symprec=1e-2)
    assert ds.number == info.spacegroup_number
    assert ds.international == info.spacegroup_symbol
    # Symmetry must not depend on a loose tolerance: the data are exact/ideal.
    assert _spglib_dataset(atoms, symprec=1e-4).number == info.spacegroup_number


def test_expected_stability_values():
    assert {p.id: p.expected_stability for p in list_presets()} == {
        "srtio3": "unstable",
        "li3ps4": "unknown",
        "si": "stable",
        "batio3": "unstable",
    }


@pytest.mark.parametrize("preset_id,a", sorted(CUBIC_A.items()))
def test_cubic_lattice_constants_exact(preset_id, a):
    atoms = load_preset(preset_id)
    np.testing.assert_allclose(atoms.cell[:], np.eye(3) * a, rtol=0, atol=1e-12)


def test_cubic_atom_counts():
    assert len(load_preset("srtio3")) == 5
    assert len(load_preset("batio3")) == 5
    assert len(load_preset("si")) == 8


# --------------------------------------------------------------------------- #
# Geometry sanity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("preset_id,a_site", [("srtio3", "Sr"), ("batio3", "Ba")])
def test_perovskite_geometry(preset_id, a_site):
    atoms = load_preset(preset_id)
    a = CUBIC_A[preset_id]
    assert sorted(atoms.get_chemical_symbols()) == sorted([a_site, "Ti", "O", "O", "O"])
    i, j, d, sym = _pairs(atoms, 0.75 * a)
    ti = np.where(sym == "Ti")[0]
    assert len(ti) == 1
    m = (i == ti[0]) & (sym[j] == "O")
    assert m.sum() == 6
    np.testing.assert_allclose(d[m], a / 2, atol=1e-10)
    # Nothing (Ti-Ti, A-O, ...) closer than the Ti-O bond.
    assert d.min() == pytest.approx(a / 2, abs=1e-10)
    # A-site is 12-coordinated by O at a/sqrt(2).
    a_idx = np.where(sym == a_site)[0][0]
    m_a = (i == a_idx) & (sym[j] == "O") & (d < a / math.sqrt(2) + 1e-6)
    assert m_a.sum() == 12


def test_si_geometry():
    atoms = load_preset("si")
    a = CUBIC_A["si"]
    bond = a * math.sqrt(3) / 4
    assert bond == pytest.approx(2.3517, abs=1e-4)
    i, j, d, _ = _pairs(atoms, 2.6)
    assert d.min() == pytest.approx(bond, abs=1e-10)
    for k in range(len(atoms)):
        m = i == k
        assert m.sum() == 4
        np.testing.assert_allclose(d[m], bond, atol=1e-10)


def test_li3ps4_composition_and_cell():
    atoms = load_preset("li3ps4")
    assert Composition(atoms.get_chemical_formula()) == Composition("Li12P4S16")
    assert len(atoms) == 32
    a, b, c, alpha, beta, gamma = atoms.cell.cellpar()
    assert (alpha, beta, gamma) == pytest.approx((90.0, 90.0, 90.0), abs=1e-10)
    # Experimental beta-Li3PS4 (Homma et al. 2011): a~12.82, b~8.22, c~6.12 A, V~645 A^3.
    assert a == pytest.approx(12.82, rel=0.05)
    assert b == pytest.approx(8.22, rel=0.05)
    assert c == pytest.approx(6.12, rel=0.05)
    assert 600.0 < atoms.get_volume() < 700.0


def test_li3ps4_ps4_tetrahedra():
    atoms = load_preset("li3ps4")
    i, j, d, sym = _pairs(atoms, 2.6)
    p_idx = np.where(sym == "P")[0]
    assert len(p_idx) == 4
    for k in p_idx:
        m = (i == k) & (sym[j] == "S")
        assert m.sum() == 4, f"P{k} has {m.sum()} S neighbours within 2.6 A"
        assert np.all((d[m] > 2.0) & (d[m] < 2.1)), d[m]
    # Every S belongs to exactly one PS4 unit (isolated tetrahedra, no P-S-P bridges).
    s_idx = np.where(sym == "S")[0]
    for k in s_idx:
        assert ((i == k) & (sym[j] == "P")).sum() == 1


def test_li3ps4_no_short_contacts():
    atoms = load_preset("li3ps4")
    i, j, d, sym = _pairs(atoms, 4.0)
    assert d.min() >= 1.9
    li_s = d[(sym[i] == "Li") & (sym[j] == "S")]
    # Shortest Li-S in the source refinement is 2.179 A (Li1, 8d; its y coordinate
    # carries a large uncertainty at 637 K), hence 2.15 rather than 2.2.
    assert li_s.min() >= 2.15
    li_li = d[(sym[i] == "Li") & (sym[j] == "Li")]
    assert li_li.min() >= 2.4
    # Every Li has at least 4 S neighbours within 2.8 A.
    for k in np.where(sym == "Li")[0]:
        assert ((i == k) & (sym[j] == "S") & (d < 2.8)).sum() >= 4
