"""WebSocket request/response payload schemas (PROJECT_SPEC.md §5.2).

Shared contract between the Phase 2 engines (which produce the event payloads),
the Phase 3 WebSocket layer and the frontend. Do not rename or remove fields;
additive changes only, and mirror any change in PROJECT_SPEC.md §5.2.

Every message on `/ws/studio` is an envelope ``{"type": <EVENT_TYPE>, "payload": {...}}``.
"""

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.crystal import CrystalStatePayload


# --------------------------------------------------------------------------- #
# Message types
# --------------------------------------------------------------------------- #
class ClientCommand(str, Enum):
    START_FLOW_GENERATION = "START_FLOW_GENERATION"
    PERTURB_OR_TRANSMUTE_ATOM = "PERTURB_OR_TRANSMUTE_ATOM"
    START_RELAXATION = "START_RELAXATION"
    RUN_PHONON_SHAKE_TEST = "RUN_PHONON_SHAKE_TEST"
    LOAD_STRUCTURE = "LOAD_STRUCTURE"
    DELETE_ATOM = "DELETE_ATOM"
    REQUEST_EIGENMODE = "REQUEST_EIGENMODE"
    FOLLOW_INSTABILITY = "FOLLOW_INSTABILITY"
    STOP_STREAM = "STOP_STREAM"
    # Phase A (IMPROVEMENT_SUGGESTIONS.md)
    COMPUTE_BULK_MODULUS = "COMPUTE_BULK_MODULUS"   # instant
    REFINE_STRUCTURE = "REFINE_STRUCTURE"           # instant, state-changing
    UNDO = "UNDO"                                   # instant, state-changing
    REDO = "REDO"                                   # instant, state-changing
    REQUEST_PHONON_DOS = "REQUEST_PHONON_DOS"       # instant, needs a valid phonon analysis


class ServerEvent(str, Enum):
    FLOW_TRAJECTORY_FRAME = "FLOW_TRAJECTORY_FRAME"
    CRYSTAL_STATE_FINAL = "CRYSTAL_STATE_FINAL"
    SINGLE_POINT_UPDATE = "SINGLE_POINT_UPDATE"
    RELAXATION_FRAME = "RELAXATION_FRAME"
    PHONON_ANALYSIS_COMPLETE = "PHONON_ANALYSIS_COMPLETE"
    EIGENMODE_DATA = "EIGENMODE_DATA"
    STREAM_STOPPED = "STREAM_STOPPED"
    ERROR = "ERROR"
    # Phase A
    BULK_MODULUS_RESULT = "BULK_MODULUS_RESULT"
    PHONON_DOS_DATA = "PHONON_DOS_DATA"
    HISTORY_STATE = "HISTORY_STATE"


class Envelope(BaseModel):
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Client -> server command payloads
# --------------------------------------------------------------------------- #
class StartFlowGenerationPayload(BaseModel):
    elements: List[str] = Field(min_length=1)
    num_atoms: int = Field(default=12, ge=1, le=64)
    composition: Optional[Dict[str, int]] = None      # explicit atoms per cell; overrides num_atoms
    target_spacegroup: Optional[int] = Field(default=None, ge=1, le=230)
    target_crystal_system: Optional[str] = None       # "triclinic" ... "cubic"; used when no number is given
    target_bulk_modulus_gpa: Optional[float] = Field(default=None, gt=0)
    steps: int = Field(default=60, ge=2, le=500)
    physics_guidance_scale: float = Field(default=1.5, ge=0)
    seed: Optional[int] = None


class PerturbOrTransmuteAtomPayload(BaseModel):
    atom_index: int = Field(ge=0)
    new_cart_coords: Optional[List[float]] = Field(default=None, min_length=3, max_length=3)
    new_element: Optional[str] = None
    apply_to_equivalent_sites: bool = False
    auto_relax: bool = False


class StartRelaxationPayload(BaseModel):
    fmax: float = Field(default=0.02, gt=0)
    max_steps: int = Field(default=100, ge=1, le=2000)
    relax_cell: bool = True
    # ---- Phase A (IMPROVEMENT_SUGGESTIONS.md §3.4); defaults reproduce the previous behaviour.
    fix_symmetry: bool = False                       # ASE FixSymmetry (symprec 1e-2) on the start structure
    pressure_gpa: float = 0.0                        # external hydrostatic pressure (+ = compressive); relax_cell only
    stress_tol_gpa: Optional[float] = Field(default=None, gt=0)
    #   when set (and relax_cell): converged additionally requires max_ij |sigma_ij + P_ext delta_ij| < stress_tol_gpa
    optimizer: Literal["FIRE", "BFGS", "LBFGS"] = "FIRE"


class RunPhononShakeTestPayload(BaseModel):
    supercell_dim: List[int] = Field(default_factory=lambda: [2, 2, 2], min_length=3, max_length=3)
    displacement_distance: float = Field(default=0.01, gt=0)


class LoadStructurePayload(BaseModel):
    preset_id: Optional[str] = None
    cif: Optional[str] = None
    # ---- Phase A (IMPROVEMENT_SUGGESTIONS.md §4.3, §4.4)
    text: Optional[str] = None                        # structure file contents in text_format
    text_format: Literal["auto", "cif", "poscar", "extxyz"] = "auto"
    crystal: Optional[CrystalStatePayload] = None     # e.g. the client re-sending its last structure after a reconnect

    @model_validator(mode="after")
    def _exactly_one(self) -> "LoadStructurePayload":
        given = sum(x is not None for x in (self.preset_id, self.cif, self.text, self.crystal))
        if given != 1:
            raise ValueError("exactly one of preset_id, cif, text or crystal is required")
        return self


class DeleteAtomPayload(BaseModel):
    atom_index: int = Field(ge=0)


class RequestEigenmodePayload(BaseModel):
    q_index: int = Field(ge=0)
    band_index: int = Field(ge=0)


class FollowInstabilityPayload(BaseModel):
    q_index: int = Field(ge=0)
    band_index: int = Field(ge=0)
    amplitude: float = Field(default=0.1, gt=0)       # max atomic displacement in Angstroms
    fmax: float = Field(default=0.02, gt=0)
    max_steps: int = Field(default=200, ge=1, le=2000)


class StopStreamPayload(BaseModel):
    pass


# ---- Phase A commands -------------------------------------------------------
class ComputeBulkModulusPayload(BaseModel):
    """Equation-of-state fit of the active structure (clamped ion, fixed shape)."""

    strain_window: float = Field(default=0.02, gt=0, le=0.10)   # max |dV/V|
    n_points: int = Field(default=5, ge=5, le=15)                # volumes evenly spaced over the window


class RefineStructurePayload(BaseModel):
    """Snap the active structure to its detected space group (state-changing; replies SINGLE_POINT_UPDATE)."""

    symprec: float = Field(default=1e-2, gt=0, le=0.5)
    cell: Literal["refined", "conventional", "primitive"] = "refined"
    #   refined: same cell and atom count, positions/cell symmetrised;
    #   conventional / primitive: spglib standardised cells


class UndoPayload(BaseModel):
    pass


class RedoPayload(BaseModel):
    pass


class RequestPhononDosPayload(BaseModel):
    mesh: Optional[List[int]] = Field(default=None, min_length=3, max_length=3)  # None = automatic
    t_min: float = Field(default=0.0, ge=0)
    t_max: float = Field(default=1000.0, gt=0, le=5000)
    t_step: float = Field(default=10.0, gt=0)


# --------------------------------------------------------------------------- #
# Server -> client event payloads
# --------------------------------------------------------------------------- #
class FlowTrajectoryFrame(BaseModel):
    step: int              # 1 .. total_steps
    total_steps: int
    t: float               # flow time (step-1)/(total_steps-1): step 1 is the t == 0 prior, the last frame t == 1.0
    crystal: CrystalStatePayload


class CrystalStateFinal(BaseModel):
    crystal: CrystalStatePayload


class SinglePointUpdate(BaseModel):
    crystal: CrystalStatePayload


class RelaxationFrame(BaseModel):
    step: int              # 0 = the starting structure, then one per FIRE iteration
    converged: bool
    final: bool = False    # True on the last frame, whether converged or stopped at max_steps
    crystal: CrystalStatePayload


class KPathLabel(BaseModel):
    index: int             # index into kpath_distances / frequencies_thz
    label: str             # e.g. "\\Gamma", "X", or "X|K" at a path discontinuity


class Eigenmode(BaseModel):
    q_index: int
    q_label: Optional[str] = None
    q_frac: List[float]                     # q in the reciprocal basis of the active cell
    band_index: int                         # index into the ascending frequencies at this q
    frequency_thz: float                    # signed; negative = imaginary
    is_imaginary: bool                      # frequency_thz < -F_TOL_THZ
    eigenvectors_real: List[List[float]]    # N x 3, indexed like CrystalStatePayload.atoms
    eigenvectors_imag: List[List[float]]    # N x 3


class PhononAnalysisComplete(BaseModel):
    is_dynamically_stable: bool
    min_frequency_thz: float
    imaginary_mode_count: int               # max number of imaginary modes at any single q-point
    kpath_distances: List[float]            # length n_q
    kpath_labels: List[KPathLabel]
    frequencies_thz: List[List[float]]      # [n_q][3N], ascending within each q-point
    eigenmodes: List[Eigenmode]             # every band at each labelled high-symmetry point


class EigenmodeData(BaseModel):
    eigenmode: Eigenmode


class StreamStopped(BaseModel):
    stopped: Optional[str] = None           # command type that was cancelled


# ---- Phase A events ---------------------------------------------------------
class BulkModulusResult(BaseModel):
    k_gpa: Optional[float]               # K = V0 d2E/dV2 at the given volume (None if the fit is not meaningful)
    k_stress_gpa: Optional[float]        # -V dP/dV from the stresses (cross-check)
    v0: float                            # given volume, Angstrom^3
    pressure_gpa: float                  # pressure of the given cell (+ = compressed)
    v_eq: Optional[float]                # volume of the fit's zero-pressure point (may be extrapolated)
    k_eq_gpa: Optional[float]            # BM B0 at v_eq
    k_eq_prime: Optional[float]          # BM B0' at v_eq (dimensionless), if determinable
    extrapolated: bool                   # v_eq lies outside the sampled volume window
    volumes: List[float]
    energies_ev: List[float]             # total energies of the cell
    pressures_gpa: List[float]
    method: str                          # human-readable: clamped-ion, fixed shape, BM-3 fit, window


class ThermalProperties(BaseModel):
    temperatures_k: List[float]
    free_energy_ev_per_atom: List[float]     # harmonic Helmholtz F_vib(T), includes the zero-point energy
    entropy_kb_per_atom: List[float]
    heat_capacity_kb_per_atom: List[float]   # C_v
    zero_point_energy_ev_per_atom: float


class PhononDosData(BaseModel):
    mesh: List[int]
    frequencies_thz: List[float]             # DOS grid (signed THz)
    total_dos: List[float]                   # states / THz / cell; integrates to 3N
    projected_dos: Dict[str, List[float]]    # element symbol -> states / THz / cell (sums to total_dos)
    thermal: ThermalProperties
    imaginary_fraction: float                # fraction of mesh modes with f < -F_TOL_THZ (excluded from thermal)


class HistoryState(BaseModel):
    """Sent after every change of the undo history (commit, undo, redo, load)."""

    can_undo: bool
    can_redo: bool
    position: int                            # index of the current entry (0-based)
    length: int                              # number of entries (bounded by HISTORY_LIMIT = 50)
    label: str                               # what produced the current entry, e.g. "Relaxation (converged)"


class ErrorCode(str, Enum):
    """Values of ``ErrorPayload.code`` (PROJECT_SPEC.md §5.2, ERROR event)."""

    INVALID_MESSAGE = "INVALID_MESSAGE"            # not JSON / not an envelope
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"            # envelope type is not a ClientCommand
    INVALID_PAYLOAD = "INVALID_PAYLOAD"            # payload failed schema validation
    INDEX_OUT_OF_RANGE = "INDEX_OUT_OF_RANGE"      # atom / q / band index outside the active data
    INVALID_ELEMENT = "INVALID_ELEMENT"            # unknown chemical symbol
    INVALID_OPERATION = "INVALID_OPERATION"        # e.g. deleting the last atom
    NO_ACTIVE_STRUCTURE = "NO_ACTIVE_STRUCTURE"
    NO_PHONON_ANALYSIS = "NO_PHONON_ANALYSIS"      # REQUEST_EIGENMODE / FOLLOW_INSTABILITY without a valid cache
    MODE_NOT_IMAGINARY = "MODE_NOT_IMAGINARY"      # FOLLOW_INSTABILITY on a stable mode
    UNKNOWN_PRESET = "UNKNOWN_PRESET"
    INVALID_STRUCTURE = "INVALID_STRUCTURE"        # unparsable CIF, empty or degenerate cell
    DISORDERED_STRUCTURE = "DISORDERED_STRUCTURE"  # CIF with partial occupancies
    STRUCTURE_TOO_LARGE = "STRUCTURE_TOO_LARGE"
    GENERATION_FAILED = "GENERATION_FAILED"        # flow generator could not satisfy the request
    RELAXATION_DIVERGED = "RELAXATION_DIVERGED"
    COMPUTE_FAILED = "COMPUTE_FAILED"              # any other engine exception
    HULL_UNAVAILABLE = "HULL_UNAVAILABLE"          # REST only: no MP_API_KEY or MP unreachable


class ErrorPayload(BaseModel):
    code: str                               # an ErrorCode value
    message: str
    command: Optional[str] = None


# Global imaginary-mode tolerance (PROJECT_SPEC.md §4.4 step 5).
F_TOL_THZ = 0.05
