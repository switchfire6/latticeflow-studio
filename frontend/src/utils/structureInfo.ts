/**
 * Pure structure analysis for the HUD (IMPROVEMENT_SUGGESTIONS.md §3.1–3.2,
 * §5.3): stress-aware relaxation state, client-side fallbacks for the Phase A
 * payload fields (pressure, density, crystal system, Z), lattice changes,
 * the phonon supercell suggestion and structure identity.
 *
 * Stress convention (protocol.ts): Voigt [xx, yy, zz, yz, xz, xy] in GPa, ASE
 * sign (positive = tensile), so P = −tr(σ)/3 is positive for a compressed cell
 * and a cell relaxed at external pressure P_ext has σ = −P_ext·I.
 */
import type { AtomSite, CrystalStatePayload, CrystalSystem, LatticeParams } from "../types/protocol";
import { atomicMass } from "./cpkColors";
import { compositionGcd } from "./formula";

/** Default force threshold of the RELAXED badge (eV/Å), the default FIRE fmax. */
export const RELAXED_FMAX = 0.02;
/** Default stress threshold of the RELAXED badge (GPa). */
export const RELAXED_STRESS_TOL_GPA = 0.1;

export type RelaxationState = "relaxed" | "stressed" | "unrelaxed";

export interface RelaxationCriteria {
  fmax: number; // eV/Å
  stressTolGpa: number; // GPa
  /** External hydrostatic pressure the cell should be at (GPa, + = compressive). */
  targetPressureGpa: number;
}

export const DEFAULT_CRITERIA: RelaxationCriteria = {
  fmax: RELAXED_FMAX,
  stressTolGpa: RELAXED_STRESS_TOL_GPA,
  targetPressureGpa: 0,
};

export interface RelaxationVerdict {
  state: RelaxationState;
  maxForce: number;
  /** max_ij |σ_ij + P_ext δ_ij| (GPa); null when the payload carries no stress. */
  maxStressDeviation: number | null;
  pressureGpa: number | null;
  criteria: RelaxationCriteria;
}

/** P = −tr(σ)/3 from the Voigt stress (GPa, + = compressed); null if absent. */
export function pressureFromStress(stress: readonly number[] | null | undefined): number | null {
  if (!stress || stress.length < 3 || !stress.slice(0, 3).every(Number.isFinite)) return null;
  return -(stress[0]! + stress[1]! + stress[2]!) / 3;
}

/** The payload's pressure, else −tr(σ)/3. */
export function pressureOf(c: Pick<CrystalStatePayload, "stress_gpa" | "pressure_gpa">): number | null {
  return c.pressure_gpa ?? pressureFromStress(c.stress_gpa);
}

/** max_ij |σ_ij + P_ext δ_ij| over the 6 Voigt components. */
export function maxStressDeviation(stress: readonly number[] | null | undefined, targetPressureGpa = 0): number | null {
  if (!stress || stress.length < 6) return null;
  let m = 0;
  for (let i = 0; i < 6; i++) {
    const v = stress[i]!;
    if (!Number.isFinite(v)) return null;
    m = Math.max(m, Math.abs(i < 3 ? v + targetPressureGpa : v));
  }
  return m;
}

/**
 * RELAXED: max|F| < fmax and max|σ_ij + P_ext δ_ij| < tol.
 * FORCES OK · CELL STRESSED ("stressed"): forces converged, cell not.
 * UNRELAXED: forces above fmax.
 * A payload without a stress tensor is judged on forces only.
 */
export function relaxationVerdict(
  c: Pick<CrystalStatePayload, "max_force" | "stress_gpa" | "pressure_gpa">,
  criteria: Partial<RelaxationCriteria> = {},
): RelaxationVerdict {
  const cr: RelaxationCriteria = { ...DEFAULT_CRITERIA, ...criteria };
  const dev = maxStressDeviation(c.stress_gpa, cr.targetPressureGpa);
  const forcesOk = c.max_force < cr.fmax;
  const state: RelaxationState = !forcesOk ? "unrelaxed" : dev !== null && dev >= cr.stressTolGpa ? "stressed" : "relaxed";
  return { state, maxForce: c.max_force, maxStressDeviation: dev, pressureGpa: pressureOf(c), criteria: cr };
}

function signedGpa(v: number): string {
  const s = Math.abs(v).toFixed(1);
  return v < 0 ? `−${s}` : `+${s}`;
}

/**
 * The phonon-button warning (null when the structure is relaxed). Same test as
 * the badge, so the two can never disagree.
 */
export function phononWarning(v: RelaxationVerdict): string | null {
  if (v.state === "relaxed") return null;
  if (v.state === "unrelaxed") {
    return `max |F| = ${v.maxForce.toFixed(3)} eV/Å (> ${v.criteria.fmax}): harmonic phonons away from a minimum are not physically meaningful; relax first`;
  }
  const p = v.pressureGpa;
  const target = v.criteria.targetPressureGpa;
  const where =
    p !== null && Math.abs(p - target) >= v.criteria.stressTolGpa
      ? `cell under ${signedGpa(p)} GPa${target !== 0 ? ` (target ${signedGpa(target)} GPa)` : ""}`
      : `cell stress ${v.maxStressDeviation?.toFixed(2)} GPa off target (shear / anisotropic)`;
  return `${where} — relax the cell first (phonons of a strained cell are biased)`;
}

// --------------------------------------------------------------------------- //
// Fallbacks for the Phase A payload fields
// --------------------------------------------------------------------------- //
const AMU_PER_A3_TO_G_CM3 = 1.66053906660; // 1 amu/Å³ = 1.66054 g/cm³

export function densityOf(c: Pick<CrystalStatePayload, "atoms" | "lattice" | "density_g_cm3">): number | null {
  if (c.density_g_cm3 != null) return c.density_g_cm3;
  const v = c.lattice.volume;
  if (!(v > 0)) return null;
  let m = 0;
  for (const a of c.atoms) m += atomicMass(a.atomic_number);
  return (m / v) * AMU_PER_A3_TO_G_CM3;
}

/** Crystal system from the space-group number (International Tables ranges). */
export function crystalSystemFromNumber(n: number): CrystalSystem | null {
  if (!Number.isFinite(n) || n < 1 || n > 230) return null;
  if (n <= 2) return "triclinic";
  if (n <= 15) return "monoclinic";
  if (n <= 74) return "orthorhombic";
  if (n <= 142) return "tetragonal";
  if (n <= 167) return "trigonal";
  if (n <= 194) return "hexagonal";
  return "cubic";
}

export function crystalSystemOf(c: Pick<CrystalStatePayload, "crystal_system" | "spacegroup_number">): CrystalSystem | null {
  return c.crystal_system ?? crystalSystemFromNumber(c.spacegroup_number);
}

/** Element counts in order of first appearance. */
export function elementCounts(atoms: readonly Pick<AtomSite, "element">[]): Map<string, number> {
  const m = new Map<string, number>();
  for (const a of atoms) m.set(a.element, (m.get(a.element) ?? 0) + 1);
  return m;
}

/** Z (formula units per cell): payload value, else the gcd of the element counts. */
export function formulaUnitsOf(c: Pick<CrystalStatePayload, "atoms" | "formula_units">): number {
  if (c.formula_units != null && c.formula_units > 0) return c.formula_units;
  if (c.atoms.length === 0) return 1;
  return compositionGcd(elementCounts(c.atoms).values());
}

/** Atoms per formula unit (N / Z). */
export function atomsPerFormulaUnit(c: Pick<CrystalStatePayload, "atoms" | "formula_units">): number {
  const z = formulaUnitsOf(c);
  return c.atoms.length / z;
}

// --------------------------------------------------------------------------- //
// Lattice changes during a relaxation / follow
// --------------------------------------------------------------------------- //
export interface LatticeReference {
  lattice: Pick<LatticeParams, "a" | "b" | "c" | "volume">;
  nAtoms: number;
  kind: "relaxation" | "follow";
  streamId: number;
}

export interface LatticeDelta {
  /** Relative changes in % (null when not comparable, e.g. a supercell). */
  a: number | null;
  b: number | null;
  c: number | null;
  volume: number | null;
  /** Change of the volume per atom (%), always comparable. */
  volumePerAtom: number;
}

const pct = (x: number, x0: number) => (x0 !== 0 ? ((x - x0) / x0) * 100 : 0);

export function latticeDelta(
  ref: Pick<LatticeReference, "lattice" | "nAtoms">,
  now: { lattice: Pick<LatticeParams, "a" | "b" | "c" | "volume">; nAtoms: number },
): LatticeDelta {
  const same = ref.nAtoms === now.nAtoms;
  return {
    a: same ? pct(now.lattice.a, ref.lattice.a) : null,
    b: same ? pct(now.lattice.b, ref.lattice.b) : null,
    c: same ? pct(now.lattice.c, ref.lattice.c) : null,
    volume: same ? pct(now.lattice.volume, ref.lattice.volume) : null,
    volumePerAtom: pct(now.lattice.volume / Math.max(1, now.nAtoms), ref.lattice.volume / Math.max(1, ref.nAtoms)),
  };
}

// --------------------------------------------------------------------------- //
// Phonon supercell suggestion (§5.3)
// --------------------------------------------------------------------------- //
export const SUPERCELL_MIN_LENGTH = 10; // Å
export const SUPERCELL_MAX_ATOMS = 500;

/**
 * Smallest n_i with n_i·|a_i| ≥ minLength, then, while the supercell exceeds
 * maxAtoms, shrink the dimension with the longest supercell vector (keeping
 * n_i ≥ 1). |a_i| are the lattice-vector lengths in Å.
 */
export function suggestSupercell(
  lengths: readonly [number, number, number],
  nAtoms: number,
  minLength = SUPERCELL_MIN_LENGTH,
  maxAtoms = SUPERCELL_MAX_ATOMS,
): [number, number, number] {
  const n = lengths.map((L) => (L > 0 ? Math.max(1, Math.ceil(minLength / L - 1e-9)) : 1)) as [number, number, number];
  const total = () => n[0] * n[1] * n[2] * Math.max(1, nAtoms);
  while (total() > maxAtoms) {
    let best = -1;
    for (let i = 0; i < 3; i++) if (n[i]! > 1 && (best < 0 || n[i]! * lengths[i]! > n[best]! * lengths[best]!)) best = i;
    if (best < 0) break;
    n[best]!--;
  }
  return n;
}

export function latticeLengths(matrix: readonly (readonly number[])[]): [number, number, number] {
  return [0, 1, 2].map((i) => Math.hypot(...(matrix[i] ?? [0, 0, 0]))) as [number, number, number];
}

// --------------------------------------------------------------------------- //
// Identity (session restore)
// --------------------------------------------------------------------------- //
/** Same species, cell and positions (within `tol` Å), atom by atom. */
export function sameStructure(a: CrystalStatePayload | null | undefined, b: CrystalStatePayload | null | undefined, tol = 1e-4): boolean {
  if (!a || !b) return false;
  if (a.atoms.length !== b.atoms.length) return false;
  for (let i = 0; i < 3; i++)
    for (let j = 0; j < 3; j++) if (Math.abs((a.lattice.matrix[i]?.[j] ?? 0) - (b.lattice.matrix[i]?.[j] ?? 0)) > tol) return false;
  for (let k = 0; k < a.atoms.length; k++) {
    const x = a.atoms[k]!;
    const y = b.atoms[k]!;
    if (x.atomic_number !== y.atomic_number) return false;
    for (let d = 0; d < 3; d++) if (Math.abs(x.cart_coords[d]! - y.cart_coords[d]!) > tol) return false;
  }
  return true;
}
