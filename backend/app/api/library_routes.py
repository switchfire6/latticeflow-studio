"""REST endpoints of the project library and the job queue (docs/protocol.md §6-§7).

``router`` (prefix ``/api``) is mounted by ``app.main``; it relies on
``routes.register_error_handlers`` for the ``ErrorResponse`` body. Errors:
404 ``NOT_FOUND`` (unknown id), 409 ``INVALID_OPERATION`` (state conflict, e.g.
deleting the default project or a running job), 422 ``INVALID_PAYLOAD`` /
structure codes. Test seams: the ``library_dep`` and ``queue_dep`` dependencies.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import ValidationError

from app.api.routes import ApiError, _payload_atoms
from app.core import jobs as jobs_core
from app.core import library as lib_core
from app.schemas.api import DiffractionResponse, HullEvaluateResponse
from app.schemas.library import (
    ClearFinishedResponse,
    Job,
    JobCreate,
    JobList,
    JobMove,
    Project,
    ProjectCreate,
    ProjectUpdate,
    Result,
    ResultCreate,
    ResultRow,
    Structure,
    StructureCreate,
    StructureListRow,
    StructureUpdate,
)
from app.schemas.messages import (
    BulkModulusResult,
    ElasticTensorResult,
    MDComplete,
    PhononAnalysisComplete,
    RelaxationFrame,
)

router = APIRouter(prefix="/api")

#: The model each result kind's ``data`` must validate against (docs/protocol.md §6.3).
RESULT_DATA_MODELS: Dict[str, type] = {
    "relax": RelaxationFrame,
    "phonons": PhononAnalysisComplete,
    "elastic": ElasticTensorResult,
    "eos": BulkModulusResult,
    "hull": HullEvaluateResponse,
    "md": MDComplete,
    "diffraction": DiffractionResponse,
}


def library_dep() -> lib_core.Library:
    return lib_core.get_library()


def queue_dep() -> jobs_core.JobQueue:
    return jobs_core.get_queue()


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except lib_core.LibraryError as exc:
        raise ApiError(exc.status, exc.code, str(exc)) from exc



# ---------------------------------------------------------------- projects
@router.get("/projects", response_model=List[Project])
def list_projects(lib: lib_core.Library = Depends(library_dep)):
    return lib.list_projects()


@router.post("/projects", response_model=Project, status_code=201)
def create_project(req: ProjectCreate, lib: lib_core.Library = Depends(library_dep)):
    return lib.create_project(req.name)


@router.get("/projects/{pid}", response_model=Project)
def get_project(pid: int, lib: lib_core.Library = Depends(library_dep)):
    return _call(lib.get_project, pid)


@router.patch("/projects/{pid}", response_model=Project)
def rename_project(pid: int, req: ProjectUpdate, lib: lib_core.Library = Depends(library_dep)):
    return _call(lib.rename_project, pid, req.name)


@router.delete("/projects/{pid}", status_code=204)
def delete_project(pid: int, lib: lib_core.Library = Depends(library_dep)):
    _call(lib.delete_project, pid)
    return Response(status_code=204)


@router.get("/projects/{pid}/export")
def export_project(pid: int, lib: lib_core.Library = Depends(library_dep)) -> Dict[str, Any]:
    return _call(lib.export_project, pid)


@router.post("/projects/import", response_model=Project, status_code=201)
def import_project(bundle: Dict[str, Any], name: Optional[str] = Query(default=None),
                   lib: lib_core.Library = Depends(library_dep)):
    try:
        return _call(lib.import_project, bundle, name)
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(422, "INVALID_PAYLOAD", f"malformed bundle: {type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------- structures
@router.post("/projects/{pid}/structures", response_model=Structure)
def add_structure(pid: int, req: StructureCreate, response: Response, lib: lib_core.Library = Depends(library_dep)):
    _payload_atoms(req.crystal)  # 422 INVALID_ELEMENT / INVALID_STRUCTURE like the other endpoints
    row = _call(lib.add_structure, pid, req.crystal.model_dump(mode="json"), name=req.name,
                parent_id=req.parent_id, origin=req.origin, notes=req.notes)
    response.status_code = 200 if row["existing"] else 201
    return row


@router.get("/projects/{pid}/structures", response_model=List[StructureListRow])
def list_structures(pid: int, lib: lib_core.Library = Depends(library_dep)):
    return _call(lib.list_structures, pid)


@router.get("/structures/{sid}", response_model=Structure)
def get_structure(sid: int, lib: lib_core.Library = Depends(library_dep)):
    return _call(lib.get_structure, sid)


@router.patch("/structures/{sid}", response_model=Structure)
def update_structure(sid: int, req: StructureUpdate, lib: lib_core.Library = Depends(library_dep)):
    return _call(lib.update_structure, sid, name=req.name, notes=req.notes)


@router.delete("/structures/{sid}", status_code=204)
def delete_structure(sid: int, lib: lib_core.Library = Depends(library_dep)):
    _call(lib.delete_structure, sid)
    return Response(status_code=204)


# ---------------------------------------------------------------- results
@router.post("/structures/{sid}/results", response_model=ResultRow)
def add_result(sid: int, req: ResultCreate, response: Response, lib: lib_core.Library = Depends(library_dep)):
    try:
        data = RESULT_DATA_MODELS[req.kind].model_validate(req.data).model_dump(mode="json")
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise ApiError(422, "INVALID_PAYLOAD", f"data is not a valid {req.kind} result: {loc}: {first.get('msg')}")
    if req.model_id is None:
        from app.core.umlip_engine import default_model_id

        model_id = default_model_id()
    else:
        model_id = req.model_id
    row = _call(lib.add_result, sid, req.kind, data, model_id=model_id, dispersion=req.dispersion,
                settings=req.settings)
    response.status_code = 200 if row["replaced"] else 201
    return row


@router.get("/structures/{sid}/results", response_model=List[ResultRow])
def list_results(sid: int, kind: Optional[str] = None, lib: lib_core.Library = Depends(library_dep)):
    return _call(lib.list_results, sid, kind)


@router.get("/results/{rid}", response_model=Result)
def get_result(rid: int, lib: lib_core.Library = Depends(library_dep)):
    return _call(lib.get_result, rid)


@router.delete("/results/{rid}", status_code=204)
def delete_result(rid: int, lib: lib_core.Library = Depends(library_dep)):
    _call(lib.delete_result, rid)
    return Response(status_code=204)


# ---------------------------------------------------------------- jobs
@router.post("/jobs", response_model=List[Job], status_code=201)
def create_jobs(req: JobCreate, queue: jobs_core.JobQueue = Depends(queue_dep)):
    return _call(queue.create, req.project_id, list(req.structure_ids), [s.model_dump() for s in req.steps],
                 req.model_id, req.dispersion)


@router.get("/jobs", response_model=JobList)
def list_jobs(project_id: Optional[int] = None, active: Optional[bool] = None,
              queue: jobs_core.JobQueue = Depends(queue_dep)):
    return {"jobs": queue.library.list_jobs(project_id, active), "paused": queue.paused}


@router.post("/jobs/clear-finished", response_model=ClearFinishedResponse)
def clear_finished(project_id: Optional[int] = None, queue: jobs_core.JobQueue = Depends(queue_dep)):
    return {"deleted": queue.clear_finished(project_id)}


@router.get("/jobs/{jid}", response_model=Job)
def get_job(jid: int, queue: jobs_core.JobQueue = Depends(queue_dep)):
    return _call(queue.library.get_job, jid)


@router.post("/jobs/{jid}/cancel", response_model=Job)
def cancel_job(jid: int, queue: jobs_core.JobQueue = Depends(queue_dep)):
    return _call(queue.cancel, jid)


@router.post("/jobs/{jid}/move", response_model=Job)
def move_job(jid: int, req: JobMove, queue: jobs_core.JobQueue = Depends(queue_dep)):
    return _call(queue.move, jid, req.position)


@router.delete("/jobs/{jid}", status_code=204)
def delete_job(jid: int, queue: jobs_core.JobQueue = Depends(queue_dep)):
    _call(queue.delete, jid)
    return Response(status_code=204)
