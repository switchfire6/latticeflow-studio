/**
 * Pure data shaping for the EOS and phonon-DOS / thermodynamics panes (unit-tested).
 */
import type { BulkModulusResult, PhononDosData } from "../types/protocol";
import { frequencyFactor, type FrequencyUnit } from "./units";

/**
 * Categorical series colours (element-projected DOS), in fixed order. The
 * reference dark-mode categorical steps, validated against the HUD surface
 * #0b0f19 (lightness band, chroma, CVD ΔE ≥ 8 adjacent, contrast ≥ 3:1).
 */
export const SERIES_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"] as const;
export const MAX_PROJECTED_SERIES = SERIES_COLORS.length;

/** [min, max] of finite values, padded by `pad`·range (and widened if degenerate). */
export function extent(values: Iterable<number>, pad = 0.05, include?: number): [number, number] {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (include !== undefined) {
    lo = Math.min(lo, include);
    hi = Math.max(hi, include);
  }
  if (!Number.isFinite(lo)) return [0, 1];
  if (hi - lo < 1e-12) {
    const w = Math.abs(lo) * 0.05 || 1;
    return [lo - w, hi + w];
  }
  const p = (hi - lo) * pad;
  return [lo - p, hi + p];
}

/** Linear interpolation of y(x) on ascending xs (clamped at the ends). */
export function interpAt(xs: readonly number[], ys: readonly number[], x: number): number | null {
  const n = Math.min(xs.length, ys.length);
  if (n === 0) return null;
  if (x <= xs[0]!) return ys[0]!;
  if (x >= xs[n - 1]!) return ys[n - 1]!;
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid]! <= x) lo = mid;
    else hi = mid;
  }
  const t = (x - xs[lo]!) / (xs[hi]! - xs[lo]! || 1);
  return ys[lo]! + t * (ys[hi]! - ys[lo]!);
}

export interface EosPoint {
  volume: number; // Å³ per cell
  dEmeVPerAtom: number; // (E − E_min)/N in meV/atom
  pressureGpa: number;
}

/** E(V), P(V) points sorted by volume, energies relative to the minimum, per atom. */
export function eosPoints(r: Pick<BulkModulusResult, "volumes" | "energies_ev" | "pressures_gpa">, nAtoms: number): EosPoint[] {
  const n = Math.min(r.volumes.length, r.energies_ev.length);
  if (n === 0) return [];
  let eMin = Infinity;
  for (let i = 0; i < n; i++) eMin = Math.min(eMin, r.energies_ev[i]!);
  const perAtom = 1000 / Math.max(1, nAtoms);
  const pts: EosPoint[] = [];
  for (let i = 0; i < n; i++) {
    pts.push({ volume: r.volumes[i]!, dEmeVPerAtom: (r.energies_ev[i]! - eMin) * perAtom, pressureGpa: r.pressures_gpa[i] ?? NaN });
  }
  return pts.sort((a, b) => a.volume - b.volume);
}

export interface DosSeries {
  /** Frequencies in the display unit. */
  frequencies: number[];
  /** states / unit / cell. */
  total: number[];
  projected: { element: string; values: number[]; color: string }[];
  unit: FrequencyUnit;
}

/** Convert a DOS to a display unit (frequencies × k, densities ÷ k, so the integral is preserved). */
export function dosInUnit(d: Pick<PhononDosData, "frequencies_thz" | "total_dos" | "projected_dos">, unit: FrequencyUnit, elementOrder?: readonly string[]): DosSeries {
  const k = frequencyFactor(unit);
  const keys = Object.keys(d.projected_dos);
  const order = elementOrder ? [...elementOrder.filter((e) => keys.includes(e)), ...keys.filter((e) => !elementOrder.includes(e))] : keys;
  return {
    frequencies: d.frequencies_thz.map((f) => f * k),
    total: d.total_dos.map((v) => v / k),
    projected: order.slice(0, MAX_PROJECTED_SERIES).map((element, i) => ({
      element,
      values: (d.projected_dos[element] ?? []).map((v) => v / k),
      color: SERIES_COLORS[i]!,
    })),
    unit,
  };
}

/** Trapezoidal integral of y over x. */
export function integrate(xs: readonly number[], ys: readonly number[]): number {
  let s = 0;
  for (let i = 0; i + 1 < Math.min(xs.length, ys.length); i++) s += 0.5 * (ys[i]! + ys[i + 1]!) * (xs[i + 1]! - xs[i]!);
  return s;
}
