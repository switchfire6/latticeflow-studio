"""Job queue: relax -> phonons -> follow chains run to the library, only_if_unstable skips, cancel, priority."""

from __future__ import annotations

import time

import pytest
from ase.build import bulk
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import library_routes, routes
from app.core.jobs import JobQueue
from app.core.library import Library
from app.presets import load_preset


@pytest.fixture
def env(tmp_path, fallback_engine):
    lib = Library(tmp_path / "data")
    live = {"n": 0}
    queue = JobQueue(lib, engine_for=lambda model_id, dispersion: fallback_engine,
                     live_streams=lambda: live["n"], idle_poll_s=0.05)
    app = FastAPI()
    routes.register_error_handlers(app)
    app.include_router(library_routes.router)
    app.dependency_overrides[library_routes.library_dep] = lambda: lib
    app.dependency_overrides[library_routes.queue_dep] = lambda: queue
    client = TestClient(app)
    pid = lib.default_project_id()

    def add(atoms):
        crystal = fallback_engine.evaluate(atoms).model_dump(mode="json")
        return client.post(f"/api/projects/{pid}/structures", json={"crystal": crystal}).json()["id"]

    queue.start()
    yield client, queue, live, pid, add
    queue.stop()


def wait_idle(client, timeout=180.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        body = client.get("/api/jobs?active=true").json()
        if not body["jobs"]:
            return client.get("/api/jobs").json()["jobs"]
        time.sleep(0.1)
    raise AssertionError(f"jobs still active: {body}")


def test_chain_relax_phonons_follow(env):
    client, _queue, _live, pid, add = env
    sto, mgo = add(load_preset("srtio3")), add(bulk("MgO", "rocksalt", a=4.21))   # unstable / stable (fallback)
    steps = [{"kind": "relax"}, {"kind": "phonons"}, {"kind": "follow", "only_if_unstable": True}]
    created = client.post("/api/jobs", json={"project_id": pid, "structure_ids": [sto, mgo], "steps": steps})
    assert created.status_code == 201, created.text
    jobs = created.json()
    assert [j["kind"] for j in jobs] == ["relax", "phonons", "follow"] * 2
    assert jobs[0]["settings"]["stress_tol_gpa"] == 0.05 and jobs[1]["settings"]["cell_mode"] == "primitive"
    assert jobs[1]["settings"]["min_supercell_length"] == 10.0 and jobs[2]["settings"]["amplitude"] == 0.1

    by_id = {j["id"]: j for j in wait_idle(client)}
    sto_chain = [by_id[j["id"]] for j in jobs[:3]]
    mgo_chain = [by_id[j["id"]] for j in jobs[3:]]
    assert [j["status"] for j in sto_chain] == ["done", "done", "done"], sto_chain
    assert [j["status"] for j in mgo_chain] == ["done", "done", "skipped"], mgo_chain

    # relax wrote a child structure; the later steps ran on it
    relaxed = sto_chain[0]["output_structure_id"]
    child = client.get(f"/api/structures/{relaxed}").json()
    assert child["parent_id"] == sto and child["origin"] == "relax"
    assert sto_chain[1]["structure_id"] == relaxed and sto_chain[2]["structure_id"] == relaxed
    phon = client.get(f"/api/results/{sto_chain[1]['result_id']}").json()
    assert phon["kind"] == "phonons" and phon["structure_id"] == relaxed and phon["model_id"] == "lj-fallback"
    assert phon["summary"]["dynamically_stable"] is False and "frequencies_thz" in phon["data"]
    relax_result = client.get(f"/api/results/{sto_chain[0]['result_id']}").json()
    assert relax_result["data"]["final"] is True and relax_result["summary"]["relax_converged"] is True

    # follow made a grandchild from the softest imaginary mode
    follow = client.get(f"/api/structures/{sto_chain[2]['output_structure_id']}").json()
    assert follow["parent_id"] == relaxed and follow["origin"].startswith("follow ") and follow["n_atoms"] > 5

    rows = {r["id"]: r for r in client.get(f"/api/projects/{pid}/structures").json()}
    assert rows[relaxed]["latest"]["steps"] >= 0 and "min_frequency_thz" in rows[relaxed]["latest"]
    assert rows[mgo_chain[1]["structure_id"]]["latest"]["dynamically_stable"] is True

    assert client.post("/api/jobs/clear-finished").json()["deleted"] == 6
    bad = client.post("/api/jobs", json={"project_id": pid, "structure_ids": [sto], "steps": [{"kind": "follow"}]})
    assert bad.status_code == 422


def test_cancel_pause_and_move(env, fallback_engine, monkeypatch):
    client, queue, live, pid, add = env
    sid = add(load_preset("srtio3"))
    live["n"] = 1                      # an interactive stream is running: nothing starts
    body = {"project_id": pid, "structure_ids": [sid], "steps": [{"kind": "relax"}, {"kind": "phonons"}]}
    a = client.post("/api/jobs", json=body).json()
    b = client.post("/api/jobs", json=body).json()
    time.sleep(0.3)
    listing = client.get("/api/jobs?active=true").json()
    assert listing["paused"] is True and all(j["status"] == "queued" for j in listing["jobs"])

    moved = client.post(f"/api/jobs/{b[0]['id']}/move", json={"position": 0}).json()
    assert moved["position"] < client.get(f"/api/jobs/{a[0]['id']}").json()["position"]
    # cancelling a queued step cancels the rest of its chain
    assert client.post(f"/api/jobs/{a[0]['id']}/cancel").json()["status"] == "cancelled"
    assert client.get(f"/api/jobs/{a[1]['id']}").json()["status"] == "cancelled"
    assert client.delete(f"/api/jobs/{b[0]['id']}").status_code == 409     # not finished

    # a running job pauses at its checkpoints while interactive work runs, and cancels there
    original = fallback_engine.energy_forces_stress
    monkeypatch.setattr(fallback_engine, "energy_forces_stress", lambda atoms: (time.sleep(0.02), original(atoms))[1])
    live["n"] = 0
    queue.wake()
    t0 = time.monotonic()
    while client.get(f"/api/jobs/{b[0]['id']}").json()["status"] != "running":
        assert time.monotonic() - t0 < 30
        time.sleep(0.02)
    live["n"] = 1
    time.sleep(0.3)
    assert client.get(f"/api/jobs/{b[0]['id']}").json()["message"].startswith("paused")
    client.post(f"/api/jobs/{b[0]['id']}/cancel")
    jobs = {j["id"]: j for j in wait_idle(client, timeout=30)}
    assert jobs[b[0]["id"]]["status"] == "cancelled" and jobs[b[1]["id"]]["status"] == "cancelled"
    assert client.delete(f"/api/jobs/{b[0]['id']}").status_code == 204
