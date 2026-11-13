"""Phase B session features: per-session model (SET_MODEL), the model registry, phonon
convergence, elastic tensor, phonon export and primitive-mode shake-tests over ``/ws/studio``.

Everything runs on the analytic fallback. The "default" model is the fallback singleton
dressed up as a MACE model (``model_id = "small"``, energies offset by ``OFFSET`` eV/atom) so
that switching to the real ``lj-fallback`` engine is observable without any download.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import numpy as np
import pytest

from app.config import Settings
from app.core import umlip_engine
from app.core.structure_io import atoms_to_poscar
from app.core.umlip_engine import ModelUnavailableError, UMLIPEngine, get_engine
from app.presets import load_preset

from test_phase_a_session import _hstudio  # noqa: F401
from test_ws_protocol import (  # noqa: F401 - fixtures are used by name
    _hang_guard,
    _wait_no_job_threads,
    client,
    slow_engine,
    ws_engine,
)

OFFSET = 0.25  # eV/atom added by the fake default model


def _fake_default(engine: UMLIPEngine, monkeypatch) -> UMLIPEngine:
    original = engine.energy_forces_stress

    def shifted(atoms):
        e, f, s = original(atoms)
        return e + OFFSET * len(atoms), f, s

    monkeypatch.setattr(engine, "energy_forces_stress", shifted)
    monkeypatch.setattr(engine, "model_id", "small")
    monkeypatch.setattr(engine, "backend_name", "fake-mace-small")
    return engine


@pytest.fixture(autouse=True)
def _clean_registry():
    umlip_engine.reset_registry()
    yield
    umlip_engine.reset_registry()


@pytest.fixture
def fake_default(ws_engine, monkeypatch):
    return _fake_default(ws_engine, monkeypatch)


@pytest.fixture
def slow_fake_default(slow_engine, monkeypatch):
    return _fake_default(slow_engine, monkeypatch)


def _session(client):
    s, cm = _hstudio(client)
    return s, cm


@pytest.fixture
def mstudio(fake_default, client):
    s, cm = _session(client)
    try:
        yield s
    finally:
        cm.__exit__(None, None, None)
    assert not _wait_no_job_threads(), "worker threads leaked after disconnect"


@pytest.fixture
def slow_mstudio(slow_fake_default, client):
    s, cm = _session(client)
    try:
        yield s
    finally:
        cm.__exit__(None, None, None)
    assert not _wait_no_job_threads(), "worker threads leaked after disconnect"


def _types(messages: List[Dict[str, Any]]) -> List[str]:
    return [m["type"] for m in messages]


def _without_progress(messages):
    return [m for m in messages if m["type"] != "PROGRESS"]


def _probe_energy(s, crystal) -> float:
    """Re-commit atom 0 at its position in ``crystal``: energy/atom with the session's engine."""
    c, _ = s.move(0, crystal["atoms"][0]["cart_coords"])
    return c["energy_per_atom"]


# --------------------------------------------------------------------------- #
# SET_MODEL
# --------------------------------------------------------------------------- #
def test_set_model_sequence_isolation_and_undo(mstudio, client):
    s = mstudio
    e_default = s.initial["energy_per_atom"]
    moved, _ = s.move(1, np.array(s.initial["atoms"][1]["cart_coords"]) + [0.05, 0.0, 0.0])

    s.send("SET_MODEL", {"model_id": "lj-fallback"})
    msgs = s.until(["HISTORY_STATE"])
    assert _types(_without_progress(msgs)) == ["MODEL_CHANGED", "SINGLE_POINT_UPDATE", "HISTORY_STATE"]
    changed = msgs[-3]["payload"]
    assert changed["model_id"] == "lj-fallback" and changed["backend_name"] == "lj-fallback"
    assert changed["dispersion"] is False
    assert msgs[-2]["payload"]["crystal"]["energy_per_atom"] == pytest.approx(moved["energy_per_atom"] - OFFSET)
    assert msgs[-1]["payload"]["label"] == "Switch model to lj-fallback"

    # A second session stays on the default model.
    other, cm = _session(client)
    try:
        assert other.initial["energy_per_atom"] == pytest.approx(e_default)
    finally:
        cm.__exit__(None, None, None)

    # Undo past the switch: the structure goes back, the model does not (re-evaluated).
    crystal, h = s.undo()
    assert h["label"] == "Move atom 1"
    assert crystal["energy_per_atom"] == pytest.approx(moved["energy_per_atom"] - OFFSET)
    crystal, _ = s.undo()
    assert crystal["energy_per_atom"] == pytest.approx(e_default - OFFSET)

    # Switching back to the default id returns to the singleton.
    s.send("SET_MODEL", {"model_id": "small"})
    msgs = _without_progress(s.until(["HISTORY_STATE"]))
    assert msgs[0]["payload"]["backend_name"] == "fake-mace-small"
    assert msgs[1]["payload"]["crystal"]["energy_per_atom"] == pytest.approx(e_default)


def test_set_model_unavailable_keeps_engine(mstudio, monkeypatch):
    s = mstudio
    s.send("SET_MODEL", {"model_id": "no-such-model"})
    s.expect_error("MODEL_UNAVAILABLE", "SET_MODEL")
    s.assert_silent(0.2)  # no history push
    assert _probe_energy(s, s.initial) == pytest.approx(s.initial["energy_per_atom"])

    # Uncached checkpoint with downloads disabled: never touches the network.
    urls = umlip_engine._known_mace_urls()
    uncached = [m for m in ("large", "medium", "medium-omat-0")
                if m in urls and not umlip_engine.mace_cached_checkpoint_path(urls[m]).is_file()]
    if not uncached:
        pytest.skip("mace not importable or every candidate checkpoint is cached")
    monkeypatch.setattr(umlip_engine, "get_settings", lambda: Settings(mace_allow_download=False))
    monkeypatch.setattr(umlip_engine, "_download_with_deadline",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("download attempted")))
    s.send("SET_MODEL", {"model_id": uncached[0]})
    msgs = s.until(["ERROR"])
    assert msgs[-1]["payload"]["code"] == "MODEL_UNAVAILABLE"
    assert "downloads are disabled" in msgs[-1]["payload"]["message"]


def test_set_model_cancel(slow_mstudio, monkeypatch):
    s = slow_mstudio
    release = threading.Event()

    def blocked_build(model_id, dispersion, progress):
        release.wait(20)
        e = UMLIPEngine(backend="fallback")
        e.model_id = model_id
        return e

    monkeypatch.setattr(umlip_engine, "_known_mace_urls", lambda: {"medium": "https://example.invalid/m.model"})
    monkeypatch.setattr(umlip_engine, "_build_engine", blocked_build)
    try:
        s.send("SET_MODEL", {"model_id": "medium"})
        progress = s.expect("PROGRESS")
        assert progress["command"] == "SET_MODEL" and progress["total"] == 0
        s.send("STOP_STREAM")
        assert s.expect("STREAM_STOPPED")["stopped"] == "SET_MODEL"
        # Not joined: the next command answers while the load is still blocked, on the old model.
        t0 = time.perf_counter()
        assert _probe_energy(s, s.initial) == pytest.approx(s.initial["energy_per_atom"])
        assert time.perf_counter() - t0 < 5.0
    finally:
        release.set()
    s.assert_silent(0.3)  # the late load is never applied


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_registry_lru_threads_and_list(fake_default, monkeypatch):
    builds: List[tuple] = []

    def build(model_id, dispersion, progress):
        time.sleep(0.05)
        builds.append((model_id, dispersion))
        e = UMLIPEngine(backend="fallback")
        e.model_id, e.dispersion = model_id, dispersion
        return e

    monkeypatch.setattr(umlip_engine, "_known_mace_urls", lambda: {k: "u" for k in ("a", "b", "c", "small")})
    monkeypatch.setattr(umlip_engine, "_build_engine", build)

    assert get_engine() is fake_default and get_engine("small") is fake_default
    out: List[UMLIPEngine] = []
    threads = [threading.Thread(target=lambda: out.append(get_engine("a"))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert builds == [("a", False)] and all(e is out[0] for e in out)
    a = out[0]
    b, c = get_engine("b"), get_engine("c")
    assert umlip_engine.resident_models() == [("b", False), ("c", False)]  # "a" evicted (LRU <= 2)
    assert get_engine("a") is a and len(builds) == 3  # still alive (held here): reused, not reloaded
    assert get_engine("a", True) is not a  # dispersion is part of the key
    with pytest.raises(ModelUnavailableError):
        get_engine("zzz")
    with pytest.raises(ModelUnavailableError):
        get_engine("lj-fallback", True)
    del b, c

    models = umlip_engine.list_models()
    ids = [m.id for m in models]
    assert ids[-1] == "lj-fallback" and [m.id for m in models if m.is_default] == ["small"]


def test_dispersion_engine_batches_through_d3():
    if not umlip_engine.dispersion_available():
        pytest.skip("torch-dftd not installed")
    urls = umlip_engine._known_mace_urls()
    if "small" not in urls or not umlip_engine.mace_cached_checkpoint_path(urls["small"]).is_file():
        pytest.skip("MACE small checkpoint not cached")
    d3 = get_engine("small", True)
    plain = UMLIPEngine(backend="mace", model="small")
    assert d3.backend_name == "mace-mp-0-small+d3" and not d3.supports_batching
    atoms = load_preset("srtio3").repeat((2, 1, 1))
    (eb, fb, _), = d3.energy_forces_stress_batch([atoms])
    es, fs, _ = d3.energy_forces_stress(atoms)
    assert eb == pytest.approx(es, abs=1e-8) and np.allclose(fb, fs, atol=1e-8)
    assert eb < plain.energy_forces_stress(atoms)[0] - 0.1  # the D3 term is included


# --------------------------------------------------------------------------- #
# Phonons: primitive mode, convergence, export
# --------------------------------------------------------------------------- #
def test_primitive_mode_shake_test(mstudio):
    s = mstudio
    doubled = load_preset("srtio3").repeat((2, 1, 1))
    s.send("LOAD_STRUCTURE", {"text": atoms_to_poscar(doubled), "text_format": "poscar"})
    s.commit()
    s.send("RUN_PHONON_SHAKE_TEST", {"cell_mode": "primitive", "min_supercell_length": 7.0})
    result = s.expect("PHONON_ANALYSIS_COMPLETE", timeout=60.0)
    flagged = s.expect("SINGLE_POINT_UPDATE")["crystal"]
    assert result["cell_mode"] == "primitive" and result["n_primitive_atoms"] == 5
    assert all(len(row) == 15 for row in result["frequencies_thz"])
    assert all(len(m["eigenvectors_real"]) == 10 for m in result["eigenmodes"])
    actors = [a["is_unstable_actor"] for a in flagged["atoms"]]
    assert len(actors) == 10 and any(actors) and not all(actors)
    s.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 14})
    s.expect("EIGENMODE_DATA")
    s.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 15})
    s.expect_error("INDEX_OUT_OF_RANGE", "REQUEST_EIGENMODE")


def test_phonon_convergence_and_export(mstudio, tmp_path):
    s = mstudio
    s.send("CHECK_PHONON_CONVERGENCE", {})
    s.expect_error("NO_PHONON_ANALYSIS", "CHECK_PHONON_CONVERGENCE")
    s.send("EXPORT_PHONON_DATA", {})
    s.expect_error("NO_PHONON_ANALYSIS", "EXPORT_PHONON_DATA")

    s.send("RUN_PHONON_SHAKE_TEST", {})
    s.expect("PHONON_ANALYSIS_COMPLETE", timeout=60.0)
    s.expect("SINGLE_POINT_UPDATE")
    s.send("CHECK_PHONON_CONVERGENCE", {"supercell_dim": [12, 12, 12]})
    s.expect_error("STRUCTURE_TOO_LARGE", "CHECK_PHONON_CONVERGENCE")

    s.send("CHECK_PHONON_CONVERGENCE", {})
    msgs = s.until(["SINGLE_POINT_UPDATE"], timeout=120.0)
    assert msgs[0]["type"] == "PROGRESS" and msgs[0]["payload"]["command"] == "CHECK_PHONON_CONVERGENCE"
    assert _types(_without_progress(msgs)) == [
        "PHONON_CONVERGENCE_RESULT", "PHONON_ANALYSIS_COMPLETE", "SINGLE_POINT_UPDATE"]
    conv = _without_progress(msgs)[0]["payload"]
    assert conv["supercell_dim_a"] == [2, 2, 2] and conv["supercell_dim_b"] == [3, 3, 3]
    assert _without_progress(msgs)[1]["payload"]["supercell_dim"] == [3, 3, 3]
    s.assert_silent(0.2)  # no history push

    s.send("EXPORT_PHONON_DATA", {})
    export = s.expect("PHONON_EXPORT")
    assert export["filename"] == "SrTiO3_phonopy_params.yaml"
    path = tmp_path / export["filename"]
    path.write_text(export["phonopy_yaml"], encoding="utf-8")
    import phonopy

    ph = phonopy.load(str(path), produce_fc=False)
    assert np.allclose(np.diag(ph.supercell_matrix), [3, 3, 3])  # the cache is the larger supercell
    assert ph.force_constants is not None


def test_phonon_convergence_cancel_keeps_cache(slow_mstudio, slow_fake_default):
    s = slow_mstudio
    s.send("RUN_PHONON_SHAKE_TEST", {})
    first = s.expect("PHONON_ANALYSIS_COMPLETE", timeout=60.0)
    s.expect("SINGLE_POINT_UPDATE")
    slow_fake_default.slow_state["delay"] = 0.5
    s.send("CHECK_PHONON_CONVERGENCE", {})
    s.expect("PROGRESS")
    s.send("STOP_STREAM")
    assert s.until(["STREAM_STOPPED"])[-1]["payload"]["stopped"] == "CHECK_PHONON_CONVERGENCE"
    slow_fake_default.slow_state["delay"] = 0.0
    s.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
    mode = s.expect("EIGENMODE_DATA", timeout=30.0)["eigenmode"]
    assert mode["frequency_thz"] == pytest.approx(first["frequencies_thz"][0][0], abs=1e-6)


# --------------------------------------------------------------------------- #
# Elastic tensor
# --------------------------------------------------------------------------- #
def test_elastic_tensor_round_trip(mstudio):
    s = mstudio
    s.send("COMPUTE_ELASTIC_TENSOR", {"relax_ions": False, "n_strains": 2})
    msgs = s.until(["ELASTIC_TENSOR_RESULT"], timeout=60.0)
    progress = [m["payload"] for m in msgs if m["type"] == "PROGRESS"]
    assert progress and all(p["command"] == "COMPUTE_ELASTIC_TENSOR" for p in progress)
    assert progress[-1]["done"] == progress[-1]["total"]
    assert _types(_without_progress(msgs)) == ["ELASTIC_TENSOR_RESULT"]
    assert np.array(msgs[-1]["payload"]["c_ij_gpa"]).shape == (6, 6)
    s.assert_silent(0.2)  # not state-changing: no history push


def test_elastic_tensor_cancel(slow_mstudio, slow_fake_default):
    s = slow_mstudio
    slow_fake_default.slow_state["delay"] = 0.2
    s.send("COMPUTE_ELASTIC_TENSOR", {"relax_ions": True})
    s.expect("PROGRESS", timeout=30.0)
    s.send("STOP_STREAM")
    msgs = s.until(["STREAM_STOPPED"])
    assert msgs[-1]["payload"]["stopped"] == "COMPUTE_ELASTIC_TENSOR"
    assert "ELASTIC_TENSOR_RESULT" not in _types(msgs)
    s.assert_silent(0.5)
