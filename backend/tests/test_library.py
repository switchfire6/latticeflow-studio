"""Project library REST API: CRUD, structure dedup, result replacement, export -> import round trip."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import library_routes, routes
from app.core import relaxer
from app.core.library import Library
from app.presets import load_preset


@pytest.fixture
def client(tmp_path):
    lib = Library(tmp_path / "data")
    app = FastAPI()
    routes.register_error_handlers(app)
    app.include_router(library_routes.router)
    app.dependency_overrides[library_routes.library_dep] = lambda: lib
    return TestClient(app)


@pytest.fixture(scope="module")
def si(fallback_engine):
    atoms = load_preset("si")
    crystal = fallback_engine.evaluate(atoms).model_dump(mode="json")
    eos = relaxer.bulk_modulus_result(atoms, fallback_engine).model_dump(mode="json")
    return crystal, eos


def test_library_crud_dedup_replace_and_round_trip(client, si):
    crystal, eos = si
    projects = client.get("/api/projects").json()
    assert [p["name"] for p in projects] == ["Scratch"] and projects[0]["is_default"]
    assert client.delete(f"/api/projects/{projects[0]['id']}").status_code == 409

    p = client.post("/api/projects", json={"name": "Perovskites"}).json()
    pid = p["id"]
    assert client.patch(f"/api/projects/{pid}", json={"name": "Silicon"}).json()["name"] == "Silicon"

    # structures: same structure (atoms reordered, coordinates shifted by a lattice vector) dedups
    r1 = client.post(f"/api/projects/{pid}/structures", json={"crystal": crystal, "notes": "diamond"})
    assert r1.status_code == 201 and r1.json()["existing"] is False
    s1 = r1.json()
    assert (s1["formula"], s1["n_atoms"], s1["spacegroup_number"], s1["origin"]) == ("Si", 8, 227, "import")
    twin = {**crystal, "atoms": [dict(a, frac_coords=[x + 1.0 for x in a["frac_coords"]])
                                 for a in reversed(crystal["atoms"])]}
    r2 = client.post(f"/api/projects/{pid}/structures", json={"crystal": twin})
    assert r2.status_code == 200 and r2.json()["existing"] is True and r2.json()["id"] == s1["id"]
    strained = {**crystal, "lattice": {**crystal["lattice"],
                                       "matrix": [[x * 1.01 for x in row] for row in crystal["lattice"]["matrix"]]}}
    child = client.post(f"/api/projects/{pid}/structures",
                        json={"crystal": strained, "parent_id": s1["id"], "origin": "strain"}).json()
    assert child["existing"] is False and child["parent_id"] == s1["id"]
    sid = s1["id"]
    assert client.patch(f"/api/structures/{sid}", json={"name": "Si-d"}).json()["name"] == "Si-d"

    # results: summary computed server-side; same key replaces in place, other settings add a row
    relax = {"step": 7, "converged": True, "final": True, "crystal": crystal}
    a = client.post(f"/api/structures/{sid}/results", json={"kind": "relax", "data": relax, "model_id": "lj-fallback"})
    assert a.status_code == 201
    summary = a.json()["summary"]
    assert summary["steps"] == 7 and summary["relax_converged"] is True
    assert summary["energy_per_atom_ev"] == pytest.approx(crystal["energy_per_atom"])
    b = client.post(f"/api/structures/{sid}/results",
                    json={"kind": "relax", "data": {**relax, "step": 9}, "model_id": "lj-fallback"})
    assert b.status_code == 200 and b.json()["replaced"] and b.json()["id"] == a.json()["id"]
    e = client.post(f"/api/structures/{sid}/results",
                    json={"kind": "eos", "data": eos, "model_id": "lj-fallback", "settings": {"n_points": 5}}).json()
    assert set(e["summary"]) == {"k_gpa", "v0"}
    assert client.post(f"/api/structures/{sid}/results", json={"kind": "eos", "data": {"k_gpa": 1}}).status_code == 422
    assert len(client.get(f"/api/structures/{sid}/results").json()) == 2
    full = client.get(f"/api/results/{a.json()['id']}").json()
    assert full["data"]["step"] == 9 and full["data"]["crystal"]["formula"] == "Si"

    rows = {r["id"]: r for r in client.get(f"/api/projects/{pid}/structures").json()}
    assert "crystal" not in rows[sid] and rows[sid]["latest"]["steps"] == 9 and "k_gpa" in rows[sid]["latest"]
    assert set(rows[sid]["latest_result_ids"]) == {"relax", "eos"} and rows[child["id"]]["latest"] == {}

    # export -> import: a new project with the same structures, lineage and results
    bundle = client.get(f"/api/projects/{pid}/export").json()
    assert bundle["format"] == "latticeflow-project" and bundle["version"] == 1
    imported = client.post("/api/projects/import", json=bundle)
    assert imported.status_code == 201
    new = imported.json()
    assert new["name"] == "Silicon" and new["n_structures"] == 2 and new["id"] != pid
    new_rows = client.get(f"/api/projects/{new['id']}/structures").json()
    by_hash = {r["content_hash"]: r for r in new_rows}
    assert set(by_hash) == {r["content_hash"] for r in rows.values()}
    new_parent = by_hash[s1["content_hash"]]
    assert by_hash[child["content_hash"]]["parent_id"] == new_parent["id"]
    assert new_parent["latest"] == rows[sid]["latest"] and new_parent["notes"] == "diamond"
    assert client.post("/api/projects/import", json={"format": "nope"}).status_code == 422

    # deletes
    assert client.delete(f"/api/results/{e['id']}").status_code == 204
    assert client.get(f"/api/results/{e['id']}").status_code == 404
    assert client.delete(f"/api/structures/{sid}").status_code == 204
    assert client.get(f"/api/structures/{child['id']}").json()["parent_id"] is None
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.get(f"/api/projects/{pid}").json()["code"] == "NOT_FOUND"


def test_import_over_size_limit_is_413(client, monkeypatch):
    monkeypatch.setattr(library_routes, "max_import_bytes", lambda: 1024)
    big = {"format": "latticeflow-project", "version": 1, "pad": "x" * 2048}
    r = client.post("/api/projects/import", json=big)
    assert r.status_code == 413 and r.json()["code"] == "INVALID_PAYLOAD"
    # without Content-Length (chunked upload): the read size is checked too
    r = client.post("/api/projects/import", content=iter([b'{"pad": "', b"x" * 2048, b'"}']),
                    headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert len(client.get("/api/projects").json()) == 1
