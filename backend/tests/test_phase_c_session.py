"""Phase C1 session features over ``/ws/studio`` (fallback calculator only).

Lean by design: one main wire sequence + the key error / cancel path per feature.
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from app.api import ws_stream
from app.schemas.messages import (
    CommitteeResult,
    FormationEnergyResult,
    LocalAnalysisData,
    MDComplete,
    MDFrame,
)

from test_phase_b_session import (  # noqa: F401 - fixtures are used by name
    _clean_registry,
    _types,
    _without_progress,
    fake_default,
    mstudio,
    slow_fake_default,
    slow_mstudio,
)
from test_ws_protocol import (  # noqa: F401
    _hang_guard,
    assert_same_structure,
    client,
    slow_engine,
    ws_engine,
)


def _active_equals(s, crystal) -> None:
    """The server's active structure equals ``crystal`` (re-commit atom 0 at its own position)."""
    probed, _ = s.move(0, crystal["atoms"][0]["cart_coords"])
    assert_same_structure(probed, crystal)


# --------------------------------------------------------------------------- #
# START_MD
# --------------------------------------------------------------------------- #
def test_md_stream_complete_and_history(mstudio):
    s = mstudio
    s.send("START_MD", {"temperature_k": 300, "ensemble": "NVT", "n_steps": 25, "frame_interval": 10, "seed": 3})
    msgs = s.until(["HISTORY_STATE"])
    assert _types(msgs) == ["MD_FRAME"] * 4 + ["MD_COMPLETE", "HISTORY_STATE"]
    frames = [MDFrame.model_validate(m["payload"]) for m in msgs[:4]]
    assert [f.step for f in frames] == [0, 10, 20, 25]
    assert frames[0].temperature_k == pytest.approx(300.0, rel=1e-6)  # rescaled MB start
    assert all(f.pressure_gpa is not None and all(v >= 0 for v in f.msd_by_element.values()) for f in frames)
    done = MDComplete.model_validate(msgs[4]["payload"])
    assert done.n_steps == 25 and done.time_ps == pytest.approx(0.025)
    assert set(done.msd_by_element) == {"Sr", "Ti", "O"}
    assert all(d is None for d in done.diffusion_coefficients_cm2_s.values())  # vibration only
    h = msgs[5]["payload"]
    assert h["label"] == "MD 300 K NVT (0.025 ps)"
    assert [e["label"] for e in h["entries"]] == ["Load preset srtio3", h["label"]]
    assert h["entries"][1]["n_atoms"] == 5
    _active_equals(s, done.final_crystal.model_dump(mode="json"))


def test_md_stop_and_divergence(slow_mstudio):
    s = slow_mstudio
    s.send("START_MD", {"n_steps": 100000, "frame_interval": 2, "seed": 1})
    first = s.expect("MD_FRAME")
    s.send("STOP_STREAM")
    msgs = s.until(["STREAM_STOPPED"])
    last = [m["payload"] for m in msgs if m["type"] == "MD_FRAME"] or [first]
    assert msgs[-1]["payload"]["stopped"] == "START_MD"
    assert s.history()["label"] == "MD 300 K NVT (stopped)"
    _active_equals(s, last[-1]["crystal"])  # the last frame sent is the active structure

    # Li3PS4 on the LJ fallback heats up explosively: COMPUTE_FAILED, the last frame stays active.
    s.load("li3ps4")
    s.send("START_MD", {"ensemble": "NVE", "n_steps": 200, "frame_interval": 5, "seed": 1})
    msgs = s.until(["ERROR"])
    frames = [m["payload"] for m in msgs if m["type"] == "MD_FRAME"]
    assert frames and msgs[-1]["payload"]["code"] == "COMPUTE_FAILED"
    assert "diverged" in msgs[-1]["payload"]["message"]
    assert s.history()["label"] == "MD 300 K NVE (failed)"
    _active_equals(s, frames[-1]["crystal"])


# --------------------------------------------------------------------------- #
# MAKE_SUPERCELL / ADD_ATOM / REQUEST_LOCAL_ANALYSIS
# --------------------------------------------------------------------------- #
def test_supercell_add_atom_and_local_analysis(mstudio):
    s = mstudio
    s.send("MAKE_SUPERCELL", {"matrix": [[2, 0, 0], [0, 1, 0], [0, 0, 1]]})
    crystal, h = s.commit()
    assert len(crystal["atoms"]) == 10 and h["label"] == "Make supercell 2x1x1"
    s.send("MAKE_SUPERCELL", {"matrix": [[9, 0, 0], [0, 9, 0], [0, 0, 9]]})
    s.expect_error("STRUCTURE_TOO_LARGE", "MAKE_SUPERCELL")

    s.send("ADD_ATOM", {"element": "Li", "frac_coords": [0.0, 0.0, 0.0]})  # on top of Sr
    s.expect_error("INVALID_OPERATION", "ADD_ATOM")
    s.send("ADD_ATOM", {"element": "Li", "frac_coords": [0.25, 0.25, 0.25]})
    crystal, h = s.commit()
    assert len(crystal["atoms"]) == 11 and h["label"] == "Add Li at (0.250, 0.250, 0.250)"
    assert [e["n_atoms"] for e in s.last_entries] == [5, 10, 11]

    s.send("REQUEST_LOCAL_ANALYSIS", {})
    data = LocalAnalysisData.model_validate(s.expect("LOCAL_ANALYSIS_DATA"))
    assert len(data.sites) == 11
    if data.bond_ideal_lengths is not None:
        assert len(data.bond_ideal_lengths) == len(crystal["bonds"])


# --------------------------------------------------------------------------- #
# COMPUTE_FORMATION_ENERGY (wiring; the physics is BC1's defects.formation_energy)
# --------------------------------------------------------------------------- #
def test_formation_energy_wiring(mstudio, monkeypatch):
    from app.core import defects, hull_analyzer

    s = mstudio
    s.send("COMPUTE_FORMATION_ENERGY", {"bulk_position": 5})
    s.expect_error("INDEX_OUT_OF_RANGE", "COMPUTE_FORMATION_ENERGY")
    s.send("DELETE_ATOM", {"atom_index": 4})
    s.commit()

    seen = {}

    def fake(defect_atoms, bulk_atoms, engine, *, api_key, should_cancel=None, progress=None, **kw):
        seen["n"] = (len(defect_atoms), len(bulk_atoms))
        progress(0, 1, "relaxing references")
        return FormationEnergyResult(
            defect_label="V_O", delta_n={"O": -1}, e_defect_ev=-1.0, e_bulk_ev=-2.0,
            chemical_potentials={"O-rich": {"O": -4.9}}, formation_energies_ev={"O-rich": 1.0},
            min_image_distance=3.9, warnings=[], method="test",
        )

    monkeypatch.setattr(defects, "formation_energy", fake)
    s.send("COMPUTE_FORMATION_ENERGY", {"bulk_position": 0})
    msgs = s.until(["FORMATION_ENERGY_RESULT"])
    assert _types(msgs) == ["PROGRESS", "FORMATION_ENERGY_RESULT"]
    assert msgs[0]["payload"]["command"] == "COMPUTE_FORMATION_ENERGY" and seen["n"] == (4, 5)
    s.assert_silent(0.2)  # not state-changing: no history push

    def no_key(*a, **k):
        raise hull_analyzer.HullNoApiKeyError("MP_API_KEY is not configured")

    monkeypatch.setattr(defects, "formation_energy", no_key)
    s.send("COMPUTE_FORMATION_ENERGY", {"bulk_position": 0})
    err = s.expect_error("INVALID_OPERATION", "COMPUTE_FORMATION_ENERGY")
    assert "MP_API_KEY" in err["message"]


# --------------------------------------------------------------------------- #
# SUBSTITUTE_FRACTION
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(importlib.util.find_spec("app.core.ordering") is None,
                    reason="app.core.ordering (BC1) not available yet")
def test_substitute_fraction(mstudio):
    s = mstudio
    s.send("SUBSTITUTE_FRACTION", {"from_element": "Li", "to_element": "Na", "fraction": 0.5})
    s.expect_error("INVALID_OPERATION", "SUBSTITUTE_FRACTION")
    s.send("SUBSTITUTE_FRACTION", {"from_element": "O", "to_element": "S", "fraction": 0.34, "seed": 0})
    msgs = _without_progress(s.until(["HISTORY_STATE"]))
    assert _types(msgs) == ["SUBSTITUTION_RESULT", "SINGLE_POINT_UPDATE", "HISTORY_STATE"]
    assert msgs[0]["payload"]["n_substituted"] == 1
    assert sorted(a["element"] for a in msgs[1]["payload"]["crystal"]["atoms"]) == ["O", "O", "S", "Sr", "Ti"]
    assert msgs[2]["payload"]["label"] == "Substitute 1 O -> S"


# --------------------------------------------------------------------------- #
# COMMITTEE_EVALUATE ("small" is the fake default = the fallback singleton: no download)
# --------------------------------------------------------------------------- #
def test_committee(mstudio):
    s = mstudio
    s.send("COMMITTEE_EVALUATE", {"model_ids": ["lj-fallback", "small"], "relax": True})
    msgs = s.until(["COMMITTEE_RESULT"])
    progress = [m["payload"] for m in msgs if m["type"] == "PROGRESS"]
    assert [(p["done"], p["total"]) for p in progress] == [(0, 2), (1, 2), (2, 2)]
    result = CommitteeResult.model_validate(msgs[-1]["payload"])
    assert [e.model_id for e in result.entries] == ["lj-fallback", "small"]
    # Same potential (the fake default only shifts energies): no spread in anything but the energy.
    assert result.spread["pressure_gpa"] == pytest.approx(0.0, abs=1e-9)
    assert result.spread["volume_percent"] == pytest.approx(0.0, abs=1e-6)
    assert "energy" not in result.spread
    s.assert_silent(0.2)

    s.send("COMMITTEE_EVALUATE", {"model_ids": ["lj-fallback", "no-such-model"], "relax": False})
    msgs = s.until(["ERROR"])
    assert msgs[-1]["payload"]["code"] == "MODEL_UNAVAILABLE"
