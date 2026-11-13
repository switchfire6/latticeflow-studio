"""REST endpoints (PROJECT_SPEC.md §5.3).

``router`` (prefix ``/api``) and ``register_error_handlers(app)`` are the
contract used by ``app.main``. Endpoints that touch the engine are plain
``def`` so FastAPI runs them in its threadpool.

Error mapping (body is always ``ErrorResponse``, ``code`` an ``ErrorCode`` value)
-----------------------------------------------------------------------------
* 422 ``INVALID_PAYLOAD``       request body failed validation (FastAPI's ``RequestValidationError``)
* 422 ``INVALID_ELEMENT``       unknown element symbol in a ``CrystalStatePayload``
* 422 ``StructureIOError.code`` ``INVALID_STRUCTURE`` / ``DISORDERED_STRUCTURE`` /
  ``STRUCTURE_TOO_LARGE`` (the request is well formed and small, its *content*
  exceeds a semantic limit, so 422 rather than 413; clients switch on ``code``)
* 503 ``HULL_UNAVAILABLE``      no (valid) MP API key and the hull is not cached
* 502 ``HULL_UNAVAILABLE``      MP query failed / unreachable, or MP has no usable
  elemental reference phase for an element (the upstream data cannot build a hull)
* 500 ``COMPUTE_FAILED``        any other exception (logged with traceback)
* Phase B: 422 ``MODEL_UNAVAILABLE`` (hull ``model_id`` / ``dispersion`` cannot be loaded);
  ``/api/mp/*``: 503 ``MP_UNAVAILABLE`` (no key), 502 ``MP_UNAVAILABLE`` (upstream failure),
  404 ``MP_NOT_FOUND``, 422 ``INVALID_PAYLOAD`` (unparsable query / id); ``/api/diffraction``:
  422 ``INVALID_PAYLOAD`` for an empty 2theta range or unknown radiation.
* Phase C1 ``/api/ordering``: 422 ``INVALID_STRUCTURE`` (unparsable CIF), 422 ``STRUCTURE_TOO_LARGE``
  (smallest ordered cell > ``max_atoms``), 422 ``MODEL_UNAVAILABLE``.

The handlers are HTTP-only: ``RequestValidationError`` never arises on the
WebSocket route (that is ``WebSocketRequestValidationError``), ``ApiError`` is
only raised by the endpoints below (which convert ``StructureIOError`` themselves,
so no global handler for it is installed), and the catch-all ``Exception`` handler runs
in Starlette's ``ServerErrorMiddleware``, which passes WebSocket scopes through
untouched. Routing errors (404 / 405) keep FastAPI's default body.

Test seams
----------
``hull_context`` is a FastAPI dependency returning the MP key, fetcher and cache
used by ``/api/hull/evaluate`` (and the key for ``/api/health``); tests override
it via ``app.dependency_overrides[routes.hull_context]``. ``mp_context`` does the same for
``/api/mp/*`` (key plus an optional injected ``MPClient``).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import List, Optional

from ase import Atoms
from ase.data import atomic_numbers
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.config import get_settings
from app.core import diffraction, hull_analyzer, mp_client, ordering
from app.core.structure_io import (
    StructureIOError,
    atoms_from_cif,
    atoms_from_payload,
    atoms_to_cif,
    atoms_to_extxyz,
    atoms_to_poscar,
    convert_cell,
)
from app.core.umlip_engine import UMLIPEngine, reduced_formula
from app.presets import PresetInfo, list_presets
from app.schemas.api import (
    DiffractionRequest,
    DiffractionResponse,
    ErrorResponse,
    ExportRequest,
    HealthResponse,
    HullEvaluateRequest,
    HullEvaluateResponse,
    ImportCifRequest,
    ModelListResponse,
    MPSearchResponse,
    MPStructureResponse,
    OrderingRequest,
    OrderingResponse,
)
from app.schemas.crystal import CrystalStatePayload
from app.schemas.messages import ErrorCode

log = logging.getLogger("latticeflow.api")

router = APIRouter(prefix="/api")

_ERROR_RESPONSES = {
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}
_HULL_RESPONSES = {**_ERROR_RESPONSES, 502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}}
_MP_RESPONSES = {**_HULL_RESPONSES, 404: {"model": ErrorResponse}}


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
class ApiError(Exception):
    """Raised by the endpoints; rendered as ``ErrorResponse`` with ``status_code``."""

    def __init__(self, status_code: int, code: ErrorCode | str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.message = message


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=ErrorResponse(code=code, message=message).model_dump())


def _format_validation_errors(errors) -> str:
    parts: List[str] = []
    for err in list(errors)[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body") or "body"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    extra = len(errors) - len(parts)
    if extra > 0:
        parts.append(f"(+{extra} more)")
    return "; ".join(parts) or "invalid request"


def register_error_handlers(app: FastAPI) -> None:
    """Install the HTTP error handlers that render every API error as ``ErrorResponse``."""

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(422, ErrorCode.INVALID_PAYLOAD.value, _format_validation_errors(exc.errors()))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:  # HTTP only (ServerErrorMiddleware)
        log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return _error_response(500, ErrorCode.COMPUTE_FAILED.value, f"internal error: {type(exc).__name__}")


def _payload_atoms(crystal: CrystalStatePayload) -> Atoms:
    """``atoms_from_payload`` with its non-``StructureIOError`` failures mapped to 422 as well.

    ``atoms_from_payload`` lets ASE's own errors escape for unknown element
    symbols (``KeyError``), singular lattices (``AssertionError`` in ``Atoms``)
    and malformed coordinate vectors; those are client errors, not 500s.
    """
    unknown = sorted({s.element for s in crystal.atoms if s.element not in atomic_numbers or s.element == "X"})
    if unknown:
        raise ApiError(422, ErrorCode.INVALID_ELEMENT, f"unknown element symbol(s): {', '.join(unknown)}")
    try:
        return atoms_from_payload(crystal)
    except StructureIOError as exc:
        raise _structure_error(exc) from exc
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        raise ApiError(422, ErrorCode.INVALID_STRUCTURE, f"invalid structure ({detail})") from exc


def _structure_error(exc: StructureIOError) -> ApiError:
    return ApiError(422, exc.code, str(exc))


def _compute_failed(what: str, exc: BaseException) -> ApiError:
    log.exception("%s failed", what, exc_info=exc)
    return ApiError(500, ErrorCode.COMPUTE_FAILED, f"{what} failed: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# dependencies
# --------------------------------------------------------------------------
@dataclass
class HullContext:
    api_key: Optional[str]
    fetcher: Optional[hull_analyzer.Fetcher] = None   # None = Materials Project (mp_api)
    cache: Optional[hull_analyzer.HullCache] = None   # None = process-wide default cache


def hull_context() -> HullContext:
    return HullContext(api_key=get_settings().mp_api_key)


@dataclass
class MPContext:
    api_key: Optional[str]
    client: Optional[mp_client.MPClient] = None   # None = mp_client.MaterialsProjectClient(api_key)


def mp_context() -> MPContext:
    return MPContext(api_key=get_settings().mp_api_key)


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
@router.get("/health", response_model=HealthResponse)
def health(ctx: HullContext = Depends(hull_context)) -> HealthResponse:
    engine = UMLIPEngine.get_instance()
    err = engine.load_error
    return HealthResponse(
        umlip_backend=engine.backend_name,
        device=str(engine.device),
        dtype=str(engine.dtype),
        load_error=f"{type(err).__name__}: {err}" if err is not None else None,
        hull_available=bool(ctx.api_key and ctx.api_key.strip()),
    )


@router.get("/presets", response_model=List[PresetInfo])
def presets() -> List[PresetInfo]:
    return list_presets()


def _safe_filename(formula: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", formula) or "structure"


@router.post(
    "/export/cif",
    responses={**_ERROR_RESPONSES, 200: {"content": {"chemical/x-cif": {}, "text/plain": {}}}},
    response_class=Response,
)
def export_structure(req: ExportRequest) -> Response:
    """Export the payload's structure.

    * ``cif``: ``cell`` conversion (as_is / conventional / primitive), then a
      symmetrised CIF (space group + asymmetric unit at ``symprec``) unless
      ``symmetrize`` is False (P1). ``<formula>.cif``.
    * ``poscar``: ``cell`` conversion, then VASP 5 POSCAR. ``POSCAR``.
    * ``extxyz``: requires ``cell == "as_is"`` (422 ``INVALID_PAYLOAD`` otherwise);
      carries the payload's total energy, per-atom forces and stress. ``<formula>.extxyz``.
    """
    atoms = _payload_atoms(req.crystal)  # 422 on invalid structures
    if req.format == "extxyz" and req.cell != "as_is":
        raise ApiError(422, ErrorCode.INVALID_PAYLOAD,
                       "extxyz export carries the payload's energy / forces / stress and needs cell 'as_is'")
    if req.format == "extxyz":
        bad = [s.index for s in req.crystal.atoms if len(s.force) != 3 or not all(math.isfinite(x) for x in s.force)]
        if bad or len(req.crystal.stress_gpa) not in (0, 6):
            raise ApiError(422, ErrorCode.INVALID_PAYLOAD,
                           "extxyz export needs 3 finite force components per atom and a 6-component stress"
                           + (f" (atoms {bad[:5]})" if bad else ""))
    try:
        if req.format != "extxyz":
            atoms = convert_cell(atoms, req.cell, symprec=req.symprec)
    except StructureIOError as exc:
        raise _structure_error(exc) from exc
    try:
        stem = _safe_filename(reduced_formula(atoms))
        if req.format == "poscar":
            text, media, filename = atoms_to_poscar(atoms), "text/plain", "POSCAR"
        elif req.format == "extxyz":
            sites = sorted(req.crystal.atoms, key=lambda site: site.index)  # atoms_from_payload order
            text = atoms_to_extxyz(
                atoms,
                energy=float(req.crystal.energy_per_atom) * len(atoms),
                forces=[site.force for site in sites],
                stress_voigt_gpa=req.crystal.stress_gpa if len(req.crystal.stress_gpa) == 6 else None,
            )
            media, filename = "text/plain", f"{stem}.extxyz"
        else:
            text = atoms_to_cif(atoms, symprec=req.symprec if req.symmetrize else None)
            media, filename = "chemical/x-cif", f"{stem}.cif"
    except Exception as exc:  # noqa: BLE001
        raise _compute_failed("export", exc) from exc
    return Response(
        content=text.encode("utf-8"),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import/cif", response_model=CrystalStatePayload, responses=_ERROR_RESPONSES)
def import_cif(req: ImportCifRequest) -> CrystalStatePayload:
    try:
        atoms = atoms_from_cif(req.cif)
    except StructureIOError as exc:
        raise _structure_error(exc) from exc
    try:
        return UMLIPEngine.get_instance().evaluate(atoms, bond_mode="fast")
    except Exception as exc:  # noqa: BLE001
        raise _compute_failed("uMLIP evaluation", exc) from exc


@router.post("/hull/evaluate", response_model=HullEvaluateResponse, responses=_HULL_RESPONSES)
def hull_evaluate(req: HullEvaluateRequest, ctx: HullContext = Depends(hull_context)) -> HullEvaluateResponse:
    atoms = _payload_atoms(req.crystal)  # 422 on invalid structures
    engine = _engine_for(req.model_id, req.dispersion)
    try:
        return hull_analyzer.evaluate_hull(
            atoms,
            engine=engine,
            api_key=ctx.api_key,
            fetcher=ctx.fetcher,
            cache=ctx.cache,
            relax_candidate=req.relax,
        )
    except hull_analyzer.HullNoApiKeyError as exc:
        raise ApiError(503, ErrorCode.HULL_UNAVAILABLE, str(exc)) from exc
    except hull_analyzer.HullFetchError as exc:
        log.warning("hull reference fetch failed: %s", exc)
        raise ApiError(502, ErrorCode.HULL_UNAVAILABLE, str(exc)) from exc
    except hull_analyzer.HullMissingElementalReferenceError as exc:
        raise ApiError(502, ErrorCode.HULL_UNAVAILABLE, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _compute_failed("hull evaluation", exc) from exc


def _engine_for(model_id: Optional[str], dispersion: bool) -> UMLIPEngine:
    """Process default engine, or BW's model registry (422 MODEL_UNAVAILABLE if it cannot load)."""
    if model_id is None and not dispersion:
        return UMLIPEngine.get_instance()
    from app.core import umlip_engine

    try:
        return umlip_engine.get_engine(model_id, bool(dispersion))
    except umlip_engine.ModelUnavailableError as exc:
        raise ApiError(422, ErrorCode.MODEL_UNAVAILABLE, str(exc)) from exc


@router.get("/models", response_model=ModelListResponse, responses=_ERROR_RESPONSES)
def models() -> ModelListResponse:
    from app.core import umlip_engine

    return ModelListResponse(models=umlip_engine.list_models(), default_model_id=umlip_engine.default_model_id())


@router.post("/diffraction", response_model=DiffractionResponse, responses=_ERROR_RESPONSES)
def diffraction_pattern(req: DiffractionRequest) -> DiffractionResponse:
    """Powder XRD / neutron pattern (``core.diffraction``); 422 INVALID_PAYLOAD for a bad range / radiation."""
    atoms = _payload_atoms(req.crystal)
    try:
        return diffraction.compute_pattern(
            atoms,
            kind=req.kind,
            radiation=req.radiation,
            wavelength_angstrom=req.wavelength_angstrom,
            two_theta_min=req.two_theta_min,
            two_theta_max=req.two_theta_max,
        )
    except diffraction.DiffractionError as exc:
        raise ApiError(422, ErrorCode.INVALID_PAYLOAD, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _compute_failed("diffraction", exc) from exc


def _mp(ctx: MPContext) -> mp_client.MPClient:
    if ctx.client is not None:
        return ctx.client
    try:
        return mp_client.MaterialsProjectClient(ctx.api_key)
    except mp_client.MPNoApiKeyError as exc:
        raise ApiError(503, ErrorCode.MP_UNAVAILABLE, str(exc)) from exc


def _mp_call(fn):
    try:
        return fn()
    except mp_client.MPNoApiKeyError as exc:
        raise ApiError(503, ErrorCode.MP_UNAVAILABLE, str(exc)) from exc
    except mp_client.MPQueryError as exc:
        raise ApiError(422, ErrorCode.INVALID_PAYLOAD, str(exc)) from exc
    except mp_client.MPNotFoundError as exc:
        raise ApiError(404, ErrorCode.MP_NOT_FOUND, str(exc)) from exc
    except mp_client.MPFetchError as exc:
        log.warning("Materials Project request failed: %s", exc)
        raise ApiError(502, ErrorCode.MP_UNAVAILABLE, str(exc)) from exc


@router.get("/mp/search", response_model=MPSearchResponse, responses=_MP_RESPONSES)
def mp_search(q: str, ctx: MPContext = Depends(mp_context)) -> MPSearchResponse:
    """Search MP by formula ("SrTiO3"), chemical system ("Li-Fe-O") or id ("mp-5229")."""
    try:
        mp_client.parse_query(q)   # 422 before touching the key / network
    except mp_client.MPQueryError as exc:
        raise ApiError(422, ErrorCode.INVALID_PAYLOAD, str(exc)) from exc
    client = _mp(ctx)
    results = _mp_call(lambda: client.search(q))
    return MPSearchResponse(query=q, results=mp_client.sort_entries(list(results)))


@router.get("/mp/structure/{material_id}", response_model=MPStructureResponse, responses=_MP_RESPONSES)
def mp_structure(material_id: str, ctx: MPContext = Depends(mp_context)) -> MPStructureResponse:
    """MP structure as a symmetrised CIF of its conventional standard cell (<= 64 atoms, else MP's cell)."""
    try:
        mid = mp_client.normalize_material_id(material_id)
    except mp_client.MPQueryError as exc:
        raise ApiError(422, ErrorCode.INVALID_PAYLOAD, str(exc)) from exc
    client = _mp(ctx)
    entry, structure = _mp_call(lambda: client.get_structure(mid))
    try:
        cif = mp_client.structure_cif(structure)
    except mp_client.MPFetchError as exc:
        raise ApiError(502, ErrorCode.MP_UNAVAILABLE, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _compute_failed("CIF conversion", exc) from exc
    return MPStructureResponse(entry=entry, cif=cif)


@router.post("/ordering", response_model=OrderingResponse, responses=_ERROR_RESPONSES)
def order_structure(req: OrderingRequest) -> OrderingResponse:
    """Ordered approximants of a (partially occupied) CIF ranked by uMLIP energy (``core.ordering``)."""
    engine = _engine_for(req.model_id, req.dispersion)
    try:
        return ordering.order_disordered(req.cif, engine, max_atoms=req.max_atoms, n_candidates=req.n_candidates)
    except StructureIOError as exc:
        raise _structure_error(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise _compute_failed("ordering", exc) from exc
