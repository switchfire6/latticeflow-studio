/**
 * Phase C1 UI state (not part of the server session): which modal dialog is
 * open, the bulk history entry chosen for formation energies, and the
 * Arrhenius scan (sequential MD runs) progress.
 */
import { create } from "zustand";
import type { ArrheniusPoint, HistoryRef } from "../utils/analysisC";

export type DialogKind = "supercell" | "add-atom" | "substitution" | "formation" | "committee" | "ordering";

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
  openDialog(d: DialogKind): void;
  closeDialog(): void;
  setBulkRef(r: HistoryRef | null): void;
  setArrhenius(a: ArrheniusScan | null): void;
}

export const createUiStore = () =>
  create<UiState>()((set) => ({
    dialog: null,
    bulkRef: null,
    arrhenius: null,
    openDialog: (dialog) => set({ dialog }),
    closeDialog: () => set({ dialog: null }),
    setBulkRef: (bulkRef) => set({ bulkRef }),
    setArrhenius: (arrhenius) => set({ arrhenius }),
  }));

export const useUi = createUiStore();

/** Arrhenius points of one element from a scan. */
export function scanPoints(scan: ArrheniusScan | null, element: string): ArrheniusPoint[] {
  return (scan?.results ?? []).map((r) => ({ temperatureK: r.temperatureK, d: r.d[element] ?? null }));
}
