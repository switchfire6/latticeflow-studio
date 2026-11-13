"""Phase C1 analysis core: local structure, defects, ordering (fallback calculator, no network)."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymatgen.core import Lattice, Structure

from app.api import routes
from app.core import defects, hull_analyzer as ha, local_structure, ordering
from app.core.geometry_analysis import compute_partial_rdf, compute_rdf, find_bonds
from app.core.umlip_engine import UMLIPEngine
from app.presets import load_preset


def _tilted_srtio3(deg: float) -> "Atoms":  # noqa: F821
    """2x2x2 SrTiO3 with the O octahedra rotated anti-phase about c (the R-point a0a0c- pattern)."""
    s = load_preset("srtio3")
    a = s.cell[0, 0]
    sc = defects.make_supercell(s, np.diag([2, 2, 2]))
    pos = sc.positions.copy()
    sym = sc.get_chemical_symbols()
    ti = [p for p, e in zip(pos, sym) if e == "Ti"]
    for i, e in enumerate(sym):
        if e != "O":
            continue
        for t in ti:
            d = pos[i] - t
            d -= np.round(d / (2 * a)) * 2 * a
            if abs(np.linalg.norm(d) - a / 2) < 1e-3:
                break
        sign = (-1) ** int(np.floor(t / a).sum())
        pos[i] = t + d + np.cross([0.0, 0.0, np.radians(deg) * sign], d)
    sc.positions = pos
    return sc


# --------------------------------------------------------------------------- #
# local structure
# --------------------------------------------------------------------------- #
def test_bond_valence_partial_rdf_and_polyhedra():
    s = load_preset("srtio3")
    s.set_cell(s.cell * (3.94 / 3.905), scale_atoms=True)   # ~ the MACE-relaxed lattice constant
    res = local_structure.analyze(s, find_bonds(s))
    bvs = {site.element: site.bond_valence_sum for site in res.sites}
    assert bvs["Ti"] == pytest.approx(4.0, abs=0.15)
    assert bvs["Sr"] == pytest.approx(2.0, abs=0.15)
    assert res.global_instability_index < 0.1
    assert len(res.bond_ideal_lengths) == len(find_bonds(s))
    octa = [p for p in res.polyhedra if p.center_element == "Ti"]
    assert octa[0].shape == "octahedron" and octa[0].quadratic_elongation == pytest.approx(1.0, abs=1e-6)
    # partial RDFs: keys alphabetical, sum rule g = sum c_A c_B g_AB
    _, g = compute_rdf(s)
    c = {"O": 0.6, "Sr": 0.2, "Ti": 0.2}
    total = sum(c[a] * c[b] * (1 if a == b else 2) * np.array(v)
                for k, v in res.partial_rdf.items() for a, b in [k.split("-")])
    assert set(res.partial_rdf) == {"O-O", "O-Sr", "O-Ti", "Sr-Sr", "Sr-Ti", "Ti-Ti"}
    np.testing.assert_allclose(total, g, atol=1e-10)
    # a metal: no oxidation states, no BVS, no polyhedra
    si = load_preset("si")
    rs = local_structure.analyze(si, find_bonds(si), include_partial_rdf=False)
    assert rs.sites[0].oxidation_state is None and rs.global_instability_index is None
    assert rs.polyhedra == [] and rs.octahedral_tilts is None and rs.partial_rdf is None


def test_glazer_cubic_and_antiphase_tilt():
    cubic = local_structure.analyze(load_preset("srtio3"), find_bonds(load_preset("srtio3")))
    assert cubic.octahedral_tilts.glazer == "a0a0a0"
    tilted = _tilted_srtio3(4.0)
    t = local_structure.analyze(tilted, find_bonds(tilted)).octahedral_tilts
    assert t.glazer == "a0a0c-"
    assert t.tilt_angles_deg[2] == pytest.approx(4.0, abs=0.1)
    li = load_preset("li3ps4")
    assert local_structure.analyze(li, find_bonds(li)).octahedral_tilts is None


# --------------------------------------------------------------------------- #
# defects
# --------------------------------------------------------------------------- #
def test_supercell_label_and_add_atom():
    s = load_preset("srtio3")
    m = [[1, 1, 0], [-1, 1, 0], [0, 0, 2]]
    sc = defects.make_supercell(s, m)
    assert len(sc) == 20 and sc.get_volume() == pytest.approx(4 * s.get_volume())
    assert sc.get_chemical_symbols()[:4] == ["Sr"] * 4                 # atom-major order
    assert defects.supercell_label(np.diag([2, 2, 2])) == "2x2x2"
    assert defects.supercell_label(m) == "[[1,1,0],[-1,1,0],[0,0,2]]"
    with pytest.raises(ValueError):
        defects.make_supercell(s, [[0, 1, 0], [1, 0, 0], [0, 0, 1]])   # det < 0
    out = defects.add_atom(s, "Li", [0.25, 0.25, 0.25])
    assert len(out) == 6 and out.get_chemical_symbols()[-1] == "Li"
    with pytest.raises(ValueError):
        defects.add_atom(s, "Li", [0.99, 0.0, 0.01])                    # periodic image of Sr at the origin


def _box(symbols, cell, frac) -> Structure:
    return Structure(Lattice(cell), symbols, frac)


def test_formation_energy_vacancy(fallback_engine, tmp_path):
    refs = [
        ha.MPReference("mp-na", _box(["Na", "Na"], np.eye(3) * 4.23, [[0, 0, 0], [0.5, 0.5, 0.5]])),
        ha.MPReference("mp-cl2", _box(["Cl", "Cl"], np.eye(3) * 10.0, [[0, 0, 0], [0.199, 0, 0]])),
        ha.MPReference("mp-nacl", Structure.from_spacegroup("Fm-3m", Lattice.cubic(5.64), ["Na", "Cl"],
                                                            [[0, 0, 0], [0.5, 0.5, 0.5]]).get_primitive_structure()),
    ]
    fetcher = lambda chemsys, key: [r for r in refs if r.structure.composition.chemical_system in chemsys]  # noqa: E731
    bulk = ha._structure_to_atoms(refs[2].structure)
    cell = defects.make_supercell(bulk, np.diag([2, 2, 2]))
    del cell[[i for i, s in enumerate(cell.get_chemical_symbols()) if s == "Cl"][0]]
    cache = ha.HullCache(tmp_path, persist=False)
    with pytest.raises(ha.HullNoApiKeyError):
        defects.formation_energy(cell, bulk, fallback_engine, api_key=None, cache=cache)
    res = defects.formation_energy(cell, bulk, fallback_engine, api_key="k" * 32, fetcher=fetcher, cache=cache)
    assert res.defect_label == "V_Cl" and res.delta_n == {"Cl": -1}
    assert {"Na-rich", "Cl-rich"} <= set(res.formation_energies_ev)
    for name, ef in res.formation_energies_ev.items():
        mu = res.chemical_potentials[name]["Cl"]
        assert ef == pytest.approx(res.e_defect_ev - res.e_bulk_ev + mu, abs=1e-9)
    # Cl-rich: mu_Cl is the elemental reference -> the largest vacancy formation energy
    assert res.formation_energies_ev["Cl-rich"] == pytest.approx(max(res.formation_energies_ev.values()))
    assert res.min_image_distance == pytest.approx(2 * 5.64 / np.sqrt(2), rel=1e-6)
    assert any("< 10 A" in w for w in res.warnings)
    with pytest.raises(defects.DefectCancelled):
        defects.formation_energy(cell, bulk, fallback_engine, api_key=None, cache=cache, should_cancel=lambda: True)


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #
def test_substitute_fraction_symmetry_classes(fallback_engine):
    sc = defects.make_supercell(load_preset("srtio3"), np.diag([2, 2, 2]))
    atoms, res = ordering.substitute_fraction(sc, "Sr", "Ba", 0.25, fallback_engine, seed=1)
    # 2 of 8 sites of a simple-cubic 2x2x2 sublattice: nearest, face- and body-diagonal pairs
    assert res.n_substituted == 2 and res.n_tried == 3
    assert atoms.get_chemical_symbols().count("Ba") == 2
    assert res.chosen_energy_per_atom == res.energies_per_atom[0] == min(res.energies_per_atom)


def test_ordering_route(fallback_engine):
    previous = UMLIPEngine._instance
    UMLIPEngine.set_instance(fallback_engine)
    try:
        app = FastAPI()
        routes.register_error_handlers(app)
        app.include_router(routes.router)
        client = TestClient(app)
        dis = Structure.from_spacegroup("Fm-3m", Lattice.cubic(5.4), [{"Li": 1 / 3, "Na": 2 / 3}, "Cl"],
                                        [[0, 0, 0], [0.5, 0.5, 0.5]])
        r = client.post("/api/ordering", json={"cif": dis.to(fmt="cif"), "n_candidates": 3})
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["candidates"]) == 3 and body["candidates"][0]["relative_energy_mev_per_atom"] == 0.0
        assert all(c["formula"] == "Na2LiCl3" for c in body["candidates"])
        r = client.post("/api/ordering", json={"cif": dis.to(fmt="cif"), "max_atoms": 8})
        assert r.status_code == 422 and r.json()["code"] == "STRUCTURE_TOO_LARGE"
        r = client.post("/api/ordering", json={"cif": "not a cif"})
        assert r.status_code == 422 and r.json()["code"] == "INVALID_STRUCTURE"
    finally:
        UMLIPEngine.set_instance(previous)
