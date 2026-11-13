"""FastAPI entrypoint: ``uvicorn app.main:app --reload`` (run from ``backend/``).

Router contract (PM-owned):
* ``app.api.routes.router``    - ``APIRouter`` whose paths already start with ``/api``.
* ``app.api.routes.register_error_handlers(app)`` - maps REST errors to ``ErrorResponse``.
* ``app.api.ws_stream.router`` - ``APIRouter`` exposing the ``/ws/studio`` WebSocket.

Startup blocks until the uMLIP engine is loaded and has run one warm-up single
point (CUDA context, MACE graph build), so the first client structure is fast
(PROJECT_SPEC.md §8.1).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes, ws_stream
from app.core.umlip_engine import UMLIPEngine
from app.presets import DEFAULT_PRESET_ID, load_preset

log = logging.getLogger("latticeflow")

# Vite dev server defaults; override with a comma-separated LATTICEFLOW_CORS_ORIGINS.
_DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


def _cors_origins() -> list[str]:
    raw = os.environ.get("LATTICEFLOW_CORS_ORIGINS", "").strip() or _DEFAULT_ORIGINS
    return [o.strip() for o in raw.split(",") if o.strip()]


def _warm_up() -> UMLIPEngine:
    engine = UMLIPEngine.get_instance()
    engine.evaluate(load_preset(DEFAULT_PRESET_ID))
    return engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = await asyncio.to_thread(_warm_up)
    log.info(
        "uMLIP ready: backend=%s device=%s dtype=%s%s",
        engine.backend_name,
        engine.device,
        engine.dtype,
        f" (fallback reason: {engine.load_error})" if engine.load_error else "",
    )
    yield


app = FastAPI(title="LatticeFlow Studio", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
routes.register_error_handlers(app)
app.include_router(routes.router)
app.include_router(ws_stream.router)
