/**
 * Supercell / defect-cell geometry (IMPROVEMENT_SUGGESTIONS.md §5.9), pure and
 * unit-tested: L' = M L, the resulting atom count, the shortest periodic
 * (defect–image) distance and a suggested diagonal supercell.
 */

import { DEFAULT_MAX_ATOMS } from "./limits";

/** Default studio atom limit; the live one comes from /api/health (utils/limits.ts). */
export const MAX_STUDIO_ATOMS = DEFAULT_MAX_ATOMS;
/** Below this shortest periodic distance a point defect interacts with its images. */
export const DEFECT_MIN_DISTANCE = 10;

export type IntMatrix = number[][];

export const SUPERCELL_PRESETS: { id: string; label: string; matrix: IntMatrix; title: string }[] = [
  { id: "2x2x2", label: "2×2×2", matrix: [[2, 0, 0], [0, 2, 0], [0, 0, 2]], title: "Doubled along every axis (8× atoms)" },
  {
    id: "r2r2x2",
    label: "√2×√2×2",
    matrix: [[1, 1, 0], [-1, 1, 0], [0, 0, 2]],
    title: "a' = a + b, b' = b − a, c' = 2c (4× atoms): the cell of a⁰a⁰c⁻ tilted perovskites",
  },
];

export function diagonalMatrix(n: readonly number[]): IntMatrix {
  return [
    [n[0] ?? 1, 0, 0],
    [0, n[1] ?? 1, 0],
    [0, 0, n[2] ?? 1],
  ];
}

export function det3m(m: readonly (readonly number[])[]): number {
  const [a, b, c] = m as number[][];
  return a![0]! * (b![1]! * c![2]! - b![2]! * c![1]!) - a![1]! * (b![0]! * c![2]! - b![2]! * c![0]!) + a![2]! * (b![0]! * c![1]! - b![1]! * c![0]!);
}

/** L' = M L (rows are lattice vectors). */
export function supercellLattice(m: readonly (readonly number[])[], L: readonly (readonly number[])[]): number[][] {
  return [0, 1, 2].map((i) => [0, 1, 2].map((j) => m[i]![0]! * L[0]![j]! + m[i]![1]! * L[1]![j]! + m[i]![2]! * L[2]![j]!));
}

/** Shortest non-zero lattice vector |n·L| (search n ∈ [−3, 3]³; exact for reasonably reduced cells). */
export function shortestPeriodicDistance(L: readonly (readonly number[])[]): number {
  let best = Infinity;
  for (let a = -3; a <= 3; a++)
    for (let b = -3; b <= 3; b++)
      for (let c = -3; c <= 3; c++) {
        if (!a && !b && !c) continue;
        const x = a * L[0]![0]! + b * L[1]![0]! + c * L[2]![0]!;
        const y = a * L[0]![1]! + b * L[1]![1]! + c * L[2]![1]!;
        const z = a * L[0]![2]! + b * L[1]![2]! + c * L[2]![2]!;
        best = Math.min(best, Math.hypot(x, y, z));
      }
  return best;
}

export interface SupercellPreview {
  det: number;
  nAtoms: number;
  minDistance: number;
  valid: boolean;
  tooLarge: boolean;
  /** Shortest periodic distance under DEFECT_MIN_DISTANCE. */
  small: boolean;
  error: string | null;
}

export function previewSupercell(m: IntMatrix, L: readonly (readonly number[])[], nAtoms: number, maxAtoms = MAX_STUDIO_ATOMS): SupercellPreview {
  const ints = m.every((r) => r.length === 3 && r.every((v) => Number.isInteger(v)));
  const det = ints ? Math.round(det3m(m)) : 0;
  const nOut = nAtoms * Math.abs(det);
  const minDistance = det > 0 ? shortestPeriodicDistance(supercellLattice(m, L)) : 0;
  const error = !ints ? "Integer entries only." : det <= 0 ? "det(M) must be positive." : nOut > maxAtoms ? `${nOut} atoms exceeds the ${maxAtoms}-atom studio limit.` : null;
  return { det, nAtoms: nOut, minDistance, valid: error === null, tooLarge: nOut > maxAtoms, small: minDistance < DEFECT_MIN_DISTANCE, error };
}

/** Smallest diagonal n with every n_i·|a_i| ≥ minLength that fits maxAtoms (shrinking the longest axis first). */
export function suggestDiagonal(L: readonly (readonly number[])[], nAtoms: number, minLength = DEFECT_MIN_DISTANCE, maxAtoms = MAX_STUDIO_ATOMS): [number, number, number] {
  const len = [0, 1, 2].map((i) => Math.hypot(...(L[i] as number[])));
  const n = len.map((l) => (l > 0 ? Math.max(1, Math.ceil(minLength / l - 1e-9)) : 1)) as [number, number, number];
  while (n[0] * n[1] * n[2] * nAtoms > maxAtoms) {
    let best = -1;
    for (let i = 0; i < 3; i++) if (n[i]! > 1 && (best < 0 || n[i]! * len[i]! > n[best]! * len[best]!)) best = i;
    if (best < 0) break;
    n[best]!--;
  }
  return n;
}

/** Python's round() (banker's rounding), as the server uses for round(fraction · n). */
export function pyRound(x: number): number {
  const f = Math.floor(x);
  const d = x - f;
  if (Math.abs(d - 0.5) < 1e-9) return f % 2 === 0 ? f : f + 1;
  return Math.round(x);
}

/** Atoms SUBSTITUTE_FRACTION will replace. */
export function substitutionCount(fraction: number, nFrom: number): number {
  return pyRound(fraction * nFrom);
}
