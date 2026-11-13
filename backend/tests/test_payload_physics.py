"""Phase A physics (IMPROVEMENT_SUGGESTIONS.md section 3.1-3.2, 4.1-4.3, 5.1, 5.11).

* payload enrichment (pressure, density, crystal system, Z, symprec, Wyckoff labels);
* structure text import (CIF / POSCAR / extxyz, format sniffing);
* symmetry refinement and standard cells; symmetrised CIF; extxyz export; export route;
* phonon DOS / projected DOS / harmonic thermodynamics;
* hull: relax the candidate first.

Tests parametrised over ``engine`` run on both backends; MACE-only physics
uses ``mace_engine`` and is skipped under ``LATTICEFLOW_SKIP_MACE``.
"""

from __future__ import annotations

import io
import time

import numpy as np
import pytest
from ase import Atoms
from ase.build import bulk
from ase.io import read as ase_read
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymatgen.core import Lattice, Structure

from app.api import routes
from app.core import geometry_analysis as geo
from app.core import hull_analyzer as ha
from app.core.phonon_engine import PhononCancelled, run_phonon_analysis
from app.core.structure_io import (
    StructureIOError,
    atoms_from_cif,
    atoms_from_text,
    atoms_to_cif,
    atoms_to_extxyz,
    atoms_to_poscar,
    convert_cell,
    refine_atoms,
    sniff_format,
)
from app.core.umlip_engine import EV_PER_A3_TO_GPA, UMLIPEngine
from app.presets import load_preset
from app.schemas.crystal import CrystalStatePayload
from app.schemas.messages import F_TOL_THZ

KB_EV = 8.617333262e-5


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def same_structure(a: Atoms, b: Atoms, atol: float = 1e-6, cell_atol: float = 1e-6) -> None:
    """Same cell and the same set of (element, fractional position) sites, in any order."""
    np.testing.assert_allclose(a.cell.array, b.cell.array, atol=cell_atol, rtol=0)
    assert sorted(a.numbers) == sorted(b.numbers)
    fa, fb = a.get_scaled_positions(wrap=True), b.get_scaled_positions(wrap=True)
    unmatched = list(range(len(b)))
    for z, f in zip(a.numbers, fa):
        for j in unmatched:
            d = fb[j] - f
            d -= np.round(d)
            if b.numbers[j] == z and np.all(np.abs(d) < atol):
                unmatched.remove(j)
                break
        else:
            raise AssertionError(f"site Z={z} {f} has no counterpart")
    assert not unmatched


def noisy(atoms: Atoms, pos: float = 1e-3, cell: float = 3e-4, seed: int = 0) -> Atoms:
    rng = np.random.default_rng(seed)
    out = atoms.copy()
    out.positions += rng.normal(scale=pos, size=out.positions.shape)
    out.set_cell(out.cell.array + rng.normal(scale=cell, size=(3, 3)), scale_atoms=True)
    return out


def max_displacement(a: Atoms, b: Atoms) -> float:
    """Largest minimum-image displacement between equally ordered atoms (same cell assumed)."""
    d = b.get_scaled_positions(wrap=True) - a.get_scaled_positions(wrap=True)
    d -= np.round(d)
    return float(np.linalg.norm(d @ b.cell.array, axis=1).max())


# --------------------------------------------------------------------------
# payload enrichment
# --------------------------------------------------------------------------
def test_srtio3_payload_fields(engine):
    p = engine.evaluate(load_preset("srtio3"))
    assert [s.element for s in p.atoms] == ["Sr", "Ti", "O", "O", "O"]
    assert [s.wyckoff for s in p.atoms] == ["1a", "1b", "3c", "3c", "3c"]
    assert p.spacegroup_symbol == "Pm-3m" and p.crystal_system == "cubic"
    assert p.formula_units == 1
    assert p.symprec == pytest.approx(1e-2)
    assert p.lattice.a == pytest.approx(3.905)
    assert p.density_g_cm3 == pytest.approx(5.117, abs=0.005)   # 183.49 g/mol in 59.55 A^3
    assert p.pressure_gpa == pytest.approx(-np.mean(p.stress_gpa[:3]), abs=1e-12)
    CrystalStatePayload(**p.model_dump())  # round-trips the contract


@pytest.mark.parametrize(
    "preset_id, system, z, wyckoff_first",
    [("si", "cubic", 8, "8a"), ("li3ps4", "orthorhombic", 4, "8d"), ("batio3", "cubic", 1, "1a")],
)
def test_other_presets_payload_fields(fallback_engine, preset_id, system, z, wyckoff_first):
    p = fallback_engine.evaluate(load_preset(preset_id))
    assert p.crystal_system == system
    assert p.formula_units == z
    assert p.atoms[0].wyckoff == wyckoff_first
    assert all(s.wyckoff is not None for s in p.atoms)
    if preset_id == "si":
        assert p.density_g_cm3 == pytest.approx(2.329, abs=0.002)


def test_wyckoff_multiplicity_is_cell_independent():
    """ITA multiplicities: a supercell or a primitive setting reports the standard-cell labels."""
    sc = geo.symmetry_info(load_preset("srtio3").repeat((2, 1, 2)))
    assert sc.symbol == "Pm-3m" and sorted(set(sc.wyckoffs)) == ["1a", "1b", "3c"]
    prim = geo.symmetry_info(bulk("Si", "diamond", 5.431))
    assert prim.number == 227 and len(set(prim.wyckoffs)) == 1 and prim.wyckoffs[0][0] == "8"
    rock = geo.symmetry_info(bulk("NaCl", "rocksalt", 5.64))
    assert sorted(rock.wyckoffs) == ["4a", "4b"]


def test_pressure_sign(engine):
    base = load_preset("srtio3")
    pressures = []
    for scale in (0.94, 1.0, 1.06):
        at = base.copy()
        at.set_cell(base.cell.array * scale, scale_atoms=True)
        pressures.append(engine.evaluate(at).pressure_gpa)
    assert pressures[0] > pressures[1] > pressures[2]   # compressing the cell raises P
    assert pressures[0] > 0


def test_mace_preset_pressures(mace_engine):
    """IMPROVEMENT_SUGGESTIONS.md table: the presets sit at a few GPa of positive pressure."""
    assert mace_engine.evaluate(load_preset("srtio3")).pressure_gpa == pytest.approx(5.1, abs=1.0)
    assert mace_engine.evaluate(load_preset("si")).pressure_gpa == pytest.approx(1.4, abs=1.0)


def test_payload_for_non_periodic_and_failed_symmetry(fallback_engine):
    molecule = Atoms("H2O", positions=[[0, 0, 0], [0.96, 0, 0], [-0.24, 0.93, 0]])
    p = CrystalStatePayload(**fallback_engine.evaluate(molecule).model_dump())
    assert p.pressure_gpa is None and p.density_g_cm3 is None
    assert p.crystal_system is None and all(s.wyckoff is None for s in p.atoms)
    assert (p.spacegroup_symbol, p.spacegroup_number) == ("P1", 1)
    assert p.formula_units == 1

    info = geo.symmetry_info(Atoms("Si", positions=[[np.nan, 0, 0]], cell=[3, 3, 3], pbc=True))
    assert not info.ok and info.wyckoffs is None and info.crystal_system is None


def test_crystal_system_ranges():
    expect = {1: "triclinic", 2: "triclinic", 3: "monoclinic", 15: "monoclinic", 16: "orthorhombic",
              74: "orthorhombic", 75: "tetragonal", 142: "tetragonal", 143: "trigonal", 167: "trigonal",
              168: "hexagonal", 194: "hexagonal", 195: "cubic", 230: "cubic"}
    for n, name in expect.items():
        assert geo.crystal_system_of(n) == name
    assert geo.crystal_system_of(0) is None and geo.crystal_system_of(231) is None


def test_symmetry_info_is_cheap():
    at = load_preset("li3ps4")
    geo.symmetry_info(at)
    t0 = time.perf_counter()
    for _ in range(20):
        geo.symmetry_info(at)
    per_call = (time.perf_counter() - t0) / 20
    assert per_call < 0.02, per_call   # ~1 ms; the per-frame budget is a few ms


# --------------------------------------------------------------------------
# text import
# --------------------------------------------------------------------------
@pytest.mark.parametrize("preset_id", ["srtio3", "li3ps4", "si"])
def test_poscar_round_trip(preset_id):
    at = load_preset(preset_id)
    text = atoms_to_poscar(at)
    assert sniff_format(text) == "poscar"
    for fmt in ("auto", "poscar"):
        back = atoms_from_text(text, fmt)
        assert back.get_chemical_symbols() == at.get_chemical_symbols()
        assert max_displacement(at, back) < 1e-8
        np.testing.assert_allclose(back.cell.array, at.cell.array, atol=1e-8)


def test_extxyz_round_trip_and_last_frame():
    at = load_preset("li3ps4")
    text = atoms_to_extxyz(at)
    assert sniff_format(text) == "extxyz"
    back = atoms_from_text(text)
    assert back.get_chemical_symbols() == at.get_chemical_symbols()
    assert max_displacement(at, back) < 1e-8
    assert back.calc is None and np.all(back.pbc)

    second = load_preset("srtio3")
    multi = atoms_to_extxyz(at) + atoms_to_extxyz(second)
    last = atoms_from_text(multi, "extxyz")
    assert last.get_chemical_formula() == second.get_chemical_formula()


def test_cif_text_import_sniffed():
    at = load_preset("srtio3")
    cif = atoms_to_cif(at)
    assert sniff_format(cif) == "cif"
    same_structure(atoms_from_text(cif), atoms_from_cif(cif))
    same_structure(atoms_from_text(cif, "cif"), at)


def test_text_import_rejections():
    plain_xyz = "3\nwater\nO 0 0 0\nH 0.96 0 0\nH -0.24 0.93 0\n"
    assert sniff_format(plain_xyz) == "xyz"
    for fmt in ("auto", "extxyz"):
        with pytest.raises(StructureIOError) as err:
            atoms_from_text(plain_xyz, fmt)
        assert err.value.code == "INVALID_STRUCTURE"
    for bad in ("", "   \n", "this is not a structure\n1 2 3\n"):
        with pytest.raises(StructureIOError):
            atoms_from_text(bad)
    with pytest.raises(StructureIOError) as err:
        atoms_from_text(atoms_to_poscar(load_preset("si")), "pdb")
    assert err.value.code == "INVALID_PAYLOAD"
    big = load_preset("li3ps4").repeat((2, 2, 1))   # 128 atoms is the limit
    assert len(atoms_from_text(atoms_to_poscar(big))) == 128
    with pytest.raises(StructureIOError) as err:
        atoms_from_text(atoms_to_extxyz(load_preset("li3ps4").repeat((2, 2, 2))))
    assert err.value.code == "STRUCTURE_TOO_LARGE"


def test_text_import_wraps_positions():
    at = load_preset("srtio3")
    at.positions += [5.0, -7.0, 11.0]
    back = atoms_from_text(atoms_to_extxyz(at))
    s = back.get_scaled_positions(wrap=False)
    assert np.all(s >= 0) and np.all(s < 1)


# --------------------------------------------------------------------------
# refinement and standard cells
# --------------------------------------------------------------------------
@pytest.mark.parametrize("preset_id, number", [("srtio3", 221), ("si", 227), ("li3ps4", 62)])
def test_refine_noisy_preset(preset_id, number):
    clean = load_preset(preset_id)
    shaken = noisy(clean)
    assert geo.get_spacegroup(shaken, symprec=1e-5)[1] == 1
    refined = refine_atoms(shaken)
    assert len(refined) == len(clean)
    assert list(refined.numbers) == list(clean.numbers)            # order preserved
    assert geo.get_spacegroup(refined, symprec=1e-6)[1] == number  # exactly symmetric
    assert max_displacement(shaken, refined) < 0.01                # noise only
    np.testing.assert_allclose(refined.cell.array, clean.cell.array, atol=5e-3)  # same orientation
    assert shaken.positions[0] is not refined.positions[0]


def test_refine_keeps_orientation_and_order_of_rotated_primitive_cell():
    si = bulk("Si", "diamond", 5.431)
    c, s = np.cos(0.4), np.sin(0.4)
    rot = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    si.set_cell(si.cell.array @ rot.T, scale_atoms=True)
    refined = refine_atoms(noisy(si, cell=0.0))
    assert len(refined) == 2
    np.testing.assert_allclose(refined.cell.array, si.cell.array, atol=1e-9)
    assert geo.get_spacegroup(refined, symprec=1e-6) == ("Fd-3m", 227)


def test_refine_of_clean_structure_is_identity():
    for preset_id in ("srtio3", "si", "li3ps4", "batio3"):
        at = load_preset(preset_id)
        r = refine_atoms(at)
        assert max_displacement(at, r) < 1e-9
        np.testing.assert_allclose(r.cell.array, at.cell.array, atol=1e-9)


def test_conventional_and_primitive_cells():
    conv = load_preset("si")
    prim = refine_atoms(conv, cell="primitive")
    assert len(prim) == 2 and prim.get_volume() == pytest.approx(conv.get_volume() / 4)
    back = refine_atoms(prim, cell="conventional")
    assert len(back) == 8 and back.get_volume() == pytest.approx(conv.get_volume())
    assert geo.get_spacegroup(back)[1] == 227
    # Supercell of SrTiO3 reduces to its 5-atom cell; species grouped in first-appearance order.
    sc = load_preset("srtio3").repeat((2, 2, 2))
    small = refine_atoms(sc, cell="primitive")
    assert len(small) == 5 and small.get_chemical_symbols() == ["Sr", "Ti", "O", "O", "O"]
    assert len(convert_cell(sc, "as_is")) == 40
    assert len(convert_cell(bulk("NaCl", "rocksalt", 5.64), "conventional")) == 8


def test_refine_failure_raises(monkeypatch):
    monkeypatch.setattr(geo, "symmetry_dataset", lambda atoms, symprec=1e-2: None)
    with pytest.raises(StructureIOError) as err:
        refine_atoms(load_preset("srtio3"))
    assert err.value.code == "INVALID_STRUCTURE"
    with pytest.raises(StructureIOError):
        refine_atoms(load_preset("srtio3"), cell="primitive")
    # the CIF writer falls back to P1 instead of failing
    assert "'x, y, z'" in atoms_to_cif(load_preset("srtio3"), symprec=1e-2)
    with pytest.raises(StructureIOError):
        refine_atoms(load_preset("srtio3"), cell="nonsense")


# --------------------------------------------------------------------------
# symmetrised CIF / extxyz export
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "factory",
    [
        lambda: load_preset("srtio3"),
        lambda: load_preset("li3ps4"),
        lambda: load_preset("si"),
        lambda: bulk("Si", "diamond", 5.431),
        lambda: bulk("ZnO", "wurtzite", a=3.25, c=5.2),
        lambda: load_preset("srtio3").repeat((2, 1, 1)),
    ],
)
def test_symmetrised_cif_round_trip(factory):
    at = factory()
    text = atoms_to_cif(at, symprec=1e-2)
    number = geo.get_spacegroup(at)[1]
    assert f"_symmetry_Int_Tables_number   {number}" in text
    back = atoms_from_cif(text)
    assert len(back) == len(at)
    assert geo.get_spacegroup(back, symprec=1e-6)[1] == number
    # CifParser rebuilds the lattice from a, b, c, alpha, beta, gamma: compare in that frame.
    matrix = Lattice.from_parameters(*at.cell.cellpar()).matrix
    frame = Atoms(numbers=at.numbers, scaled_positions=at.get_scaled_positions(), cell=matrix, pbc=True)
    same_structure(back, frame, atol=1e-6)


def test_symmetrised_cif_is_asymmetric_unit():
    text = atoms_to_cif(load_preset("srtio3"), symprec=1e-2)
    assert "Pm-3m" in text
    sites = [ln for ln in text.splitlines() if ln.strip().split()[:1] in (["Sr"], ["Ti"], ["O"])]
    assert len(sites) == 3                     # Sr 1a, Ti 1b, O 3c
    p1 = atoms_to_cif(load_preset("srtio3"))   # symprec None: P1, every atom listed
    assert "P 1" in p1 and "'x, y, z'" in p1


def test_symmetrised_cif_with_arbitrary_origin_and_noise():
    at = noisy(load_preset("li3ps4"), seed=3)
    at.positions += [0.123, 0.456, 0.789]       # origin far from any special position
    back = atoms_from_cif(atoms_to_cif(at, symprec=1e-2))
    assert len(back) == len(at)
    assert geo.get_spacegroup(back, symprec=1e-5)[1] == 62


def test_extxyz_export_content(fallback_engine):
    at = load_preset("li3ps4")
    p = fallback_engine.evaluate(at)
    forces = np.array([s.force for s in p.atoms])
    energy = p.energy_per_atom * len(at)
    text = atoms_to_extxyz(at, energy=energy, forces=forces, stress_voigt_gpa=p.stress_gpa)
    first, second = text.splitlines()[:2]
    assert first.strip() == "32"
    assert "Lattice=" in second and "energy=" in second and "stress=" in second and "forces:R:3" in second
    back = ase_read(io.StringIO(text), format="extxyz")
    assert back.get_potential_energy() == pytest.approx(energy, abs=1e-6)
    np.testing.assert_allclose(back.get_forces(), forces, atol=1e-6)
    np.testing.assert_allclose(back.get_stress(voigt=True) * EV_PER_A3_TO_GPA, p.stress_gpa, atol=1e-6)
    plain = atoms_to_extxyz(at)
    assert "energy=" not in plain.splitlines()[1]


# --------------------------------------------------------------------------
# export route
# --------------------------------------------------------------------------
@pytest.fixture
def client(fallback_engine):
    previous = UMLIPEngine._instance
    UMLIPEngine.set_instance(fallback_engine)
    app = FastAPI()
    routes.register_error_handlers(app)
    app.include_router(routes.router)
    yield TestClient(app)
    UMLIPEngine.set_instance(previous)


def _crystal(engine, preset_id="si"):
    return engine.evaluate(load_preset(preset_id)).model_dump()


def test_export_route_formats_and_cells(client, fallback_engine):
    crystal = _crystal(fallback_engine, "si")
    resp = client.post("/api/export/cif", json={"crystal": crystal})
    assert resp.status_code == 200 and "Fd-3m" in resp.text          # symmetrised by default
    resp = client.post("/api/export/cif", json={"crystal": crystal, "symmetrize": False})
    assert "P 1" in resp.text
    resp = client.post("/api/export/cif", json={"crystal": crystal, "cell": "primitive"})
    assert len(atoms_from_cif(resp.text)) == 2
    resp = client.post("/api/export/cif", json={"crystal": crystal, "format": "poscar", "cell": "primitive"})
    assert resp.status_code == 200 and len(ase_read(io.StringIO(resp.text), format="vasp")) == 2

    resp = client.post("/api/export/cif", json={"crystal": crystal, "format": "extxyz"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == 'attachment; filename="Si.extxyz"'
    back = ase_read(io.StringIO(resp.text), format="extxyz")
    assert back.get_potential_energy() == pytest.approx(crystal["energy_per_atom"] * 8, abs=1e-6)
    np.testing.assert_allclose(back.get_forces(), [s["force"] for s in crystal["atoms"]], atol=1e-6)


def test_export_route_rejections(client, fallback_engine):
    crystal = _crystal(fallback_engine, "si")
    for cell in ("conventional", "primitive"):
        resp = client.post("/api/export/cif", json={"crystal": crystal, "format": "extxyz", "cell": cell})
        assert resp.status_code == 422 and resp.json()["code"] == "INVALID_PAYLOAD"
    resp = client.post("/api/export/cif", json={"crystal": crystal, "symprec": 0})
    assert resp.status_code == 422 and resp.json()["code"] == "INVALID_PAYLOAD"
    bad = dict(crystal, atoms=[dict(crystal["atoms"][0], force=[0.0, 1.0])] + crystal["atoms"][1:])
    resp = client.post("/api/export/cif", json={"crystal": bad, "format": "extxyz"})
    assert resp.status_code == 422 and resp.json()["code"] == "INVALID_PAYLOAD"


# --------------------------------------------------------------------------
# phonon DOS and thermodynamics
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fallback_phonons(fallback_engine):
    return {pid: run_phonon_analysis(load_preset(pid), fallback_engine) for pid in ("batio3", "si", "srtio3")}


def _check_dos(d, n_atoms: int):
    f = np.asarray(d.frequencies_thz)
    total = np.asarray(d.total_dos)
    assert len(f) == len(total) and np.all(np.diff(f) > 0)
    assert np.trapezoid(total, f) == pytest.approx(3 * n_atoms, rel=1e-4)
    psum = sum(np.asarray(v) for v in d.projected_dos.values())
    np.testing.assert_allclose(psum, total, atol=1e-9 * total.max())
    assert all(np.all(np.asarray(v) >= -1e-12) for v in d.projected_dos.values())


def test_dos_integral_and_projection(fallback_phonons):
    an = fallback_phonons["batio3"]
    d = an.dos_and_thermal()
    _check_dos(d, 5)
    assert set(d.projected_dos) == {"Ba", "Ti", "O"}
    # each element's DOS integrates to 3 x its atom count
    f = np.asarray(d.frequencies_thz)
    assert np.trapezoid(d.projected_dos["O"], f) == pytest.approx(9.0, rel=1e-3)
    assert np.trapezoid(d.projected_dos["Ba"], f) == pytest.approx(3.0, rel=1e-3)
    assert len(d.mesh) == 3 and np.prod(d.mesh) >= 1000


def test_thermal_properties_units(fallback_phonons):
    an = fallback_phonons["batio3"]   # dynamically stable with the fallback calculator
    d = an.dos_and_thermal(t_min=0, t_max=5000, t_step=10)
    assert d.imaginary_fraction == 0.0
    th = d.thermal
    T = np.asarray(th.temperatures_k)
    F = np.asarray(th.free_energy_ev_per_atom)
    S = np.asarray(th.entropy_kb_per_atom)
    cv = np.asarray(th.heat_capacity_kb_per_atom)
    assert T[0] == 0 and T[-1] == pytest.approx(5000)
    assert cv[-1] == pytest.approx(3.0, abs=0.02)                 # Dulong-Petit: 3 k_B / atom
    assert np.all(np.diff(cv) >= -1e-9) and cv[0] == pytest.approx(0.0, abs=1e-9)
    assert F[0] == pytest.approx(th.zero_point_energy_ev_per_atom, rel=1e-5)  # F(0) = ZPE
    # S = -dF/dT (k_B units), and F = U - T S with U = F + T S >= ZPE, -> 3 k_B T at high T
    i = int(np.searchsorted(T, 300.0))
    dfdt = (F[i + 1] - F[i - 1]) / (T[i + 1] - T[i - 1])
    assert -dfdt / KB_EV == pytest.approx(S[i], rel=2e-3)
    U = F + T * S * KB_EV
    assert U[i] > th.zero_point_energy_ev_per_atom
    assert U[-1] / (KB_EV * T[-1]) == pytest.approx(3.0, abs=0.01)   # equipartition (ZPE cancels)


def test_imaginary_modes_are_excluded_not_nan(fallback_phonons):
    an = fallback_phonons["srtio3"]     # unstable with the fallback calculator
    assert not an.result.is_dynamically_stable
    d = an.dos_and_thermal(t_max=5000, t_step=50)
    assert 0 < d.imaginary_fraction < 1
    th = d.thermal
    for arr in (th.free_energy_ev_per_atom, th.entropy_kb_per_atom, th.heat_capacity_kb_per_atom):
        assert np.all(np.isfinite(arr))
    # only the real modes count: C_v -> 3 k_B (1 - imaginary fraction)
    assert th.heat_capacity_kb_per_atom[-1] == pytest.approx(3 * (1 - d.imaginary_fraction), abs=0.05)
    _check_dos(d, 5)
    assert np.asarray(d.frequencies_thz)[0] < -F_TOL_THZ


def test_dos_explicit_mesh_cache_and_validation(fallback_phonons):
    an = fallback_phonons["si"]
    d1 = an.dos_and_thermal(mesh=[6, 6, 6], t_max=500, t_step=50)
    assert d1.mesh == [6, 6, 6] and len(d1.thermal.temperatures_k) == 11
    assert an.dos_and_thermal(mesh=[6, 6, 6], t_max=500, t_step=50) is d1   # cached
    _check_dos(d1, 8)
    for kwargs in ({"mesh": [0, 4, 4]}, {"mesh": [4, 4]}, {"t_min": 100, "t_max": 50}, {"t_step": 0}):
        with pytest.raises(ValueError):
            an.dos_and_thermal(**kwargs)
    # the band-structure cache still works after the mesh was released
    assert an.eigenmode(0, 3).band_index == 3


def test_dos_cancellation(fallback_phonons):
    an = fallback_phonons["si"]
    calls = {"n": 0}

    def cancel_on_second():
        calls["n"] += 1
        return calls["n"] >= 2

    with pytest.raises(PhononCancelled):
        an.dos_and_thermal(mesh=[5, 5, 5], should_cancel=cancel_on_second)
    with pytest.raises(PhononCancelled):
        an.dos_and_thermal(mesh=[5, 5, 5], should_cancel=lambda: True)


def test_auto_mesh_is_memory_capped(fallback_engine):
    an = run_phonon_analysis(load_preset("li3ps4"), fallback_engine, supercell_dim=(1, 1, 1))
    mesh = an.auto_dos_mesh()
    n_modes = 3 * 32
    assert np.prod(mesh) * 16 * n_modes ** 2 <= 96 * 2**20
    assert mesh[0] <= mesh[1] <= mesh[2]   # a > b > c for this cell: denser along the short axes


def test_dos_speed(fallback_phonons, capsys):
    an = fallback_phonons["batio3"]
    t0 = time.perf_counter()
    an.dos_and_thermal(t_step=20)       # automatic mesh, not cached (different t_step)
    elapsed = time.perf_counter() - t0
    with capsys.disabled():
        print(f"\n[dos timing] BaTiO3 (5 atoms), auto mesh: {elapsed * 1e3:.0f} ms")
    assert elapsed < 5.0


def test_mace_si_dos_and_thermal(mace_engine):
    an = run_phonon_analysis(load_preset("si"), mace_engine)
    d = an.dos_and_thermal(t_max=3000, t_step=50)
    _check_dos(d, 8)
    assert d.imaginary_fraction < 0.01
    assert d.thermal.heat_capacity_kb_per_atom[-1] == pytest.approx(3.0, abs=0.05)
    assert np.asarray(d.frequencies_thz)[np.argmax(d.total_dos)] > 10.0   # optical peak of Si ~ 14-15 THz
    assert 0.03 < d.thermal.zero_point_energy_ev_per_atom < 0.09          # Si ZPE ~ 0.06 eV/atom


# --------------------------------------------------------------------------
# hull: relax first
# --------------------------------------------------------------------------
def _nacl_refs():
    def bcc(el, a):
        return Structure.from_spacegroup("Im-3m", Lattice.cubic(a), [el], [[0, 0, 0]]).get_primitive_structure()

    rocksalt = Structure.from_spacegroup(
        "Fm-3m", Lattice.cubic(5.64), ["Na", "Cl"], [[0, 0, 0], [0.5, 0.5, 0.5]]
    ).get_primitive_structure()
    dimer = Structure(Lattice.cubic(8.0), ["Cl", "Cl"], [[0, 0, 0], [1.99, 0, 0]], coords_are_cartesian=True)
    return [ha.MPReference("mp-na", bcc("Na", 4.23), 0.0), ha.MPReference("mp-cl2", dimer, 0.0),
            ha.MPReference("mp-nacl", rocksalt, 0.0)]


class _Fetcher:
    def __init__(self, refs):
        self.refs = refs
        self.calls = 0

    def __call__(self, chemsys_list, api_key):
        self.calls += 1
        wanted = set(chemsys_list)
        return [r for r in self.refs if r.structure.composition.chemical_system in wanted]


@pytest.fixture
def nacl_hull(fallback_engine, tmp_path):
    cache = ha.HullCache(tmp_path / "hull")
    fetcher = _Fetcher(_nacl_refs())
    refset = ha.get_reference_set(["Na", "Cl"], fallback_engine, fetcher=fetcher, cache=cache)
    relaxed = next(r for r in refset.references if r.material_id == "mp-nacl").atoms()
    return fetcher, cache, relaxed


def test_hull_relaxes_candidate_by_default(fallback_engine, nacl_hull):
    fetcher, cache, relaxed = nacl_hull
    strained = relaxed.copy()
    strained.set_cell(relaxed.cell.array * 1.04, scale_atoms=True)
    as_given = ha.evaluate_hull(strained, engine=fallback_engine, fetcher=fetcher, cache=cache, relax_candidate=False)
    assert not as_given.candidate_relaxed and as_given.energy_per_atom_as_given is None
    assert as_given.e_hull_ev > 0.005
    assert "single point as given" in as_given.method

    res = ha.evaluate_hull(strained, engine=fallback_engine, fetcher=fetcher, cache=cache)
    assert res.candidate_relaxed
    assert res.energy_per_atom_as_given == pytest.approx(as_given.energy_per_atom, abs=1e-9)
    assert res.energy_per_atom < res.energy_per_atom_as_given
    assert abs(res.e_hull_ev) < 2e-3                 # relaxes back onto its own reference
    assert "candidate relaxed with the reference settings" in res.method
    assert np.allclose(strained.cell.array, relaxed.cell.array * 1.04)   # input not mutated


def test_hull_candidate_divergence_falls_back(fallback_engine, nacl_hull, monkeypatch):
    from app.core import relaxer

    fetcher, cache, relaxed = nacl_hull

    def diverging(atoms, engine, **kwargs):
        yield relaxer.RelaxationFrame(step=0, converged=False, final=False, crystal=engine.evaluate(atoms))
        raise relaxer.RelaxationDivergedError("volume blew up", 3)

    monkeypatch.setattr(relaxer, "iter_relaxation", diverging)
    res = ha.evaluate_hull(relaxed, engine=fallback_engine, fetcher=fetcher, cache=cache)
    assert not res.candidate_relaxed
    assert "diverged" in res.method
    single = fallback_engine.energy_forces_stress(relaxed)[0] / len(relaxed)
    assert res.energy_per_atom == pytest.approx(single, abs=1e-9)
    assert res.energy_per_atom_as_given == pytest.approx(single, abs=1e-9)


def test_hull_route_passes_relax_flag(client, fallback_engine, monkeypatch):
    seen = {}

    def fake(atoms, **kwargs):
        seen.update(kwargs)
        raise ha.HullNoApiKeyError("no key")

    monkeypatch.setattr(ha, "evaluate_hull", fake)
    crystal = _crystal(fallback_engine, "si")
    for flag in (None, False, True):
        body = {"crystal": crystal} if flag is None else {"crystal": crystal, "relax": flag}
        assert client.post("/api/hull/evaluate", json=body).status_code == 503
        assert seen["relax_candidate"] is (True if flag is None else flag)

