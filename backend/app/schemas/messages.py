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
    # Phase B
    CHECK_PHONON_CONVERGENCE = "CHECK_PHONON_CONVERGENCE"   # streaming, needs a valid phonon analysis
    COMPUTE_ELASTIC_TENSOR = "COMPUTE_ELASTIC_TENSOR"       # streaming (PROGRESS events)
    EXPORT_PHONON_DATA = "EXPORT_PHONON_DATA"               # instant, needs a valid phonon analysis
    SET_MODEL = "SET_MODEL"                                 # streaming (load / download), state-changing
    # Phase C1
    REQUEST_LOCAL_ANALYSIS = "REQUEST_LOCAL_ANALYSIS"       # instant
    MAKE_SUPERCELL = "MAKE_SUPERCELL"                       # instant, state-changing
    ADD_ATOM = "ADD_ATOM"                                   # instant, state-changing
    COMPUTE_FORMATION_ENERGY = "COMPUTE_FORMATION_ENERGY"   # streaming (may fetch + relax hull references)
    SUBSTITUTE_FRACTION = "SUBSTITUTE_FRACTION"             # streaming, state-changing
    START_MD = "START_MD"                                   # streaming, state-changing
    COMMITTEE_EVALUATE = "COMMITTEE_EVALUATE"               # streaming, not state-changing
    # Phase C2
    PROTOTYPE_SEARCH = "PROTOTYPE_SEARCH"                   # streaming, not state-changing
    # Phase D
    REQUEST_HISTORY_ENTRY = "REQUEST_HISTORY_ENTRY"         # instant, cancels nothing


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
    # Phase B
    PROGRESS = "PROGRESS"
    PHONON_CONVERGENCE_RESULT = "PHONON_CONVERGENCE_RESULT"
    ELASTIC_TENSOR_RESULT = "ELASTIC_TENSOR_RESULT"
    PHONON_EXPORT = "PHONON_EXPORT"
    MODEL_CHANGED = "MODEL_CHANGED"
    # Phase C1
    LOCAL_ANALYSIS_DATA = "LOCAL_ANALYSIS_DATA"
    FORMATION_ENERGY_RESULT = "FORMATION_ENERGY_RESULT"
    SUBSTITUTION_RESULT = "SUBSTITUTION_RESULT"
    MD_FRAME = "MD_FRAME"
    MD_COMPLETE = "MD_COMPLETE"
    COMMITTEE_RESULT = "COMMITTEE_RESULT"
    # Phase C2
    GENERATION_CANDIDATES = "GENERATION_CANDIDATES"
    PROTOTYPE_CANDIDATES = "PROTOTYPE_CANDIDATES"
    # Phase D
    HISTORY_ENTRY = "HISTORY_ENTRY"


class Envelope(BaseModel):
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    # Phase D: optional client-chosen id (<= 64 chars). Every server message caused by that command
    # (replies, stream frames, PROGRESS, terminal messages, the HISTORY_STATE it pushes, its ERROR,
    # and the STREAM_STOPPED of its own cancellation) carries the same top-level "request_id".
    # Messages with no originating request (e.g. the connect-time load) carry none.
    request_id: Optional[str] = Field(default=None, max_length=64)


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
    # ---- Phase C2 (IMPROVEMENT_SUGGESTIONS.md §7.5)
    top_k: int = Field(default=1, ge=1, le=10)
    #   > 1: after the stream, the server also sends GENERATION_CANDIDATES (just before CRYSTAL_STATE_FINAL)
    #   with the top_k distinct endpoint candidates of the search, each relaxed under its symmetry and
    #   ranked by energy; the streamed trajectory still lands on rank 1, which becomes active.


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
    # ---- Phase B (IMPROVEMENT_SUGGESTIONS.md §5.2-5.3)
    cell_mode: Literal["as_displayed", "primitive"] = "as_displayed"
    #   primitive: force constants and bands of the spglib primitive cell with the standard seekpath
    #   path (textbook dispersion, 3 N_prim branches); eigenvectors are still mapped onto the
    #   displayed atoms (N_displayed x 3) with q_frac in the displayed cell's reciprocal basis.
    min_supercell_length: Optional[float] = Field(default=None, gt=0, le=40)
    #   when set, overrides supercell_dim: diagonal n_i = ceil(L / |a_i|) of the phonon cell, reduced
    #   (largest n first) until the supercell has <= PHONON_SUPERCELL_MAX_ATOMS (600) atoms.


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
    # ---- Phase B (all optional; v1.1 / Phase A clients ignore them)
    cell_mode: str = "as_displayed"                    # "as_displayed" | "primitive"
    n_primitive_atoms: Optional[int] = None            # N of the phonon cell in primitive mode (3N branches)
    supercell_dim: Optional[List[int]] = None          # supercell actually used
    min_image_distance: Optional[float] = None         # Angstrom: shortest lattice vector of that supercell
    band_order: Optional[List[List[int]]] = None
    #   band connection: band_order[q][k] = index into the ascending frequencies_thz[q] of connected
    #   branch k (draw branch k through q by following it); None if not computed.
    gamma_modes: Optional[List["GammaModeIrrep"]] = None   # irreps at Gamma (None if unavailable)
    band_path_source: Optional[str] = None             # "seekpath-standard" | "seekpath-orig-cell" | "fallback"


class GammaModeIrrep(BaseModel):
    """One (possibly degenerate) set of modes at Gamma with its irreducible representation."""

    band_indices: List[int]                 # indices into the ascending Gamma frequencies
    frequency_thz: float
    label: Optional[str] = None             # Mulliken / BSW label from phonopy, e.g. "T1u", "A1g"
    is_acoustic: bool = False
    ir_active: Optional[bool] = None        # irrep occurs in the vector representation
    raman_active: Optional[bool] = None     # irrep occurs in the symmetric square of the vector representation


class EigenmodeData(BaseModel):
    eigenmode: Eigenmode


class StreamStopped(BaseModel):
    stopped: Optional[str] = None           # command type that was cancelled
    history_pushed: bool = False            # Phase D: a HISTORY_STATE for the cancelled stream follows


# ---- Phase B commands -------------------------------------------------------
class CheckPhononConvergencePayload(BaseModel):
    """Re-run the current phonon analysis (same cell_mode / displacement) in a larger supercell.

    Terminal messages: PHONON_CONVERGENCE_RESULT, then PHONON_ANALYSIS_COMPLETE of the larger
    supercell (which replaces the session's phonon cache) and its flagging SINGLE_POINT_UPDATE.
    """

    supercell_dim: Optional[List[int]] = Field(default=None, min_length=3, max_length=3)
    #   None = current supercell + 1 along every axis, reduced (largest first) to <= 600 atoms


class ComputeElasticTensorPayload(BaseModel):
    max_strain: float = Field(default=0.01, gt=0, le=0.05)    # largest |strain| per component
    n_strains: int = Field(default=4, ge=2, le=8)             # strain magnitudes per component, symmetric (+/-)
    relax_ions: bool = True                                   # relax positions at fixed strain (relaxed-ion C_ij)
    fmax: float = Field(default=0.01, gt=0)                   # ionic relaxation tolerance (eV/A)


class ExportPhononDataPayload(BaseModel):
    pass


class SetModelPayload(BaseModel):
    """Switch this session's force field. Terminal messages: MODEL_CHANGED, then a re-evaluated
    SINGLE_POINT_UPDATE of the active structure and HISTORY_STATE (label "Switch model to <id>").
    Invalidates the phonon cache. PROGRESS events may report a checkpoint download."""

    model_id: str                         # an id from GET /api/models, e.g. "small", "medium-mpa-0", "lj-fallback"
    dispersion: bool = False              # add D3(BJ) dispersion (MACE models only)


# ---- Phase B events ---------------------------------------------------------
class Progress(BaseModel):
    command: str                          # the streaming command this belongs to
    done: int
    total: int
    message: str = ""


class PhononConvergenceResult(BaseModel):
    supercell_dim_a: List[int]            # previous supercell
    supercell_dim_b: List[int]            # larger supercell
    min_image_distance_a: float
    min_image_distance_b: float
    min_frequency_thz_a: float
    min_frequency_thz_b: float
    max_abs_delta_thz: float              # max over the band path and all branches |f_b - f_a|
    rms_delta_thz: float
    per_label_max_delta_thz: Dict[str, float]   # labelled q-point -> max |delta f| there
    tolerance_thz: float = 0.1
    converged: bool                       # max_abs_delta_thz < tolerance and the stability verdict agrees


class ElasticTensorResult(BaseModel):
    c_ij_gpa: List[List[float]]           # 6x6 Voigt (xx, yy, zz, yz, xz, xy), symmetrised
    eigenvalues_gpa: List[float]          # of C (Born stability: all > 0)
    born_stable: bool
    k_voigt_gpa: float
    k_reuss_gpa: float
    k_vrh_gpa: float
    g_voigt_gpa: float
    g_reuss_gpa: float
    g_vrh_gpa: float
    youngs_modulus_vrh_gpa: float
    poisson_ratio_vrh: float
    pugh_ratio: float                     # G_VRH / K_VRH (< ~0.57 ductile)
    universal_anisotropy: float           # A^U = 5 G_V/G_R + K_V/K_R - 6
    debye_temperature_k: Optional[float] = None
    relaxed_ions: bool
    max_strain: float
    reference_pressure_gpa: float         # pressure of the unstrained structure (should be ~0)
    fit_residual_gpa: float               # rms residual of the linear stress-strain fit
    method: str


class PhononExport(BaseModel):
    filename: str                         # e.g. "SrTiO3_phonopy_params.yaml"
    phonopy_yaml: str                     # phonopy_params.yaml incl. force constants (loadable with phonopy.load)


class ModelChanged(BaseModel):
    model_id: str
    backend_name: str                     # e.g. "mace-mpa-0-medium+d3"
    dispersion: bool
    device: str
    dtype: str


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
    # Phase C1: the whole history (oldest first), so the client can pick e.g. a bulk reference.
    entries: Optional[List["HistoryEntryInfo"]] = None


class HistoryEntryInfo(BaseModel):
    position: int
    label: str
    formula: str
    n_atoms: int
    energy_per_atom: float                   # with the model that produced the stored payload
    spacegroup_symbol: str


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
    MP_UNAVAILABLE = "MP_UNAVAILABLE"              # REST only (Phase B): no MP_API_KEY (503) or MP unreachable (502)
    MP_NOT_FOUND = "MP_NOT_FOUND"                  # REST only (Phase B): unknown material id (404)
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"        # SET_MODEL / model_id: unknown model or it cannot be loaded


class ErrorPayload(BaseModel):
    code: str                               # an ErrorCode value
    message: str
    command: Optional[str] = None
    history_pushed: bool = False            # Phase D: a HISTORY_STATE for the failed stream follows
    after_terminal: bool = False            # Phase D: this ERROR follows the stream's terminal frame
    #   (e.g. RELAXATION_DIVERGED after the final RELAXATION_FRAME): the stream is over either way


# Global imaginary-mode tolerance (PROJECT_SPEC.md §4.4 step 5).
F_TOL_THZ = 0.05


# Resolve the forward reference PhononAnalysisComplete -> GammaModeIrrep (Phase B).
PhononAnalysisComplete.model_rebuild()


# =========================================================================== #
# Phase C1 (IMPROVEMENT_SUGGESTIONS.md §5.7-5.10, §6.2)
# =========================================================================== #
# ---- commands ---------------------------------------------------------------
class RequestLocalAnalysisPayload(BaseModel):
    """Local structure of the active structure -> LOCAL_ANALYSIS_DATA (instant, cancels nothing)."""

    include_partial_rdf: bool = True


class MakeSupercellPayload(BaseModel):
    """Replace the active structure by L' = M L (M integer, det(M) > 0). SINGLE_POINT_UPDATE + HISTORY_STATE
    (label "Make supercell <n1>x<n2>x<n3>" or "Make supercell [[...]]"). STRUCTURE_TOO_LARGE above MAX_ATOMS."""

    matrix: List[List[int]] = Field(min_length=3, max_length=3)   # rows: new lattice vectors in units of the old


class AddAtomPayload(BaseModel):
    """Insert an atom (interstitial / adsorbate). SINGLE_POINT_UPDATE + HISTORY_STATE ("Add <El> at (u, v, w)").
    INVALID_OPERATION if it lands closer than 0.5 A to an existing atom (any periodic image)."""

    element: str
    frac_coords: List[float] = Field(min_length=3, max_length=3)


class ComputeFormationEnergyPayload(BaseModel):
    """Neutral point-defect formation energy of the ACTIVE structure (the defect cell) relative to the
    bulk history entry at `bulk_position` (same lattice up to relaxation; the atoms differ by vacancies,
    substitutions or interstitials). Chemical potentials come from the uMLIP-relaxed MP reference phases
    of the chemical system of the bulk plus any added or removed elements (same pipeline and cache as the
    hull; needs MP_API_KEY unless cached). Terminal: FORMATION_ENERGY_RESULT. PROGRESS while references relax."""

    bulk_position: int = Field(ge=0)


class SubstituteFractionPayload(BaseModel):
    """Replace round(fraction * n_from) atoms of `from_element` by `to_element`, trying up to n_candidates
    symmetry-distinct configurations (random if too many), keep the lowest uMLIP energy (single points).
    Terminal: SUBSTITUTION_RESULT, then SINGLE_POINT_UPDATE + HISTORY_STATE ("Substitute <n> <A> -> <B>")."""

    from_element: str
    to_element: str
    fraction: float = Field(gt=0, le=1)
    n_candidates: int = Field(default=12, ge=1, le=100)
    seed: Optional[int] = None


class StartMDPayload(BaseModel):
    """Molecular dynamics with the session engine. Streams MD_FRAME every frame_interval steps; terminal
    MD_COMPLETE (or STREAM_STOPPED). The structure of the last frame becomes active and is pushed to the
    history ("MD <T> K <ensemble> (<t> ps)", or "... (stopped)")."""

    temperature_k: float = Field(default=300.0, gt=0, le=5000)
    ensemble: Literal["NVE", "NVT", "NPT"] = "NVT"        # NVT: Langevin; NPT: Langevin + isotropic barostat
    timestep_fs: float = Field(default=1.0, gt=0, le=5)
    n_steps: int = Field(default=2000, ge=1, le=200000)
    friction_per_fs: float = Field(default=0.01, gt=0, le=1)   # Langevin friction
    pressure_gpa: float = 0.0                               # NPT only
    frame_interval: int = Field(default=10, ge=1, le=1000)
    seed: Optional[int] = None                              # Maxwell-Boltzmann velocities + thermostat noise


class CommitteeEvaluatePayload(BaseModel):
    """Evaluate the active structure with several models (not state-changing). Terminal COMMITTEE_RESULT;
    PROGRESS per model (loading may download a checkpoint)."""

    model_ids: List[str] = Field(min_length=2, max_length=4)
    relax: bool = True                                      # also relax (positions + cell, fmax 0.02) per model
    dispersion: bool = False


# ---- events -----------------------------------------------------------------
class NeighborInfo(BaseModel):
    index: int
    element: str
    distance: float
    image_offset: List[int]


class SiteEnvironment(BaseModel):
    index: int
    element: str
    coordination_number: int
    neighbors: List[NeighborInfo]
    oxidation_state: Optional[float] = None
    bond_valence_sum: Optional[float] = None


class Polyhedron(BaseModel):
    center_index: int
    center_element: str
    ligand_indices: List[int]
    ligand_image_offsets: List[List[int]]   # periodic image of each ligand relative to the centre (as in BondEdge)
    shape: str                              # "octahedron" | "tetrahedron" | "square-planar" | "trigonal-planar" | "other"
    mean_bond_length: float
    bond_length_distortion: float           # (1/n) sum |l_i - l_mean| / l_mean
    quadratic_elongation: Optional[float] = None   # octahedra / tetrahedra (Robinson et al.)


class OctahedralTilts(BaseModel):
    """Perovskite-like corner-sharing BX6 networks only (None otherwise)."""

    tilt_angles_deg: List[float]            # rotation magnitudes about the pseudo-cubic a, b, c axes
    phases: List[str]                       # per axis "0" | "+" (in-phase) | "-" (anti-phase)
    glazer: Optional[str]                   # e.g. "a0a0c-" (None if not determinable)
    pseudo_cubic_axes: List[List[float]]    # unit vectors used as a, b, c
    method: str


class LocalAnalysisData(BaseModel):
    rdf_r: List[float]                                  # same grid as CrystalStatePayload.rdf_r
    partial_rdf: Optional[Dict[str, List[float]]] = None   # "A-B" (alphabetical) -> g_AB(r), normalised so g -> 1
    sites: List[SiteEnvironment]
    bond_ideal_lengths: Optional[List[Optional[float]]] = None
    #   aligned with the ACTIVE payload bonds: bond-valence ideal length r0 - b ln(V/CN) (None if unknown);
    #   the frontend colours bonds by (length - ideal) / ideal instead of the covalent-radius strain_ratio
    global_instability_index: Optional[float] = None    # sqrt(mean (BVS - |oxidation state|)^2), v.u.
    polyhedra: List[Polyhedron]
    octahedral_tilts: Optional[OctahedralTilts] = None
    method: str


class FormationEnergyResult(BaseModel):
    defect_label: str                       # e.g. "V_O", "Na_Li", "Li_i", or a combination "V_O + V_Sr"
    delta_n: Dict[str, int]                 # n_defect - n_bulk per element (negative = removed)
    e_defect_ev: float                      # total energy of the defect cell (as given)
    e_bulk_ev: float                        # total energy of the bulk entry (re-evaluated with the session model)
    chemical_potentials: Dict[str, Dict[str, float]]   # limit name -> element -> mu (eV/atom)
    formation_energies_ev: Dict[str, float]            # limit name -> E_f = E_def - E_bulk - sum dn_i mu_i
    min_image_distance: float               # shortest defect-defect periodic distance (A)
    warnings: List[str]                     # e.g. cell too small, defect cell unrelaxed, bulk under pressure
    method: str


class SubstitutionResult(BaseModel):
    from_element: str
    to_element: str
    n_substituted: int
    n_tried: int
    energies_per_atom: List[float]          # of all tried configurations, ascending
    chosen_energy_per_atom: float
    method: str


class MDFrame(BaseModel):
    step: int
    time_ps: float
    temperature_k: float                    # instantaneous kinetic temperature
    potential_energy_ev: float              # total
    kinetic_energy_ev: float                # total
    pressure_gpa: Optional[float]           # incl. kinetic contribution
    volume: float
    msd_by_element: Dict[str, float]        # A^2, from unwrapped positions relative to step 0
    crystal: CrystalStatePayload            # wrapped, fast bonds


class MDComplete(BaseModel):
    n_steps: int
    time_ps: float
    mean_temperature_k: float
    mean_pressure_gpa: Optional[float]
    mean_volume: float
    times_ps: List[float]                   # MSD sampling times
    msd_by_element: Dict[str, List[float]]
    diffusion_coefficients_cm2_s: Dict[str, Optional[float]]   # slope/6 over the last half; None if no linear regime
    final_crystal: CrystalStatePayload
    method: str


class CommitteeEntry(BaseModel):
    model_id: str
    backend_name: str
    pressure_gpa: float                     # of the structure as given
    max_force: float
    relaxed_volume_per_atom: Optional[float] = None
    relaxed_lattice_abc: Optional[List[float]] = None
    relaxed_spacegroup: Optional[str] = None
    relaxed_energy_per_atom: Optional[float] = None   # NOT comparable across models (different references)


class CommitteeResult(BaseModel):
    entries: List[CommitteeEntry]
    spread: Dict[str, float]                # e.g. "pressure_gpa", "max_force", "volume_percent" (max - min)
    method: str


HistoryState.model_rebuild()


# =========================================================================== #
# Phase C2 (IMPROVEMENT_SUGGESTIONS.md §7.2, §7.5)
# =========================================================================== #
class PrototypeSearchPayload(BaseModel):
    """Ionic-substitution search over known Materials Project prototypes (pymatgen SubstitutionPredictor).

    Not state-changing. PROGRESS while sources are fetched and candidates relaxed; terminal
    PROTOTYPE_CANDIDATES. The client loads a chosen candidate with LOAD_STRUCTURE {crystal}.
    Needs MP_API_KEY (INVALID_OPERATION with an explanation otherwise)."""

    composition: Dict[str, int]              # target, e.g. {"Na": 1, "Nb": 1, "O": 3} (reduced or per cell)
    max_candidates: int = Field(default=8, ge=1, le=30)
    probability_threshold: float = Field(default=1e-3, gt=0, lt=1)
    relax: bool = True                       # relax each candidate (positions + cell, fmax 0.05)
    with_e_hull: bool = True                 # E_hull of each candidate against the uMLIP reference hull


class GenerationSearchSummary(BaseModel):
    spacegroup_number: int
    spacegroup_symbol: str
    composition: Dict[str, int]
    n_wyckoff_combinations: int              # distinct Wyckoff assignments explored
    n_structures_screened: int               # pre-screened with the analytic potential
    n_structures_scored: int                 # scored with the session engine


class GenerationCandidate(BaseModel):
    rank: int                                # 1 = lowest energy (the streamed endpoint)
    energy_per_atom: float
    relative_energy_mev_per_atom: float
    spacegroup_symbol: str
    spacegroup_number: int
    wyckoff_assignment: str                  # e.g. "Sr 1a, Ti 1b, O 3c"
    crystal: CrystalStatePayload


class GenerationCandidates(BaseModel):
    summary: GenerationSearchSummary
    candidates: List[GenerationCandidate]


class PrototypeCandidate(BaseModel):
    rank: int
    formula: str
    source_material_id: str                  # MP prototype the candidate was derived from
    source_formula: str
    substitutions: Dict[str, str]            # source species -> target species, e.g. {"K": "Na", "Ta": "Nb"}
    probability: float                       # SubstitutionPredictor probability
    spacegroup_symbol: str
    energy_per_atom: float
    e_hull_ev: Optional[float] = None
    relaxed: bool
    crystal: CrystalStatePayload


class PrototypeCandidates(BaseModel):
    target_formula: str
    n_sources: int
    candidates: List[PrototypeCandidate]     # sorted by e_hull_ev (then energy) when available
    method: str


# =========================================================================== #
# Phase D: protocol ergonomics
# =========================================================================== #
class RequestHistoryEntryPayload(BaseModel):
    """Fetch the stored structure of a history entry without changing state -> HISTORY_ENTRY
    (INDEX_OUT_OF_RANGE for an unknown position)."""

    position: int = Field(ge=0)


class HistoryEntry(BaseModel):
    position: int
    label: str
    crystal: CrystalStatePayload            # the stored payload (model that produced it)
