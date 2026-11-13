"""Phase C2: generator top-k, prototype search, compare, MP match (fallback calculator, fake MP clients).

Lean by design: one wire sequence / one physics check per feature.
"""

from __future__ import annotations

import numpy as np
import pytest
from ase import Atoms
from ase.build import bulk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymatgen.core import Lattice, Structure
from pymatgen.io.ase import AseAtomsAdaptor

from app.api import routes, ws_stream
from app.core import hull_analyzer as ha
from app.core import mp_client, prototypes
from app.presets import load_preset
from app.schemas.api import MPEntry
from app.schemas.messages import GenerationCandidates, PrototypeCandidates, PrototypeSearchPayload

from test_phase_b_session import (  # noqa: F401 - fixtures are used by name
    _clean_registry,
    _types,
    _without_progress,
    fake_default,
    mstudio,
)
from test_ws_protocol import SRTIO3_FLOW, _hang_guard, client, slow_engine, ws_engine  # noqa: F401


def _entry(mid: str, formula: str, nsites: int = 5) -> MPEntry:
    return MPEntry(material_id=mid, formula=formula, spacegroup_symbol="Pm-3m", spacegroup_number=221,
                   nsites=nsites, energy_above_hull=0.0, is_stable=True, theoretical=False)


def _perovskite(a_site: str, b_site: str) -> Structure:
    atoms = load_preset("srtio3")
    atoms.set_chemical_symbols([{"Sr": a_site, "Ti": b_site}.get(s, s) for s in atoms.get_chemical_symbols()])
    return AseAtomsAdaptor.get_structure(atoms)


def _sto_i4mcm() -> Atoms:
    """Tetragonal (a0a0c-) SrTiO3, 20 atoms: Sr 4b, Ti 4c, O 4a + 8h (x = 0.22)."""
    s = Structure.from_spacegroup("I4/mcm", Lattice.tetragonal(5.52, 7.81), ["Sr", "Ti", "O", "O"],
                                  [[0, 0.5, 0.25], [0, 0, 0], [0, 0, 0.25], [0.22, 0.72, 0]])
    return AseAtomsAdaptor.get_atoms(s)


@pytest.fixture
def rest(fallback_engine):
    from app.core.umlip_engine import UMLIPEngine

    previous = UMLIPEngine._instance
    UMLIPEngine.set_instance(fallback_engine)
    app = FastAPI()
    app.include_router(routes.router)
    routes.register_error_handlers(app)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, app
    UMLIPEngine.set_instance(previous)


# --------------------------------------------------------------------------- #
# START_FLOW_GENERATION top_k
# --------------------------------------------------------------------------- #
def test_flow_top_k_wire(mstudio):
    s = mstudio
    s.send("START_FLOW_GENERATION", {**SRTIO3_FLOW, "steps": 6, "top_k": 3})
    msgs = s.until(["CRYSTAL_STATE_FINAL"], timeout=60.0)
    assert _types(msgs) == ["FLOW_TRAJECTORY_FRAME"] * 6 + ["GENERATION_CANDIDATES", "CRYSTAL_STATE_FINAL"]
    result = GenerationCandidates.model_validate(msgs[-2]["payload"])
    final = msgs[-1]["payload"]["crystal"]
    assert result.summary.spacegroup_number == 221 and result.summary.composition == {"Sr": 1, "Ti": 1, "O": 3}
    assert result.summary.n_structures_scored >= len(result.candidates) >= 2
    first = result.candidates[0]
    assert first.rank == 1 and first.relative_energy_mev_per_atom == 0.0
    assert first.crystal.model_dump() == final        # rank 1 is the streamed, now active endpoint
    assert [c.rank for c in result.candidates] == list(range(1, len(result.candidates) + 1))
    assert all(c.wyckoff_assignment.startswith("Sr ") for c in result.candidates)
    h = s.history()
    assert h["label"] == "Flow generation"
    # top_k = 1 (default): unchanged wire sequence
    s.send("START_FLOW_GENERATION", {**SRTIO3_FLOW, "steps": 4})
    msgs = s.until(["CRYSTAL_STATE_FINAL"], timeout=60.0)
    assert _types(msgs) == ["FLOW_TRAJECTORY_FRAME"] * 4 + ["CRYSTAL_STATE_FINAL"]


# --------------------------------------------------------------------------- #
# PROTOTYPE_SEARCH
# --------------------------------------------------------------------------- #
class FakeMP:
    """Knows a single prototype: cubic KNbO3 (mp-7375)."""

    def __init__(self):
        self.searched = []

    def search(self, query):
        self.searched.append(query)
        return [_entry("mp-7375", "KNbO3")] if query == "KNbO3" else []

    def get_structure(self, material_id):
        return _entry("mp-7375", "KNbO3"), _perovskite("K", "Nb")


def _dimer(el: str, d: float) -> Atoms:
    return Atoms(f"{el}2", positions=[[0, 0, 0], [d, 0, 0]], cell=[8, 8, 8], pbc=True)


def test_prototype_search_knbo3_to_nanbo3(fallback_engine, tmp_path):
    refs = [ha.MPReference("mp-na", AseAtomsAdaptor.get_structure(bulk("Na", "bcc", a=4.23)), 0.0),
            ha.MPReference("mp-nb", AseAtomsAdaptor.get_structure(bulk("Nb", "bcc", a=3.3)), 0.0),
            ha.MPReference("mp-o2", AseAtomsAdaptor.get_structure(_dimer("O", 1.21)), 0.0)]

    def fetcher(chemsys_list, api_key):
        return [r for r in refs if r.structure.composition.chemical_system in set(chemsys_list)]

    fake = FakeMP()
    seen = []
    result = prototypes.search(
        PrototypeSearchPayload(composition={"Na": 2, "Nb": 2, "O": 6}), fallback_engine, client=fake,
        fetcher=fetcher, cache=ha.HullCache(tmp_path / "hull"), progress=lambda *a: seen.append(a),
    )
    assert result.target_formula == "NaNbO3" and result.n_sources == 1
    assert "NaNbO3" in fake.searched and "KNbO3" in fake.searched   # identity source tried first
    (cand,) = result.candidates
    assert cand.source_material_id == "mp-7375" and cand.substitutions == {"K": "Na"}
    assert cand.relaxed and cand.e_hull_ev is not None and cand.crystal.e_hull_ev == cand.e_hull_ev
    assert cand.spacegroup_symbol == "Pm-3m" and sorted(a.element for a in cand.crystal.atoms) == \
        ["Na", "Nb", "O", "O", "O"]
    assert 0 < cand.probability < 1 and seen


def test_prototype_search_wiring(mstudio, monkeypatch):
    s = mstudio
    monkeypatch.setattr(ws_stream, "prototype_client", lambda key: mp_client.MaterialsProjectClient(None))
    s.send("PROTOTYPE_SEARCH", {"composition": {"Na": 1, "Nb": 1, "O": 3}})
    err = s.expect_error("INVALID_OPERATION", "PROTOTYPE_SEARCH")
    assert "MP_API_KEY" in err["message"]

    def fake_search(request, engine, *, client, api_key, should_cancel, progress, **kw):
        progress(0, 1, "fetching")
        return PrototypeCandidates(target_formula="NaNbO3", n_sources=0, candidates=[], method="test")

    monkeypatch.setattr(ws_stream, "prototype_client", lambda key: FakeMP())
    monkeypatch.setattr(prototypes, "search", fake_search)
    s.send("PROTOTYPE_SEARCH", {"composition": {"Na": 1, "Nb": 1, "O": 3}})
    msgs = s.until(["PROTOTYPE_CANDIDATES"])
    assert _types(msgs) == ["PROGRESS", "PROTOTYPE_CANDIDATES"]
    s.assert_silent(0.2)  # not state-changing: no history push


# --------------------------------------------------------------------------- #
# REST: /api/compare, /api/mp/match
# --------------------------------------------------------------------------- #
def test_compare_cubic_vs_tetragonal_srtio3(rest, fallback_engine):
    c, _ = rest
    cubic = fallback_engine.evaluate(load_preset("srtio3")).model_dump()
    tetra = fallback_engine.evaluate(_sto_i4mcm()).model_dump()
    r = c.post("/api/compare", json={"crystal_a": cubic, "crystal_b": tetra})
    assert r.status_code == 200, r.text
    r = r.json()
    assert r["same_composition"] and r["spacegroup_a"] == "Pm-3m" and r["spacegroup_b"] == "I4/mcm"
    assert r["group_subgroup"] == "b is a subgroup of a"
    de = r["energy_difference_mev_per_atom"]
    assert de == pytest.approx((tetra["energy_per_atom"] - cubic["energy_per_atom"]) * 1000)
    assert r["energy_difference_mev_per_formula_unit"] == pytest.approx(5 * de)
    assert r["structures_match"] == (r["rms_displacement"] is not None)
    same = c.post("/api/compare", json={"crystal_a": cubic, "crystal_b": cubic}).json()
    assert same["structures_match"] and same["rms_displacement"] == pytest.approx(0.0, abs=1e-6)
    assert same["group_subgroup"] == "same space group"
    si = fallback_engine.evaluate(load_preset("si")).model_dump()
    other = c.post("/api/compare", json={"crystal_a": cubic, "crystal_b": si}).json()
    assert not other["same_composition"] and other["group_subgroup"] is None
    assert other["energy_difference_mev_per_formula_unit"] is None


def test_mp_match(rest, fallback_engine):
    c, app = rest
    crystal = fallback_engine.evaluate(load_preset("srtio3")).model_dump()
    app.dependency_overrides[routes.mp_context] = lambda: routes.MPContext(api_key=None)
    r = c.post("/api/mp/match", json={"crystal": crystal})
    assert r.status_code == 503 and r.json()["code"] == "MP_UNAVAILABLE"

    class MatchMP:
        def search(self, query):
            assert query == "SrTiO3"
            return [_entry("mp-5229", "SrTiO3"), _entry("mp-4651", "SrTiO3", 20), _entry("mp-big", "SrTiO3", 500)]

        def get_structure(self, mid):
            if mid == "mp-5229":
                strained = _perovskite("Sr", "Ti")
                strained.perturb(0.05, min_distance=0.05)
                return _entry(mid, "SrTiO3"), strained
            return _entry(mid, "SrTiO3", 20), AseAtomsAdaptor.get_structure(_sto_i4mcm())

    app.dependency_overrides[routes.mp_context] = lambda: routes.MPContext(api_key="k" * 32, client=MatchMP())
    r = c.post("/api/mp/match", json={"crystal": crystal}).json()
    assert r["formula"] == "SrTiO3" and r["n_searched"] == 3
    ids = [m["entry"]["material_id"] for m in r["matches"]]
    assert ids[0] == "mp-5229" and "mp-big" not in ids
    rms = [m["rms_displacement"] for m in r["matches"]]
    assert rms == sorted(rms)

    class DownMP(MatchMP):
        def search(self, query):
            raise mp_client.MPFetchError("upstream down")

    app.dependency_overrides[routes.mp_context] = lambda: routes.MPContext(api_key="k" * 32, client=DownMP())
    assert c.post("/api/mp/match", json={"crystal": crystal}).status_code == 502
