"""REST layer (app/api/routes.py, PROJECT_SPEC.md §5.3).

Uses a minimal ``FastAPI()`` app with only the REST router and error handlers
(``app.main`` also imports the WebSocket module, which is developed separately).
The process-wide engine is the analytic fallback; the hull endpoint gets a fake
fetcher and a ``tmp_path`` cache through the ``hull_context`` dependency, so no
test touches the network or the real cache directory.
"""

from __future__ import annotations

import io
import time

import numpy as np
import pytest
from ase import Atoms
from ase.io import read as ase_read
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from pymatgen.core import Lattice, Structure
from pymatgen.io.ase import AseAtomsAdaptor

from app.api import routes
from app.core import hull_analyzer as ha
from app.core.structure_io import MAX_ATOMS, atoms_to_cif
from app.core.umlip_engine import UMLIPEngine
from app.presets import list_presets, load_preset
from app.schemas.api import ErrorResponse, HullEvaluateResponse
from app.schemas.crystal import CrystalStatePayload

PRESET_IDS = ["srtio3", "li3ps4", "si", "batio3"]
VALID_KEY = "k" * 32


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def engine(fallback_engine):
    previous = UMLIPEngine._instance
    UMLIPEngine.set_instance(fallback_engine)
    yield fallback_engine
    UMLIPEngine.set_instance(previous)


class FakeFetcher:
    def __init__(self, refs=(), error: Exception | None = None):
        self.refs = list(refs)
        self.error = error
        self.calls = []

    def __call__(self, chemsys_list, api_key):
        self.calls.append((list(chemsys_list), api_key))
        if self.error is not None:
            raise self.error
        wanted = set(chemsys_list)
        return [r for r in self.refs if r.structure.composition.chemical_system in wanted]


@pytest.fixture
def hull_ctx(tmp_path):
    # Default for every test: no key, the real (MP) fetcher, an isolated cache.
    return routes.HullContext(api_key=None, fetcher=None, cache=ha.HullCache(tmp_path / "hull"))


@pytest.fixture
def app(engine, hull_ctx) -> FastAPI:
    app = FastAPI()
    routes.register_error_handlers(app)
    app.include_router(routes.router)
    app.dependency_overrides[routes.hull_context] = lambda: hull_ctx

    @app.websocket("/ws/echo")
    async def echo(ws: WebSocket):
        await ws.accept()
        await ws.send_json(await ws.receive_json())
        await ws.close()

    @app.get("/boom")
    def boom():
        raise RuntimeError("unexpected")

    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def assert_error(resp, status: int, code: str) -> ErrorResponse:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"code", "message"}, body
    err = ErrorResponse(**body)
    assert err.code == code
    assert err.message
    return err


def payload_for(preset_id: str, engine: UMLIPEngine) -> dict:
    return engine.evaluate(load_preset(preset_id)).model_dump()


def frac_and_symbols(crystal: dict):
    sites = sorted(crystal["atoms"], key=lambda s: s["index"])
    return [s["element"] for s in sites], np.array([s["frac_coords"] for s in sites])


def assert_same_structure(symbols_a, frac_a, cell_a, symbols_b, frac_b, cell_b, atol=1e-6):
    """Same lattice and the same set of (element, fractional position) sites, in any order."""
    np.testing.assert_allclose(np.asarray(cell_a), np.asarray(cell_b), atol=atol, rtol=0)
    assert sorted(symbols_a) == sorted(symbols_b)
    unmatched = list(range(len(symbols_b)))
    for sym, f in zip(symbols_a, np.asarray(frac_a)):
        for j in unmatched:
            d = np.asarray(frac_b[j]) - f
            d -= np.round(d)
            if symbols_b[j] == sym and np.all(np.abs(d) < atol):
                unmatched.remove(j)
                break
        else:
            raise AssertionError(f"site {sym} {f} has no counterpart")
    assert not unmatched


# --------------------------------------------------------------------------
# health / presets
# --------------------------------------------------------------------------
def test_health(client, engine, hull_ctx):
    body = client.get("/api/health").json()
    assert body == {
        "status": "ok",
        "umlip_backend": "lj-fallback",
        "device": "cpu",
        "dtype": "float64",
        "load_error": None,
        "hull_available": False,
    }
    hull_ctx.api_key = VALID_KEY
    assert client.get("/api/health").json()["hull_available"] is True


def test_health_reports_load_error(client, engine, monkeypatch):
    monkeypatch.setattr(engine, "load_error", FileNotFoundError("checkpoint not cached"))
    assert client.get("/api/health").json()["load_error"] == "FileNotFoundError: checkpoint not cached"


def test_presets(client):
    resp = client.get("/api/presets")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["id"] for p in body] == PRESET_IDS
    assert body == [p.model_dump() for p in list_presets()]


# --------------------------------------------------------------------------
# export / import
# --------------------------------------------------------------------------
@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_cif_round_trip(client, engine, preset_id):
    crystal = payload_for(preset_id, engine)
    resp = client.post("/api/export/cif", json={"crystal": crystal, "format": "cif"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("chemical/x-cif")
    assert resp.headers["content-disposition"] == f'attachment; filename="{crystal["formula"]}.cif"'
    assert "_cell_length_a" in resp.text

    back = client.post("/api/import/cif", json={"cif": resp.text})
    assert back.status_code == 200, back.text
    imported = CrystalStatePayload(**back.json()).model_dump()
    assert imported["formula"] == crystal["formula"]
    assert imported["spacegroup_number"] == crystal["spacegroup_number"]
    assert_same_structure(
        *frac_and_symbols(crystal), crystal["lattice"]["matrix"],
        *frac_and_symbols(imported), imported["lattice"]["matrix"],
    )
    assert imported["energy_per_atom"] == pytest.approx(crystal["energy_per_atom"], abs=1e-6)
    assert len(imported["bonds"]) == len(crystal["bonds"])  # fast bonds, as LOAD_STRUCTURE


@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_poscar_export(client, engine, preset_id):
    crystal = payload_for(preset_id, engine)
    resp = client.post("/api/export/cif", json={"crystal": crystal, "format": "poscar"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"] == 'attachment; filename="POSCAR"'
    atoms = ase_read(io.StringIO(resp.text), format="vasp")
    symbols, frac = frac_and_symbols(crystal)
    assert atoms.get_chemical_symbols() == symbols  # order preserved
    np.testing.assert_allclose(atoms.cell.array, crystal["lattice"]["matrix"], atol=1e-8)
    d = atoms.get_scaled_positions(wrap=True) - frac
    d -= np.round(d)
    assert np.abs(d).max() < 1e-8


def test_export_defaults_to_cif(client, engine):
    resp = client.post("/api/export/cif", json={"crystal": payload_for("si", engine)})
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'attachment; filename="Si.cif"'


def test_export_uses_the_structure_not_the_claimed_formula(client, engine):
    crystal = payload_for("si", engine)
    crystal["formula"] = 'Evil"; filename=x.exe'
    resp = client.post("/api/export/cif", json={"crystal": crystal})
    assert resp.headers["content-disposition"] == 'attachment; filename="Si.cif"'


MALFORMED_CIF = "data_broken\n_cell_length_a 5.0\nthis is not a cif\n"

DISORDERED_CIF = """data_disordered
_symmetry_space_group_name_H-M   'P 1'
_cell_length_a   4.0
_cell_length_b   4.0
_cell_length_c   4.0
_cell_angle_alpha   90.0
_cell_angle_beta   90.0
_cell_angle_gamma   90.0
_symmetry_Int_Tables_number   1
loop_
 _symmetry_equiv_pos_as_xyz
  'x, y, z'
loop_
 _atom_site_label
 _atom_site_type_symbol
 _atom_site_fract_x
 _atom_site_fract_y
 _atom_site_fract_z
 _atom_site_occupancy
  Fe1  Fe  0.0  0.0  0.0  0.5
  Co1  Co  0.0  0.0  0.0  0.5
  O1   O   0.5  0.5  0.5  1.0
"""


def test_import_malformed_cif(client):
    assert_error(client.post("/api/import/cif", json={"cif": MALFORMED_CIF}), 422, "INVALID_STRUCTURE")
    assert_error(client.post("/api/import/cif", json={"cif": "   \n"}), 422, "INVALID_STRUCTURE")


def test_import_disordered_cif(client):
    err = assert_error(client.post("/api/import/cif", json={"cif": DISORDERED_CIF}), 422, "DISORDERED_STRUCTURE")
    assert "disordered" in err.message


def test_import_and_export_too_large(client, engine):
    big = load_preset("si") * (3, 3, 3)  # 216 atoms > MAX_ATOMS
    assert len(big) > MAX_ATOMS
    assert_error(client.post("/api/import/cif", json={"cif": atoms_to_cif(big)}), 422, "STRUCTURE_TOO_LARGE")

    crystal = payload_for("si", engine)
    site = crystal["atoms"][0]
    crystal["atoms"] = [dict(site, index=k, frac_coords=[k / 200, 0.0, 0.0]) for k in range(MAX_ATOMS + 1)]
    assert_error(client.post("/api/export/cif", json={"crystal": crystal}), 422, "STRUCTURE_TOO_LARGE")
    assert_error(client.post("/api/hull/evaluate", json={"crystal": crystal}), 422, "STRUCTURE_TOO_LARGE")


def test_export_empty_or_degenerate_structure(client, engine):
    crystal = payload_for("si", engine)
    assert_error(client.post("/api/export/cif", json={"crystal": dict(crystal, atoms=[])}), 422, "INVALID_STRUCTURE")
    flat = dict(crystal, lattice=dict(crystal["lattice"], matrix=[[1, 0, 0], [0, 1, 0], [0, 0, 0]]))
    assert_error(client.post("/api/export/cif", json={"crystal": flat}), 422, "INVALID_STRUCTURE")
    bad_coords = dict(crystal, atoms=[dict(crystal["atoms"][0], frac_coords=[0.1, 0.2])])
    assert_error(client.post("/api/export/cif", json={"crystal": bad_coords}), 422, "INVALID_STRUCTURE")
    unknown = dict(crystal, atoms=[dict(crystal["atoms"][0], element="Xx")])
    err = assert_error(client.post("/api/export/cif", json={"crystal": unknown}), 422, "INVALID_ELEMENT")
    assert "Xx" in err.message
    assert_error(client.post("/api/hull/evaluate", json={"crystal": unknown}), 422, "INVALID_ELEMENT")


# --------------------------------------------------------------------------
# validation and generic errors
# --------------------------------------------------------------------------
def test_validation_errors_use_error_shape(client, engine):
    err = assert_error(client.post("/api/export/cif", json={"crystal": {}}), 422, "INVALID_PAYLOAD")
    assert "crystal." in err.message
    crystal = payload_for("si", engine)
    err = assert_error(client.post("/api/export/cif", json={"crystal": crystal, "format": "xyz"}), 422, "INVALID_PAYLOAD")
    assert "format" in err.message
    assert_error(client.post("/api/import/cif", json={"cif": ""}), 422, "INVALID_PAYLOAD")  # min_length=1
    assert_error(client.post("/api/import/cif", json={}), 422, "INVALID_PAYLOAD")
    assert_error(
        client.post("/api/import/cif", content=b"{not json", headers={"content-type": "application/json"}),
        422, "INVALID_PAYLOAD",
    )
    assert_error(client.post("/api/hull/evaluate", json={"crystal": {"formula": "Si"}}), 422, "INVALID_PAYLOAD")


def test_engine_failure_is_compute_failed(client, engine, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("CUDA exploded")

    monkeypatch.setattr(engine, "evaluate", broken)
    si_cif = atoms_to_cif(load_preset("si"))
    err = assert_error(client.post("/api/import/cif", json={"cif": si_cif}), 500, "COMPUTE_FAILED")
    assert "CUDA exploded" in err.message


def test_unhandled_exception_backstop(app):
    client = TestClient(app, raise_server_exceptions=False)
    assert_error(client.get("/boom"), 500, "COMPUTE_FAILED")


def test_handlers_leave_websockets_and_routing_alone(client):
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_json({"type": "PING"})
        assert ws.receive_json() == {"type": "PING"}
    assert client.get("/api/nope").status_code == 404


# --------------------------------------------------------------------------
# hull
# --------------------------------------------------------------------------
def si_refs():
    si = AseAtomsAdaptor.get_structure(load_preset("si"))
    fcc = Structure.from_spacegroup("Fm-3m", Lattice.cubic(3.85), ["Si"], [[0, 0, 0]]).get_primitive_structure()
    return [ha.MPReference("mp-149", si, 0.0), ha.MPReference("mp-fcc-si", fcc, 0.08)]


def test_hull_without_key_is_503(client, engine):
    err = assert_error(client.post("/api/hull/evaluate", json={"crystal": payload_for("si", engine)}),
                       503, "HULL_UNAVAILABLE")
    assert "MP_API_KEY" in err.message


def test_hull_malformed_key_is_503(client, engine, hull_ctx):
    hull_ctx.api_key = "short"
    assert_error(client.post("/api/hull/evaluate", json={"crystal": payload_for("si", engine)}),
                 503, "HULL_UNAVAILABLE")


def test_hull_fetch_failure_is_502(client, engine, hull_ctx):
    hull_ctx.api_key = VALID_KEY
    hull_ctx.fetcher = FakeFetcher(error=ha.HullFetchError("Materials Project query failed: timeout"))
    err = assert_error(client.post("/api/hull/evaluate", json={"crystal": payload_for("si", engine)}),
                       502, "HULL_UNAVAILABLE")
    assert "timeout" in err.message
    hull_ctx.fetcher = FakeFetcher(error=OSError("network unreachable"))  # arbitrary fetcher exception
    assert_error(client.post("/api/hull/evaluate", json={"crystal": payload_for("si", engine)}),
                 502, "HULL_UNAVAILABLE")


def test_hull_missing_elemental_reference_is_502(client, engine, hull_ctx):
    hull_ctx.fetcher = FakeFetcher(si_refs())  # no O phase for SiO-like candidates
    crystal = payload_for("si", engine)
    crystal["atoms"][0]["element"] = "O"
    err = assert_error(client.post("/api/hull/evaluate", json={"crystal": crystal}), 502, "HULL_UNAVAILABLE")
    assert "O" in err.message


def test_hull_success_and_cache(client, engine, hull_ctx):
    fetcher = FakeFetcher(si_refs())
    hull_ctx.api_key = VALID_KEY
    hull_ctx.fetcher = fetcher
    crystal = payload_for("si", engine)
    t0 = time.perf_counter()
    resp = client.post("/api/hull/evaluate", json={"crystal": crystal, "relax": False})  # pins the as-given energy
    cold = time.perf_counter() - t0
    assert resp.status_code == 200, resp.text
    res = HullEvaluateResponse(**resp.json())
    assert res.chemsys == "Si"
    assert res.umlip_backend == "lj-fallback"
    assert res.energy_per_atom == pytest.approx(crystal["energy_per_atom"], abs=1e-9)
    assert res.e_hull_ev >= -1e-9  # the unrelaxed preset vs. its own relaxed structure
    assert res.decomposition == pytest.approx({"Si": 1.0})
    assert res.formation_energy_per_atom == pytest.approx(res.e_hull_ev, abs=1e-9)  # elemental system
    assert {r.material_id for r in res.references} == {"mp-149", "mp-fcc-si"}
    assert "lj-fallback" in res.method
    assert fetcher.calls == [(["Si"], VALID_KEY)]

    # Same chemical system again: no fetch, no key needed.
    hull_ctx.api_key = None
    t0 = time.perf_counter()
    again = client.post("/api/hull/evaluate", json={"crystal": crystal, "relax": False})
    warm = time.perf_counter() - t0
    assert again.status_code == 200
    assert again.json() == resp.json()
    assert len(fetcher.calls) == 1
    print(f"[hull route timing] Si, lj-fallback: cold {cold * 1e3:.0f} ms, cached {warm * 1e3:.1f} ms")


def test_hull_unexpected_error_is_500(client, engine, hull_ctx, monkeypatch):
    def broken(*args, **kwargs):
        raise ZeroDivisionError("bad hull")

    monkeypatch.setattr(ha, "evaluate_hull", broken)
    assert_error(client.post("/api/hull/evaluate", json={"crystal": payload_for("si", engine)}),
                 500, "COMPUTE_FAILED")


# --------------------------------------------------------------------------
# latency
# --------------------------------------------------------------------------
def test_import_export_latency(client, engine, capsys):
    crystal = payload_for("li3ps4", engine)  # largest preset (32 atoms)
    t0 = time.perf_counter()
    cif = client.post("/api/export/cif", json={"crystal": crystal}).text
    t_export = time.perf_counter() - t0
    t0 = time.perf_counter()
    assert client.post("/api/import/cif", json={"cif": cif}).status_code == 200
    t_import = time.perf_counter() - t0
    with capsys.disabled():
        print(f"\n[route timing] Li3PS4 (32 atoms), lj-fallback: export {t_export * 1e3:.1f} ms, "
              f"import {t_import * 1e3:.1f} ms")
    assert t_export < 5.0 and t_import < 5.0
