/**
 * Superconductivity Tier 0 (docs/design/SUPERCONDUCTIVITY_TIER0.md §1): the
 * Allen–Dynes T_c for an *assumed* λ and μ*, driven by the phonon scales the
 * backend derives from the DOS (constant-coupling proxy α²F ∝ F). This is a
 * ranking scale, never a prediction: MACE has no electrons, so λ is not computed.
 */

export const THZ_TO_K = 47.99243;
export const LAMBDA_GRID = [0.3, 0.5, 0.75, 1.0, 1.5, 2.0] as const;
export const MU_STAR_GRID = [0.1, 0.13] as const;
export const LAMBDA_RANGE: readonly [number, number] = [0.2, 3.0];
export const MU_STAR_RANGE: readonly [number, number] = [0.05, 0.2];

/** Allen–Dynes T_c (K) with the f1·f2 strong-coupling corrections; 0 when λ − μ*(1 + 0.62λ) ≤ 0. */
export function allenDynesTc(lam: number, muStar: number, omegaLogK: number, omega2K: number): number {
  const den = lam - muStar * (1 + 0.62 * lam);
  if (!(den > 0) || !(omegaLogK > 0)) return 0;
  const ratio = omega2K > 0 ? omega2K / omegaLogK : 1;
  const l1 = 2.46 * (1 + 3.8 * muStar);
  const l2 = 1.82 * (1 + 6.3 * muStar) * ratio;
  const f1 = Math.cbrt(1 + Math.pow(lam / l1, 1.5));
  const f2 = 1 + ((ratio - 1) * lam * lam) / (lam * lam + l2 * l2);
  return f1 * f2 * (omegaLogK / 1.2) * Math.exp((-1.04 * (1 + lam)) / den);
}

/** T_c(λ) sampled evenly over [lo, hi] (n points) for one μ*. */
export function tcCurve(
  muStar: number,
  omegaLogK: number,
  omega2K: number,
  range: readonly [number, number] = LAMBDA_RANGE,
  n = 57,
): { lam: number[]; tc: number[] } {
  const [lo, hi] = range;
  const lam: number[] = [];
  const tc: number[] = [];
  for (let i = 0; i < n; i++) {
    const l = lo + ((hi - lo) * i) / (n - 1);
    lam.push(l);
    tc.push(allenDynesTc(l, muStar, omegaLogK, omega2K));
  }
  return { lam, tc };
}
