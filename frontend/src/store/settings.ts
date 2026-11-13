/**
 * User settings (§3.4, §3.6): relaxation and phonon parameters, display units
 * and a few UI preferences. Kept in localStorage (utils/persist.ts, never
 * throws). Pure builders turn them into protocol payloads (unit-tested).
 */
import { create } from "zustand";
import type { RunPhononShakeTestPayload, StartRelaxationPayload } from "../types/protocol";
import { loadJSON, saveJSON } from "../utils/persist";
import { RELAXED_STRESS_TOL_GPA, suggestSupercell, type RelaxationCriteria } from "../utils/structureInfo";
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

export interface PhononSettings {
  /** null = automatic (suggestSupercell). */
  supercell: [number, number, number] | null;
  displacement: number; // Å
  /** Request the phonon DOS + thermodynamics right after each shake-test. */
  autoDos: boolean;
}

export const DEFAULT_PHONON: PhononSettings = { supercell: null, displacement: 0.01, autoDos: true };

export interface SettingsState {
  relax: RelaxSettings;
  phonon: PhononSettings;
  units: UnitPrefs;
  /** Collapsed sidebar cards by id. */
  collapsed: Record<string, boolean>;
  setRelax(p: Partial<RelaxSettings>): void;
  setPhonon(p: Partial<PhononSettings>): void;
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
  };
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

/** RUN_PHONON_SHAKE_TEST payload: the manual supercell, else the automatic suggestion. */
export function phononPayload(p: PhononSettings, lengths: [number, number, number], nAtoms: number): RunPhononShakeTestPayload {
  return {
    supercell_dim: p.supercell ?? suggestSupercell(lengths, nAtoms),
    displacement_distance: p.displacement,
  };
}

export interface SettingsOptions {
  storageKey?: string | null;
}

interface Persisted {
  relax: RelaxSettings;
  phonon: PhononSettings;
  units: UnitPrefs;
  collapsed: Record<string, boolean>;
}

export function createSettingsStore(opts: SettingsOptions = {}) {
  const key = opts.storageKey ?? null;
  const stored: Partial<Persisted> = key ? loadJSON<Partial<Persisted>>(key, {}) : {};
  return create<SettingsState>()((set, get) => {
    const save = () => {
      if (!key) return;
      const { relax, phonon, units, collapsed } = get();
      saveJSON(key, { relax, phonon, units, collapsed } satisfies Persisted);
    };
    return {
      relax: normalizeRelax(stored.relax ?? {}),
      phonon: normalizePhonon(stored.phonon ?? {}),
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
