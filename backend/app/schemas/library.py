"""REST bodies of the project library and the job queue (docs/protocol.md §6-§7)."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.crystal import CrystalStatePayload

ResultKind = Literal["relax", "phonons", "elastic", "eos", "hull", "md", "diffraction"]
StepKind = Literal["relax", "phonons", "elastic", "eos", "hull", "follow"]
JobStatus = Literal["queued", "running", "done", "failed", "cancelled", "skipped"]


# ---------------------------------------------------------------- projects
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class Project(BaseModel):
    id: int
    name: str
    is_default: bool                      # the "Scratch" project; cannot be deleted
    created_at: str                       # ISO 8601 UTC, e.g. "2026-11-13T09:30:00.123Z"
    updated_at: str                       # bumped by any structure / result change in the project
    n_structures: int


# ---------------------------------------------------------------- structures
class StructureCreate(BaseModel):
    crystal: CrystalStatePayload
    name: Optional[str] = Field(default=None, max_length=200)   # default: crystal.formula
    parent_id: Optional[int] = None                             # a structure of the same project
    origin: str = Field(default="import", max_length=200)       # e.g. "import", "relax", "follow R ν0"
    notes: str = Field(default="", max_length=20000)


class StructureUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=20000)


class StructureRow(BaseModel):
    id: int
    project_id: int
    name: str
    formula: str
    n_atoms: int
    spacegroup_symbol: str
    spacegroup_number: int
    content_hash: str
    parent_id: Optional[int] = None
    origin: str
    notes: str
    created_at: str
    updated_at: str


class StructureListRow(StructureRow):
    latest: Dict[str, Any]                # newest summary per kind, merged into one flat dict
    latest_result_ids: Dict[str, int]     # kind -> id of that newest result


class Structure(StructureRow):
    crystal: CrystalStatePayload
    existing: bool = False                # POST only: true = the structure was already stored (dedup)


# ---------------------------------------------------------------- results
class ResultCreate(BaseModel):
    kind: ResultKind
    data: Dict[str, Any]                  # the event / response payload of that kind (validated)
    model_id: Optional[str] = None        # None = the process default model id
    dispersion: bool = False
    settings: Dict[str, Any] = Field(default_factory=dict)


class ResultRow(BaseModel):
    id: int
    structure_id: int
    kind: ResultKind
    model_id: str
    dispersion: bool
    settings: Dict[str, Any]
    summary: Dict[str, Any]
    created_at: str
    code_version: str
    replaced: Optional[bool] = None       # POST only: true = an older result with the same key was overwritten


class Result(ResultRow):
    data: Dict[str, Any]


# ---------------------------------------------------------------- jobs
class JobStep(BaseModel):
    kind: StepKind
    settings: Dict[str, Any] = Field(default_factory=dict)
    only_if_unstable: bool = False        # follow only


class JobCreate(BaseModel):
    project_id: int
    structure_ids: List[int] = Field(min_length=1, max_length=500)
    steps: List[JobStep] = Field(min_length=1, max_length=20)
    model_id: Optional[str] = None
    dispersion: bool = False


class Job(BaseModel):
    id: int
    project_id: int
    structure_id: Optional[int]           # input structure of this step (updated when an earlier relax/follow ends)
    chain_id: str
    step_index: int
    n_steps: int
    kind: StepKind
    settings: Dict[str, Any]              # full settings, defaults filled in
    only_if_unstable: bool
    model_id: Optional[str]
    dispersion: bool
    status: JobStatus
    progress: float                       # 0..1
    message: str
    error: Optional[str] = None
    result_id: Optional[int] = None
    output_structure_id: Optional[int] = None
    position: int
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class JobList(BaseModel):
    jobs: List[Job]
    paused: bool                          # the worker is waiting for interactive streams to finish


class JobMove(BaseModel):
    position: int = Field(ge=0)


class ClearFinishedResponse(BaseModel):
    deleted: int
