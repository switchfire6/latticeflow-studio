/**
 * Phase C1 UI state (not part of the server session): which modal dialog is
 * open, the bulk history entry chosen for formation energies, and the
 * Arrhenius scan (sequential MD runs) progress.
 */
import { create } from "zustand";
import type { Snapshot } from "../services/snapshotCache";
import type { ArrheniusPoint, HistoryRef } from "../utils/analysis";
import type { Vec3 } from "../utils/latticeMath";

export type DialogKind = "supercell" | "add-atom" | "substitution" | "formation" | "committee" | "ordering" | "compare" | "prototype";

/** Phase C2: the Add-atom dialog's fields (kept while the dialog is hidden for viewport picking). */
export interface AddAtomDraft {
  element: string;
  frac: [string, string, string];
}
/** Phase C2: click-to-place mode: a plane s_axis = depth through the cell; `hover` is the ghost atom. */
export interface Placement {
  axis: 0 | 1 | 2;
  depth: number;
  hover: Vec3 | null;
}
/** Phase C2: the two structures of the compare panel. */
export interface CompareSelection {
  a: Snapshot | null;
  b: Snapshot | null;
}

export interface ArrheniusScan {
  temperatures: number[];
  /** Index of the run in progress (= temperatures.length when done). */
  index: number;
  startPosition: number;
  /** One entry per finished temperature: D per element (cm²/s). */
  results: { temperatureK: number; d: Record<string, number | null> }[];
  status: "running" | "done" | "aborted";
  message: string | null;
}

export interface UiState {
  dialog: DialogKind | null;
  /** Bulk reference for COMPUTE_FORMATION_ENERGY. */
  bulkRef: HistoryRef | null;
  arrhenius: ArrheniusScan | null;
  addAtomDraft: AddAtomDraft;
  placement: Placement | null;
  compare: CompareSelection;
  openDialog(d: DialogKind): void;
  closeDialog(): void;
  setBulkRef(r: HistoryRef | null): void;
  setArrhenius(a: ArrheniusScan | null): void;
  setAddAtomDraft(p: Partial<AddAtomDraft>): void;
  setPlacement(p: Placement | null): void;
  /** Open the compare panel (optionally preset with two structures). */
  openCompare(a?: Snapshot | null, b?: Snapshot | null): void;
  setCompare(p: Partial<CompareSelection>): void;
}

export const createUiStore = () =>
  create<UiState>()((set) => ({
    dialog: null,
    bulkRef: null,
    arrhenius: null,
    addAtomDraft: { element: "Li", frac: ["0.5", "0.5", "0.5"] },
    placement: null,
    compare: { a: null, b: null },
    openDialog: (dialog) => set({ dialog }),
    closeDialog: () => set({ dialog: null }),
    setBulkRef: (bulkRef) => set({ bulkRef }),
    setArrhenius: (arrhenius) => set({ arrhenius }),
    setAddAtomDraft: (p) => set((s) => ({ addAtomDraft: { ...s.addAtomDraft, ...p } })),
    setPlacement: (placement) => set({ placement }),
    openCompare: (a, b) => set((s) => ({ dialog: "compare", compare: { a: a === undefined ? s.compare.a : a, b: b === undefined ? s.compare.b : b } })),
    setCompare: (p) => set((s) => ({ compare: { ...s.compare, ...p } })),
  }));

export const useUi = createUiStore();

/** Arrhenius points of one element from a scan. */
export function scanPoints(scan: ArrheniusScan | null, element: string): ArrheniusPoint[] {
  return (scan?.results ?? []).map((r) => ({ temperatureK: r.temperatureK, d: r.d[element] ?? null }));
}
