"""Shared fixtures: one UMLIPEngine per backend.

* ``fallback_engine`` always works (pure numpy).
* ``mace_engine`` loads MACE-MP-0 (float64). If the checkpoint is not cached and
  cannot be downloaded within ``LATTICEFLOW_TEST_MACE_DOWNLOAD_TIMEOUT`` seconds
  (default 60), or torch/mace fail to load, every test using it is *skipped*.
  Set ``LATTICEFLOW_SKIP_MACE=1`` to skip MACE tests without trying.
* ``engine`` is parametrised over both; the MACE param carries the
  ``requires_mace`` marker so ``-m "not requires_mace"`` deselects it.

Engines built here are independent of the process-wide singleton.

``LATTICEFLOW_DATA_DIR`` is pointed at a throw-away directory before anything
is imported, so no test can write the real ``~/.latticeflow/library.db``.
"""

from __future__ import annotations

import os
import tempfile

os.environ["LATTICEFLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="latticeflow-test-data-")

import pytest  # noqa: E402

from app.core.umlip_engine import UMLIPEngine  # noqa: E402


@pytest.fixture(scope="session")
def fallback_engine() -> UMLIPEngine:
    return UMLIPEngine(backend="fallback")


def _load_mace(dtype: str) -> UMLIPEngine:
    if os.environ.get("LATTICEFLOW_SKIP_MACE", "").strip() in ("1", "true", "yes"):
        pytest.skip("LATTICEFLOW_SKIP_MACE is set")
    timeout = float(os.environ.get("LATTICEFLOW_TEST_MACE_DOWNLOAD_TIMEOUT", "60"))
    try:
        return UMLIPEngine(backend="mace", dtype=dtype, download_timeout_s=timeout)
    except Exception as exc:  # offline, timeout, corrupt checkpoint, torch/CUDA failure
        pytest.skip(f"MACE checkpoint unavailable: {exc}")


@pytest.fixture(scope="session")
def mace_engine() -> UMLIPEngine:
    return _load_mace("float64")


BACKENDS = ["fallback", pytest.param("mace", marks=pytest.mark.requires_mace)]


@pytest.fixture(params=BACKENDS)
def engine(request) -> UMLIPEngine:
    return request.getfixturevalue(f"{request.param}_engine")
