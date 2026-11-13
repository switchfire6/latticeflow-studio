/**
 * Phonon eigenmode animation (PROJECT_SPEC.md §4.4 step 6):
 *
 *   u_lκ(t) = A / (2π max(|f|, f_floor)) · sqrt(k_B T / m_κ)
 *             · Re[ e_κ(q, ν) · exp(i (q·R_lκ − 2π |f| t_anim)) ]
 *
 * e_κ is the mass-weighted eigenvector (the sqrt(1/m_κ) factor turns it into a
 * displacement), q·R_lκ = 2π q_frac · (s_κ + n_l) uses the atom's full position
 * in image l (phonopy phase convention), f is in THz and t_anim in ps, so
 * 2π f t_anim is in radians.
 *
 * Per frame we fold everything except the image phase into one complex vector
 * per atom, c_κ = pref_κ · e_κ · exp(i (2π q·s_κ − 2π f t)); the displacement of
 * image n is then Re[c_κ · exp(2πi q·n)], so the 2x2x2 halo and periodic bond
 * endpoints cost one complex multiply each.
 */
import type { Eigenmode } from "../types/protocol";

export const K_B = 1.380649e-23; // J/K
export const AMU = 1.66053906660e-27; // kg
export const F_FLOOR_THZ = 0.5;

export interface PhononAnimParams {
  /** Dimensionless user gain A_amp (default 1). */
  amplitude: number;
  /** Temperature in K for the equipartition amplitude. */
  temperatureK: number;
  /** Slow-motion factor: animated picoseconds per wall-clock second. */
  slowMotionPsPerSecond: number;
}

export const DEFAULT_PHONON_PARAMS: PhononAnimParams = {
  amplitude: 1,
  temperatureK: 300,
  slowMotionPsPerSecond: 0.3,
};

/** Equipartition amplitude prefactor in Å for one atom (§4.4 step 6). */
export function amplitudePrefactor(frequencyThz: number, massAmu: number, p: PhononAnimParams): number {
  const f = Math.max(Math.abs(frequencyThz), F_FLOOR_THZ) * 1e12; // Hz
  const thermalVelocity = Math.sqrt((K_B * Math.max(p.temperatureK, 0)) / (massAmu * AMU)); // m/s
  return (p.amplitude / (2 * Math.PI * f)) * thermalVelocity * 1e10; // Å
}

/**
 * Fill the per-atom complex displacement field c_κ (re/im, length 3N) for the
 * animation time `wallSeconds`. Returns false (and leaves the buffers alone)
 * when the mode does not match the structure.
 */
export function computeModeField(
  mode: Eigenmode,
  frac: ArrayLike<number>,
  masses: ArrayLike<number>,
  n: number,
  wallSeconds: number,
  p: PhononAnimParams,
  outRe: Float32Array,
  outIm: Float32Array,
): boolean {
  if (mode.eigenvectors_real.length !== n || mode.eigenvectors_imag.length !== n) return false;
  const q = mode.q_frac;
  // The phase speed uses the same floor as the amplitude so near-zero acoustic
  // modes still visibly oscillate instead of freezing.
  const fAnim = Math.max(Math.abs(mode.frequency_thz), F_FLOOR_THZ);
  const tAnim = wallSeconds * p.slowMotionPsPerSecond;
  const timePhase = 2 * Math.PI * fAnim * tAnim;
  for (let k = 0; k < n; k++) {
    const pref = amplitudePrefactor(mode.frequency_thz, masses[k]!, p);
    const qs = q[0] * frac[3 * k]! + q[1] * frac[3 * k + 1]! + q[2] * frac[3 * k + 2]!;
    const phi = 2 * Math.PI * qs - timePhase;
    const cos = Math.cos(phi);
    const sin = Math.sin(phi);
    const er = mode.eigenvectors_real[k]!;
    const ei = mode.eigenvectors_imag[k]!;
    for (let a = 0; a < 3; a++) {
      const r = er[a]!;
      const i = ei[a]!;
      outRe[3 * k + a] = pref * (r * cos - i * sin);
      outIm[3 * k + a] = pref * (r * sin + i * cos);
    }
  }
  return true;
}

/** Cartesian displacement of atom k in periodic image n: Re[c_k · exp(2πi q·n)]. */
export function imageDisplacement(
  re: ArrayLike<number>,
  im: ArrayLike<number>,
  k: number,
  q: ArrayLike<number>,
  n0: number,
  n1: number,
  n2: number,
  out: { 0: number; 1: number; 2: number },
): void {
  if (n0 === 0 && n1 === 0 && n2 === 0) {
    out[0] = re[3 * k]!;
    out[1] = re[3 * k + 1]!;
    out[2] = re[3 * k + 2]!;
    return;
  }
  const theta = 2 * Math.PI * (q[0]! * n0 + q[1]! * n1 + q[2]! * n2);
  const c = Math.cos(theta);
  const s = Math.sin(theta);
  out[0] = re[3 * k]! * c - im[3 * k]! * s;
  out[1] = re[3 * k + 1]! * c - im[3 * k + 1]! * s;
  out[2] = re[3 * k + 2]! * c - im[3 * k + 2]! * s;
}

/** The default mode to animate after a shake-test: the softest imaginary mode, else the softest optical one. */
export function pickDefaultEigenmode(modes: Eigenmode[]): Eigenmode | null {
  if (modes.length === 0) return null;
  const imaginary = modes.filter((m) => m.is_imaginary);
  if (imaginary.length > 0) return imaginary.reduce((a, b) => (b.frequency_thz < a.frequency_thz ? b : a));
  const optical = modes.filter((m) => Math.abs(m.frequency_thz) > F_FLOOR_THZ);
  if (optical.length === 0) return null;
  return optical.reduce((a, b) => (b.frequency_thz < a.frequency_thz ? b : a));
}
