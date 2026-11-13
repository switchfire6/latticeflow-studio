"""Process settings for the LatticeFlow Studio backend.

Plain ``os.environ`` + dataclass (pydantic-settings is intentionally not a
dependency). Read once via :func:`get_settings`; tests can build their own
``Settings`` instances (``Settings.from_env({...})``) without touching the
process environment.

Environment variables
---------------------
LATTICEFLOW_DEVICE                 ``auto`` (cuda if available, else cpu) | ``cuda`` | ``cuda:N`` | ``cpu``
LATTICEFLOW_DTYPE                  ``float64`` (default) | ``float32``
LATTICEFLOW_UMLIP                  ``auto`` (MACE, fall back to analytic) | ``mace`` | ``fallback``
LATTICEFLOW_MACE_MODEL             ``small`` (default), any ``mace_mp`` model name, or a local checkpoint path
LATTICEFLOW_MACE_DOWNLOAD_TIMEOUT  total wall-clock budget (s) for a first-time checkpoint download (default 180)
LATTICEFLOW_MACE_ALLOW_DOWNLOAD    ``1`` (default) | ``0``: never touch the network, use the local cache only
MP_API_KEY                         optional Materials Project key (convex-hull lookup, Phase 5)
LATTICEFLOW_HULL_CACHE_DIR         relaxed hull reference cache (default ~/.cache/latticeflow/hull), read by app/core/hull_analyzer.py
LATTICEFLOW_CORS_ORIGINS           comma-separated allowed origins (default: Vite dev server on :5173), read by app/main.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping, Optional

_VALID_DTYPES = ("float32", "float64")
_VALID_UMLIP = ("auto", "mace", "fallback")
_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    device: str = "auto"
    dtype: str = "float64"
    umlip: str = "auto"
    mace_model: str = "small"
    mace_download_timeout_s: float = 180.0
    mace_allow_download: bool = True
    mp_api_key: Optional[str] = None

    def __post_init__(self) -> None:
        if self.dtype not in _VALID_DTYPES:
            raise ValueError(f"LATTICEFLOW_DTYPE must be one of {_VALID_DTYPES}, got {self.dtype!r}")
        if self.umlip not in _VALID_UMLIP:
            raise ValueError(f"LATTICEFLOW_UMLIP must be one of {_VALID_UMLIP}, got {self.umlip!r}")
        if self.mace_download_timeout_s <= 0:
            raise ValueError("LATTICEFLOW_MACE_DOWNLOAD_TIMEOUT must be > 0")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env

        def get(name: str, default: str) -> str:
            value = env.get(name)
            return default if value is None or value.strip() == "" else value.strip()

        return cls(
            device=get("LATTICEFLOW_DEVICE", "auto").lower(),
            dtype=get("LATTICEFLOW_DTYPE", "float64").lower(),
            umlip=get("LATTICEFLOW_UMLIP", "auto").lower(),
            mace_model=get("LATTICEFLOW_MACE_MODEL", "small"),
            mace_download_timeout_s=float(get("LATTICEFLOW_MACE_DOWNLOAD_TIMEOUT", "180")),
            mace_allow_download=get("LATTICEFLOW_MACE_ALLOW_DOWNLOAD", "1").lower() in _TRUE,
            mp_api_key=env.get("MP_API_KEY") or None,
        )


def resolve_device(device: str) -> str:
    """Map ``auto`` to ``cuda``/``cpu``. Imports torch only when actually needed."""
    device = (device or "auto").lower()
    if device != "auto":
        return device
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # torch missing or broken CUDA install
        return "cpu"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
