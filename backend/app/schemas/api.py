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
    # ---- Phase B §6.1: score with the session's force field (None = process default)
    model_id: Optional[str] = None
    dispersion: bool = False


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


# --------------------------------------------------------------------------- #
# Phase B (IMPROVEMENT_SUGGESTIONS.md §4.3, §5.6, §6.1)
# --------------------------------------------------------------------------- #
class ModelInfo(BaseModel):
    id: str                             # "small", "medium", "large", "medium-mpa-0", "medium-omat-0", "lj-fallback"
    label: str                          # e.g. "MACE-MPA-0 medium"
    description: str                    # training data / intended use / accuracy notes
    family: str                         # "mace" | "fallback"
    checkpoint_cached: bool             # True = loads without a download
    approx_size_mb: Optional[float] = None
    supports_dispersion: bool
    is_default: bool                    # the process default (LATTICEFLOW_MACE_MODEL)


class ModelListResponse(BaseModel):
    """GET /api/models."""

    models: List[ModelInfo]
    default_model_id: str


class DiffractionRequest(BaseModel):
    """POST /api/diffraction (stateless; pymatgen XRDCalculator / NDCalculator)."""

    crystal: CrystalStatePayload
    kind: Literal["xray", "neutron"] = "xray"
    radiation: str = "CuKa"             # pymatgen wavelength key (CuKa, MoKa, CrKa, FeKa, CoKa, AgKa, ...)
    wavelength_angstrom: Optional[float] = Field(default=None, gt=0, le=5)   # overrides radiation
    two_theta_min: float = Field(default=5.0, ge=0, lt=180)
    two_theta_max: float = Field(default=90.0, gt=0, le=180)


class DiffractionHkl(BaseModel):
    hkl: List[int]                      # Miller indices (3 integers; hexagonal 4-index reduced to 3)
    multiplicity: int


class DiffractionPeak(BaseModel):
    two_theta: float                    # degrees
    intensity: float                    # scaled so the strongest peak is 100
    d_spacing: float                    # Angstrom
    hkls: List[DiffractionHkl]


class DiffractionResponse(BaseModel):
    kind: str
    radiation: Optional[str]            # None when wavelength_angstrom was given
    wavelength_angstrom: float
    peaks: List[DiffractionPeak]


class MPEntry(BaseModel):
    material_id: str                    # "mp-5229"
    formula: str                        # reduced formula
    spacegroup_symbol: str
    spacegroup_number: int
    nsites: int
    energy_above_hull: Optional[float]  # MP DFT value, eV/atom
    is_stable: Optional[bool]
    theoretical: Optional[bool]


class MPSearchResponse(BaseModel):
    """GET /api/mp/search?q=<formula | chemsys like Li-Fe-O | mp-id> (sorted by energy_above_hull, <= 50)."""

    query: str
    results: List[MPEntry]


class MPStructureResponse(BaseModel):
    """GET /api/mp/structure/{material_id}: the client loads `cif` via LOAD_STRUCTURE {cif}."""

    entry: MPEntry
    cif: str


# --------------------------------------------------------------------------- #
# Phase C1 (IMPROVEMENT_SUGGESTIONS.md §5.10): ordering of disordered structures
# --------------------------------------------------------------------------- #
class OrderingRequest(BaseModel):
    """POST /api/ordering: enumerate ordered approximants of a disordered CIF, ranked by uMLIP energy."""

    cif: str = Field(min_length=1)
    max_atoms: int = Field(default=64, ge=1, le=128)      # largest ordered cell considered
    n_candidates: int = Field(default=8, ge=1, le=50)     # returned (lowest energy first)
    model_id: Optional[str] = None                        # session model (None = process default)
    dispersion: bool = False


class OrderingCandidate(BaseModel):
    cif: str                                              # ordered structure, load with LOAD_STRUCTURE {cif}
    formula: str
    n_atoms: int
    spacegroup_symbol: str
    energy_per_atom: float                                # uMLIP single point (not relaxed)
    relative_energy_mev_per_atom: float                   # vs the lowest candidate


class OrderingResponse(BaseModel):
    disordered_formula: str
    n_enumerated: int                                     # configurations generated before ranking
    candidates: List[OrderingCandidate]
    method: str
