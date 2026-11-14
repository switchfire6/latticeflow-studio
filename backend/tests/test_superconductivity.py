"""Superconductivity Tier 0 (docs/design/SUPERCONDUCTIVITY_TIER0.md): Allen–Dynes reference values,
DOS descriptors, the metallicity heuristic, the job step and the REST gate. Fallback calculator, no network.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from ase.build import bulk
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import library_routes, routes
from app.core import superconductivity as sc
from app.core.jobs import HullJobContext, JobQueue
from app.core.library import Library


@pytest.mark.parametrize("lam, mu, w_log, w_2, expected", [
    (1.0, 0.10, 300, 300, 21.950675143220174),
    (1.0, 0.10, 300, 450, 22.478294763352174),
    (0.5, 0.13, 300, 400, 2.248854551769687),
    (0.1, 0.13, 300, 300, 0.0),
])
def test_allen_dynes_reference_table(lam, mu, w_log, w_2, expected):
    assert sc.allen_dynes_tc(lam, mu, w_log, w_2) == pytest.approx(expected, rel=1e-6, abs=1e-12)


def test_descriptors_of_a_narrow_peak():
    f = np.linspace(-1.0, 20.0, 2101)
    total = np.exp(-(f - 6.0) ** 2 / (2 * 0.05 ** 2))
    d = sc.eph_proxy_descriptors(f, total, {"B": 0.25 * total, "Mg": 0.75 * total}, imaginary_fraction=0.0)
    assert d.omega_log_k == pytest.approx(6.0 * sc.THZ_TO_K, rel=1e-3)
    assert d.omega_2_k == pytest.approx(6.0 * sc.THZ_TO_K, rel=1e-3)
    assert d.light_element_fraction == pytest.approx(0.25, abs=1e-9) and d.reliable
    assert len(d.tc_grid) == 12 and (d.tc_grid[0].lam, d.tc_grid[0].mu_star) == (0.3, 0.10)
    assert d.tc_ref_k == pytest.approx(sc.allen_dynes_tc(1.0, 0.10, d.omega_log_k, d.omega_2_k))
    unstable = sc.eph_proxy_descriptors(f, total, {}, imaginary_fraction=0.05)
    assert not unstable.reliable and unstable.tc_ref_k is None and unstable.warnings
    assert sc.sc_flags(unstable, "likely_insulator", dynamically_stable=False) == ["unstable", "insulating"]


@pytest.mark.parametrize("formula, verdict", [
    ("Al", "likely_metal"), ("NbN", "likely_metal"), ("NaCl", "likely_insulator"), ("SrTiO3", "likely_insulator"), ("Si", "likely_insulator"),
])
def test_metallicity_heuristic(formula, verdict):
    r = sc.assess_metallicity(_formula_atoms(formula), None, True, unavailable_reason="no key")
    assert r.verdict == verdict and r.source == "heuristic" and r.reason.startswith("no key")


def _formula_atoms(formula):
    class _F:   # assess_metallicity only needs the chemical formula without an MP client
        def get_chemical_formula(self, mode="metal"):
            return formula
    return _F()


def test_metallicity_route_without_key(fallback_engine):
    app = FastAPI()
    app.include_router(routes.router)
    routes.register_error_handlers(app)
    app.dependency_overrides[routes.mp_context] = lambda: routes.MPContext(api_key=None)
    c = TestClient(app)
    crystal = fallback_engine.evaluate(bulk("NaCl", "rocksalt", a=5.64)).model_dump(mode="json")
    r = c.post("/api/superconductivity/metallicity", json={"crystal": crystal})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "likely_insulator" and body["source"] == "heuristic" and "MP_API_KEY" in body["reason"]


def test_job_chain_phonons_superconductivity(tmp_path, fallback_engine):
    lib = Library(tmp_path / "data")
    queue = JobQueue(lib, engine_for=lambda m, d: fallback_engine, hull_context=lambda: HullJobContext(api_key=None),
                     live_streams=lambda: 0, idle_poll_s=0.05)
    app = FastAPI()
    routes.register_error_handlers(app)
    app.include_router(library_routes.router)
    app.dependency_overrides[library_routes.library_dep] = lambda: lib
    app.dependency_overrides[library_routes.queue_dep] = lambda: queue
    client = TestClient(app)
    pid = lib.default_project_id()
    crystal = fallback_engine.evaluate(bulk("MgO", "rocksalt", a=4.21)).model_dump(mode="json")
    sid = client.post(f"/api/projects/{pid}/structures", json={"crystal": crystal}).json()["id"]
    bad = client.post("/api/jobs", json={"project_id": pid, "structure_ids": [sid],
                                         "steps": [{"kind": "superconductivity"}]})
    assert bad.status_code == 422
    steps = [{"kind": "phonons"}, {"kind": "superconductivity", "settings": {"mesh": [6, 6, 6]}},
             {"kind": "follow", "only_if_unstable": True}]      # superconductivity keeps phonons readiness
    jobs = client.post("/api/jobs", json={"project_id": pid, "structure_ids": [sid], "steps": steps})
    assert jobs.status_code == 201, jobs.text
    queue.start()
    try:
        t0 = time.monotonic()
        while client.get("/api/jobs?active=true").json()["jobs"]:
            assert time.monotonic() - t0 < 180
            time.sleep(0.1)
    finally:
        queue.stop()
    done = {j["kind"]: j for j in client.get("/api/jobs").json()["jobs"]}
    assert done["phonons"]["status"] == "done" and done["superconductivity"]["status"] == "done", done
    assert done["follow"]["status"] == "skipped"
    assert "ω_log" in done["superconductivity"]["message"] and "heuristic" in done["superconductivity"]["message"]
    res = client.get(f"/api/results/{done['superconductivity']['result_id']}").json()
    assert res["kind"] == "superconductivity" and res["data"]["dos_mesh"] == [6, 6, 6]
    assert len(res["data"]["descriptors"]["tc_grid"]) == 12
    summary = res["summary"]
    assert set(summary) == {"omega_log_k", "omega_2_k", "tc_ref_k", "light_element_fraction", "metallicity",
                            "sc_flags", "sc_gate_pass"}
    assert summary["metallicity"] == "likely_insulator" and summary["sc_gate_pass"] is False
    assert "insulating" in summary["sc_flags"].split(",") and summary["omega_log_k"] > 0
    row = next(r for r in client.get(f"/api/projects/{pid}/structures").json() if r["id"] == sid)
    assert row["latest"]["tc_ref_k"] == summary["tc_ref_k"]
