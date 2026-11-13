"""Verification Gate 3: ``/ws/studio`` protocol round trips (PROJECT_SPEC.md §5.2, §7 Phase 3).

Runs against a minimal FastAPI app holding only ``app.api.ws_stream.router``
(``app.main`` also needs ``routes.py``). The engine singleton is swapped for the
analytic fallback calculator, so everything except the ``requires_mace`` tests
is fast and offline.

Hang guards: every receive has a timeout (TestClient's ``receive_json`` has
none), and an autouse ``faulthandler`` watchdog dumps all stacks and aborts the
run if a single test exceeds ``TEST_TIMEOUT_S`` (pytest-timeout is not
installed).
"""

from __future__ import annotations

import faulthandler
import json
import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import anyio
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ws_stream
from app.core import relaxer
from app.core.structure_io import atoms_to_cif
from app.core.umlip_engine import UMLIPEngine
from app.presets import load_preset
from app.schemas.messages import F_TOL_THZ

RECV_TIMEOUT_S = 20.0
TEST_TIMEOUT_S = 180.0
MACE_TEST_TIMEOUT_S = 900.0
STOP_LATENCY_BOUND_S = 2.0

SRTIO3_FLOW = {
    "elements": ["Sr", "Ti", "O"],
    "composition": {"Sr": 1, "Ti": 1, "O": 3},
    "target_spacegroup": 221,
    "seed": 7,
}

DISORDERED_CIF = """data_disordered
_cell_length_a 4.0
_cell_length_b 4.0
_cell_length_c 4.0
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_symmetry_space_group_name_H-M 'P 1'
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
Na1 Na 0.0 0.0 0.0 0.5
Cl1 Cl 0.5 0.5 0.5 1.0
"""


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _hang_guard(request):
    timeout = MACE_TEST_TIMEOUT_S if request.node.get_closest_marker("requires_mace") else TEST_TIMEOUT_S
    faulthandler.dump_traceback_later(timeout, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


def _job_threads() -> List[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("ws-job-") and t.is_alive()]


def _wait_no_job_threads(timeout: float = 10.0) -> List[threading.Thread]:
    deadline = time.monotonic() + timeout
    while _job_threads() and time.monotonic() < deadline:
        time.sleep(0.02)
    return _job_threads()


def _install(engine: UMLIPEngine):
    previous = UMLIPEngine._instance
    UMLIPEngine.set_instance(engine)
    return previous


@pytest.fixture
def ws_engine():
    """A fresh fallback engine installed as the singleton (restored afterwards)."""
    engine = UMLIPEngine(backend="fallback")
    previous = _install(engine)
    yield engine
    UMLIPEngine.set_instance(previous)


@pytest.fixture
def slow_engine(ws_engine, monkeypatch):
    """The fallback singleton with every model call slowed down (deterministic long streams)."""
    original = ws_engine.energy_forces_stress
    state = {"delay": 0.01}

    def slow(atoms):
        time.sleep(state["delay"])
        return original(atoms)

    monkeypatch.setattr(ws_engine, "energy_forces_stress", slow)
    ws_engine.slow_state = state
    return ws_engine


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ws_stream.router)
    with TestClient(app) as c:
        yield c


async def _receive_with_timeout(stream, timeout: float):
    with anyio.fail_after(timeout):
        return await stream.receive()


class Studio:
    """Thin protocol driver around a TestClient websocket session.

    ``HISTORY_STATE`` events (undo history, Phase A) are skipped unless
    ``skip_history`` is False; they are covered by ``test_session_relax.py``.
    """

    skip_history = True

    def __init__(self, ws) -> None:
        self.ws = ws
        self.initial: Optional[Dict[str, Any]] = None

    # ---- raw I/O
    def send(self, type_: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.ws.send_json({"type": type_, "payload": payload if payload is not None else {}})

    def recv(self, timeout: float = RECV_TIMEOUT_S) -> Dict[str, Any]:
        while True:
            try:
                message = self.ws.portal.call(_receive_with_timeout, self.ws._send_rx, timeout)
            except TimeoutError:
                raise AssertionError(f"no server message within {timeout} s") from None
            assert message["type"] == "websocket.send", f"socket closed: {message}"
            text = message.get("text")
            if text is None:
                text = message["bytes"].decode("utf-8")
            msg = json.loads(text)
            assert set(msg) == {"type", "payload"}, msg
            if not (self.skip_history and msg["type"] == "HISTORY_STATE"):
                return msg

    def expect(self, type_: str, timeout: float = RECV_TIMEOUT_S) -> Dict[str, Any]:
        msg = self.recv(timeout)
        assert msg["type"] == type_, f"expected {type_}, got {msg['type']}: {str(msg['payload'])[:300]}"
        return msg["payload"]

    def expect_error(self, code: str, command: Optional[str]) -> Dict[str, Any]:
        payload = self.expect("ERROR")
        assert payload["code"] == code, payload
        assert payload["command"] == command, payload
        assert payload["message"]
        return payload

    def until(self, types: Iterable[str], max_messages: int = 3000,
              timeout: float = RECV_TIMEOUT_S) -> List[Dict[str, Any]]:
        """Read until a message whose type is in ``types``; return all messages read (that one last)."""
        wanted = set(types)
        out: List[Dict[str, Any]] = []
        for _ in range(max_messages):
            msg = self.recv(timeout)
            out.append(msg)
            if msg["type"] in wanted:
                return out
        raise AssertionError(f"no {sorted(wanted)} within {max_messages} messages")

    def assert_silent(self, seconds: float = 0.3) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                message = self.ws.portal.call(_receive_with_timeout, self.ws._send_rx, remaining)
            except TimeoutError:
                return
            if self.skip_history and message.get("type") == "websocket.send":
                text = message.get("text") or (message.get("bytes") or b"").decode("utf-8", "replace")
                try:
                    if json.loads(text).get("type") == "HISTORY_STATE":
                        continue
                except ValueError:
                    pass
            raise AssertionError(f"unexpected message: {str(message)[:300]}")

    # ---- protocol helpers
    def load(self, preset_id: str) -> Dict[str, Any]:
        self.send("LOAD_STRUCTURE", {"preset_id": preset_id})
        return self.expect("SINGLE_POINT_UPDATE")["crystal"]

    def probe(self, reference: Dict[str, Any]) -> Dict[str, Any]:
        """Assert the active structure equals ``reference`` (a CrystalStatePayload dict).

        Moves atom 0 onto its own position in ``reference``: the reply then equals
        ``reference`` iff the server's active structure does.
        """
        self.send("PERTURB_OR_TRANSMUTE_ATOM",
                  {"atom_index": 0, "new_cart_coords": reference["atoms"][0]["cart_coords"]})
        crystal = self.expect("SINGLE_POINT_UPDATE")["crystal"]
        assert_same_structure(crystal, reference)
        return crystal

    def phonon(self, payload: Optional[Dict[str, Any]] = None):
        self.send("RUN_PHONON_SHAKE_TEST", payload or {})
        result = self.expect("PHONON_ANALYSIS_COMPLETE", timeout=60.0)
        flagged = self.expect("SINGLE_POINT_UPDATE")["crystal"]
        return result, flagged

    def stream(self, terminal: Iterable[str]) -> List[Dict[str, Any]]:
        return self.until(terminal)


def assert_same_structure(a: Dict[str, Any], b: Dict[str, Any], tol: float = 1e-6) -> None:
    assert [s["element"] for s in a["atoms"]] == [s["element"] for s in b["atoms"]]
    la = np.array(a["lattice"]["matrix"])
    lb = np.array(b["lattice"]["matrix"])
    np.testing.assert_allclose(la, lb, atol=tol)
    fa = np.array([s["frac_coords"] for s in a["atoms"]])
    fb = np.array([s["frac_coords"] for s in b["atoms"]])
    d = fa - fb
    d -= np.round(d)
    assert float(np.abs(d @ la).max()) < tol, float(np.abs(d @ la).max())


@pytest.fixture
def studio(ws_engine, client):
    with client.websocket_connect("/ws/studio") as ws:
        s = Studio(ws)
        s.initial = s.expect("SINGLE_POINT_UPDATE")["crystal"]
        yield s
    assert not _wait_no_job_threads(), "worker threads leaked after disconnect"


@pytest.fixture
def slow_studio(slow_engine, client):
    with client.websocket_connect("/ws/studio") as ws:
        s = Studio(ws)
        s.initial = s.expect("SINGLE_POINT_UPDATE")["crystal"]
        yield s
    assert not _wait_no_job_threads(), "worker threads leaked after disconnect"


def _types(messages: List[Dict[str, Any]]) -> List[str]:
    return [m["type"] for m in messages]


def _long_relax(studio: Studio) -> None:
    """Start a relaxation that cannot finish within a test (Li3PS4, 2000 steps, fmax 1e-6)."""
    studio.load("li3ps4")
    studio.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000})


def _until_final(studio: Studio, stop_types: Iterable[str] = ("STREAM_STOPPED",),
                 max_messages: int = 5000, timeout: float = 60.0) -> List[Dict[str, Any]]:
    """Read every message until the final RELAXATION_FRAME (or a message of ``stop_types``)."""
    stop = set(stop_types)
    out: List[Dict[str, Any]] = []
    for _ in range(max_messages):
        msg = studio.recv(timeout)
        out.append(msg)
        if msg["type"] in stop or (msg["type"] == "RELAXATION_FRAME" and msg["payload"]["final"]):
            return out
    raise AssertionError(f"no final RELAXATION_FRAME within {max_messages} messages")


def _check_relaxation_frames(frames: List[Dict[str, Any]], *, complete: bool) -> None:
    assert frames, "no relaxation frames"
    steps = [f["payload"]["step"] for f in frames]
    assert steps == list(range(len(steps))), steps
    finals = [f["payload"]["final"] for f in frames]
    if complete:
        assert finals[-1] is True and not any(finals[:-1])
    else:
        assert not any(finals)


# --------------------------------------------------------------------------- #
# Connect / LOAD_STRUCTURE
# --------------------------------------------------------------------------- #
def test_connect_sends_default_preset(studio):
    c = studio.initial
    assert c["formula"] == "SrTiO3"
    assert c["spacegroup_number"] == 221
    assert len(c["atoms"]) == 5
    assert c["bonds"] and c["rdf_g"]
    studio.assert_silent(0.2)


def test_load_structure_preset_and_cif(studio):
    si = studio.load("si")
    assert si["formula"] == "Si" and len(si["atoms"]) == 8 and si["spacegroup_number"] == 227

    cif = atoms_to_cif(load_preset("batio3"))
    studio.send("LOAD_STRUCTURE", {"cif": cif})
    bto = studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    assert bto["formula"] == "BaTiO3" and len(bto["atoms"]) == 5
    studio.probe(bto)


def test_load_structure_errors_keep_connection(studio):
    studio.send("LOAD_STRUCTURE", {"preset_id": "unobtainium"})
    studio.expect_error("UNKNOWN_PRESET", "LOAD_STRUCTURE")
    studio.send("LOAD_STRUCTURE", {"cif": "this is not a CIF"})
    studio.expect_error("INVALID_STRUCTURE", "LOAD_STRUCTURE")
    studio.send("LOAD_STRUCTURE", {"cif": DISORDERED_CIF})
    studio.expect_error("DISORDERED_STRUCTURE", "LOAD_STRUCTURE")
    studio.send("LOAD_STRUCTURE", {"cif": atoms_to_cif(load_preset("srtio3").repeat((3, 3, 3)))})  # 135 atoms
    studio.expect_error("STRUCTURE_TOO_LARGE", "LOAD_STRUCTURE")
    studio.send("LOAD_STRUCTURE", {"preset_id": "si", "cif": "x"})
    studio.expect_error("INVALID_PAYLOAD", "LOAD_STRUCTURE")
    studio.send("LOAD_STRUCTURE", {})
    studio.expect_error("INVALID_PAYLOAD", "LOAD_STRUCTURE")
    # Nothing changed and the session still works.
    studio.probe(studio.initial)


# --------------------------------------------------------------------------- #
# PERTURB_OR_TRANSMUTE_ATOM / DELETE_ATOM
# --------------------------------------------------------------------------- #
def test_perturb_moves_and_wraps(studio):
    c0 = studio.initial
    a = c0["lattice"]["a"]
    target = [-0.25 * a, 0.1, 0.2]  # outside the cell along x
    studio.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 1, "new_cart_coords": target})
    c1 = studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    site = c1["atoms"][1]
    assert all(0.0 <= x < 1.0 for x in site["frac_coords"])
    np.testing.assert_allclose(site["frac_coords"][0], 0.75, atol=1e-9)
    np.testing.assert_allclose(site["cart_coords"], [0.75 * a, 0.1, 0.2], atol=1e-9)
    assert c1["max_force"] > c0["max_force"]
    assert c1["energy_per_atom"] != c0["energy_per_atom"]
    studio.probe(c1)


def test_transmute_single_and_equivalent_sites(studio):
    # Lower-case symbols are normalised.
    studio.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 2, "new_element": "f"})
    one = studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    assert [s["element"] for s in one["atoms"]] == ["Sr", "Ti", "F", "O", "O"]
    assert one["atoms"][2]["atomic_number"] == 9

    studio.load("srtio3")
    studio.send("PERTURB_OR_TRANSMUTE_ATOM",
                {"atom_index": 3, "new_element": "F", "apply_to_equivalent_sites": True})
    allf = studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    assert [s["element"] for s in allf["atoms"]] == ["Sr", "Ti", "F", "F", "F"]
    assert allf["formula"] == "SrTiF3"

    # Element + position in one command.
    studio.send("PERTURB_OR_TRANSMUTE_ATOM",
                {"atom_index": 0, "new_element": "Ba", "new_cart_coords": [0.1, 0.0, 0.0]})
    both = studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    assert both["atoms"][0]["element"] == "Ba"
    np.testing.assert_allclose(both["atoms"][0]["cart_coords"], [0.1, 0.0, 0.0], atol=1e-9)


def test_perturb_errors(studio):
    cmd = "PERTURB_OR_TRANSMUTE_ATOM"
    studio.send(cmd, {"atom_index": 5, "new_element": "O"})
    studio.expect_error("INDEX_OUT_OF_RANGE", cmd)
    for bad in ("Xx", "X", "U", ""):  # unknown, dummy, Z > 89, empty
        studio.send(cmd, {"atom_index": 0, "new_element": bad})
        studio.expect_error("INVALID_ELEMENT", cmd)
    studio.send(cmd, {"atom_index": 0})
    studio.expect_error("INVALID_PAYLOAD", cmd)
    studio.send(cmd, {"atom_index": -1, "new_element": "O"})
    studio.expect_error("INVALID_PAYLOAD", cmd)
    studio.send(cmd, {"atom_index": 0, "new_cart_coords": [1.0, 2.0]})
    studio.expect_error("INVALID_PAYLOAD", cmd)
    studio.probe(studio.initial)


def test_perturb_auto_relax_streams_relaxation(studio):
    studio.send("PERTURB_OR_TRANSMUTE_ATOM",
                {"atom_index": 1, "new_cart_coords": [2.05, 1.85, 1.95], "auto_relax": True})
    spu = studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    frames = _until_final(studio)
    _check_relaxation_frames(frames, complete=True)
    assert_same_structure(frames[0]["payload"]["crystal"], spu)
    assert frames[-1]["payload"]["crystal"]["max_force"] < spu["max_force"]
    studio.assert_silent()
    studio.probe(frames[-1]["payload"]["crystal"])


def test_coalesced_drag_perturbs_apply_latest(studio):
    xs = np.linspace(0.05, 0.6, 30)
    for x in xs:
        studio.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 0, "new_cart_coords": [float(x), 0.0, 0.0]})
    studio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})  # in-order sentinel
    messages = studio.until({"ERROR"})
    spus = messages[:-1]
    assert messages[-1]["payload"]["code"] == "NO_PHONON_ANALYSIS"
    assert spus and set(_types(spus)) == {"SINGLE_POINT_UPDATE"} and len(spus) <= len(xs)
    last = spus[-1]["payload"]["crystal"]
    np.testing.assert_allclose(last["atoms"][0]["cart_coords"], [xs[-1], 0.0, 0.0], atol=1e-9)
    studio.probe(last)


def test_delete_atom(studio):
    studio.send("DELETE_ATOM", {"atom_index": 1})
    c = studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    assert [s["element"] for s in c["atoms"]] == ["Sr", "O", "O", "O"]
    assert [s["index"] for s in c["atoms"]] == [0, 1, 2, 3]
    studio.send("DELETE_ATOM", {"atom_index": 4})
    studio.expect_error("INDEX_OUT_OF_RANGE", "DELETE_ATOM")
    for _ in range(3):
        studio.send("DELETE_ATOM", {"atom_index": 0})
        c = studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    assert len(c["atoms"]) == 1
    studio.send("DELETE_ATOM", {"atom_index": 0})
    studio.expect_error("INVALID_OPERATION", "DELETE_ATOM")
    studio.probe(c)


# --------------------------------------------------------------------------- #
# Protocol-level errors
# --------------------------------------------------------------------------- #
def test_malformed_messages_never_close_the_socket(studio):
    studio.ws.send_text("not json {")
    studio.expect_error("INVALID_MESSAGE", None)
    studio.ws.send_text("[1, 2, 3]")
    studio.expect_error("INVALID_MESSAGE", None)
    studio.ws.send_json({"payload": {}})
    studio.expect_error("INVALID_MESSAGE", None)
    studio.ws.send_json({"type": 42, "payload": {}})
    studio.expect_error("INVALID_MESSAGE", None)
    studio.ws.send_json({"type": "START_RELAXATION", "payload": "fmax"})
    studio.expect_error("INVALID_MESSAGE", None)
    studio.ws.send_bytes(b"\xff\xfe\x00")
    studio.expect_error("INVALID_MESSAGE", None)
    studio.send("MAKE_COFFEE")
    studio.expect_error("UNKNOWN_COMMAND", "MAKE_COFFEE")
    studio.send("START_RELAXATION", {"fmax": -1, "max_steps": 0})
    err = studio.expect_error("INVALID_PAYLOAD", "START_RELAXATION")
    assert "fmax" in err["message"] and "max_steps" in err["message"] and len(err["message"]) < 400
    studio.send("REQUEST_EIGENMODE", {"q_index": "a"})
    studio.expect_error("INVALID_PAYLOAD", "REQUEST_EIGENMODE")
    # A null payload is accepted as {} and binary JSON frames work too.
    studio.ws.send_json({"type": "STOP_STREAM", "payload": None})
    assert studio.expect("STREAM_STOPPED")["stopped"] is None
    studio.ws.send_json({"type": "STOP_STREAM", "payload": {}}, mode="binary")
    assert studio.expect("STREAM_STOPPED")["stopped"] is None
    studio.probe(studio.initial)


def test_compute_failed_leaves_state_untouched(studio, ws_engine, monkeypatch):
    original = ws_engine.evaluate

    def boom(atoms, bond_mode="fast"):
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(ws_engine, "evaluate", boom)
    studio.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 0, "new_element": "Ba"})
    err = studio.expect_error("COMPUTE_FAILED", "PERTURB_OR_TRANSMUTE_ATOM")
    assert "synthetic engine failure" in err["message"]
    monkeypatch.setattr(ws_engine, "evaluate", original)
    studio.probe(studio.initial)

    # A failure inside a stream is that stream's terminal message.
    calls = {"n": 0}
    efs = ws_engine.energy_forces_stress

    def fail_later(atoms):
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("synthetic failure mid-relaxation")
        return efs(atoms)

    monkeypatch.setattr(ws_engine, "energy_forces_stress", fail_later)
    studio.send("START_RELAXATION", {"fmax": 1e-6})
    messages = studio.until({"ERROR"})
    assert set(_types(messages[:-1])) <= {"RELAXATION_FRAME"}
    assert messages[-1]["payload"]["code"] == "COMPUTE_FAILED"
    assert messages[-1]["payload"]["command"] == "START_RELAXATION"
    studio.assert_silent()
    monkeypatch.setattr(ws_engine, "energy_forces_stress", efs)
    reference = messages[-2]["payload"]["crystal"] if len(messages) > 1 else studio.initial
    studio.probe(reference)


def test_no_active_structure(ws_engine, client, monkeypatch):
    original = ws_engine.evaluate
    monkeypatch.setattr(ws_engine, "evaluate", lambda atoms, bond_mode="fast": 1 / 0)
    with client.websocket_connect("/ws/studio") as ws:
        s = Studio(ws)
        s.expect_error("COMPUTE_FAILED", None)
        monkeypatch.setattr(ws_engine, "evaluate", original)
        for cmd, payload in [
            ("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 0, "new_element": "O"}),
            ("DELETE_ATOM", {"atom_index": 0}),
            ("START_RELAXATION", {}),
            ("RUN_PHONON_SHAKE_TEST", {}),
        ]:
            s.send(cmd, payload)
            s.expect_error("NO_ACTIVE_STRUCTURE", cmd)
        s.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
        s.expect_error("NO_PHONON_ANALYSIS", "REQUEST_EIGENMODE")
        crystal = s.load("srtio3")
        assert crystal["formula"] == "SrTiO3"
    assert not _wait_no_job_threads()


# --------------------------------------------------------------------------- #
# START_RELAXATION
# --------------------------------------------------------------------------- #
def test_relaxation_round_trip(studio):
    studio.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 1, "new_cart_coords": [2.1, 1.9, 2.0]})
    start = studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    studio.send("START_RELAXATION", {"fmax": 0.02, "max_steps": 200, "relax_cell": True})
    frames = _until_final(studio)
    assert set(_types(frames)) == {"RELAXATION_FRAME"}
    _check_relaxation_frames(frames, complete=True)
    last = frames[-1]["payload"]
    assert last["converged"] is True and last["crystal"]["max_force"] < 0.02
    assert_same_structure(frames[0]["payload"]["crystal"], start)
    studio.assert_silent()
    studio.send("STOP_STREAM")  # nothing running any more
    assert studio.expect("STREAM_STOPPED")["stopped"] is None
    studio.probe(last["crystal"])


def test_relaxation_max_steps_final_frame(studio):
    studio.load("li3ps4")
    studio.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 3})
    frames = _until_final(studio)
    _check_relaxation_frames(frames, complete=True)
    assert len(frames) == 4 and frames[-1]["payload"]["converged"] is False
    studio.assert_silent()


def test_relaxation_diverged(studio, monkeypatch):
    # Any cell-volume change now counts as a runaway cell: step 1 diverges.
    monkeypatch.setattr(relaxer, "MAX_VOLUME_RATIO", 1.0 + 1e-12)
    studio.send("START_RELAXATION", {"relax_cell": True})
    messages = studio.until({"ERROR"})
    assert _types(messages) == ["RELAXATION_FRAME", "RELAXATION_FRAME", "ERROR"]
    f0, f1 = messages[0]["payload"], messages[1]["payload"]
    assert (f0["step"], f0["final"]) == (0, False)
    assert (f1["step"], f1["final"], f1["converged"]) == (1, True, False)
    assert_same_structure(f1["crystal"], f0["crystal"])  # restored to the last good structure
    err = messages[2]["payload"]
    assert err["code"] == "RELAXATION_DIVERGED" and err["command"] == "START_RELAXATION"
    studio.assert_silent()
    monkeypatch.undo()
    studio.probe(f0["crystal"])


def test_stop_mid_relaxation(slow_studio):
    _long_relax(slow_studio)
    frames = [slow_studio.until({"RELAXATION_FRAME"}, max_messages=1)[0] for _ in range(3)]
    t0 = time.perf_counter()
    slow_studio.send("STOP_STREAM")
    messages = slow_studio.until({"STREAM_STOPPED"})
    latency = time.perf_counter() - t0
    assert messages[-1]["payload"]["stopped"] == "START_RELAXATION"
    frames += messages[:-1]
    assert set(_types(frames)) == {"RELAXATION_FRAME"}
    _check_relaxation_frames(frames, complete=False)
    slow_studio.assert_silent(0.5)
    print(f"\n[measure] STOP_STREAM -> STREAM_STOPPED mid-relaxation: {latency * 1e3:.1f} ms")
    assert latency < STOP_LATENCY_BOUND_S
    slow_studio.probe(frames[-1]["payload"]["crystal"])
    slow_studio.send("STOP_STREAM")
    assert slow_studio.expect("STREAM_STOPPED")["stopped"] is None


def test_stop_immediately_after_start_stops_that_stream(slow_studio):
    reference = slow_studio.load("li3ps4")
    slow_studio.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000})
    slow_studio.send("STOP_STREAM")
    messages = slow_studio.until({"STREAM_STOPPED"})
    assert messages[-1]["payload"]["stopped"] == "START_RELAXATION"
    frames = messages[:-1]
    assert set(_types(frames)) <= {"RELAXATION_FRAME"}
    slow_studio.assert_silent(0.5)
    slow_studio.probe(frames[-1]["payload"]["crystal"] if frames else reference)


# --------------------------------------------------------------------------- #
# START_FLOW_GENERATION
# --------------------------------------------------------------------------- #
def test_flow_round_trip(studio):
    steps = 12
    studio.send("START_FLOW_GENERATION", {**SRTIO3_FLOW, "steps": steps})
    messages = studio.until({"CRYSTAL_STATE_FINAL"}, timeout=60.0)
    frames = [m["payload"] for m in messages[:-1]]
    assert _types(messages) == ["FLOW_TRAJECTORY_FRAME"] * steps + ["CRYSTAL_STATE_FINAL"]
    assert [f["step"] for f in frames] == list(range(1, steps + 1))
    assert all(f["total_steps"] == steps for f in frames)
    assert frames[0]["t"] == 0.0 and frames[-1]["t"] == 1.0
    assert all(b > a for a, b in zip([f["t"] for f in frames], [f["t"] for f in frames][1:]))
    assert len({len(f["crystal"]["atoms"]) for f in frames}) == 1
    final = messages[-1]["payload"]["crystal"]
    assert "bulk_modulus_gpa" in final
    assert final["bulk_modulus_gpa"] is None or final["bulk_modulus_gpa"] > 0
    assert final["bonds"], "CrystalNN bonds expected on the final structure"
    assert final["spacegroup_number"] == 221 and final["formula"] == "SrTiO3"
    assert_same_structure(final, frames[-1]["crystal"])
    studio.assert_silent()
    studio.probe(final)


def test_flow_generation_failed(studio):
    studio.send("START_FLOW_GENERATION", {"elements": ["Sr", "O"], "composition": {"Sr": 1, "Ti": 1, "O": 3}})
    studio.expect_error("GENERATION_FAILED", "START_FLOW_GENERATION")
    studio.send("START_FLOW_GENERATION", {"elements": ["Qq"]})
    studio.expect_error("GENERATION_FAILED", "START_FLOW_GENERATION")
    studio.send("START_FLOW_GENERATION", {"elements": []})
    studio.expect_error("INVALID_PAYLOAD", "START_FLOW_GENERATION")
    studio.assert_silent()
    studio.probe(studio.initial)


def test_stop_mid_flow(slow_engine, slow_studio):
    slow_engine.slow_state["delay"] = 0.02
    slow_studio.phonon()  # a cache that the stopped flow must invalidate
    slow_studio.send("START_FLOW_GENERATION", {**SRTIO3_FLOW, "steps": 400})
    frames = [slow_studio.until({"FLOW_TRAJECTORY_FRAME"}, max_messages=1, timeout=60.0)[0] for _ in range(3)]
    t0 = time.perf_counter()
    slow_studio.send("STOP_STREAM")
    messages = slow_studio.until({"STREAM_STOPPED"})
    latency = time.perf_counter() - t0
    assert messages[-1]["payload"]["stopped"] == "START_FLOW_GENERATION"
    frames += messages[:-1]
    assert set(_types(frames)) == {"FLOW_TRAJECTORY_FRAME"}
    assert [f["payload"]["step"] for f in frames] == list(range(1, len(frames) + 1))
    assert len(frames) < 400
    slow_studio.assert_silent(0.5)
    print(f"\n[measure] STOP_STREAM -> STREAM_STOPPED mid-flow: {latency * 1e3:.1f} ms")
    assert latency < STOP_LATENCY_BOUND_S
    slow_studio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
    slow_studio.expect_error("NO_PHONON_ANALYSIS", "REQUEST_EIGENMODE")
    slow_studio.probe(frames[-1]["payload"]["crystal"])


# --------------------------------------------------------------------------- #
# RUN_PHONON_SHAKE_TEST / REQUEST_EIGENMODE
# --------------------------------------------------------------------------- #
def test_phonon_round_trip_and_eigenmodes(studio):
    result, flagged = studio.phonon()
    n = 5
    nq = len(result["kpath_distances"])
    assert len(result["frequencies_thz"]) == nq and all(len(row) == 3 * n for row in result["frequencies_thz"])
    freqs = np.array(result["frequencies_thz"])
    assert result["min_frequency_thz"] == pytest.approx(freqs.min())
    assert result["is_dynamically_stable"] is False and result["imaginary_mode_count"] > 0
    assert result["kpath_labels"][0]["label"] == "\\Gamma"
    assert result["eigenmodes"] and all(len(m["eigenvectors_real"]) == n for m in result["eigenmodes"])

    # SINGLE_POINT_UPDATE with unstable actors: same structure / energy, flags set from the softest mode.
    assert_same_structure(flagged, studio.initial)
    assert flagged["energy_per_atom"] == studio.initial["energy_per_atom"]
    actors = [s["is_unstable_actor"] for s in flagged["atoms"]]
    assert any(actors) and not all(actors)
    for b in flagged["bonds"]:
        assert b["is_unstable"] == (actors[b["source_index"]] or actors[b["target_index"]])

    # REQUEST_EIGENMODE at an arbitrary (unlabelled) path point.
    q, band = 5, 2
    studio.send("REQUEST_EIGENMODE", {"q_index": q, "band_index": band})
    mode = studio.expect("EIGENMODE_DATA")["eigenmode"]
    assert (mode["q_index"], mode["band_index"]) == (q, band)
    assert mode["frequency_thz"] == pytest.approx(freqs[q, band], abs=1e-6)
    assert mode["is_imaginary"] == (mode["frequency_thz"] < -F_TOL_THZ)
    assert len(mode["q_frac"]) == 3 and np.array(mode["eigenvectors_real"]).shape == (n, 3)
    norm = np.sum(np.array(mode["eigenvectors_real"]) ** 2 + np.array(mode["eigenvectors_imag"]) ** 2)
    assert norm == pytest.approx(1.0, abs=1e-4)

    studio.send("REQUEST_EIGENMODE", {"q_index": nq, "band_index": 0})
    studio.expect_error("INDEX_OUT_OF_RANGE", "REQUEST_EIGENMODE")
    studio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 3 * n})
    studio.expect_error("INDEX_OUT_OF_RANGE", "REQUEST_EIGENMODE")
    # The cache survives eigenmode requests and errors.
    studio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
    assert studio.expect("EIGENMODE_DATA")["eigenmode"]["q_label"] == "\\Gamma"


def test_phonon_structure_too_large(studio):
    big = load_preset("srtio3").repeat((2, 2, 4))  # 80 atoms: loadable, but > 64 for phonons
    studio.send("LOAD_STRUCTURE", {"cif": atoms_to_cif(big)})
    crystal = studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    assert len(crystal["atoms"]) == 80
    studio.send("RUN_PHONON_SHAKE_TEST", {})
    studio.expect_error("STRUCTURE_TOO_LARGE", "RUN_PHONON_SHAKE_TEST")
    studio.send("RUN_PHONON_SHAKE_TEST", {"supercell_dim": [2, 2]})
    studio.expect_error("INVALID_PAYLOAD", "RUN_PHONON_SHAKE_TEST")
    studio.assert_silent()


INVALIDATING = {
    "perturb": ("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 1, "new_cart_coords": [2.0, 1.9, 1.95]}, {"SINGLE_POINT_UPDATE"}),
    "transmute": ("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 0, "new_element": "Ba"}, {"SINGLE_POINT_UPDATE"}),
    "delete": ("DELETE_ATOM", {"atom_index": 4}, {"SINGLE_POINT_UPDATE"}),
    "load": ("LOAD_STRUCTURE", {"preset_id": "srtio3"}, {"SINGLE_POINT_UPDATE"}),
    "relax": ("START_RELAXATION", {"max_steps": 5}, set()),
    "flow": ("START_FLOW_GENERATION", {**SRTIO3_FLOW, "steps": 4}, {"CRYSTAL_STATE_FINAL"}),
}


@pytest.mark.parametrize("name", sorted(INVALIDATING))
def test_phonon_cache_invalidation(studio, name):
    studio.phonon()
    studio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
    studio.expect("EIGENMODE_DATA")
    cmd, payload, terminal = INVALIDATING[name]
    studio.send(cmd, payload)
    if terminal:
        messages = studio.until(terminal, timeout=60.0)
    else:
        messages = _until_final(studio)
    assert messages[-1]["type"] != "ERROR", messages[-1]
    for c in ("REQUEST_EIGENMODE", "FOLLOW_INSTABILITY"):
        studio.send(c, {"q_index": 0, "band_index": 0})
        studio.expect_error("NO_PHONON_ANALYSIS", c)


def test_stop_mid_phonon_latency(studio):
    """Li3PS4 (32 atoms -> 256-atom supercells) takes ~8 s with the fallback: a genuinely long job."""
    reference = studio.load("li3ps4")
    studio.send("RUN_PHONON_SHAKE_TEST", {})
    studio.assert_silent(0.5)  # the job is now inside force evaluation
    t0 = time.perf_counter()
    studio.send("STOP_STREAM")
    messages = studio.until({"STREAM_STOPPED"}, max_messages=1)
    latency = time.perf_counter() - t0
    assert messages[-1]["payload"]["stopped"] == "RUN_PHONON_SHAKE_TEST"
    studio.assert_silent(0.3)
    t1 = time.perf_counter()
    studio.probe(reference)  # waits for the worker to reach its next checkpoint
    follow_up = time.perf_counter() - t1
    print(f"\n[measure] STOP_STREAM -> STREAM_STOPPED mid-phonon: {latency * 1e3:.1f} ms; "
          f"next command answered after {follow_up * 1e3:.0f} ms")
    assert latency < STOP_LATENCY_BOUND_S
    studio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
    studio.expect_error("NO_PHONON_ANALYSIS", "REQUEST_EIGENMODE")


# --------------------------------------------------------------------------- #
# Implicit cancellation
# --------------------------------------------------------------------------- #
def test_new_stream_implicitly_cancels_old(slow_studio):
    _long_relax(slow_studio)
    first = slow_studio.until({"RELAXATION_FRAME"}, max_messages=1)
    slow_studio.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 3})
    messages = slow_studio.until({"STREAM_STOPPED"})
    assert messages[-1]["payload"]["stopped"] == "START_RELAXATION"
    old = first + messages[:-1]
    _check_relaxation_frames(old, complete=False)
    new = _until_final(slow_studio)
    _check_relaxation_frames(new, complete=True)
    assert_same_structure(new[0]["payload"]["crystal"], old[-1]["payload"]["crystal"])

    # A phonon run cancels a relaxation too, and works on the last emitted frame.
    slow_studio.load("srtio3")
    slow_studio.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000})
    frames = slow_studio.until({"RELAXATION_FRAME"}, max_messages=1)
    slow_studio.send("RUN_PHONON_SHAKE_TEST", {})
    messages = slow_studio.until({"STREAM_STOPPED"})
    assert messages[-1]["payload"]["stopped"] == "START_RELAXATION"
    frames += messages[:-1]
    slow_studio.expect("PHONON_ANALYSIS_COMPLETE", timeout=60.0)
    flagged = slow_studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    assert_same_structure(flagged, frames[-1]["payload"]["crystal"])
    slow_studio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
    slow_studio.expect("EIGENMODE_DATA")


def test_instant_command_implicitly_cancels_stream(slow_studio):
    _long_relax(slow_studio)
    frames = [slow_studio.until({"RELAXATION_FRAME"}, max_messages=1)[0] for _ in range(2)]
    slow_studio.send("DELETE_ATOM", {"atom_index": 0})
    messages = slow_studio.until({"STREAM_STOPPED"})
    assert messages[-1]["payload"]["stopped"] == "START_RELAXATION"
    frames += messages[:-1]
    _check_relaxation_frames(frames, complete=False)
    after = slow_studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    last = frames[-1]["payload"]["crystal"]
    expected = dict(last, atoms=last["atoms"][1:])
    assert_same_structure(after, expected)  # deletion applied to the last emitted frame
    slow_studio.assert_silent(0.3)

    # LOAD_STRUCTURE and PERTURB cancel as well.
    slow_studio.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000})
    slow_studio.until({"RELAXATION_FRAME"}, max_messages=1)
    slow_studio.send("LOAD_STRUCTURE", {"preset_id": "si"})
    slow_studio.until({"STREAM_STOPPED"})
    assert slow_studio.expect("SINGLE_POINT_UPDATE")["crystal"]["formula"] == "Si"
    slow_studio.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000})
    slow_studio.until({"RELAXATION_FRAME"}, max_messages=1)
    slow_studio.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 0, "new_element": "Ge"})
    messages = slow_studio.until({"STREAM_STOPPED"})
    ge = slow_studio.expect("SINGLE_POINT_UPDATE")["crystal"]
    assert ge["atoms"][0]["element"] == "Ge"
    slow_studio.assert_silent(0.3)


def test_failed_command_does_not_cancel_stream(slow_studio):
    slow_studio.send("START_RELAXATION", {"fmax": 1e-6, "max_steps": 40})
    slow_studio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})   # never cancels
    slow_studio.send("DELETE_ATOM", {"atom_index": 99})                     # rejected: no side effects
    slow_studio.send("RUN_PHONON_SHAKE_TEST", {"displacement_distance": 0})  # invalid payload
    messages = _until_final(slow_studio)
    errors = [m["payload"] for m in messages if m["type"] == "ERROR"]
    assert [(e["code"], e["command"]) for e in errors] == [
        ("NO_PHONON_ANALYSIS", "REQUEST_EIGENMODE"),
        ("INDEX_OUT_OF_RANGE", "DELETE_ATOM"),
        ("INVALID_PAYLOAD", "RUN_PHONON_SHAKE_TEST"),
    ]
    frames = [m for m in messages if m["type"] == "RELAXATION_FRAME"]
    _check_relaxation_frames(frames, complete=True)
    assert "STREAM_STOPPED" not in _types(messages)
    slow_studio.assert_silent()


# --------------------------------------------------------------------------- #
# FOLLOW_INSTABILITY
# --------------------------------------------------------------------------- #
def _commensurate(q_frac: List[float], max_multiple: int = 4) -> bool:
    return all(any(abs(x * n - round(x * n)) < 1e-6 for n in range(1, max_multiple + 1)) for x in q_frac)


def test_follow_instability_srtio3(studio):
    """The fallback calculator does give SrTiO3 imaginary modes (Gamma, X, M, R): no monkeypatching needed."""
    result, _ = studio.phonon()
    r_modes = [m for m in result["eigenmodes"] if m["q_label"] == "R" and m["is_imaginary"]]
    assert r_modes, "expected an imaginary R-point mode for cubic SrTiO3"
    mode = r_modes[0]
    cubic_energy = studio.initial["energy_per_atom"]
    studio.send("FOLLOW_INSTABILITY", {"q_index": mode["q_index"], "band_index": mode["band_index"],
                                       "amplitude": 0.1, "max_steps": 150})
    frames = _until_final(studio)
    _check_relaxation_frames(frames, complete=True)
    first, last = frames[0]["payload"]["crystal"], frames[-1]["payload"]["crystal"]
    assert len(first["atoms"]) == 40  # R = (1/2, 1/2, 1/2): 2x2x2 supercell
    assert last["energy_per_atom"] < cubic_energy
    assert last["energy_per_atom"] <= first["energy_per_atom"]
    studio.assert_silent()
    studio.probe(last)  # the supercell is now the active structure
    studio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
    studio.expect_error("NO_PHONON_ANALYSIS", "REQUEST_EIGENMODE")


def test_follow_instability_errors(studio):
    studio.send("FOLLOW_INSTABILITY", {"q_index": 0, "band_index": 0})
    studio.expect_error("NO_PHONON_ANALYSIS", "FOLLOW_INSTABILITY")
    result, _ = studio.phonon()
    freqs = np.array(result["frequencies_thz"])
    nq, nb = freqs.shape
    studio.send("FOLLOW_INSTABILITY", {"q_index": nq, "band_index": 0})
    studio.expect_error("INDEX_OUT_OF_RANGE", "FOLLOW_INSTABILITY")
    studio.send("FOLLOW_INSTABILITY", {"q_index": 0, "band_index": nb})
    studio.expect_error("INDEX_OUT_OF_RANGE", "FOLLOW_INSTABILITY")
    studio.send("FOLLOW_INSTABILITY", {"q_index": 0, "band_index": nb - 1})  # highest optical mode at Gamma
    studio.expect_error("MODE_NOT_IMAGINARY", "FOLLOW_INSTABILITY")
    studio.send("FOLLOW_INSTABILITY", {"q_index": 0, "band_index": 0, "amplitude": 0})
    studio.expect_error("INVALID_PAYLOAD", "FOLLOW_INSTABILITY")
    # The global minimum of SrTiO3 (fallback) lies at an incommensurate point of the Gamma-R line.
    q_soft = int(np.argmin(freqs[:, 0]))
    studio.send("REQUEST_EIGENMODE", {"q_index": q_soft, "band_index": 0})
    soft = studio.expect("EIGENMODE_DATA")["eigenmode"]
    assert soft["is_imaginary"] and not _commensurate(soft["q_frac"]), soft["q_frac"]
    studio.send("FOLLOW_INSTABILITY", {"q_index": q_soft, "band_index": 0})
    studio.expect_error("INVALID_OPERATION", "FOLLOW_INSTABILITY")
    studio.assert_silent()
    # Errors left the cache and the structure alone.
    studio.send("REQUEST_EIGENMODE", {"q_index": 0, "band_index": 0})
    studio.expect("EIGENMODE_DATA")
    studio.probe(studio.initial)


def test_stop_mid_follow(slow_studio):
    slow_studio.phonon()
    slow_studio.send("FOLLOW_INSTABILITY", {"q_index": 0, "band_index": 0, "fmax": 1e-6, "max_steps": 2000})
    frames = [slow_studio.until({"RELAXATION_FRAME"}, max_messages=1)[0] for _ in range(2)]
    slow_studio.send("STOP_STREAM")
    messages = slow_studio.until({"STREAM_STOPPED"})
    assert messages[-1]["payload"]["stopped"] == "FOLLOW_INSTABILITY"
    frames += messages[:-1]
    _check_relaxation_frames(frames, complete=False)
    slow_studio.assert_silent(0.3)
    slow_studio.probe(frames[-1]["payload"]["crystal"])


# --------------------------------------------------------------------------- #
# Disconnect
# --------------------------------------------------------------------------- #
def test_disconnect_mid_stream_cleans_up(slow_engine, client, caplog):
    caplog.set_level(logging.WARNING)
    for command, payload in [
        ("START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000}),
        ("START_FLOW_GENERATION", {**SRTIO3_FLOW, "steps": 400}),
    ]:
        with client.websocket_connect("/ws/studio") as ws:
            s = Studio(ws)
            s.expect("SINGLE_POINT_UPDATE")
            s.send(command, payload)
            s.until({"RELAXATION_FRAME", "FLOW_TRAJECTORY_FRAME"}, max_messages=1, timeout=60.0)
            assert _job_threads()
        assert not _wait_no_job_threads(), "worker thread survived the disconnect"
    with client.websocket_connect("/ws/studio") as ws:  # mid-phonon (inside a force chunk)
        s = Studio(ws)
        s.expect("SINGLE_POINT_UPDATE")
        s.load("li3ps4")
        s.send("RUN_PHONON_SHAKE_TEST", {})
        s.assert_silent(0.3)
    assert not _wait_no_job_threads(timeout=15.0)
    problems = [r for r in caplog.records
                if r.levelno >= logging.WARNING and (r.name.startswith("app.api") or r.name == "asyncio")]
    assert not problems, [r.getMessage() for r in problems]


# --------------------------------------------------------------------------- #
# Real MACE-MP-0
# --------------------------------------------------------------------------- #
@pytest.fixture
def mace_client(mace_engine, client):
    previous = _install(mace_engine)
    try:
        yield client
    finally:
        UMLIPEngine.set_instance(previous)


@pytest.mark.requires_mace
def test_mace_srtio3_phonon_and_follow_instability(mace_client):
    with mace_client.websocket_connect("/ws/studio") as ws:
        s = Studio(ws)
        cubic = s.expect("SINGLE_POINT_UPDATE", timeout=60.0)["crystal"]
        result, flagged = s.phonon()
        assert result["is_dynamically_stable"] is False
        assert result["min_frequency_thz"] < -F_TOL_THZ and result["imaginary_mode_count"] > 0
        assert any(a["is_unstable_actor"] for a in flagged["atoms"])
        candidates = [m for m in result["eigenmodes"] if m["is_imaginary"] and _commensurate(m["q_frac"])]
        assert candidates
        r_point = [m for m in candidates if m["q_label"] == "R"]
        mode = min(r_point or candidates, key=lambda m: m["frequency_thz"])
        s.send("FOLLOW_INSTABILITY", {"q_index": mode["q_index"], "band_index": mode["band_index"],
                                      "amplitude": 0.1, "max_steps": 200})
        frames = _until_final(s, timeout=120.0)
        _check_relaxation_frames(frames, complete=True)
        last = frames[-1]["payload"]["crystal"]
        print(f"\n[measure] MACE follow {mode['q_label']} band {mode['band_index']} "
              f"({mode['frequency_thz']:.3f} THz): E/atom {cubic['energy_per_atom']:.5f} -> "
              f"{last['energy_per_atom']:.5f} eV in {len(frames)} frames, sg {last['spacegroup_symbol']}")
        assert last["energy_per_atom"] < cubic["energy_per_atom"]
    assert not _wait_no_job_threads()


@pytest.mark.requires_mace
def test_mace_latency_and_flow_rate(mace_client):
    with mace_client.websocket_connect("/ws/studio") as ws:
        s = Studio(ws)
        start = s.expect("SINGLE_POINT_UPDATE", timeout=60.0)["crystal"]
        pos = np.array(start["atoms"][1]["cart_coords"])
        latencies = []
        for k in range(30):
            delta = 0.02 * np.sin(k)
            t0 = time.perf_counter()
            s.send("PERTURB_OR_TRANSMUTE_ATOM", {"atom_index": 1, "new_cart_coords": (pos + delta).tolist()})
            s.expect("SINGLE_POINT_UPDATE")
            latencies.append(time.perf_counter() - t0)
        lat = np.array(latencies[5:]) * 1e3
        s.send("START_FLOW_GENERATION", {**SRTIO3_FLOW, "steps": 60})
        t_first = None
        stamps = []
        messages = []
        t_send = time.perf_counter()
        while True:
            msg = s.recv(timeout=120.0)
            messages.append(msg)
            stamps.append(time.perf_counter())
            if msg["type"] == "CRYSTAL_STATE_FINAL":
                break
        frames = [t for m, t in zip(messages, stamps) if m["type"] == "FLOW_TRAJECTORY_FRAME"]
        assert len(frames) == 60
        t_first = frames[0]
        rate = (len(frames) - 1) / (frames[-1] - t_first)
        print(f"\n[measure] MACE PERTURB round trip: median {np.median(lat):.1f} ms, p90 {np.percentile(lat, 90):.1f} ms;"
              f" flow 60 steps: first frame after {(t_first - t_send) * 1e3:.0f} ms, {rate:.1f} frames/s,"
              f" final after {(stamps[-1] - frames[-1]) * 1e3:.0f} ms")
        assert rate > 0
    assert not _wait_no_job_threads()


def test_fallback_perturb_latency(studio):
    pos = np.array(studio.initial["atoms"][1]["cart_coords"])
    latencies = []
    for k in range(40):
        t0 = time.perf_counter()
        studio.send("PERTURB_OR_TRANSMUTE_ATOM",
                    {"atom_index": 1, "new_cart_coords": (pos + 0.02 * np.sin(k)).tolist()})
        studio.expect("SINGLE_POINT_UPDATE")
        latencies.append(time.perf_counter() - t0)
    lat = np.array(latencies[5:]) * 1e3
    print(f"\n[measure] fallback PERTURB round trip: median {np.median(lat):.1f} ms, p90 {np.percentile(lat, 90):.1f} ms")
    assert np.median(lat) < 500
