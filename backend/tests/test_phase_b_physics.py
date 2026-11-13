"""Phase B physics core: primitive dispersion, band connection, Gamma irreps, supercell size and
convergence, phonopy export, elastic tensor, diffraction, Materials Project and model routes.

Kept deliberately small: one physics check plus the main error path per feature, fallback
calculator unless the assertion needs MACE physics. No network (fake MP client).
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.core import elastic, mp_client, phonon_engine as pe, relaxer
from app.core import geometry_analysis as geo
from app.presets import load_preset
from app.schemas.api import MPEntry

_CACHE: dict = {}


def _an(engine, preset: str, **kw) -> pe.PhononAnalysis:
    key = (id(engine), preset, tuple(sorted(kw.items())))
    if key not in _CACHE:
        _CACHE[key] = pe.run_phonon_analysis(load_preset(preset), engine, **kw)
    return _CACHE[key]


def _idx(an, name):
    return min(i for i, n in an.path.point_names.items() if n == name)


# --------------------------------------------------------------------------
# primitive dispersion, eigenvector mapping, band connection, irreps
# --------------------------------------------------------------------------
def test_si_primitive_mode_folds_onto_conventional(fallback_engine):
    conv = _an(fallback_engine, "si")
    prim = _an(fallback_engine, "si", cell_mode="primitive")
    r = prim.result
    assert (prim.n_atoms, prim.n_branches, r.n_primitive_atoms) == (8, 6, 2)
    assert np.array(r.frequencies_thz).shape[1] == 6
    assert [l.label for l in r.kpath_labels] == ["\\Gamma", "X", "U|K", "\\Gamma", "L", "W", "X"]
    assert r.band_path_source == "seekpath-standard" and r.cell_mode == "primitive"
    assert all(len(m.eigenvectors_real) == 8 for m in r.eigenmodes)
    # band_order: a permutation per q
    assert all(sorted(row) == list(range(6)) for row in r.band_order)

    # Gamma + the three X points of the primitive zone fold onto Gamma of the 8-atom cube.
    g, x = _idx(prim, "\\Gamma"), _idx(prim, "X")
    assert np.allclose(prim.path.qpoints[x] % 1.0, 0.0)          # X is a reciprocal vector of the cube
    folded = np.sort(np.concatenate([prim.frequencies[g], np.repeat(prim.frequencies[x], 3)]))
    fc, conv_modes = conv.eigenvectors_at(_idx(conv, "\\Gamma"))
    assert np.abs(folded - np.sort(fc)).max() < 0.01

    # The mapped eigenvectors give the physical displacement pattern of the cube: the field the
    # frontend animates, e_j exp(2 pi i q.s_j), lies in the cube's Gamma subspace of equal frequency.
    fx, prim_modes = prim.eigenvectors_at(x)
    s = prim.scaled_positions
    for band in range(6):
        u = (prim_modes[band] * np.exp(2j * np.pi * (s @ prim.path.qpoints[x]))[:, None]).reshape(-1)
        assert abs(np.linalg.norm(u) - 1.0) < 1e-9
        sub = np.array([conv_modes[k].reshape(-1) for k in range(24) if abs(fc[k] - fx[band]) < 0.01]).T
        assert np.linalg.norm(sub.conj().T @ u) > 0.99

    # Gamma irreps of the primitive cell: acoustic T1u, Raman-active / IR-inactive optical T2g.
    labels = {(m.label, m.is_acoustic, m.ir_active, m.raman_active) for m in r.gamma_modes}
    assert labels == {("T1u", True, False, False), ("T2g", False, False, True)}
    assert conv.result.gamma_modes is None                       # non-primitive phonon cell


def test_srtio3_primitive_equals_as_displayed_and_irreps(fallback_engine):
    a = _an(fallback_engine, "srtio3")
    b = _an(fallback_engine, "srtio3", cell_mode="primitive")
    assert np.array_equal(a.frequencies, b.frequencies)
    assert a.result.eigenmodes == b.result.eigenmodes
    modes = b.result.gamma_modes
    assert sum(len(m.band_indices) for m in modes) == 15
    optical = [m for m in modes if not m.is_acoustic]
    assert sorted(m.label for m in optical) == ["T1u", "T1u", "T1u", "T2u"]
    assert [m.label for m in modes if m.is_acoustic] == ["T1u"]
    for m in optical:   # T1u: IR only; T2u: silent
        assert (m.ir_active, m.raman_active) == ((True, False) if m.label == "T1u" else (False, False))
    with pytest.raises(ValueError):
        pe.run_phonon_analysis(load_preset("srtio3"), fallback_engine, cell_mode="bogus")


@pytest.mark.requires_mace
def test_follow_instability_in_primitive_mode(mace_engine):
    """2x2x2 SrTiO3 displayed, phonons on the 5-atom primitive cell; following R gives I4/mcm."""
    big = load_preset("srtio3").repeat((2, 2, 2))
    an = pe.run_phonon_analysis(big, mace_engine, cell_mode="primitive")
    assert an.n_branches == 15 and an.n_atoms == 40
    r = _idx(an, "R")
    assert an.result.eigenmodes[0].q_frac is not None and an.payload_frequencies[r, 0] < -0.05
    atoms, frames = pe.stabilize_mode(an, r, 0, amplitude=0.1, fmax=0.02, max_steps=200)
    assert len(atoms) == 40
    for last in frames:
        pass
    assert last.final and last.converged
    assert geo.get_spacegroup(atoms)[1] == 140


# --------------------------------------------------------------------------
# supercell size, convergence, export
# --------------------------------------------------------------------------
def test_supercell_choice_and_convergence(fallback_engine):
    an = _an(fallback_engine, "srtio3", min_supercell_length=10.0)
    assert an.result.supercell_dim == [3, 3, 3]                 # ceil(10 / 3.9)
    assert an.result.min_image_distance == pytest.approx(3 * load_preset("srtio3").cell.lengths()[0], abs=1e-5)
    assert pe.auto_supercell_dim(np.eye(3) * 2.0, 5, 20.0) == [4, 5, 5]   # 10^3 * 5 > 600 -> reduced (ties: first axis)
    assert pe.next_supercell_dim([2, 2, 2], 5) == [3, 3, 3]
    assert pe.next_supercell_dim([4, 4, 4], 5) == [4, 5, 5]
    with pytest.raises(ValueError):
        pe.next_supercell_dim([4, 5, 5], 5)

    small = _an(fallback_engine, "srtio3")
    res = pe.compare_phonon_analyses(small, an)
    assert res.supercell_dim_a == [2, 2, 2] and res.supercell_dim_b == [3, 3, 3]
    assert res.max_abs_delta_thz == pytest.approx(np.abs(an.frequencies - small.frequencies).max(), abs=1e-6)
    assert set(res.per_label_max_delta_thz) >= {"\\Gamma", "R"}
    assert res.per_label_max_delta_thz["\\Gamma"] < 1e-3       # Gamma is exact in both supercells
    with pytest.raises(ValueError):
        pe.compare_phonon_analyses(small, _an(fallback_engine, "srtio3", cell_mode="primitive"))


def test_phonopy_export_round_trip(fallback_engine, tmp_path):
    import phonopy

    an = _an(fallback_engine, "si", cell_mode="primitive")
    path = tmp_path / "phonopy_params.yaml"
    path.write_text(an.export_phonopy_yaml(), encoding="utf-8")
    ph = phonopy.load(str(path), produce_fc=True, log_level=0)
    freqs = ph.run_qpoints(an.phonon_qpoints, with_eigenvectors=False).frequencies
    assert np.abs(np.asarray(freqs) - an.frequencies).max() < 1e-6


# --------------------------------------------------------------------------
# elastic tensor
# --------------------------------------------------------------------------
def test_elastic_fallback_mechanics(fallback_engine):
    atoms = load_preset("srtio3")
    for _ in relaxer.iter_relaxation(atoms, fallback_engine, fmax=1e-3, max_steps=300, relax_cell=True):
        pass
    before = atoms.positions.copy()
    calls = []
    res = elastic.compute_elastic_tensor(atoms, fallback_engine, relax_ions=False,
                                         progress=lambda d, t, m: calls.append((d, t)))
    assert np.array_equal(atoms.positions, before)
    assert calls[-1] == (25, 25) and len(calls) == 25
    C = np.array(res.c_ij_gpa)
    assert np.allclose(np.diag(C)[:3], C[0, 0], rtol=1e-4) and np.allclose(np.diag(C)[3:], C[3, 3], rtol=1e-4)
    assert np.allclose([C[0, 1], C[0, 2], C[1, 2]], C[0, 1], rtol=1e-4)
    assert np.abs(C[:3, 3:]).max() < 1e-2 and C[0, 0] > 0
    assert res.born_stable and res.k_vrh_gpa == pytest.approx((C[0, 0] + 2 * C[0, 1]) / 3, rel=1e-3)
    k_eos = relaxer.bulk_modulus_result(atoms, fallback_engine).k_gpa
    assert res.k_vrh_gpa == pytest.approx(k_eos, rel=0.03)
    assert res.debye_temperature_k and res.debye_temperature_k > 0

    with pytest.raises(elastic.ElasticCancelled):
        elastic.compute_elastic_tensor(atoms, fallback_engine, should_cancel=lambda: True)
    molecule = atoms.copy()
    molecule.pbc = False
    with pytest.raises(ValueError):
        elastic.compute_elastic_tensor(molecule, fallback_engine)


@pytest.mark.requires_mace
def test_elastic_si_internal_relaxation_softens_c44(mace_engine):
    from ase.build import bulk

    si = bulk("Si", "diamond", a=5.465)          # ~MACE equilibrium, 2-atom primitive cell
    clamped = np.array(elastic.compute_elastic_tensor(si, mace_engine, relax_ions=False).c_ij_gpa)
    relaxed = np.array(elastic.compute_elastic_tensor(si, mace_engine, relax_ions=True).c_ij_gpa)
    assert clamped[3, 3] > relaxed[3, 3] + 5.0
    assert np.allclose(relaxed[:3, :3], clamped[:3, :3], rtol=0.02)   # no internal strain for normal strains


# --------------------------------------------------------------------------
# REST: diffraction, Materials Project, models
# --------------------------------------------------------------------------
@pytest.fixture
def client(fallback_engine):
    from app.core.umlip_engine import UMLIPEngine

    previous = UMLIPEngine._instance
    UMLIPEngine.set_instance(fallback_engine)
    app = FastAPI()
    app.include_router(routes.router)
    routes.register_error_handlers(app)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, app
    UMLIPEngine.set_instance(previous)


def _payload(engine, preset):
    return engine.evaluate(load_preset(preset)).model_dump()


def test_diffraction_route(client, fallback_engine):
    c, _ = client
    si = c.post("/api/diffraction", json={"crystal": _payload(fallback_engine, "si")}).json()
    first = si["peaks"][0]
    assert first["hkls"][0]["hkl"] == [1, 1, 1] and abs(first["two_theta"] - 28.44) < 0.1
    sto = c.post("/api/diffraction", json={"crystal": _payload(fallback_engine, "srtio3")}).json()
    strongest = max(sto["peaks"], key=lambda p: p["intensity"])
    assert strongest["hkls"][0]["hkl"] == [1, 1, 0] and strongest["intensity"] == 100
    bad = c.post("/api/diffraction", json={"crystal": _payload(fallback_engine, "si"), "radiation": "XxKa"})
    assert bad.status_code == 422 and bad.json()["code"] == "INVALID_PAYLOAD"
    bad = c.post("/api/diffraction", json={"crystal": _payload(fallback_engine, "si"),
                                           "two_theta_min": 60, "two_theta_max": 30})
    assert bad.status_code == 422 and bad.json()["code"] == "INVALID_PAYLOAD"


def _entry(mid, ehull):
    return MPEntry(material_id=mid, formula="SrTiO3", spacegroup_symbol="Pm-3m", spacegroup_number=221,
                   nsites=5, energy_above_hull=ehull, is_stable=ehull == 0, theoretical=False)


class FakeMP:
    def search(self, query):
        if query == "Fe2O3":
            raise mp_client.MPFetchError("upstream down")
        return [_entry("mp-2", 0.1), _entry("mp-1", 0.0), _entry("mp-3", None)]

    def get_structure(self, material_id):
        if material_id != "mp-5229":
            raise mp_client.MPNotFoundError(material_id)
        from pymatgen.io.ase import AseAtomsAdaptor

        return _entry("mp-5229", 0.0), AseAtomsAdaptor.get_structure(load_preset("srtio3"))


def test_mp_routes(client):
    c, app = client
    app.dependency_overrides[routes.mp_context] = lambda: routes.MPContext(api_key=None)
    r = c.get("/api/mp/search", params={"q": "SrTiO3"})
    assert r.status_code == 503 and r.json()["code"] == "MP_UNAVAILABLE"

    app.dependency_overrides[routes.mp_context] = lambda: routes.MPContext(api_key="k" * 32, client=FakeMP())
    r = c.get("/api/mp/search", params={"q": "SrTiO3"}).json()
    assert [e["material_id"] for e in r["results"]] == ["mp-1", "mp-2", "mp-3"]
    assert c.get("/api/mp/search", params={"q": "Fe2O3"}).status_code == 502
    assert c.get("/api/mp/search", params={"q": "Xx-Qq"}).json()["code"] == "INVALID_PAYLOAD"
    r = c.get("/api/mp/structure/mp-1")
    assert r.status_code == 404 and r.json()["code"] == "MP_NOT_FOUND"
    r = c.get("/api/mp/structure/mp-5229").json()
    from app.core.structure_io import atoms_from_cif

    assert len(atoms_from_cif(r["cif"])) == 5 and r["entry"]["material_id"] == "mp-5229"
    assert mp_client.parse_query("li-fe-o") == ("chemsys", "Fe-Li-O")
    assert mp_client.parse_query("MP-5229") == ("material_ids", ["mp-5229"])
    # newer Materials Project ids use letters instead of digits
    assert mp_client.parse_query("mp-aaaieiuj") == ("material_ids", ["mp-aaaieiuj"])
    assert mp_client.normalize_material_id(" MP-AAAIEIUJ ") == "mp-aaaieiuj"


def test_models_route_and_hull_model_unavailable(client, fallback_engine):
    c, app = client
    r = c.get("/api/models").json()
    assert "lj-fallback" in [m["id"] for m in r["models"]] and r["default_model_id"]
    app.dependency_overrides[routes.hull_context] = lambda: routes.HullContext(api_key=None)
    r = c.post("/api/hull/evaluate", json={"crystal": _payload(fallback_engine, "si"), "model_id": "no-such-model"})
    assert r.status_code == 422 and r.json()["code"] == "MODEL_UNAVAILABLE"
