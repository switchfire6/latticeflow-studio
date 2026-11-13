"""Phase A session features (IMPROVEMENT_SUGGESTIONS.md §3.3, §3.4, §4.1, §4.3, §4.4, §5.1).

* relaxer: ``fix_symmetry``, ``pressure_gpa``, ``stress_tol_gpa``, ``optimizer``
  and :func:`bulk_modulus_result` (fallback + MACE numbers);
* ``/ws/studio``: COMPUTE_BULK_MODULUS, REFINE_STRUCTURE, UNDO / REDO with
  HISTORY_STATE, REQUEST_PHONON_DOS, LOAD_STRUCTURE ``text`` / ``crystal``, and
  the new START_RELAXATION options.

Everything except the ``requires_mace`` tests runs on the analytic fallback.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import numpy as np
import pytest
import spglib
from ase import Atoms
from ase.constraints import FixAtoms

from app.api import ws_stream
from app.core import relaxer
from app.core.relaxer import (
    BULK_STRAINS,
    bulk_modulus_result,
    bulk_modulus_strains,
    fit_bulk_modulus,
    iter_relaxation,
)
from app.core.structure_io import atoms_to_poscar
from app.core.umlip_engine import EV_PER_A3_TO_GPA, EngineCalculator, UMLIPEngine
from app.presets import load_preset
from app.schemas.messages import BulkModulusResult, HistoryState

# Shared driver, fixtures and helpers of the Phase 3 protocol tests.
from test_ws_protocol import (  # noqa: F401 - fixtures are used by name
    Studio,
    _hang_guard,
    _install,
    _long_relax,
    _until_final,
    _wait_no_job_threads,
    assert_same_structure,
    client,
    slow_engine,
    ws_engine,
)

SRTIO3_FLOW = {"elements": ["Sr", "Ti", "O"], "composition": {"Sr": 1, "Ti": 1, "O": 3},
               "target_spacegroup": 221, "seed": 7, "steps": 8}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class HStudio(Studio):
    """Driver that does *not* hide HISTORY_STATE."""

    skip_history = False

    def history(self) -> Dict[str, Any]:
        state = self.expect("HISTORY_STATE")
        HistoryState.model_validate(state)
        # Phase C1: the full entry list is checked in test_session_md_defects.py; keep it aside.
        self.last_entries = state.pop("entries", None)
        return state

    def commit(self) -> Dict[str, Any]:
        """SINGLE_POINT_UPDATE immediately followed by HISTORY_STATE -> (crystal, history)."""
        crystal = self.expect("SINGLE_POINT_UPDATE")["crystal"]
        return crystal, self.history()

    def load(self, preset_id: str) -> Dict[str, Any]:
        self.send("LOAD_STRUCTURE", {"preset_id": preset_id})
        return self.commit()[0]

    def move(self, index: int, coords) -> Dict[str, Any]:
        self.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": index, "new_cart_coords": list(map(float, coords))})
        return self.commit()

    def undo(self):
        self.send("UNDO")
        return self.commit()

    def redo(self):
        self.send("REDO")
        return self.commit()


def _hstudio(client):
    ws_cm = client.websocket_connect("/ws/studio")
    ws = ws_cm.__enter__()
    s = HStudio(ws)
    s.initial = s.expect("SINGLE_POINT_UPDATE")["crystal"]
    s.initial_history = s.history()
    return s, ws_cm


@pytest.fixture
def hstudio(ws_engine, client):
    s, cm = _hstudio(client)
    try:
        yield s
    finally:
        cm.__exit__(None, None, None)
    assert not _wait_no_job_threads(), "worker threads leaked after disconnect"


@pytest.fixture
def slow_hstudio(slow_engine, client):
    s, cm = _hstudio(client)
    try:
        yield s
    finally:
        cm.__exit__(None, None, None)
    assert not _wait_no_job_threads(), "worker threads leaked after disconnect"


def _types(messages: List[Dict[str, Any]]) -> List[str]:
    return [m["type"] for m in messages]


def _spacegroup(atoms: Atoms, symprec: float = 1e-2) -> int:
    ds = spglib.get_symmetry_dataset((atoms.cell.array, atoms.get_scaled_positions(), atoms.numbers), symprec=symprec)
    return int(ds.number if hasattr(ds, "number") else ds["number"])


def _rattled(name: str, sigma: float = 0.05, seed: int = 1) -> Atoms:
    atoms = load_preset(name)
    atoms.positions += np.random.default_rng(seed).normal(0.0, sigma, atoms.positions.shape)
    return atoms


def _polar_srtio3(noise: float = 1e-3) -> Atoms:
    """SrTiO3 with Ti displaced along z (P4mm) plus noise below FixSymmetry's symprec."""
    atoms = load_preset("srtio3")
    ti = [i for i, s in enumerate(atoms.get_chemical_symbols()) if s == "Ti"][0]
    atoms.positions[ti, 2] += 0.15
    atoms.positions += np.random.default_rng(3).normal(0.0, noise, atoms.positions.shape)
    return atoms


class CountingEngine:
    """Delegates to a real engine and counts single-structure model calls."""

    def __init__(self, base: UMLIPEngine):
        self.base = base
        self.calls = 0

    def energy_forces_stress(self, atoms):
        self.calls += 1
        return self.base.energy_forces_stress(atoms)

    def get_calculator(self):
        return EngineCalculator(self)

    def payload_from_results(self, *args, **kwargs):
        return self.base.payload_from_results(*args, **kwargs)


# =========================================================================== #
# relaxer: options
# =========================================================================== #
@pytest.mark.parametrize("optimizer", ["BFGS", "LBFGS"])
@pytest.mark.parametrize("preset", ["si", "srtio3"])
def test_quasi_newton_optimizers_converge(engine, preset, optimizer):
    if engine.backend_name == "lj-fallback" and preset == "si" and optimizer == "LBFGS":
        pytest.skip("plain ASE LBFGS + FrechetCellFilter also fails on the LJ fallback's 3x Si volume collapse")
    atoms = _rattled(preset)
    counter = CountingEngine(engine)
    frames = list(iter_relaxation(atoms, counter, optimizer=optimizer, max_steps=400))
    assert frames[-1].final and frames[-1].converged, (len(frames), frames[-1].crystal.max_force)
    assert frames[-1].crystal.max_force < 0.02
    assert counter.calls == len(frames)  # one model call per geometry, for every optimizer
    np.testing.assert_allclose(atoms.positions, [s.cart_coords for s in frames[-1].crystal.atoms], atol=1e-9)
    assert atoms.calc is None


@pytest.mark.parametrize("optimizer, relax_cell", [("BFGS", True), ("BFGS", False), ("LBFGS", False)])
def test_quasi_newton_survives_wrapping(fallback_engine, optimizer, relax_cell):
    """Atoms crossing the cell boundary (wrapped every step) must not corrupt the BFGS/LBFGS memory.

    (Without shifting the optimizer's remembered point by the wrap, these runs do not converge in 400 steps.)
    """
    base = _rattled("si", sigma=0.05, seed=1)
    ref = base.copy()
    n_ref = len(list(iter_relaxation(ref, fallback_engine, optimizer=optimizer, max_steps=400, relax_cell=relax_cell)))
    shifted = base.copy()
    # Put an atom right at the boundary: a rigid translation is physically irrelevant.
    shifted.positions += -shifted.positions[0] + 1e-4
    frames = list(iter_relaxation(shifted, fallback_engine, optimizer=optimizer, max_steps=400, relax_cell=relax_cell))
    assert frames[-1].converged
    assert abs(len(frames) - n_ref) <= 2, (len(frames), n_ref)
    np.testing.assert_allclose(frames[-1].crystal.energy_per_atom,
                               iter_energy(ref, fallback_engine), atol=1e-6)


def iter_energy(atoms: Atoms, engine: UMLIPEngine) -> float:
    return float(engine.energy_forces_stress(atoms)[0]) / len(atoms)


def test_unknown_optimizer_and_bad_options(fallback_engine):
    with pytest.raises(ValueError):
        iter_relaxation(load_preset("si"), fallback_engine, optimizer="GD")
    with pytest.raises(ValueError):
        iter_relaxation(load_preset("si"), fallback_engine, stress_tol_gpa=0.0)
    with pytest.raises(ValueError):
        iter_relaxation(load_preset("si"), fallback_engine, pressure_gpa=float("nan"))


def test_fix_symmetry_keeps_space_group_and_constraints(engine):
    start = _polar_srtio3()
    assert _spacegroup(start) == 99  # P4mm at symprec 1e-2
    atoms = start.copy()
    keep = FixAtoms(indices=[0])
    atoms.set_constraint(keep)
    frames = list(iter_relaxation(atoms, engine, fix_symmetry=True, max_steps=300))
    assert frames[-1].final
    # Never below the starting symmetry, and the noise below symprec is gone (refined start).
    assert _spacegroup(atoms, symprec=1e-4) in (99, 123, 221)
    assert all(f.crystal.spacegroup_number in (99, 123, 221) for f in frames)
    # Only the caller's constraint is left, and it was honoured.
    assert len(atoms.constraints) == 1 and isinstance(atoms.constraints[0], FixAtoms)
    assert atoms.calc is None
    np.testing.assert_allclose(atoms.positions, [s.cart_coords for s in frames[-1].crystal.atoms], atol=1e-9)


def test_fix_symmetry_without_frames_leaves_input_untouched(fallback_engine):
    atoms = _polar_srtio3()
    before = atoms.positions.copy(), atoms.cell.array.copy()
    calls = {"n": 0}

    def cancel_after_setup():
        calls["n"] += 1
        return calls["n"] > 1  # first poll (entry) passes, the step-1 poll cancels

    gen = iter_relaxation(atoms, fallback_engine, fix_symmetry=True, should_cancel=cancel_after_setup)
    first = next(gen)
    assert first.step == 0
    gen.close()
    assert atoms.constraints == []
    # Frame 0 is the FixSymmetry-refined start; atoms is that frame.
    np.testing.assert_allclose(atoms.positions, [s.cart_coords for s in first.crystal.atoms], atol=1e-9)
    # An engine failure before frame 0 restores the exact input (undoes the refinement).
    atoms2 = _polar_srtio3()

    class Broken(CountingEngine):
        def energy_forces_stress(self, a):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        list(iter_relaxation(atoms2, Broken(fallback_engine), fix_symmetry=True))
    np.testing.assert_array_equal(atoms2.positions, before[0])
    np.testing.assert_array_equal(atoms2.cell.array, before[1])
    assert atoms2.constraints == []


def test_pressure_shrinks_the_relaxed_volume(engine):
    volumes = {}
    for p in (0.0, 10.0):
        atoms = load_preset("srtio3")
        frames = list(iter_relaxation(atoms, engine, pressure_gpa=p, fmax=0.01, max_steps=400))
        assert frames[-1].converged
        volumes[p] = atoms.get_volume()
        pressure = -np.mean(frames[-1].crystal.stress_gpa[:3])
        assert pressure == pytest.approx(p, abs=0.5)  # the cell sits at the target pressure
    assert volumes[10.0] < volumes[0.0] * 0.99


def test_pressure_ignored_without_cell_relaxation(fallback_engine):
    a, b = load_preset("si"), load_preset("si")
    fa = list(iter_relaxation(a, fallback_engine, relax_cell=False))
    fb = list(iter_relaxation(b, fallback_engine, relax_cell=False, pressure_gpa=20.0, stress_tol_gpa=1e-3))
    assert len(fa) == len(fb)
    np.testing.assert_array_equal(a.positions, b.positions)


def test_stress_tol_makes_convergence_stricter(engine):
    loose = load_preset("srtio3")
    f_loose = list(iter_relaxation(loose, engine, max_steps=500))
    strict = load_preset("srtio3")
    f_strict = list(iter_relaxation(strict, engine, max_steps=500, stress_tol_gpa=0.01))
    assert f_loose[-1].converged and f_strict[-1].converged
    assert len(f_strict) >= len(f_loose)
    assert float(np.abs(f_strict[-1].crystal.stress_gpa).max()) < 0.01
    # And relative to a target pressure: sigma + P delta.
    hp = load_preset("srtio3")
    f_hp = list(iter_relaxation(hp, engine, max_steps=500, pressure_gpa=5.0, stress_tol_gpa=0.02))
    assert f_hp[-1].converged
    sig = np.array(f_hp[-1].crystal.stress_gpa)
    sig[:3] += 5.0
    assert float(np.abs(sig).max()) < 0.02


def test_defaults_unchanged(fallback_engine):
    """Explicit defaults reproduce the default call bit for bit."""
    a, b = _rattled("srtio3"), _rattled("srtio3")
    fa = list(iter_relaxation(a, fallback_engine, max_steps=40))
    fb = list(iter_relaxation(b, fallback_engine, max_steps=40, fix_symmetry=False, pressure_gpa=0.0,
                              stress_tol_gpa=None, optimizer="FIRE"))
    assert [f.model_dump() for f in fa] == [f.model_dump() for f in fb]


# =========================================================================== #
# relaxer: bulk modulus
# =========================================================================== #
def test_bulk_strains_grid():
    assert bulk_modulus_strains() == list(BULK_STRAINS)
    eps = bulk_modulus_strains(0.06, 9)
    assert len(eps) == 9 and eps[0] == pytest.approx(-0.06) and eps[-1] == pytest.approx(0.06) and 0.0 in eps
    with pytest.raises(ValueError):
        bulk_modulus_strains(0.0, 5)


def test_fit_bulk_modulus_custom_strains(fallback_engine):
    atoms = load_preset("srtio3")
    default = fit_bulk_modulus(atoms, fallback_engine)
    same = fit_bulk_modulus(atoms, fallback_engine, strains=list(BULK_STRAINS))
    assert same.k_gpa == default.k_gpa and same.pressure_gpa == default.pressure_gpa
    even = fit_bulk_modulus(atoms, fallback_engine, strains=bulk_modulus_strains(0.03, 6))  # no V0 point
    assert even is not None and len(even.volumes) == 6
    assert even.k_gpa == pytest.approx(default.k_gpa, rel=0.05)
    assert even.pressure_gpa == pytest.approx(default.pressure_gpa, abs=0.05 * abs(default.k_gpa) * 0.01 + 0.05)
    with pytest.raises(ValueError):
        fit_bulk_modulus(atoms, fallback_engine, strains=[0.0, 0.01, 0.02])


def test_bulk_modulus_result_fallback(fallback_engine):
    atoms = load_preset("srtio3")
    before = atoms.positions.copy()
    r = bulk_modulus_result(atoms, fallback_engine, strain_window=0.04, n_points=7)
    assert isinstance(r, BulkModulusResult)
    BulkModulusResult.model_validate(r.model_dump())
    assert len(r.volumes) == len(r.energies_ev) == len(r.pressures_gpa) == 7
    assert r.v0 == pytest.approx(atoms.get_volume())
    assert min(r.volumes) == pytest.approx(atoms.get_volume() * 0.96)
    assert r.k_gpa is not None and r.k_gpa > 0 and r.k_stress_gpa > 0
    assert "clamped-ion" in r.method and "4 %" in r.method
    if r.v_eq is not None:
        assert r.extrapolated == (not min(r.volumes) <= r.v_eq <= max(r.volumes))
    np.testing.assert_array_equal(atoms.positions, before)


def test_bulk_modulus_result_rejected_fit_keeps_raw_data(fallback_engine, monkeypatch):
    monkeypatch.setattr(relaxer, "K_CONSISTENCY_RTOL", -1.0)  # reject every fit
    r = bulk_modulus_result(load_preset("si"), fallback_engine)
    assert r.k_gpa is None and r.v_eq is None and r.k_eq_gpa is None and r.k_eq_prime is None
    assert r.extrapolated is False
    assert len(r.volumes) == len(r.energies_ev) == len(r.pressures_gpa) == 5
    assert "rejected" in r.method
    assert fit_bulk_modulus(load_preset("si"), fallback_engine) is None


def test_bulk_modulus_result_non_periodic(fallback_engine):
    molecule = Atoms("H2O", positions=[[0, 0, 0], [0.96, 0, 0], [-0.24, 0.93, 0]], pbc=False)
    with pytest.raises(ValueError):
        bulk_modulus_result(molecule, fallback_engine)


@pytest.mark.requires_mace
def test_bulk_modulus_result_mace_srtio3(mace_engine):
    r = bulk_modulus_result(load_preset("srtio3"), mace_engine)
    print(f"\n[measure] MACE SrTiO3 EOS (±2 %, 5 pts): K(V0) {r.k_gpa:.1f} GPa, P {r.pressure_gpa:.3f} GPa, "
          f"V_eq {r.v_eq:.3f} A^3, B0 {r.k_eq_gpa:.1f} GPa, B0' {r.k_eq_prime:.2f}")
    assert r.k_gpa == pytest.approx(185.0, abs=2.0)
    assert r.k_stress_gpa == pytest.approx(r.k_gpa, rel=0.01)
    assert r.pressure_gpa == pytest.approx(5.08, abs=0.05)
    assert r.v_eq == pytest.approx(61.3, abs=0.1)
    assert r.k_eq_gpa == pytest.approx(161.0, abs=3.0)
    assert r.extrapolated is True  # V_eq lies beyond +2 %
    wide = bulk_modulus_result(load_preset("srtio3"), mace_engine, strain_window=0.06, n_points=9)
    print(f"[measure] MACE SrTiO3 EOS (±6 %, 9 pts): B0 {wide.k_eq_gpa:.1f} GPa, B0' {wide.k_eq_prime:.2f}")
    assert wide.extrapolated is False
    assert 3.0 < wide.k_eq_prime < 6.0
    assert wide.v_eq == pytest.approx(r.v_eq, rel=0.002)
    assert wide.k_eq_gpa == pytest.approx(r.k_eq_gpa, rel=0.03)


# =========================================================================== #
# /ws/studio: history
# =========================================================================== #
def test_initial_history_state(hstudio):
    h = hstudio.initial_history
    assert h == {"can_undo": False, "can_redo": False, "position": 0, "length": 1,
                 "label": "Load preset srtio3"}
    hstudio.assert_silent(0.2)


def test_undo_redo_round_trip(hstudio):
    si = hstudio.load("si")
    hstudio.send("UNDO")
    back, h = hstudio.commit()
    assert_same_structure(back, hstudio.initial)
    assert h == {"can_undo": False, "can_redo": True, "position": 0, "length": 2, "label": "Load preset srtio3"}
    hstudio.send("UNDO")
    hstudio.expect_error("INVALID_OPERATION", "UNDO")
    fwd, h = hstudio.redo()
    assert_same_structure(fwd, si)
    assert h["position"] == 1 and h["can_undo"] and not h["can_redo"] and h["label"] == "Load preset si"
    hstudio.send("REDO")
    hstudio.expect_error("INVALID_OPERATION", "REDO")
    hstudio.assert_silent(0.2)


def test_drag_gesture_is_one_undo_step(hstudio):
    c0 = hstudio.initial
    p1 = np.array(c0["atoms"][1]["cart_coords"])
    labels = []
    for k in range(1, 6):  # one gesture on atom 1 (each reply awaited: no FIFO coalescing)
        crystal, h = hstudio.move(1, p1 + 0.02 * k)
        assert h["position"] == 1 and h["length"] == 2
        labels.append(h["label"])
    assert set(labels) == {"Move atom 1"}
    after_drag1 = crystal
    p2 = np.array(c0["atoms"][2]["cart_coords"])
    after_drag2, h = hstudio.move(2, p2 + 0.05)
    assert h["position"] == 2 and h["label"] == "Move atom 2"
    # A transmute always pushes, also twice in a row on the same atom.
    hstudio.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 1, "new_element": "Zr"})
    _, h = hstudio.commit()
    assert h["position"] == 3 and h["label"] == "Transmute atom 1 to Zr"
    hstudio.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 1, "new_element": "Hf"})
    _, h = hstudio.commit()
    assert h["position"] == 4
    # A drag after a transmute starts a new gesture even on the same atom.
    _, h = hstudio.move(1, p1)
    assert h["position"] == 5
    # Walk back: each gesture is one step.
    for _ in range(2):
        hstudio.undo()
    crystal, h = hstudio.undo()
    assert_same_structure(crystal, after_drag2)
    crystal, h = hstudio.undo()
    assert_same_structure(crystal, after_drag1)
    crystal, h = hstudio.undo()
    assert_same_structure(crystal, c0)
    assert h["position"] == 0 and h["length"] == 6


def test_drag_after_undo_pushes_and_truncates(hstudio):
    p1 = np.array(hstudio.initial["atoms"][1]["cart_coords"])
    hstudio.move(1, p1 + 0.03)
    hstudio.move(1, p1 + 0.06)
    hstudio.load("si")
    hstudio.undo()  # back to the drag entry
    _, h = hstudio.move(1, p1 + 0.09)  # same atom, but the gesture was closed by UNDO
    assert h == {"can_undo": True, "can_redo": False, "position": 2, "length": 3, "label": "Move atom 1"}


def test_push_after_undo_truncates_redo_branch(hstudio):
    hstudio.load("si")
    hstudio.load("batio3")
    hstudio.undo()
    _, h = hstudio.undo()
    assert h["position"] == 0 and h["length"] == 3
    li = hstudio.load("li3ps4")
    hstudio.send("LOAD_STRUCTURE", {"preset_id": "li3ps4"})
    _, h = hstudio.commit()
    assert h == {"can_undo": True, "can_redo": False, "position": 2, "length": 3, "label": "Load preset li3ps4"}
    hstudio.send("REDO")
    hstudio.expect_error("INVALID_OPERATION", "REDO")
    crystal, h = hstudio.undo()
    assert_same_structure(crystal, li)
    crystal, h = hstudio.undo()
    assert_same_structure(crystal, hstudio.initial)


def test_history_limit(hstudio):
    limit = ws_stream.HISTORY_LIMIT
    for k in range(limit + 5):
        hstudio.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 0, "new_element": "Ba" if k % 2 else "Ca"})
        _, h = hstudio.commit()
    assert h["length"] == limit and h["position"] == limit - 1
    for _ in range(limit - 1):
        _, h = hstudio.undo()
    assert h["position"] == 0 and not h["can_undo"] and h["can_redo"]
    assert h["label"].startswith("Transmute atom 0")  # the initial load fell off the bottom
    hstudio.send("UNDO")
    hstudio.expect_error("INVALID_OPERATION", "UNDO")


def test_delete_and_refine_push(hstudio):
    hstudio.send("DELETE_ATOM", {"atom_index": 4})
    crystal, h = hstudio.commit()
    assert len(crystal["atoms"]) == 4 and h["label"] == "Delete atom 4 (O)" and h["position"] == 1
    crystal, h = hstudio.undo()
    assert len(crystal["atoms"]) == 5


def test_undo_after_relaxation_returns_to_pre_relax(hstudio):
    c0 = hstudio.initial
    moved, h = hstudio.move(1, np.array(c0["atoms"][1]["cart_coords"]) + [0.1, -0.05, 0.08])
    assert h["position"] == 1
    hstudio.send("START_RELAXATION", {"max_steps": 300})
    msgs = _until_final(hstudio)
    final = msgs[-1]["payload"]
    assert final["final"]
    assert "HISTORY_STATE" not in _types(msgs[:-1])  # intermediate frames never push
    h = hstudio.history()  # immediately after the final frame
    assert h["label"] == ("Relaxation (converged)" if final["converged"] else "Relaxation (max steps)")
    assert h["position"] == 2 and h["length"] == 3
    crystal, h = hstudio.undo()
    assert_same_structure(crystal, moved)
    assert h["label"] == "Move atom 1"
    crystal, h = hstudio.redo()
    assert_same_structure(crystal, final["crystal"])
    hstudio.assert_silent(0.2)


def test_relaxation_max_steps_label(hstudio):
    hstudio.move(1, np.array(hstudio.initial["atoms"][1]["cart_coords"]) + 0.1)
    hstudio.send("START_RELAXATION", {"max_steps": 2, "fmax": 1e-6})
    msgs = _until_final(hstudio)
    assert len(msgs) == 3 and not msgs[-1]["payload"]["converged"]
    assert hstudio.history()["label"] == "Relaxation (max steps)"


def test_auto_relax_pushes_twice(hstudio):
    p = np.array(hstudio.initial["atoms"][1]["cart_coords"]) + 0.08
    hstudio.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 1, "new_cart_coords": p.tolist(), "auto_relax": True})
    _, h = hstudio.commit()
    assert h["label"] == "Move atom 1" and h["position"] == 1
    _until_final(hstudio)
    h = hstudio.history()
    assert h["label"].startswith("Relaxation (") and h["position"] == 2


def test_stop_stream_records_stopped_entry(slow_hstudio):
    s = slow_hstudio
    s.load("li3ps4")
    li = None
    s.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000})
    frames = [s.expect("RELAXATION_FRAME") for _ in range(3)]
    s.send("STOP_STREAM")
    msgs = s.until(["STREAM_STOPPED"])
    assert _types(msgs[:-1]) == ["RELAXATION_FRAME"] * (len(msgs) - 1)
    assert msgs[-1]["payload"]["stopped"] == "START_RELAXATION"
    h = s.history()
    assert h["label"] == "Relaxation (stopped)" and h["position"] == 2
    last = (msgs[:-1] or [{"payload": frames[-1]}])[-1]["payload"]["crystal"]
    crystal, h = s.undo()
    assert h["label"] == "Load preset li3ps4"
    crystal, h = s.redo()
    assert_same_structure(crystal, last)  # the stopped entry is the last frame sent
    s.assert_silent(0.2)


def test_undo_cancels_running_stream(slow_hstudio):
    s = slow_hstudio
    li = s.load("li3ps4")
    s.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000})
    for _ in range(3):
        s.expect("RELAXATION_FRAME")
    s.send("UNDO")
    msgs = s.until(["STREAM_STOPPED"])
    assert msgs[-1]["payload"]["stopped"] == "START_RELAXATION"
    # Wire order: STREAM_STOPPED, HISTORY_STATE (stopped push), SINGLE_POINT_UPDATE, HISTORY_STATE (undo).
    h_stop = s.history()
    assert h_stop["label"] == "Relaxation (stopped)" and h_stop["position"] == 2 and h_stop["length"] == 3
    crystal, h = s.commit()
    assert_same_structure(crystal, li)  # back to before the stream
    assert h == {"can_undo": True, "can_redo": True, "position": 1, "length": 3, "label": "Load preset li3ps4"}
    s.assert_silent(0.3)
    assert not _wait_no_job_threads()


def test_load_during_stream_records_stopped_then_load(slow_hstudio):
    s = slow_hstudio
    s.load("li3ps4")
    s.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000})
    s.expect("RELAXATION_FRAME")
    s.expect("RELAXATION_FRAME")
    s.send("LOAD_STRUCTURE", {"preset_id": "si"})
    s.until(["STREAM_STOPPED"])
    assert s.history()["label"] == "Relaxation (stopped)"
    _, h = s.commit()
    assert h["label"] == "Load preset si" and h["position"] == 3


def test_phonon_run_does_not_push_and_undo_invalidates(hstudio):
    hstudio.load("si")
    hstudio.send("RUN_PHONON_SHAKE_TEST", {})
    hstudio.expect("PHONON_ANALYSIS_COMPLETE", timeout=60.0)
    hstudio.expect("SINGLE_POINT_UPDATE")  # flagging update: no history push
    hstudio.assert_silent(0.3)
    hstudio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
    hstudio.expect("EIGENMODE_DATA")
    hstudio.undo()
    hstudio.redo()  # same structure again, but a new version: the cache is gone
    hstudio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
    hstudio.expect_error("NO_PHONON_ANALYSIS", "REQUEST_EIGENMODE")
    hstudio.send("REQUEST_PHONON_DOS", {})
    hstudio.expect_error("NO_PHONON_ANALYSIS", "REQUEST_PHONON_DOS")


def test_flow_generation_pushes(hstudio):
    hstudio.send("START_FLOW_GENERATION", SRTIO3_FLOW)
    msgs = hstudio.until(["CRYSTAL_STATE_FINAL"], timeout=60.0)
    assert "HISTORY_STATE" not in _types(msgs)
    h = hstudio.history()
    assert h["label"] == "Flow generation" and h["position"] == 1
    crystal, _ = hstudio.undo()
    assert_same_structure(crystal, hstudio.initial)
    crystal, _ = hstudio.redo()
    assert_same_structure(crystal, msgs[-1]["payload"]["crystal"])


# =========================================================================== #
# /ws/studio: COMPUTE_BULK_MODULUS
# =========================================================================== #
def test_compute_bulk_modulus_round_trip(hstudio):
    si = hstudio.load("srtio3")  # (the LJ fallback's Si fit is rejected as inconsistent)
    assert si["bulk_modulus_gpa"] is None
    hstudio.send("COMPUTE_BULK_MODULUS", {})
    r = hstudio.expect("BULK_MODULUS_RESULT")
    BulkModulusResult.model_validate(r)
    assert len(r["volumes"]) == 5 and r["k_gpa"] > 0
    assert r["v0"] == pytest.approx(si["lattice"]["volume"], rel=1e-9)
    hstudio.assert_silent(0.2)  # no SINGLE_POINT_UPDATE / HISTORY_STATE
    # Stored on the active payload and its history entry: visible after undo + redo.
    hstudio.undo()
    crystal, _ = hstudio.redo()
    assert crystal["bulk_modulus_gpa"] == pytest.approx(r["k_gpa"])
    hstudio.send("COMPUTE_BULK_MODULUS", {"strain_window": 0.06, "n_points": 9})
    wide = hstudio.expect("BULK_MODULUS_RESULT")
    assert len(wide["volumes"]) == 9
    hstudio.send("COMPUTE_BULK_MODULUS", {"n_points": 3})
    hstudio.expect_error("INVALID_PAYLOAD", "COMPUTE_BULK_MODULUS")
    hstudio.send("COMPUTE_BULK_MODULUS", {"strain_window": 0.5})
    hstudio.expect_error("INVALID_PAYLOAD", "COMPUTE_BULK_MODULUS")


def test_compute_bulk_modulus_does_not_cancel_stream(slow_hstudio):
    s = slow_hstudio
    s.load("li3ps4")
    s.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000})
    s.expect("RELAXATION_FRAME")
    s.send("COMPUTE_BULK_MODULUS", {})
    msgs = s.until(["BULK_MODULUS_RESULT"])
    assert set(_types(msgs[:-1])) <= {"RELAXATION_FRAME"}
    s.expect("RELAXATION_FRAME")  # still streaming
    s.send("STOP_STREAM")
    s.until(["STREAM_STOPPED"])
    assert s.history()["label"] == "Relaxation (stopped)"


# =========================================================================== #
# /ws/studio: REFINE_STRUCTURE
# =========================================================================== #
def test_refine_structure(hstudio):
    c0 = hstudio.initial
    noisy, _ = hstudio.move(1, np.array(c0["atoms"][1]["cart_coords"]) + [0.001, -0.0005, 0.0003])
    assert noisy["spacegroup_number"] == 221  # within symprec, but not exact
    hstudio.send("REFINE_STRUCTURE", {})
    refined, h = hstudio.commit()
    assert refined["spacegroup_number"] == 221 and len(refined["atoms"]) == 5
    assert h["label"].startswith("Refine symmetry") and h["position"] == 2
    assert_same_structure(refined, c0, tol=1e-6)
    crystal, _ = hstudio.undo()
    assert_same_structure(crystal, noisy)


def test_refine_primitive_and_conventional(hstudio):
    si = hstudio.load("si")
    assert len(si["atoms"]) == 8
    hstudio.send("REFINE_STRUCTURE", {"cell": "primitive"})
    prim, h = hstudio.commit()
    assert len(prim["atoms"]) == 2 and prim["spacegroup_number"] == 227
    hstudio.send("REFINE_STRUCTURE", {"cell": "conventional"})
    conv, _ = hstudio.commit()
    assert len(conv["atoms"]) == 8 and conv["spacegroup_number"] == 227
    hstudio.send("REFINE_STRUCTURE", {"cell": "niggli"})
    hstudio.expect_error("INVALID_PAYLOAD", "REFINE_STRUCTURE")


def test_refine_cancels_stream(slow_hstudio):
    s = slow_hstudio
    s.load("li3ps4")
    s.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000})
    s.expect("RELAXATION_FRAME")
    s.expect("RELAXATION_FRAME")
    s.send("REFINE_STRUCTURE", {"symprec": 0.1})
    s.until(["STREAM_STOPPED"])
    assert s.history()["label"] == "Relaxation (stopped)"
    msg = s.recv()
    assert msg["type"] in ("SINGLE_POINT_UPDATE", "ERROR")
    if msg["type"] == "SINGLE_POINT_UPDATE":
        assert s.history()["label"].startswith("Refine symmetry")


# =========================================================================== #
# /ws/studio: LOAD_STRUCTURE text / crystal
# =========================================================================== #
def test_load_structure_text(hstudio):
    poscar = atoms_to_poscar(load_preset("batio3"))
    hstudio.send("LOAD_STRUCTURE", {"text": poscar, "text_format": "poscar"})
    bto, h = hstudio.commit()
    assert bto["formula"] == "BaTiO3" and len(bto["atoms"]) == 5 and h["label"] == "Load file"
    hstudio.send("LOAD_STRUCTURE", {"text": poscar})  # auto-detected
    again, _ = hstudio.commit()
    assert_same_structure(again, bto)
    hstudio.send("LOAD_STRUCTURE", {"text": "certainly not a structure file"})
    err = hstudio.expect("ERROR")
    assert err["code"] in ("INVALID_STRUCTURE", "INVALID_PAYLOAD") and err["command"] == "LOAD_STRUCTURE"
    hstudio.send("LOAD_STRUCTURE", {"text": poscar, "text_format": "pdb"})
    hstudio.expect_error("INVALID_PAYLOAD", "LOAD_STRUCTURE")
    hstudio.send("LOAD_STRUCTURE", {"text": poscar, "preset_id": "si"})
    hstudio.expect_error("INVALID_PAYLOAD", "LOAD_STRUCTURE")
    hstudio.assert_silent(0.2)


def test_load_structure_crystal_round_trip(hstudio):
    bto = hstudio.load("batio3")
    hstudio.load("si")
    hstudio.send("LOAD_STRUCTURE", {"crystal": bto})  # e.g. a client restoring its structure after a reconnect
    back, h = hstudio.commit()
    assert_same_structure(back, bto)
    assert back["energy_per_atom"] == pytest.approx(bto["energy_per_atom"], abs=1e-9)
    assert h["label"] == "Load structure" and h["position"] == 3


def test_load_structure_crystal_errors(hstudio):
    base = hstudio.initial
    singular = {**base, "lattice": {**base["lattice"], "matrix": [[1, 0, 0], [2, 0, 0], [0, 0, 1]]}}
    hstudio.send("LOAD_STRUCTURE", {"crystal": singular})
    hstudio.expect_error("INVALID_STRUCTURE", "LOAD_STRUCTURE")
    bad_atoms = [dict(a) for a in base["atoms"]]
    bad_atoms[0] = {**bad_atoms[0], "element": "Xx"}
    hstudio.send("LOAD_STRUCTURE", {"crystal": {**base, "atoms": bad_atoms}})
    hstudio.expect_error("INVALID_ELEMENT", "LOAD_STRUCTURE")
    uranium = [dict(a) for a in base["atoms"]]
    uranium[0] = {**uranium[0], "element": "U", "atomic_number": 92}
    hstudio.send("LOAD_STRUCTURE", {"crystal": {**base, "atoms": uranium}})
    hstudio.expect_error("INVALID_ELEMENT", "LOAD_STRUCTURE")
    hstudio.send("LOAD_STRUCTURE", {"crystal": {**base, "atoms": []}})
    hstudio.expect_error("INVALID_STRUCTURE", "LOAD_STRUCTURE")
    hstudio.send("LOAD_STRUCTURE", {"crystal": {"formula": "x"}})
    hstudio.expect_error("INVALID_PAYLOAD", "LOAD_STRUCTURE")
    hstudio.assert_silent(0.2)  # nothing was committed


# =========================================================================== #
# /ws/studio: START_RELAXATION options
# =========================================================================== #
def test_relaxation_options_over_ws(hstudio):
    hstudio.load("si")
    p = np.array(hstudio.initial["atoms"][0]["cart_coords"])
    hstudio.send("START_RELAXATION", {"optimizer": "LBFGS", "max_steps": 300})
    msgs = _until_final(hstudio)
    assert msgs[-1]["payload"]["converged"]
    hstudio.history()
    hstudio.send("START_RELAXATION", {"optimizer": "SD"})
    hstudio.expect_error("INVALID_PAYLOAD", "START_RELAXATION")
    hstudio.send("START_RELAXATION", {"stress_tol_gpa": 0})
    hstudio.expect_error("INVALID_PAYLOAD", "START_RELAXATION")
    # Pressure: the relaxed cell is smaller than at 0 GPa.
    volumes = {}
    for pressure in (0.0, 10.0):
        hstudio.load("srtio3")
        hstudio.send("START_RELAXATION", {"pressure_gpa": pressure, "max_steps": 400, "optimizer": "BFGS"})
        msgs = _until_final(hstudio)
        assert msgs[-1]["payload"]["converged"]
        volumes[pressure] = msgs[-1]["payload"]["crystal"]["lattice"]["volume"]
        hstudio.history()
    assert volumes[10.0] < volumes[0.0]


def test_fix_symmetry_over_ws(hstudio):
    c0 = hstudio.initial
    ti = [a["index"] for a in c0["atoms"] if a["element"] == "Ti"][0]
    polar, _ = hstudio.move(ti, np.array(c0["atoms"][ti]["cart_coords"]) + [0.0, 0.0, 0.15])
    assert polar["spacegroup_number"] == 99
    hstudio.send("START_RELAXATION", {"fix_symmetry": True, "max_steps": 300})
    msgs = _until_final(hstudio)
    assert all(m["payload"]["crystal"]["spacegroup_number"] in (99, 123, 221) for m in msgs)
    hstudio.history()
    # The session atoms carry no constraint: a free drag afterwards breaks the symmetry.
    final = msgs[-1]["payload"]["crystal"]
    moved, _ = hstudio.move(0, np.array(final["atoms"][0]["cart_coords"]) + [0.05, 0.03, 0.01])
    assert moved["spacegroup_number"] < 99


# =========================================================================== #
# /ws/studio: REQUEST_PHONON_DOS
# =========================================================================== #
def test_request_phonon_dos(hstudio):
    hstudio.load("si")
    hstudio.send("RUN_PHONON_SHAKE_TEST", {})
    hstudio.expect("PHONON_ANALYSIS_COMPLETE", timeout=60.0)
    hstudio.expect("SINGLE_POINT_UPDATE")
    hstudio.send("REQUEST_PHONON_DOS", {"mesh": [6, 6, 6], "t_max": 600, "t_step": 50})
    dos = hstudio.expect("PHONON_DOS_DATA", timeout=60.0)
    assert dos["mesh"] == [6, 6, 6]
    assert len(dos["frequencies_thz"]) == len(dos["total_dos"])
    assert set(dos["projected_dos"]) == {"Si"}
    temps = dos["thermal"]["temperatures_k"]
    assert temps[0] == pytest.approx(0.0) and temps[-1] == pytest.approx(600.0)
    hstudio.send("REQUEST_PHONON_DOS", {"t_min": 500, "t_max": 100})
    hstudio.expect_error("INVALID_PAYLOAD", "REQUEST_PHONON_DOS")
    hstudio.send("REQUEST_PHONON_DOS", {"mesh": [0, 4, 4]})
    hstudio.expect_error("INVALID_PAYLOAD", "REQUEST_PHONON_DOS")
    hstudio.assert_silent(0.2)  # read-only: no history change


# =========================================================================== #
# MACE over the socket
# =========================================================================== #
@pytest.mark.requires_mace
def test_mace_compute_bulk_modulus_ws(mace_engine, client):
    previous = _install(mace_engine)
    try:
        with client.websocket_connect("/ws/studio") as ws:
            s = HStudio(ws)
            s.expect("SINGLE_POINT_UPDATE", timeout=60.0)
            s.history()
            s.send("COMPUTE_BULK_MODULUS", {})
            r = s.expect("BULK_MODULUS_RESULT", timeout=60.0)
            assert r["k_gpa"] == pytest.approx(185.0, abs=2.0)
            assert r["k_eq_gpa"] == pytest.approx(161.0, abs=3.0)
    finally:
        UMLIPEngine.set_instance(previous)
    assert not _wait_no_job_threads()
