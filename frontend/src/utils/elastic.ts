/**
 * Elastic-tensor interpretation (IMPROVEMENT_SUGGESTIONS.md §5.5), pure and
 * unit-tested. The server reports the Born flag; the helpers here derive the
 * presentation (Pugh's G/K with the ductile / brittle hint, reference-pressure
 * warning) and a client-side Born check from the eigenvalues.
 */
import type { ElasticTensorResult } from "../types/protocol";

/** Pugh's criterion: G/K < 0.571 (K/G > 1.75) is ductile. */
export const PUGH_DUCTILE_GK = 0.571;
/** Above this |P| (GPa) the constants are referred to a stressed state. */
export const ELASTIC_PRESSURE_WARN_GPA = 0.5;

/** Born stability for an arbitrary lattice: the 6×6 C (Voigt) is positive definite ⇔ every eigenvalue > 0. */
export function bornStableFromEigenvalues(eigs: readonly number[], tol = 0): boolean {
  return eigs.length === 6 && eigs.every((e) => Number.isFinite(e) && e > tol);
}

export type Ductility = "ductile" | "brittle";

export interface ElasticSummary {
  /** G_VRH / K_VRH (null when K ≤ 0). */
  gOverK: number | null;
  kOverG: number | null;
  ductility: Ductility | null;
  minEigenvalue: number | null;
  /** Warning text when the reference state is stressed (|P| > 0.5 GPa). */
  pressureWarning: string | null;
  /** The server flag agrees with the eigenvalues. */
  bornConsistent: boolean;
}

export function elasticSummary(r: ElasticTensorResult): ElasticSummary {
  const k = r.k_vrh_gpa;
  const g = r.g_vrh_gpa;
  const gOverK = k > 0 && Number.isFinite(g) ? g / k : null;
  const kOverG = g > 0 && Number.isFinite(k) ? k / g : null;
  const minEig = r.eigenvalues_gpa.length ? Math.min(...r.eigenvalues_gpa) : null;
  const p = r.reference_pressure_gpa;
  const pressureWarning =
    Number.isFinite(p) && Math.abs(p) > ELASTIC_PRESSURE_WARN_GPA
      ? `The reference cell is under ${p > 0 ? "+" : "−"}${Math.abs(p).toFixed(2)} GPa: the constants describe a stressed state (relax the cell first for zero-pressure values).`
      : null;
  return {
    gOverK,
    kOverG,
    ductility: gOverK === null ? null : gOverK < PUGH_DUCTILE_GK ? "ductile" : "brittle",
    minEigenvalue: minEig,
    pressureWarning,
    bornConsistent: r.eigenvalues_gpa.length !== 6 || bornStableFromEigenvalues(r.eigenvalues_gpa) === r.born_stable,
  };
}
