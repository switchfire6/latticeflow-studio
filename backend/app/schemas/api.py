"""REST request/response bodies (PROJECT_SPEC.md §5.3).

Shared contract between ``app/api/routes.py`` and the frontend ``apiClient.ts``.
Additive changes only; mirror any change in PROJECT_SPEC.md §5.3.
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.crystal import CrystalStatePayload


class HealthResponse(BaseModel):
    """GET /api/health."""

    status: Literal["ok"] = "ok"
    umlip_backend: str                  # "mace-mp-0-small" | "lj-fallback"
    device: str
    dtype: str
    load_error: Optional[str] = None    # why MACE fell back, if it did
    hull_available: bool                # MP_API_KEY configured


class ExportRequest(BaseModel):
    """POST /api/export/cif -> text/plain attachment (``<formula>.cif`` or ``POSCAR``)."""

    crystal: CrystalStatePayload
    format: Literal["cif", "poscar", "extxyz"] = "cif"
    # ---- Phase A (IMPROVEMENT_SUGGESTIONS.md §4.1-4.2)
    symmetrize: bool = True                   # cif only: space group + asymmetric unit (False = P1)
    symprec: float = Field(default=1e-2, gt=0, le=0.5)
    cell: Literal["as_is", "conventional", "primitive"] = "as_is"
    #   extxyz always carries energy / forces / stress from the payload and requires cell "as_is"


class ImportCifRequest(BaseModel):
    """POST /api/import/cif -> CrystalStatePayload (stateless preview)."""

    cif: str = Field(min_length=1)


class HullEvaluateRequest(BaseModel):
    """POST /api/hull/evaluate."""

    crystal: CrystalStatePayload
    relax: bool = True   # Phase A §5.11: relax the candidate (reference settings) before scoring


class HullReferencePhase(BaseModel):
    material_id: str                    # Materials Project id, e.g. "mp-149"
    formula: str
    energy_per_atom: float              # eV/atom, evaluated with the studio's uMLIP after relaxation


class HullEvaluateResponse(BaseModel):
    chemsys: str                        # e.g. "Li-O-Ti" (alphabetical, pymatgen convention)
    energy_per_atom: float              # candidate, eV/atom, of the scored structure (relaxed when candidate_relaxed)
    e_hull_ev: float                    # eV/atom above the hull of the reference phases (candidate NOT
                                        # included); 0 = on it, negative = below it (would be newly stable)
    formation_energy_per_atom: Optional[float] = None
    decomposition: Dict[str, float]     # competing-phase formula -> atom fraction at the candidate composition (sums to 1)
    references: List[HullReferencePhase]
    method: str                         # human-readable description of how the hull was built
    umlip_backend: str
    # ---- Phase A
    candidate_relaxed: bool = False                    # the scored candidate is the relaxed structure
    energy_per_atom_as_given: Optional[float] = None   # single point of the structure as sent (when relaxed)


class ErrorResponse(BaseModel):
    """Body of every 4xx/5xx from the REST API (FastAPI ``detail`` replaced by this shape)."""

    code: str                           # an app.schemas.messages.ErrorCode value
    message: str
