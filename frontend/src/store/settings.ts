/**
 * User settings (§3.4, §3.6): relaxation and phonon parameters, display units
 * and a few UI preferences. Kept in localStorage (utils/persist.ts, never
 * throws). Pure builders turn them into protocol payloads (unit-tested).
 */
import { create } from "zustand";
import type { ComputeElasticTensorPayload, DiffractionRequest, RunPhononShakeTestPayload, StartRelaxationPayload } from "../types/protocol";
import { RADIATIONS, type ProfileShape } from "../utils/diffraction";
import { loadJSON, saveJSON } from "../utils/persist";
import { RELAXED_STRESS_TOL_GPA, SUPERCELL_MIN_LENGTH, suggestSupercell, type RelaxationCriteria } from "../utils/structureInfo";
import { DEFAULT_UNITS, normalizeUnits, type UnitPrefs } from "../utils/units";

export type Optimizer = NonNullable<StartRelaxationPayload["optimizer"]>;
export const OPTIMIZERS: readonly Optimizer[] = ["FIRE", "BFGS", "LBFGS"];

export interface RelaxSettings {
  fmax: number; // eV/Å
  maxSteps: number;
  relaxCell: boolean;
  fixSymmetry: boolean;
  pressureGpa: number; // target hydrostatic pressure, + = compressive
  stressTolGpa: number; // sent as stress_tol_gpa
  optimizer: Optimizer;
}

export const DEFAULT_RELAX: RelaxSettings = {
  fmax: 0.02,
  maxSteps: 100,
  relaxCell: true,
  fixSymmetry: false,
  pressureGpa: 0,
  stressTolGpa: 0.05,
  optimizer: "FIRE",
};

export type PhononCellMode = NonNullable<RunPhononShakeTestPayload["cell_mode"]>;

export interface PhononSettings {
  /** null = automatic (every supercell vector ≥ minLength). */
  supercell: [number, number, number] | null;
  displacement: number; // Å
  /** Request the phonon DOS + thermodynamics right after each shake-test. */
  autoDos: boolean;
  /** Phase B (§5.2): "primitive" = textbook dispersion (default), "as_displayed" = the displayed cell. */
  cellMode?: PhononCellMode;
  /** Phase B (§5.3): automatic supercell target length (Å), sent as min_supercell_length. */
  minLength?: number;
  /** Colour connected branches individually (dispersion chart). */
  colorBranches?: boolean;
}

export const DEFAULT_PHONON: PhononSettings = {
  supercell: null,
  displacement: 0.01,
  autoDos: true,
  cellMode: "primitive",
  minLength: SUPERCELL_MIN_LENGTH,
  colorBranches: false,
};

/** Elastic tensor settings (§5.5). */
export interface ElasticSettings {
  maxStrain: number; // fraction, ≤ 0.05
  nStrains: number; // 2–8 (±)
  relaxIons: boolean;
  fmax: number; // eV/Å, relaxed-ion internal relaxation
}
export const DEFAULT_ELASTIC: ElasticSettings = { maxStrain: 0.01, nStrains: 4, relaxIons: true, fmax: 0.01 };

/** Diffraction settings (§5.6). */
export interface DiffractionSettings {
  kind: "xray" | "neutron";
  /** pymatgen radiation key or "custom" (then `wavelength`). */
  radiation: string;
  wavelength: number; // Å (custom)
  twoThetaMin: number;
  twoThetaMax: number;
  shape: ProfileShape;
  fwhm: number; // degrees 2θ
  eta: number; // pseudo-Voigt Lorentzian fraction
}
export const DEFAULT_DIFFRACTION: DiffractionSettings = {
  kind: "xray",
  radiation: "CuKa",
  wavelength: 1.54184,
  twoThetaMin: 5,
  twoThetaMax: 90,
  shape: "pseudo-voigt",
  fwhm: 0.15,
  eta: 0.5,
};

export interface SettingsState {
  relax: RelaxSettings;
  phonon: PhononSettings;
  elastic: ElasticSettings;
  diffraction: DiffractionSettings;
  units: UnitPrefs;
  /** Collapsed sidebar cards by id. */
  collapsed: Record<string, boolean>;
  setRelax(p: Partial<RelaxSettings>): void;
  setPhonon(p: Partial<PhononSettings>): void;
  setElastic(p: Partial<ElasticSettings>): void;
  setDiffraction(p: Partial<DiffractionSettings>): void;
  setUnits(p: Partial<UnitPrefs>): void;
  setCollapsed(id: string, collapsed: boolean): void;
  resetRelax(): void;
  resetPhonon(): void;
}

const clamp = (v: number, lo: number, hi: number, fallback: number) => (Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : fallback);

/** Sanitise relaxation settings (stored or typed). */
export function normalizeRelax(r: Partial<RelaxSettings>): RelaxSettings {
  const d = DEFAULT_RELAX;
  return {
    fmax: clamp(Number(r.fmax), 1e-4, 1, d.fmax),
    maxSteps: Math.round(clamp(Number(r.maxSteps), 1, 5000, d.maxSteps)),
    relaxCell: typeof r.relaxCell === "boolean" ? r.relaxCell : d.relaxCell,
    fixSymmetry: typeof r.fixSymmetry === "boolean" ? r.fixSymmetry : d.fixSymmetry,
    pressureGpa: clamp(Number(r.pressureGpa), -50, 500, d.pressureGpa),
    stressTolGpa: clamp(Number(r.stressTolGpa), 1e-3, 10, d.stressTolGpa),
    optimizer: OPTIMIZERS.includes(r.optimizer as Optimizer) ? (r.optimizer as Optimizer) : d.optimizer,
  };
}

export function normalizePhonon(p: Partial<PhononSettings>): PhononSettings {
  const sc = Array.isArray(p.supercell) && p.supercell.length === 3 && p.supercell.every((n) => Number.isFinite(n) && n >= 1)
    ? (p.supercell.map((n) => Math.min(8, Math.round(n))) as [number, number, number])
    : null;
  return {
    supercell: sc,
    displacement: clamp(Number(p.displacement), 1e-3, 0.1, DEFAULT_PHONON.displacement),
    autoDos: typeof p.autoDos === "boolean" ? p.autoDos : DEFAULT_PHONON.autoDos,
    cellMode: p.cellMode === "as_displayed" || p.cellMode === "primitive" ? p.cellMode : DEFAULT_PHONON.cellMode,
    minLength: clamp(Number(p.minLength), 4, 30, DEFAULT_PHONON.minLength!),
    colorBranches: typeof p.colorBranches === "boolean" ? p.colorBranches : DEFAULT_PHONON.colorBranches,
  };
}

export function normalizeElastic(e: Partial<ElasticSettings>): ElasticSettings {
  const d = DEFAULT_ELASTIC;
  return {
    maxStrain: clamp(Number(e.maxStrain), 0.001, 0.05, d.maxStrain),
    nStrains: Math.round(clamp(Number(e.nStrains), 2, 8, d.nStrains)),
    relaxIons: typeof e.relaxIons === "boolean" ? e.relaxIons : d.relaxIons,
    fmax: clamp(Number(e.fmax), 1e-4, 0.1, d.fmax),
  };
}

const SHAPES: readonly ProfileShape[] = ["pseudo-voigt", "gaussian", "sticks"];

export function normalizeDiffraction(x: Partial<DiffractionSettings>): DiffractionSettings {
  const d = DEFAULT_DIFFRACTION;
  const lo = clamp(Number(x.twoThetaMin), 0, 170, d.twoThetaMin);
  const hi = clamp(Number(x.twoThetaMax), 1, 180, d.twoThetaMax);
  return {
    kind: x.kind === "neutron" || x.kind === "xray" ? x.kind : d.kind,
    radiation: x.radiation === "custom" || RADIATIONS.some((r) => r.key === x.radiation) ? (x.radiation as string) : d.radiation,
    wavelength: clamp(Number(x.wavelength), 0.1, 5, d.wavelength),
    twoThetaMin: Math.min(lo, hi - 1),
    twoThetaMax: Math.max(hi, lo + 1),
    shape: SHAPES.includes(x.shape as ProfileShape) ? (x.shape as ProfileShape) : d.shape,
    fwhm: clamp(Number(x.fwhm), 0.01, 3, d.fwhm),
    eta: clamp(Number(x.eta), 0, 1, d.eta),
  };
}

/** The wavelength actually used (Å). */
export function diffractionWavelength(d: Pick<DiffractionSettings, "radiation" | "wavelength">): number {
  return d.radiation === "custom" ? d.wavelength : (RADIATIONS.find((r) => r.key === d.radiation)?.wavelength ?? d.wavelength);
}

/** POST /api/diffraction body without the crystal. Neutrons and custom λ send an explicit wavelength. */
export function diffractionRequest(d: DiffractionSettings): Omit<DiffractionRequest, "crystal"> {
  const custom = d.radiation === "custom";
  return {
    kind: d.kind,
    ...(custom ? {} : { radiation: d.radiation }),
    wavelength_angstrom: custom || d.kind === "neutron" ? diffractionWavelength(d) : null,
    two_theta_min: d.twoThetaMin,
    two_theta_max: d.twoThetaMax,
  };
}

export function elasticPayload(e: ElasticSettings): ComputeElasticTensorPayload {
  return { max_strain: e.maxStrain, n_strains: e.nStrains, relax_ions: e.relaxIons, fmax: e.fmax };
}

/** START_RELAXATION payload from the settings. */
export function relaxPayload(r: RelaxSettings): StartRelaxationPayload {
  return {
    fmax: r.fmax,
    max_steps: r.maxSteps,
    relax_cell: r.relaxCell,
    fix_symmetry: r.fixSymmetry,
    pressure_gpa: r.pressureGpa,
    stress_tol_gpa: r.relaxCell ? r.stressTolGpa : null,
    optimizer: r.optimizer,
  };
}

/** Criteria of the RELAXED badge / phonon warning derived from the settings. */
export function badgeCriteria(r: RelaxSettings): RelaxationCriteria {
  return {
    fmax: r.fmax,
    stressTolGpa: Math.max(RELAXED_STRESS_TOL_GPA, r.stressTolGpa),
    targetPressureGpa: r.pressureGpa,
  };
}

/**
 * RUN_PHONON_SHAKE_TEST payload: the manual supercell, else the automatic one.
 * Automatic + minLength sends `min_supercell_length` (the server sizes the cell
 * it actually diagonalises, e.g. the primitive one) plus a client estimate for
 * the displayed cell as `supercell_dim` (older servers).
 */
export function phononPayload(p: PhononSettings, lengths: [number, number, number], nAtoms: number): RunPhononShakeTestPayload {
  const out: RunPhononShakeTestPayload = {
    supercell_dim: p.supercell ?? suggestSupercell(lengths, nAtoms, p.minLength ?? SUPERCELL_MIN_LENGTH),
    displacement_distance: p.displacement,
  };
  if (p.cellMode) out.cell_mode = p.cellMode;
  if (!p.supercell && p.minLength !== undefined) out.min_supercell_length = p.minLength;
  return out;
}

export interface SettingsOptions {
  storageKey?: string | null;
}

interface Persisted {
  relax: RelaxSettings;
  phonon: PhononSettings;
  elastic: ElasticSettings;
  diffraction: DiffractionSettings;
  units: UnitPrefs;
  collapsed: Record<string, boolean>;
}

export function createSettingsStore(opts: SettingsOptions = {}) {
  const key = opts.storageKey ?? null;
  const stored: Partial<Persisted> = key ? loadJSON<Partial<Persisted>>(key, {}) : {};
  return create<SettingsState>()((set, get) => {
    const save = () => {
      if (!key) return;
      const { relax, phonon, elastic, diffraction, units, collapsed } = get();
      saveJSON(key, { relax, phonon, elastic, diffraction, units, collapsed } satisfies Persisted);
    };
    return {
      relax: normalizeRelax(stored.relax ?? {}),
      phonon: normalizePhonon(stored.phonon ?? {}),
      elastic: normalizeElastic(stored.elastic ?? {}),
      diffraction: normalizeDiffraction(stored.diffraction ?? {}),
      units: normalizeUnits(stored.units ?? DEFAULT_UNITS),
      collapsed: stored.collapsed && typeof stored.collapsed === "object" ? { ...stored.collapsed } : {},
      setRelax: (p) => {
        set((s) => ({ relax: normalizeRelax({ ...s.relax, ...p }) }));
        save();
      },
      setPhonon: (p) => {
        set((s) => ({ phonon: normalizePhonon({ ...s.phonon, ...p }) }));
        save();
      },
      setElastic: (p) => {
        set((s) => ({ elastic: normalizeElastic({ ...s.elastic, ...p }) }));
        save();
      },
      setDiffraction: (p) => {
        set((s) => ({ diffraction: normalizeDiffraction({ ...s.diffraction, ...p }) }));
        save();
      },
      setUnits: (p) => {
        set((s) => ({ units: normalizeUnits({ ...s.units, ...p }) }));
        save();
      },
      setCollapsed: (id, collapsed) => {
        set((s) => ({ collapsed: { ...s.collapsed, [id]: collapsed } }));
        save();
      },
      resetRelax: () => {
        set({ relax: { ...DEFAULT_RELAX } });
        save();
      },
      resetPhonon: () => {
        set({ phonon: { ...DEFAULT_PHONON } });
        save();
      },
    };
  });
}

export type SettingsStore = ReturnType<typeof createSettingsStore>;

export const useSettings: SettingsStore = createSettingsStore({ storageKey: "latticeflow.settings.v1" });
