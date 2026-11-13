/**
 * Studio size limits (Phase D): the backend reports them in /api/health
 * (LATTICEFLOW_MAX_ATOMS / LATTICEFLOW_PHONON_MAX_ATOMS); before the first
 * health answer, or from an older backend, the historical defaults apply.
 */
import type { HealthResponse } from "../types/protocol";

/** Default studio atom limit (backend structure_io.MAX_ATOMS). */
export const DEFAULT_MAX_ATOMS = 128;
/** Default shake-test atom limit (per cell). */
export const DEFAULT_PHONON_MAX_ATOMS = 64;

export interface StudioLimits {
  maxAtoms: number;
  phononMaxAtoms: number;
}

const valid = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v) && v > 0;

export function studioLimits(health: Pick<HealthResponse, "max_atoms" | "phonon_max_atoms"> | null | undefined): StudioLimits {
  return {
    maxAtoms: valid(health?.max_atoms) ? health!.max_atoms! : DEFAULT_MAX_ATOMS,
    phononMaxAtoms: valid(health?.phonon_max_atoms) ? health!.phonon_max_atoms! : DEFAULT_PHONON_MAX_ATOMS,
  };
}
