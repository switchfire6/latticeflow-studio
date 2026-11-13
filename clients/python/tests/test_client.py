"""Client round trips against a real backend (fallback force field): session, streams, errors, REST."""

from __future__ import annotations

import pytest

from latticeflow_client import StreamStopped, Studio, StudioError, job_step, parse_formula


@pytest.fixture()
def studio(server_url):
    with Studio(server_url, timeout=120) as s:
        yield s


def test_connect_load_relax_phonons_undo(studio):
    # connect sequence: default preset + one history entry
    assert studio.crystal["formula"] == "SrTiO3"
    assert studio.history["length"] == 1 and not studio.history["can_undo"]

    si = studio.load(preset="si")
    assert si["formula"] == "Si" and studio.history["length"] == 2

    frames = []
    final = studio.relax(fmax=0.05, max_steps=60, on_frame=frames.append)
    assert final["final"] is True and final["crystal"]["formula"] == "Si"
    assert studio.crystal == final["crystal"]
    assert studio.history["length"] == 3 and studio.history["label"].startswith("Relaxation")

    ph = studio.phonons(supercell_dim=[2, 2, 2], cell_mode="primitive", dos=True)
    assert ph["n_primitive_atoms"] == 2 and len(ph["frequencies_thz"][0]) == 6
    assert studio.phonon["min_frequency_thz"] == ph["min_frequency_thz"]
    assert abs(sum(ph["dos"]["total_dos"]) > 0)
    q, band, f = studio.soft_mode("Gamma")
    assert band == 0 and abs(f) < 0.5   # acoustic mode at Gamma

    before = studio.undo()
    assert before["formula"] == "Si" and studio.history["position"] == 1 and studio.history["can_redo"]
    studio.redo()
    assert studio.history["position"] == 2


def test_ase_round_trip_and_supercell(studio):
    pytest.importorskip("ase")
    studio.load(preset="srtio3")
    atoms = studio.to_ase()
    assert len(atoms) == 5 and atoms.get_potential_energy() == pytest.approx(5 * studio.crystal["energy_per_atom"])
    again = studio.load(atoms=atoms)
    assert again["spacegroup_number"] == 221
    big = studio.make_supercell(2)
    assert len(big["atoms"]) == 40
    assert studio.local_analysis()["sites"]


def test_typed_errors(studio):
    studio.load(preset="si")   # a load invalidates the phonon cache
    with pytest.raises(StudioError) as err:
        studio.follow_instability(q_index=0, band_index=0)
    assert err.value.code == "NO_PHONON_ANALYSIS"
    with pytest.raises(StudioError) as err:
        studio.delete_atom(99)
    assert err.value.code == "INDEX_OUT_OF_RANGE"
    # the session is still usable afterwards
    assert studio.bulk_modulus()["v0"] > 0


def test_stop_stream(studio):
    studio.load(preset="si")
    with pytest.raises(StreamStopped):
        studio.md(temperature_k=300, n_steps=100000, frame_interval=1, seed=1, on_frame=lambda f: studio.stop())
    assert studio.history["label"].endswith("(stopped)")


def test_request_ids_history_entry_and_compare_by_position(studio):
    studio.load(preset="si")
    entry = studio.history_entry(0)
    assert entry["position"] == 0 and entry["crystal"]["formula"] == "SrTiO3"
    assert studio.server_echoes_request_ids and studio.last_reply.request_id
    assert studio.history["position"] == 1          # fetching an entry changes nothing
    cmp = studio.compare(0, 1)
    assert cmp == studio.compare(entry["crystal"])
    with pytest.raises(StudioError) as err:
        studio.history_entry(99)
    assert err.value.code == "INDEX_OUT_OF_RANGE"


def test_fallback_without_request_ids(server_url):
    # a client that sends no request_id gets none back: the ordering heuristics still work
    with Studio(server_url, timeout=120) as s:
        s.send_request_ids = False
        s.load(preset="si")
        with pytest.raises(StreamStopped):
            s.md(temperature_k=300, n_steps=100000, frame_interval=1, seed=1, on_frame=lambda f: s.stop())
        assert s.history["label"].endswith("(stopped)")
        assert s.relax(fmax=0.05, max_steps=60)["final"] and not s.server_echoes_request_ids


def test_rest(studio):
    health = studio.health()
    assert health["umlip_backend"] == "lj-fallback" and health["hull_available"] is False
    xrd = studio.diffraction(two_theta_max=60)
    assert xrd["peaks"] and max(p["intensity"] for p in xrd["peaks"]) == pytest.approx(100, abs=1e-6)
    assert studio.export("poscar").splitlines()[0]
    with pytest.raises(StudioError) as err:
        studio.hull()
    assert err.value.code == "HULL_UNAVAILABLE" and err.value.status == 503


def test_parse_formula():
    assert parse_formula("SrTiO3") == {"Sr": 1, "Ti": 1, "O": 3}
    assert parse_formula("Ba(TiO3)2") == {"Ba": 1, "Ti": 2, "O": 6}
    with pytest.raises(ValueError):
        parse_formula("Li0.5O")


def test_screen_cli_survives_bad_rows(server_url, tmp_path):
    from latticeflow_client.screen import main

    src = tmp_path / "compositions.csv"
    src.write_text("composition,target_spacegroup\nNa4Cl4,225\nXx2O,\n", encoding="utf-8")
    out = tmp_path / "results.csv"
    assert main([str(src), "--out", str(out), "--url", server_url, "--top-k", "1", "--steps", "10"]) == 0
    import csv

    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["composition"] for r in rows] == ["Na4Cl4", "Xx2O"]
    assert rows[0]["spacegroup_generated"] == "Fm-3m" and rows[0]["min_frequency_thz"] and not rows[0]["error"]
    assert rows[1]["error"].startswith("generate:")


def test_library_and_job_queue(studio, tmp_path):
    import os
    from pathlib import Path

    assert (Path(os.environ["LATTICEFLOW_DATA_DIR"]) / "library.db").exists()   # temp library, not ~/.latticeflow
    pid = studio.create_project("client test")["id"]
    studio.load(preset="si")
    saved = studio.save_structure(pid, name="Si preset")
    assert saved["name"] == "Si preset" and saved["existing"] is False
    assert studio.save_structure(pid, saved["crystal"])["existing"] is True        # dedup by content hash

    jobs = studio.submit_jobs(pid, [saved["id"]], [job_step("relax", fmax=0.05, max_steps=60),
                                                   job_step("phonons", supercell_dim=[2, 2, 2])])
    assert [j["kind"] for j in jobs] == ["relax", "phonons"] and len({j["chain_id"] for j in jobs}) == 1
    seen = []
    done = studio.wait_jobs(jobs[0]["chain_id"], poll=0.2, on_progress=seen.append, timeout=300)
    assert [j["status"] for j in done] == ["done", "done"] and seen

    child_id = done[0]["output_structure_id"]
    child = studio.structure(child_id)
    assert child["parent_id"] == saved["id"] and child["origin"] == "relax" and done[1]["structure_id"] == child_id
    row = next(r for r in studio.structures(pid) if r["id"] == child_id)
    assert row["latest"]["relax_converged"] is not None and "min_frequency_thz" in row["latest"]
    phonons = studio.result(row["latest_result_ids"]["phonons"])
    assert phonons["data"]["min_frequency_thz"] == row["latest"]["min_frequency_thz"]
    assert [r["kind"] for r in studio.results(child_id)] == ["phonons", "relax"]

    bundle = tmp_path / "project.json"
    studio.export_project(pid, str(bundle))
    copy = studio.import_project(str(bundle), name="client test copy")
    assert copy["n_structures"] == 2
    assert studio.load_structure(child_id)["formula"] == "Si"
    studio.delete_project(copy["id"])
    with pytest.raises(StudioError) as err:
        studio.cancel_job(jobs[0]["id"])
    assert err.value.code == "INVALID_OPERATION" and err.value.status == 409
