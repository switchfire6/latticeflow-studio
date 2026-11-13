/**
 * Pure helpers for the phonon dispersion chart (unit-tested): k-path labels,
 * path discontinuities, mode classification, nearest-mode picking and axis ticks.
 */
import { F_TOL_THZ, type KPathLabel } from "../types/protocol";

const GREEK: Record<string, string> = {
  GAMMA: "Γ",
  Gamma: "Γ",
  SIGMA: "Σ",
  Sigma: "Σ",
  DELTA: "Δ",
  Delta: "Δ",
  LAMBDA: "Λ",
  Lambda: "Λ",
};

/** "\\Gamma" → "Γ", "X|K" kept as "X|K", "S_0" → "S₀". */
export function formatKLabel(label: string | null | undefined): string {
  if (!label) return "";
  return label
    .split("|")
    .map((part) =>
      part
        .replace(/\\?(GAMMA|Gamma|SIGMA|Sigma|DELTA|Delta|LAMBDA|Lambda)/g, (_, g: string) => GREEK[g] ?? g)
        .replace(/_(\d)/g, (_, d: string) => String.fromCharCode(0x2080 + Number(d))),
    )
    .join("|");
}

/** breakAfter[i] is true when q_i → q_{i+1} is a path discontinuity (zero-length jump). */
export function segmentBreaks(distances: readonly number[], eps = 1e-9): boolean[] {
  const out: boolean[] = new Array(Math.max(0, distances.length - 1)).fill(false);
  for (let i = 0; i + 1 < distances.length; i++) out[i] = distances[i + 1]! - distances[i]! <= eps;
  return out;
}

export type ModeClass = "imaginary" | "zero" | "real";

/** imaginary: f < −F_TOL; zero: |f| ≤ F_TOL (acoustic modes at Γ, numerical noise); real otherwise. */
export function classifyFrequency(f: number, tol = F_TOL_THZ): ModeClass {
  if (f < -tol) return "imaginary";
  if (Math.abs(f) <= tol) return "zero";
  return "real";
}

/** A dispersion segment is drawn crimson when either end is imaginary. */
export function isImaginarySegment(fa: number, fb: number, tol = F_TOL_THZ): boolean {
  return Math.min(fa, fb) < -tol;
}

export interface BandSummary {
  nq: number;
  nBands: number;
  /** bands that are imaginary somewhere on the path */
  imaginaryBands: number[];
  /** number of imaginary modes per q-point */
  imaginaryPerQ: number[];
  minFrequency: number;
  maxFrequency: number;
}

export function classifyBands(freqs: readonly (readonly number[])[], tol = F_TOL_THZ): BandSummary {
  const nq = freqs.length;
  const nBands = nq ? freqs[0]!.length : 0;
  const imag = new Set<number>();
  const perQ: number[] = [];
  let lo = Infinity;
  let hi = -Infinity;
  for (let q = 0; q < nq; q++) {
    let c = 0;
    const row = freqs[q]!;
    for (let b = 0; b < row.length; b++) {
      const f = row[b]!;
      if (f < -tol) {
        c++;
        imag.add(b);
      }
      if (f < lo) lo = f;
      if (f > hi) hi = f;
    }
    perQ.push(c);
  }
  return {
    nq,
    nBands,
    imaginaryBands: [...imag].sort((a, b) => a - b),
    imaginaryPerQ: perQ,
    minFrequency: nq ? lo : 0,
    maxFrequency: nq ? hi : 0,
  };
}

/**
 * Nearest (q_index, band) to a click at path distance `x` and frequency `y`.
 * The q-point is the closest in x (all q-points sharing that distance, i.e.
 * both sides of a discontinuity, are candidates); the band is the one whose
 * frequency there is closest to y.
 */
export function nearestMode(
  distances: readonly number[],
  freqs: readonly (readonly number[])[],
  x: number,
  y: number,
): { qIndex: number; band: number } | null {
  const n = distances.length;
  if (n === 0 || freqs.length !== n) return null;
  // binary search for the first distance >= x
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (distances[mid]! < x) lo = mid + 1;
    else hi = mid;
  }
  let best = lo;
  if (lo > 0 && Math.abs(distances[lo - 1]! - x) <= Math.abs(distances[lo]! - x)) best = lo - 1;
  const d = distances[best]!;
  const candidates: number[] = [];
  for (let i = best; i >= 0 && Math.abs(distances[i]! - d) <= 1e-9; i--) candidates.push(i);
  for (let i = best + 1; i < n && Math.abs(distances[i]! - d) <= 1e-9; i++) candidates.push(i);
  let out = { qIndex: best, band: 0 };
  let bestErr = Infinity;
  for (const q of candidates) {
    const row = freqs[q]!;
    for (let b = 0; b < row.length; b++) {
      const err = Math.abs(row[b]! - y);
      if (err < bestErr) {
        bestErr = err;
        out = { qIndex: q, band: b };
      }
    }
  }
  return out;
}

/** "Nice" axis ticks covering [lo, hi]. */
export function niceTicks(lo: number, hi: number, target = 5): number[] {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / Math.max(1, target);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return out;
}

/** y-range of the chart: always includes 0 and a little headroom. */
export function frequencyRange(summary: Pick<BandSummary, "minFrequency" | "maxFrequency">): [number, number] {
  const lo = Math.min(0, summary.minFrequency);
  const hi = Math.max(1, summary.maxFrequency);
  const pad = 0.06 * (hi - lo);
  return [lo < 0 ? lo - pad : 0, hi + pad];
}

/** Tick positions (path distance) and formatted labels. */
export function pathTicks(labels: readonly KPathLabel[], distances: readonly number[]): { x: number; label: string }[] {
  return labels
    .filter((l) => l.index >= 0 && l.index < distances.length)
    .map((l) => ({ x: distances[l.index]!, label: formatKLabel(l.label) }));
}

/** Label of a q-point if it is a labelled point, else null. */
export function qLabelAt(labels: readonly KPathLabel[], qIndex: number): string | null {
  const l = labels.find((x) => x.index === qIndex);
  return l ? formatKLabel(l.label) : null;
}
