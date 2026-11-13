"""Phase D protocol ergonomics (fallback calculator only; lean by design).

request_id echo, history_pushed / after_terminal, REQUEST_HISTORY_ENTRY,
configurable size limits, generator composition scaling.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.api import routes
from app.core import relaxer
from app.core.flow_generator import _resolve_plan
from app.schemas.messages import StartFlowGenerationPayload

from test_ws_protocol import (  # noqa: F401 - fixtures are used by name
    RECV_TIMEOUT_S,
    Studio,
    _hang_guard,
    _receive_with_timeout,
    assert_same_structure,
    client,
    slow_engine,
    slow_studio,
    studio,
    ws_engine,
)


def _send(s, type_: str, payload: Optional[Dict[str, Any]] = None, rid: Optional[str] = None) -> None:
    msg: Dict[str, Any] = {"type": type_, "payload": payload or {}}
    if rid is not None:
        msg["request_id"] = rid
    s.ws.send_json(msg)


def _raw(s, timeout: float = RECV_TIMEOUT_S) -> Dict[str, Any]:
    """Next server message, HISTORY_STATE and request_id included."""
    message = s.ws.portal.call(_receive_with_timeout, s.ws._send_rx, timeout)
    assert message["type"] == "websocket.send", message
    return json.loads(message.get("text") or message["bytes"].decode("utf-8"))


def _raw_until(s, type_: str, max_messages: int = 5000) -> List[Dict[str, Any]]:
    out = []
    for _ in range(max_messages):
        out.append(_raw(s))
        if out[-1]["type"] == type_:
            return out
    raise AssertionError(f"no {type_}")


@pytest.fixture
def limits(monkeypatch):
    """Set LATTICEFLOW_*_MAX_ATOMS for one test (settings cache cleared before and after)."""
    def set_(max_atoms: int, phonon_max_atoms: int) -> None:
        monkeypatch.setenv("LATTICEFLOW_MAX_ATOMS", str(max_atoms))
        monkeypatch.setenv("LATTICEFLOW_PHONON_MAX_ATOMS", str(phonon_max_atoms))
        config.get_settings.cache_clear()

    yield set_
    monkeypatch.undo()
    config.get_settings.cache_clear()


def test_request_id_echo_and_history_entry(studio):
    s = studio
    assert _raw(s)["type"] == "HISTORY_STATE" and "request_id" not in s.initial  # connect load: no id
    _send(s, "LOAD_STRUCTURE", {"preset_id": "si"}, rid="load-1")
    spu, hist = _raw(s), _raw(s)
    assert (spu["type"], spu["request_id"]) == ("SINGLE_POINT_UPDATE", "load-1")
    assert (hist["type"], hist["request_id"]) == ("HISTORY_STATE", "load-1")

    _send(s, "DELETE_ATOM", {"atom_index": "x"}, rid="bad")
    err = _raw(s)
    assert (err["payload"]["code"], err["request_id"]) == ("INVALID_PAYLOAD", "bad")

    _send(s, "REQUEST_HISTORY_ENTRY", {"position": 1}, rid="h")
    entry = _raw(s)
    assert (entry["type"], entry["request_id"]) == ("HISTORY_ENTRY", "h")
    assert (entry["payload"]["position"], entry["payload"]["label"]) == (1, "Load preset si")
    assert_same_structure(entry["payload"]["crystal"], spu["payload"]["crystal"])
    _send(s, "REQUEST_HISTORY_ENTRY", {"position": 7})
    err = _raw(s)
    assert err["payload"]["code"] == "INDEX_OUT_OF_RANGE" and "request_id" not in err


def test_cancelled_stream_keeps_its_request_id(slow_studio):
    s = slow_studio
    _raw(s)  # connect HISTORY_STATE
    _send(s, "LOAD_STRUCTURE", {"preset_id": "li3ps4"})
    _raw_until(s, "HISTORY_STATE")
    _send(s, "START_RELAXATION", {"fmax": 1e-6, "max_steps": 2000}, rid="relax")
    frame = _raw(s)
    assert (frame["type"], frame["request_id"]) == ("RELAXATION_FRAME", "relax")
    _send(s, "LOAD_STRUCTURE", {"preset_id": "si"}, rid="load")  # implicitly cancels the relaxation
    msgs = _raw_until(s, "STREAM_STOPPED")
    assert all(m["request_id"] == "relax" for m in msgs)
    assert msgs[-1]["payload"] == {"stopped": "START_RELAXATION", "history_pushed": True}
    hist = _raw(s)
    assert (hist["type"], hist["request_id"]) == ("HISTORY_STATE", "relax")
    assert hist["payload"]["label"] == "Relaxation (stopped)"
    assert [(m["type"], m["request_id"]) for m in (_raw(s), _raw(s))] == [
        ("SINGLE_POINT_UPDATE", "load"), ("HISTORY_STATE", "load")]
    _send(s, "STOP_STREAM", rid="stop")
    stop = _raw(s)
    assert stop["payload"] == {"stopped": None, "history_pushed": False} and stop["request_id"] == "stop"


def test_diverged_error_after_terminal(studio, monkeypatch):
    monkeypatch.setattr(relaxer, "MAX_VOLUME_RATIO", 1.0 + 1e-12)  # step 1 diverges
    _raw(studio)
    _send(studio, "START_RELAXATION", {"relax_cell": True}, rid="d")
    msgs = _raw_until(studio, "ERROR")
    assert msgs[-2]["payload"]["final"] is True
    err = msgs[-1]
    assert err["request_id"] == "d"
    assert err["payload"]["code"] == "RELAXATION_DIVERGED"
    assert err["payload"]["after_terminal"] is True and err["payload"]["history_pushed"] is True
    hist = _raw(studio)
    assert (hist["type"], hist["payload"]["label"]) == ("HISTORY_STATE", "Relaxation (diverged, restored)")


def test_configurable_limits(limits, ws_engine, client):
    limits(max_atoms=8, phonon_max_atoms=4)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as rest:
        health = rest.get("/api/health").json()
    assert (health["max_atoms"], health["phonon_max_atoms"]) == (8, 4)

    with client.websocket_connect("/ws/studio") as ws:
        s = Studio(ws)
        s.expect("SINGLE_POINT_UPDATE")  # srtio3, 5 atoms
        s.send("RUN_PHONON_SHAKE_TEST")
        assert "at most 4 atoms" in s.expect_error("STRUCTURE_TOO_LARGE", "RUN_PHONON_SHAKE_TEST")["message"]
        s.send("MAKE_SUPERCELL", {"matrix": [[2, 0, 0], [0, 1, 0], [0, 0, 1]]})
        assert "limit is 8" in s.expect_error("STRUCTURE_TOO_LARGE", "MAKE_SUPERCELL")["message"]


def test_generator_composition_scaling(limits):
    def plan(group: int):
        return _resolve_plan(StartFlowGenerationPayload(
            elements=["Na", "Cl"], composition={"Na": 1, "Cl": 1}, target_spacegroup=group))

    assert plan(225).counts == [4, 4]  # NaCl in Fm-3m -> Na4Cl4
    with pytest.raises(ValueError, match=r"x2, x3, x4"):
        plan(230)
    limits(max_atoms=6, phonon_max_atoms=4)  # x4 (8 atoms) is over the studio limit
    with pytest.raises(ValueError, match=r"x2, x3 within the 6-atom limit"):
        plan(225)
