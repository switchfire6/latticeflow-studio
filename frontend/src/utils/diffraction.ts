/**
 * Simulated diffraction helpers (IMPROVEMENT_SUGGESTIONS.md §5.6), all pure and
 * unit-tested: radiation table, peak broadening (Gaussian / pseudo-Voigt),
 * experimental pattern parsing (.xy / .xye / .csv), normalisation, the 2θ
 * shift / scale transform and hover picking.
 */
import type { DiffractionPeak } from "../types/protocol";

export interface Radiation {
  key: string; // pymatgen XRDCalculator key
  label: string;
  wavelength: number; // Å (Kα weighted mean)
}

/** Common Kα sources (pymatgen WAVELENGTHS, Kα weighted average). */
export const RADIATIONS: readonly Radiation[] = [
  { key: "CuKa", label: "Cu Kα", wavelength: 1.54184 },
  { key: "MoKa", label: "Mo Kα", wavelength: 0.71073 },
  { key: "CoKa", label: "Co Kα", wavelength: 1.79026 },
  { key: "CrKa", label: "Cr Kα", wavelength: 2.29100 },
  { key: "FeKa", label: "Fe Kα", wavelength: 1.93735 },
  { key: "AgKa", label: "Ag Kα", wavelength: 0.560885 },
];

export type ProfileShape = "pseudo-voigt" | "gaussian" | "sticks";

/** Normalised Gaussian / Lorentzian line shapes with full width at half maximum `fwhm`, peak value 1. */
export function gaussian(dx: number, fwhm: number): number {
  const s = fwhm / (2 * Math.sqrt(2 * Math.LN2));
  return Math.exp(-(dx * dx) / (2 * s * s));
}
export function lorentzian(dx: number, fwhm: number): number {
  const g = fwhm / 2;
  return (g * g) / (dx * dx + g * g);
}
/** Pseudo-Voigt (peak height 1): η·L + (1 − η)·G with the same FWHM. */
export function pseudoVoigt(dx: number, fwhm: number, eta = 0.5): number {
  return eta * lorentzian(dx, fwhm) + (1 - eta) * gaussian(dx, fwhm);
}

export interface Profile {
  x: number[];
  y: number[];
}

/**
 * Broadened pattern on a uniform 2θ grid: Σ I_k · shape(2θ − 2θ_k). Peak heights
 * are preserved for isolated peaks (shape(0) = 1); the result is not renormalised.
 * Contributions beyond 12 FWHM are skipped (negligible for the Gaussian, < 0.2 % for the Lorentzian tail).
 */
export function broadenPattern(
  peaks: readonly Pick<DiffractionPeak, "two_theta" | "intensity">[],
  range: [number, number],
  fwhm: number,
  shape: Exclude<ProfileShape, "sticks"> = "pseudo-voigt",
  eta = 0.5,
  step?: number,
): Profile {
  const [lo, hi] = range;
  const w = Math.max(1e-3, fwhm);
  const dx = step ?? Math.min(0.02, w / 6);
  const n = Math.max(2, Math.floor((hi - lo) / dx) + 1);
  const x = new Array<number>(n);
  const y = new Array<number>(n).fill(0);
  for (let i = 0; i < n; i++) x[i] = lo + i * dx;
  const cut = (shape === "gaussian" ? 4 : 12) * w;
  for (const p of peaks) {
    const i0 = Math.max(0, Math.floor((p.two_theta - cut - lo) / dx));
    const i1 = Math.min(n - 1, Math.ceil((p.two_theta + cut - lo) / dx));
    for (let i = i0; i <= i1; i++) {
      const d = x[i]! - p.two_theta;
      y[i]! += p.intensity * (shape === "gaussian" ? gaussian(d, w) : pseudoVoigt(d, w, eta));
    }
  }
  return { x, y };
}

export interface ExperimentalPattern {
  name: string;
  twoTheta: number[];
  intensity: number[];
  /** σ column of .xye files (if present). */
  sigma: number[] | null;
}

export type ParseResult = { ok: true; pattern: ExperimentalPattern; skipped: number } | { ok: false; error: string };

/**
 * Parse a powder pattern: two or three numeric columns (2θ, I[, σ]) separated by
 * whitespace, commas or semicolons. Lines starting with `#`, `!`, `;` or `//`
 * are comments; a non-numeric header line is skipped. Rows are sorted by 2θ.
 */
export function parsePattern(text: string, name = "experiment"): ParseResult {
  const tt: number[] = [];
  const ii: number[] = [];
  const ss: number[] = [];
  let skipped = 0;
  let threeCol = true;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || /^(#|!|;|\/\/)/.test(line)) continue;
    const parts = line.split(/[\s,;]+/).filter(Boolean);
    const nums = parts.map(Number);
    if (parts.length < 2 || !Number.isFinite(nums[0]) || !Number.isFinite(nums[1])) {
      skipped++;
      continue;
    }
    tt.push(nums[0]!);
    ii.push(nums[1]!);
    if (parts.length >= 3 && Number.isFinite(nums[2])) ss.push(nums[2]!);
    else threeCol = false;
  }
  if (tt.length < 2) return { ok: false, error: "No two-column numeric data (2θ, intensity) found." };
  const order = tt.map((_, i) => i).sort((a, b) => tt[a]! - tt[b]!);
  return {
    ok: true,
    skipped,
    pattern: {
      name,
      twoTheta: order.map((i) => tt[i]!),
      intensity: order.map((i) => ii[i]!),
      sigma: threeCol && ss.length === tt.length ? order.map((i) => ss[i]!) : null,
    },
  };
}

/** Scale to max = 100 after subtracting the minimum (a crude background offset). */
export function normalizeIntensity(ys: readonly number[], subtractMin = true): number[] {
  if (ys.length === 0) return [];
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of ys) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  const base = subtractMin ? lo : 0;
  const span = hi - base;
  if (!(span > 0)) return ys.map(() => 0);
  return ys.map((v) => ((v - base) / span) * 100);
}

/** The overlay as drawn: 2θ + shift, normalised intensity × scale. */
export function transformPattern(p: Pick<ExperimentalPattern, "twoTheta" | "intensity">, shift: number, scaleFactor: number): Profile {
  const y = normalizeIntensity(p.intensity);
  return { x: p.twoTheta.map((t) => t + shift), y: y.map((v) => v * scaleFactor) };
}

/** The peak closest in 2θ to `x` within `tol` degrees (null if none). */
export function nearestPeak<T extends Pick<DiffractionPeak, "two_theta">>(peaks: readonly T[], x: number, tol = 1): T | null {
  let best: T | null = null;
  let bestD = tol;
  for (const p of peaks) {
    const d = Math.abs(p.two_theta - x);
    if (d <= bestD) {
      bestD = d;
      best = p;
    }
  }
  return best;
}

/** "(1 1 0)" / "(1 1 0), (1 0 1)"; negative indices with a leading minus. */
export function formatHkl(hkls: DiffractionPeak["hkls"], max = 3): string {
  const parts = hkls.slice(0, max).map((h) => `(${h.hkl.join(" ")})`);
  return parts.join(", ") + (hkls.length > max ? ` +${hkls.length - max}` : "");
}

/** Bragg's law: d = λ / (2 sin θ). */
export function dSpacing(twoThetaDeg: number, wavelength: number): number {
  return wavelength / (2 * Math.sin((twoThetaDeg * Math.PI) / 360));
}
