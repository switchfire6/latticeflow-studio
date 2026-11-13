/**
 * A tiny hook for REST calculations (hull, diffraction) to announce a finished
 * interactive result without importing the library code (which auto-saves it).
 */
import type { CrystalStatePayload } from "../types/protocol";
import type { ResultKind } from "../types/library";

export interface ResultEvent {
  kind: ResultKind;
  data: object;
  settings: Record<string, unknown>;
  /** The structure the result belongs to and its state version at request time. */
  crystal: CrystalStatePayload;
  stateVersion: number;
  modelId: string | null;
  dispersion: boolean;
}

const listeners = new Set<(e: ResultEvent) => void>();

export function onResult(fn: (e: ResultEvent) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function emitResult(e: ResultEvent): void {
  for (const fn of listeners) {
    try {
      fn(e);
    } catch (err) {
      console.warn("[results] listener failed", err);
    }
  }
}
