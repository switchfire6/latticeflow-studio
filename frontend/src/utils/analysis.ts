/**
 * Phase C1 pure helpers, unit-tested: Arrhenius fit of diffusion coefficients
 * (§5.8), committee disagreement (§6.2), history navigation (UNDO / REDO
 * steps to reach an entry) and number formatting.
 */
import type { CommitteeResult, HistoryEntryInfo } from "../types/protocol";

export const K_B_EV = 8.617333262e-5; // eV/K

/** 1.23e-5 -> "1.23 × 10⁻⁵". */
export function sciText(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (v === 0) return "0";
  const e = Math.floor(Math.log10(Math.abs(v)));
  const m = v / 10 ** e;
  const sup = String(e)
    .split("")
    .map((ch) => ({ "-": "⁻", "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹" })[ch] ?? ch)
    .join("");
  return `${m.toFixed(digits)} × 10${sup}`;
}

export interface ArrheniusPoint {
  temperatureK: number;
  d: number | null; // cm²/s
}
export interface ArrheniusFit {
  /** Activation energy (eV). */
  ea: number;
  /** Prefactor D0 (cm²/s). */
  d0: number;
  r2: number;
  n: number;
}

/** Least-squares fit of ln D = ln D0 − Ea / (k_B T); null with fewer than two usable points. */
export function arrheniusFit(points: readonly ArrheniusPoint[]): ArrheniusFit | null {
  const xs: number[] = [];
  const ys: number[] = [];
  for (const p of points) {
    if (p.d == null || !(p.d > 0) || !(p.temperatureK > 0)) continue;
    xs.push(1 / p.temperatureK);
    ys.push(Math.log(p.d));
  }
  const n = xs.length;
  if (n < 2) return null;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let sxx = 0;
  let sxy = 0;
  let syy = 0;
  for (let i = 0; i < n; i++) {
    sxx += (xs[i]! - mx) ** 2;
    sxy += (xs[i]! - mx) * (ys[i]! - my);
    syy += (ys[i]! - my) ** 2;
  }
  if (sxx === 0) return null;
  const slope = sxy / sxx;
  const intercept = my - slope * mx;
  return { ea: -slope * K_B_EV, d0: Math.exp(intercept), r2: syy > 0 ? (sxy * sxy) / (sxx * syy) : 1, n };
}

/** Parse "300, 500 700" into temperatures (K), dropping invalid values. */
export function parseTemperatures(text: string): number[] {
  return text
    .split(/[\s,;]+/)
    .map(Number)
    .filter((t) => Number.isFinite(t) && t > 0 && t <= 5000);
}

// --------------------------------------------------------------------------- committee
export const COMMITTEE_VOLUME_SPREAD_PCT = 3;

export interface CommitteeVerdict {
  disagree: boolean;
  volumeSpreadPct: number | null;
  spacegroups: string[];
  reasons: string[];
}

export function committeeVerdict(r: Pick<CommitteeResult, "entries" | "spread">): CommitteeVerdict {
  const vols = r.entries.map((e) => e.relaxed_volume_per_atom).filter((v): v is number => v != null && v > 0);
  let spread: number | null = r.spread.volume_percent ?? null;
  if (spread == null && vols.length >= 2) {
    const mean = vols.reduce((a, b) => a + b, 0) / vols.length;
    spread = ((Math.max(...vols) - Math.min(...vols)) / mean) * 100;
  }
  const spacegroups = [...new Set(r.entries.map((e) => e.relaxed_spacegroup).filter((s): s is string => !!s))];
  const reasons: string[] = [];
  if (spread != null && spread > COMMITTEE_VOLUME_SPREAD_PCT) reasons.push(`relaxed volumes differ by ${spread.toFixed(1)} % (> ${COMMITTEE_VOLUME_SPREAD_PCT} %)`);
  if (spacegroups.length > 1) reasons.push(`space groups differ (${spacegroups.join(" vs ")})`);
  return { disagree: reasons.length > 0, volumeSpreadPct: spread, spacegroups, reasons };
}

// --------------------------------------------------------------------------- history
/** UNDO / REDO commands that take the history from `current` to `target` (there is no GOTO). */
export function historySteps(current: number, target: number): { command: "UNDO" | "REDO"; count: number } | null {
  if (target === current) return null;
  return target < current ? { command: "UNDO", count: current - target } : { command: "REDO", count: target - current };
}

export interface HistoryRef {
  position: number;
  label: string;
  formula: string;
  n_atoms: number;
  energy_per_atom: number;
}

/** Find a remembered entry again (positions shift when the oldest entries are dropped). */
export function resolveHistoryRef(entries: readonly HistoryEntryInfo[] | null | undefined, ref: HistoryRef | null): HistoryEntryInfo | null {
  if (!entries || !ref) return null;
  const same = (e: HistoryEntryInfo) => e.label === ref.label && e.formula === ref.formula && e.n_atoms === ref.n_atoms && Math.abs(e.energy_per_atom - ref.energy_per_atom) < 1e-9;
  const at = entries.find((e) => e.position === ref.position);
  if (at && same(at)) return at;
  return entries.find(same) ?? null;
}
