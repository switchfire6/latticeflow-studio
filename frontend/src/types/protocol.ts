/**
 * Wire protocol — TypeScript mirror of the backend Pydantic contracts.
 *
 * PM-owned shared contract. Sources of truth:
 *   backend/app/schemas/crystal.py   (PROJECT_SPEC.md §5.1)
 *   backend/app/schemas/messages.py  (PROJECT_SPEC.md §5.2)
 *   backend/app/schemas/api.py       (PROJECT_SPEC.md §5.3)
 * Additive changes only; keep in sync with the Python models.
 *
 * Conventions: lattice `matrix` rows are the lattice vectors a, b, c (row-vector
 * convention, r = s · L). Lengths Å, energies eV, forces eV/Å, stress GPa (Voigt
 * [xx, yy, zz, yz, xz, xy], ASE sign: positive = tensile), frequencies THz
 * (signed; negative = imaginary), kpath distances Å⁻¹ without 2π.
 */

// --------------------------------------------------------------------------- //
// §5.1 Core data contracts
// --------------------------------------------------------------------------- //
export interface LatticeParams {
  a: number;
  b: number;
  c: number;
  alpha: number;
  beta: number;
  gamma: number;
  matrix: number[][]; // 3x3, rows = lattice vectors (Å)
  volume: number;
}

export interface AtomSite {
  index: number;
  element: string;
  atomic_number: number;
  frac_coords: [number, number, number]; // in [0, 1)
  cart_coords: [number, number, number]; // Å
  force: [number, number, number]; // eV/Å
  force_norm: number;
  is_unstable_actor: boolean;
  /** Phase A: Wyckoff label "<multiplicity><letter>" (e.g. "3c"); null/absent if symmetry detection failed. */
  wyckoff?: string | null;
}

export interface BondEdge {
  source_index: number;
  target_index: number;
  /** Periodic image of the target atom: bond vector = r_target + n·L − r_source. */
  image_offset: [number, number, number];
  length: number;
  strain_ratio: number; // (length − r_cov_sum) / r_cov_sum
  is_unstable: boolean;
}

export interface CrystalStatePayload {
  formula: string;
  spacegroup_symbol: string;
  spacegroup_number: number;
  lattice: LatticeParams;
  atoms: AtomSite[];
  bonds: BondEdge[];
  energy_per_atom: number;
  max_force: number;
  stress_gpa: number[]; // Voigt 6-vector
  bulk_modulus_gpa: number | null;
  e_hull_ev: number | null;
  rdf_r: number[]; // 151 points, 0.5–8.0 Å
  rdf_g: number[];
  // ---- Phase A (IMPROVEMENT_SUGGESTIONS.md §3.1–3.2); optional for older recordings/fixtures.
  pressure_gpa?: number | null; // −tr(σ)/3, + = compressed
  density_g_cm3?: number | null;
  crystal_system?: CrystalSystem | null;
  formula_units?: number | null; // Z
  symprec?: number | null;
}

// --------------------------------------------------------------------------- //
// §5.2 WebSocket envelope, commands and events  (endpoint: /ws/studio)
// --------------------------------------------------------------------------- //
export interface Envelope<T extends string = string, P = unknown> {
  type: T;
  payload: P;
}

export type CrystalSystem =
  | "triclinic"
  | "monoclinic"
  | "orthorhombic"
  | "tetragonal"
  | "trigonal"
  | "hexagonal"
  | "cubic";

export interface StartFlowGenerationPayload {
  elements: string[];
  num_atoms?: number; // 1–64, default 12
  composition?: Record<string, number> | null; // overrides num_atoms
  target_spacegroup?: number | null; // 1–230
  target_crystal_system?: CrystalSystem | null; // used when no number is given
  target_bulk_modulus_gpa?: number | null;
  steps?: number; // 2–500, default 60
  physics_guidance_scale?: number; // ≥ 0, default 1.5
  seed?: number | null;
}

export interface PerturbOrTransmuteAtomPayload {
  atom_index: number;
  new_cart_coords?: [number, number, number] | null;
  new_element?: string | null;
  apply_to_equivalent_sites?: boolean; // element changes only
  auto_relax?: boolean;
}

export interface StartRelaxationPayload {
  fmax?: number; // default 0.02 eV/Å
  max_steps?: number; // default 100
  relax_cell?: boolean; // default true
  // Phase A (§3.4)
  fix_symmetry?: boolean; // default false
  pressure_gpa?: number; // external hydrostatic pressure, + = compressive; default 0
  stress_tol_gpa?: number | null; // when set: converged also needs max|σ_ij + P δ_ij| < tol
  optimizer?: "FIRE" | "BFGS" | "LBFGS"; // default FIRE
}

export interface RunPhononShakeTestPayload {
  supercell_dim?: [number, number, number]; // default [2, 2, 2]
  displacement_distance?: number; // default 0.01 Å
}

export type StructureTextFormat = "auto" | "cif" | "poscar" | "extxyz";
export type LoadStructurePayload =
  | { preset_id: string }
  | { cif: string }
  | { text: string; text_format?: StructureTextFormat } // Phase A §4.3
  | { crystal: CrystalStatePayload }; // Phase A §4.4 (restore after reconnect)

export interface DeleteAtomPayload {
  atom_index: number;
}

export interface RequestEigenmodePayload {
  q_index: number;
  band_index: number;
}

export interface FollowInstabilityPayload {
  q_index: number;
  band_index: number;
  amplitude?: number; // max displacement Å, default 0.1
  fmax?: number; // default 0.02
  max_steps?: number; // default 200
}

export type StopStreamPayload = Record<string, never>;

// ---- Phase A commands
export interface ComputeBulkModulusPayload {
  strain_window?: number; // max |ΔV/V|, default 0.02, ≤ 0.10
  n_points?: number; // 5–15, default 5
}
export interface RefineStructurePayload {
  symprec?: number; // default 0.01
  cell?: "refined" | "conventional" | "primitive"; // default "refined"
}
export interface RequestPhononDosPayload {
  mesh?: [number, number, number] | null; // null = automatic
  t_min?: number; // K, default 0
  t_max?: number; // K, default 1000
  t_step?: number; // K, default 10
}

export interface ClientCommandMap {
  START_FLOW_GENERATION: StartFlowGenerationPayload;
  PERTURB_OR_TRANSMUTE_ATOM: PerturbOrTransmuteAtomPayload;
  START_RELAXATION: StartRelaxationPayload;
  RUN_PHONON_SHAKE_TEST: RunPhononShakeTestPayload;
  LOAD_STRUCTURE: LoadStructurePayload;
  DELETE_ATOM: DeleteAtomPayload;
  REQUEST_EIGENMODE: RequestEigenmodePayload;
  FOLLOW_INSTABILITY: FollowInstabilityPayload;
  STOP_STREAM: StopStreamPayload;
  COMPUTE_BULK_MODULUS: ComputeBulkModulusPayload;
  REFINE_STRUCTURE: RefineStructurePayload;
  UNDO: Record<string, never>;
  REDO: Record<string, never>;
  REQUEST_PHONON_DOS: RequestPhononDosPayload;
}
export type ClientCommand = keyof ClientCommandMap;

export interface FlowTrajectoryFrame {
  step: number; // 1..total_steps
  total_steps: number;
  t: number; // (step−1)/(total_steps−1): first frame is the t = 0 prior, last is t = 1
  crystal: CrystalStatePayload;
}

export interface CrystalStateFinal {
  crystal: CrystalStatePayload; // CrystalNN bonds, bulk_modulus_gpa filled
}

export interface SinglePointUpdate {
  crystal: CrystalStatePayload;
}

export interface RelaxationFrame {
  step: number; // 0 = starting structure
  converged: boolean;
  final: boolean; // last frame of the stream
  crystal: CrystalStatePayload;
}

export interface KPathLabel {
  index: number; // into kpath_distances / frequencies_thz
  label: string; // e.g. "\\Gamma", "X", or "X|K" at a discontinuity
}

export interface Eigenmode {
  q_index: number;
  q_label: string | null;
  q_frac: [number, number, number]; // reciprocal basis of the active cell
  band_index: number; // into the ascending frequencies at this q
  frequency_thz: number; // signed
  is_imaginary: boolean; // frequency_thz < −F_TOL_THZ
  eigenvectors_real: number[][]; // N x 3, mass-weighted, indexed like atoms
  eigenvectors_imag: number[][]; // N x 3
}

export interface PhononAnalysisComplete {
  is_dynamically_stable: boolean;
  min_frequency_thz: number;
  imaginary_mode_count: number; // max over q-points
  kpath_distances: number[]; // length n_q
  kpath_labels: KPathLabel[];
  frequencies_thz: number[][]; // [n_q][3N], ascending per q
  eigenmodes: Eigenmode[]; // every band at each distinct labelled point
}

export interface EigenmodeData {
  eigenmode: Eigenmode;
}

export interface StreamStopped {
  stopped: ClientCommand | null;
}

// ---- Phase A events
export interface BulkModulusResult {
  k_gpa: number | null; // at the given volume
  k_stress_gpa: number | null;
  v0: number;
  pressure_gpa: number;
  v_eq: number | null;
  k_eq_gpa: number | null; // BM B0 at v_eq
  k_eq_prime: number | null;
  extrapolated: boolean;
  volumes: number[];
  energies_ev: number[];
  pressures_gpa: number[];
  method: string;
}

export interface ThermalProperties {
  temperatures_k: number[];
  free_energy_ev_per_atom: number[]; // includes ZPE
  entropy_kb_per_atom: number[];
  heat_capacity_kb_per_atom: number[];
  zero_point_energy_ev_per_atom: number;
}

export interface PhononDosData {
  mesh: [number, number, number];
  frequencies_thz: number[];
  total_dos: number[]; // states/THz/cell
  projected_dos: Record<string, number[]>; // by element
  thermal: ThermalProperties;
  imaginary_fraction: number;
}

export interface HistoryState {
  can_undo: boolean;
  can_redo: boolean;
  position: number;
  length: number;
  label: string;
}

export type ErrorCode =
  | "INVALID_MESSAGE"
  | "UNKNOWN_COMMAND"
  | "INVALID_PAYLOAD"
  | "INDEX_OUT_OF_RANGE"
  | "INVALID_ELEMENT"
  | "INVALID_OPERATION"
  | "NO_ACTIVE_STRUCTURE"
  | "NO_PHONON_ANALYSIS"
  | "MODE_NOT_IMAGINARY"
  | "UNKNOWN_PRESET"
  | "INVALID_STRUCTURE"
  | "DISORDERED_STRUCTURE"
  | "STRUCTURE_TOO_LARGE"
  | "GENERATION_FAILED"
  | "RELAXATION_DIVERGED"
  | "COMPUTE_FAILED"
  | "HULL_UNAVAILABLE";

export interface ErrorPayload {
  code: ErrorCode;
  message: string;
  command: ClientCommand | null;
}

export interface ServerEventMap {
  FLOW_TRAJECTORY_FRAME: FlowTrajectoryFrame;
  CRYSTAL_STATE_FINAL: CrystalStateFinal;
  SINGLE_POINT_UPDATE: SinglePointUpdate;
  RELAXATION_FRAME: RelaxationFrame;
  PHONON_ANALYSIS_COMPLETE: PhononAnalysisComplete;
  EIGENMODE_DATA: EigenmodeData;
  STREAM_STOPPED: StreamStopped;
  ERROR: ErrorPayload;
  BULK_MODULUS_RESULT: BulkModulusResult;
  PHONON_DOS_DATA: PhononDosData;
  HISTORY_STATE: HistoryState;
}
export type ServerEvent = keyof ServerEventMap;

/** Discriminated union of every server → client message. */
export type ServerMessage = {
  [K in ServerEvent]: Envelope<K, ServerEventMap[K]>;
}[ServerEvent];

/** Global imaginary-mode tolerance (PROJECT_SPEC.md §4.4 step 5). */
export const F_TOL_THZ = 0.05;

// --------------------------------------------------------------------------- //
// §5.3 REST bodies  (base path: /api)
// --------------------------------------------------------------------------- //
export interface HealthResponse {
  status: "ok";
  umlip_backend: string; // "mace-mp-0-small" | "lj-fallback"
  device: string;
  dtype: string;
  load_error: string | null;
  hull_available: boolean;
}

export type StabilityExpectation = "stable" | "unstable" | "unknown";

export interface PresetInfo {
  id: string;
  name: string;
  formula: string;
  description: string;
  spacegroup_symbol: string;
  spacegroup_number: number;
  num_atoms: number;
  expected_stability: StabilityExpectation;
  provenance: string;
}

export interface ExportRequest {
  crystal: CrystalStatePayload;
  format?: "cif" | "poscar" | "extxyz";
  symmetrize?: boolean; // cif: space group + asymmetric unit (default true)
  symprec?: number; // default 0.01
  cell?: "as_is" | "conventional" | "primitive"; // default "as_is"; extxyz requires "as_is"
}

export interface ImportCifRequest {
  cif: string;
}

export interface HullEvaluateRequest {
  crystal: CrystalStatePayload;
  relax?: boolean; // default true: relax the candidate before scoring
}

export interface HullReferencePhase {
  material_id: string;
  formula: string;
  energy_per_atom: number;
}

export interface HullEvaluateResponse {
  chemsys: string;
  energy_per_atom: number;
  e_hull_ev: number; // negative = below the reference hull
  formation_energy_per_atom: number | null;
  decomposition: Record<string, number>;
  references: HullReferencePhase[];
  method: string;
  umlip_backend: string;
  candidate_relaxed?: boolean;
  energy_per_atom_as_given?: number | null;
}

export interface ErrorResponse {
  code: ErrorCode;
  message: string;
}
