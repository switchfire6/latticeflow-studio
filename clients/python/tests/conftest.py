"""A real in-process LatticeFlow backend (analytic fallback force field) on a free port.

Run with the backend venv (the client is installed there editable):

    backend/.venv/Scripts/python.exe -m pytest clients/python/tests -q
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3] / "backend"

# Before the app is imported (settings are read once): no MACE, no network, no MP key.
os.environ["LATTICEFLOW_UMLIP"] = "fallback"
os.environ["LATTICEFLOW_MACE_ALLOW_DOWNLOAD"] = "0"
os.environ["MP_API_KEY"] = ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server_url():
    if not (BACKEND / "app" / "main.py").exists():
        pytest.skip("backend/ not found next to clients/python")
    sys.path.insert(0, str(BACKEND))
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=port, log_level="warning",
                                           ws_max_size=64 * 2**20))
    thread = threading.Thread(target=server.run, name="latticeflow-test-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 120
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            pytest.fail("backend did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
