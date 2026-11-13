/**
 * Phase C2 pure helpers (unit-tested): Nernst–Einstein ionic conductivity
 * (§5.8), click-to-place ray / cell-plane intersection (§7.3), and text for
 * generation search summaries and prototype substitutions (§7.5).
 */
import type { GenerationSearchSummary, SiteEnvironment } from "../types/protocol";
import type { Vec3 } from "./latticeMath";

// --------------------------------------------------------------------------- ionic conductivity
export const ELEMENTARY_CHARGE_C = 1.602176634e-19;
export const K_B_J = 1.380649e-23; // J/K
export const A3_TO_CM3 = 1e-24;

/**
 * Nernst–Einstein σ = n q² D / (k_B T) in S/cm, with n = nCarriers / V
 * (V in Å³ → cm⁻³), q = z·e, D in cm²/s. Tracer D, no ion–ion correlation
 * (Haven ratio 1). Null for unusable inputs.
 */
export function nernstEinsteinSigma(nCarriers: number, volumeA3: number, charge: number, dCm2s: number | null | undefined, temperatureK: number): number | null {
  if (dCm2s == null || !(dCm2s >= 0) || !(volumeA3 > 0) || !(temperatureK > 0) || !(nCarriers > 0) || !Number.isFinite(charge) || charge === 0) return null;
  const n = nCarriers / (volumeA3 * A3_TO_CM3); // cm⁻³
  const q = charge * ELEMENTARY_CHARGE_C;
  return (n * q * q * dCm2s) / (K_B_J * temperatureK); // C² cm⁻³ cm² s⁻¹ / J = S/cm
}

/** Common oxidation states (fallback when no bond-valence analysis is available). */
const COMMON_OXIDATION: Record<string, number> = {
  H: 1, Li: 1, Na: 1, K: 1, Rb: 1, Cs: 1, Ag: 1, Cu: 1, Tl: 1,
  Be: 2, Mg: 2, Ca: 2, Sr: 2, Ba: 2, Zn: 2, Cd: 2, Pb: 2, Mn: 2, Fe: 2, Co: 2, Ni: 2,
  B: 3, Al: 3, Ga: 3, In: 3, Sc: 3, Y: 3, La: 3, Bi: 3,
  Ti: 4, Zr: 4, Hf: 4, Si: 4, Ge: 4, Sn: 4, Ce: 4,
  P: 5, Nb: 5, Ta: 5, V: 5,
  Mo: 6, W: 6,
  O: -2, S: -2, Se: -2, Te: -2,
  F: -1, Cl: -1, Br: -1, I: -1,
  N: -3,
};

export function guessOxidationState(element: string): number | null {
  return COMMON_OXIDATION[element] ?? null;
}

/** Mean bond-valence oxidation state per element from a local analysis (null entries ignored). */
export function oxidationStatesFromSites(sites: readonly Pick<SiteEnvironment, "element" | "oxidation_state">[] | null | undefined): Record<string, number> {
  const sum: Record<string, { s: number; n: number }> = {};
  for (const x of sites ?? []) {
    if (x.oxidation_state == null || !Number.isFinite(x.oxidation_state)) continue;
    const e = (sum[x.element] ??= { s: 0, n: 0 });
    e.s += x.oxidation_state;
    e.n++;
  }
  return Object.fromEntries(Object.entries(sum).map(([el, v]) => [el, v.s / v.n]));
}

export interface ConductivityRow {
  element: string;
  charge: number | null;
  chargeSource: "analysis" | "guess" | null;
  /** Carrier density (cm⁻³). */
  density: number;
  d: number | null;
  /** S/cm. */
  sigma: number | null;
}

/** σ per element with a diffusion coefficient; the charge comes from the analysis, else a common oxidation state. */
export function ionicConductivities(
  counts: Readonly<Record<string, number>>,
  volumeA3: number,
  diffusion: Readonly<Record<string, number | null>>,
  temperatureK: number,
  analysisCharges: Readonly<Record<string, number>> = {},
): ConductivityRow[] {
  return Object.keys(diffusion)
    .sort()
    .map((element) => {
      const fromAnalysis = analysisCharges[element];
      const guess = guessOxidationState(element);
      const charge = fromAnalysis != null && fromAnalysis !== 0 ? fromAnalysis : guess;
      const chargeSource = fromAnalysis != null && fromAnalysis !== 0 ? "analysis" : guess != null ? "guess" : null;
      const n = counts[element] ?? 0;
      const d = diffusion[element] ?? null;
      return {
        element,
        charge,
        chargeSource,
        density: volumeA3 > 0 ? n / (volumeA3 * A3_TO_CM3) : 0,
        d,
        sigma: charge == null ? null : nernstEinsteinSigma(n, volumeA3, charge, d, temperatureK),
      };
    });
}

export function elementCountMap(atoms: readonly { element: string }[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const a of atoms) out[a.element] = (out[a.element] ?? 0) + 1;
  return out;
}

/** "+1" / "−2" / "+1.03". */
export function chargeText(z: number | null): string {
  if (z == null) return "?";
  const r = Math.abs(z - Math.round(z)) < 0.05 ? String(Math.abs(Math.round(z))) : Math.abs(z).toFixed(2);
  return `${z < 0 ? "−" : "+"}${r}`;
}

// --------------------------------------------------------------------------- click-to-place
/**
 * Intersect a ray (crystal Cartesian Å) with the cell plane {s_axis = depth}
 * spanned by the other two lattice vectors. Returns the fractional
 * coordinates of the hit (s_axis = depth exactly), or null when the ray is
 * parallel, points away, or the hit lies outside the face [0, 1]² (with a
 * small tolerance; in-face hits are clamped).
 * Linv: inverse of the row-major lattice (rows a, b, c), so s = r · Linv.
 */
export function rayCellPlaneFrac(origin: Vec3, dir: Vec3, Linv: ArrayLike<number>, axis: 0 | 1 | 2, depth: number, tol = 0.02): Vec3 | null {
  // s_axis(r) = r · Linv[:, axis]; the plane is s_axis = depth.
  const g: Vec3 = [Linv[axis]!, Linv[3 + axis]!, Linv[6 + axis]!];
  const denom = dir[0] * g[0] + dir[1] * g[1] + dir[2] * g[2];
  if (Math.abs(denom) < 1e-12) return null;
  const s0 = origin[0] * g[0] + origin[1] * g[1] + origin[2] * g[2];
  const t = (depth - s0) / denom;
  if (!(t > 0)) return null;
  const p: Vec3 = [origin[0] + t * dir[0], origin[1] + t * dir[1], origin[2] + t * dir[2]];
  const f: Vec3 = [0, 0, 0];
  for (let j = 0; j < 3; j++) f[j] = p[0] * Linv[j]! + p[1] * Linv[3 + j]! + p[2] * Linv[6 + j]!;
  for (let j = 0; j < 3; j++) {
    if (j === axis) continue;
    if (f[j]! < -tol || f[j]! > 1 + tol) return null;
    f[j] = Math.min(1, Math.max(0, f[j]!));
  }
  f[axis] = depth;
  return f;
}

// --------------------------------------------------------------------------- generator / prototypes text
export function searchSummaryText(s: GenerationSearchSummary, nCandidates: number): string {
  return `searched ${s.n_wyckoff_combinations} Wyckoff assignment${s.n_wyckoff_combinations === 1 ? "" : "s"} in ${s.spacegroup_symbol} (#${s.spacegroup_number}), screened ${s.n_structures_screened}, scored ${s.n_structures_scored} → top ${nCandidates}`;
}

/** {"K": "Na", "Ta": "Nb"} → "K→Na, Ta→Nb" (identity substitutions dropped). */
export function substitutionText(sub: Readonly<Record<string, string>>): string {
  const parts = Object.entries(sub)
    .filter(([a, b]) => a !== b)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([a, b]) => `${a}→${b}`);
  return parts.length ? parts.join(", ") : "—";
}
