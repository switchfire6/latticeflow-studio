/**
 * Stability scorecard (IMPROVEMENT_SUGGESTIONS.md §7.1): "is this crystal real?"
 * as four checks, each pass / fail / not run / stale (or running / unavailable),
 * with its key number and the prerequisite to offer before running it.
 * Pure (unit-tested); the view is components/hud/Scorecard.tsx.
 *
 *   relaxed      forces AND stress below tolerance (the RELAXED badge verdict, §3.2)
 *   dynamic      no imaginary phonon modes; supercell size + convergence status (§5.3)
 *   mechanical   Born criteria from the elastic tensor (§5.5)
 *   thermo       E_hull of the relaxed candidate (§5.11)
 *
 * Staleness: results are stamped with the structure state version they were
 * computed for (a model switch bumps it too), so any later change marks them stale.
 */
import type { CrystalStatePayload, PhononConvergenceResult } from "../types/protocol";
import type { ElasticState, HullState, PhononSummary } from "../store/useCrystalStore";
import { relaxationVerdict, type RelaxationCriteria, type RelaxationVerdict } from "./structureInfo";
import { formatFrequency, formatPressure, type FrequencyUnit, type PressureUnit } from "./units";

export type CheckId = "relaxed" | "dynamic" | "mechanical" | "thermo";
export type CheckState = "pass" | "fail" | "notrun" | "stale" | "running" | "unavailable";

export interface ScoreCheck {
  id: CheckId;
  title: string;
  state: CheckState;
  /** Key number ("min f −2.50 THz"). */
  value: string;
  detail: string | null;
  /** Secondary warning (e.g. supercell not converged). */
  warning: string | null;
  /** "Cell under +5.10 GPa" — offer "relax first?" before running (null = no prerequisite pending). */
  prerequisite: string | null;
}

/** E_hull at or below this counts as (near) the hull, within the typical uMLIP error. */
export const E_HULL_PASS_EV = 0.025;
/** Up to this, "metastable" in the detail text. */
export const E_HULL_METASTABLE_EV = 0.1;

export interface ScorecardInput {
  crystal: Pick<CrystalStatePayload, "max_force" | "stress_gpa" | "pressure_gpa" | "e_hull_ev"> | null;
  criteria: RelaxationCriteria;
  stateVersion: number;
  streams: readonly { command: string }[];
  phononSummary: PhononSummary | null;
  elastic: ElasticState | null;
  hull: HullState;
  frequencyUnit?: FrequencyUnit;
  pressureUnit?: PressureUnit;
}

const RELAX_CMDS = new Set(["START_RELAXATION", "FOLLOW_INSTABILITY"]);
const PHONON_CMDS = new Set(["RUN_PHONON_SHAKE_TEST", "CHECK_PHONON_CONVERGENCE"]);

/** Why the structure is not relaxed, phrased for "… — relax first?" (null when relaxed). */
export function relaxPrerequisite(v: RelaxationVerdict, unit: PressureUnit = "GPa"): string | null {
  if (v.state === "relaxed") return null;
  if (v.state === "unrelaxed") return `max |F| = ${v.maxForce.toFixed(3)} eV/Å (> ${v.criteria.fmax})`;
  const p = v.pressureGpa;
  const target = v.criteria.targetPressureGpa;
  if (p !== null && Math.abs(p - target) >= v.criteria.stressTolGpa) return `Cell under ${formatPressure(p, unit)}`;
  return `Cell stress ${v.maxStressDeviation?.toFixed(2) ?? "?"} GPa off target`;
}

export function supercellText(dim: readonly number[] | null | undefined, minImage: number | null | undefined): string | null {
  if (!dim) return minImage != null ? `d_min ${minImage.toFixed(1)} Å` : null;
  return `${dim.join("×")} supercell${minImage != null ? ` · d_min ${minImage.toFixed(1)} Å` : ""}`;
}

export function convergenceText(c: Pick<PhononConvergenceResult, "converged" | "max_abs_delta_thz" | "tolerance_thz"> | null, unit: FrequencyUnit = "THz"): string {
  if (!c) return "convergence not checked";
  const d = formatFrequency(c.max_abs_delta_thz, unit, 3);
  return c.converged ? `converged ✓ (max |Δf| ${d})` : `not converged ✗ (max |Δf| ${d} > ${formatFrequency(c.tolerance_thz, unit, 3)})`;
}

export function computeScorecard(i: ScorecardInput): ScoreCheck[] {
  const fu = i.frequencyUnit ?? "THz";
  const pu = i.pressureUnit ?? "GPa";
  const running = (cmds: Set<string>) => i.streams.some((s) => cmds.has(s.command));
  const verdict = i.crystal ? relaxationVerdict(i.crystal, i.criteria) : null;
  const prereq = verdict ? relaxPrerequisite(verdict, pu) : null;

  // ---- relaxed
  let relaxed: ScoreCheck;
  if (!verdict) {
    relaxed = { id: "relaxed", title: "Relaxed", state: "notrun", value: "—", detail: "no structure", warning: null, prerequisite: null };
  } else {
    const p = verdict.pressureGpa;
    relaxed = {
      id: "relaxed",
      title: "Relaxed",
      state: running(RELAX_CMDS) ? "running" : verdict.state === "relaxed" ? "pass" : "fail",
      value: `max |F| ${verdict.maxForce.toFixed(3)} eV/Å${p !== null ? ` · P ${formatPressure(p, pu)}` : ""}`,
      detail:
        verdict.state === "relaxed"
          ? "forces and stress below tolerance"
          : verdict.state === "stressed"
            ? "forces converged, cell stressed"
            : `forces above fmax ${verdict.criteria.fmax} eV/Å`,
      warning: null,
      prerequisite: null,
    };
  }

  // ---- dynamical stability
  const ps = i.phononSummary;
  let dynamic: ScoreCheck;
  if (running(PHONON_CMDS)) {
    dynamic = {
      id: "dynamic",
      title: "Dynamically stable",
      state: "running",
      value: i.streams.some((s) => s.command === "CHECK_PHONON_CONVERGENCE") ? "checking convergence…" : "computing phonons…",
      detail: ps ? supercellText(ps.supercellDim, ps.minImageDistance) : null,
      warning: null,
      prerequisite: null,
    };
  } else if (!ps) {
    dynamic = { id: "dynamic", title: "Dynamically stable", state: "notrun", value: "—", detail: "finite-displacement phonons", warning: null, prerequisite: prereq };
  } else {
    const stale = ps.stateVersion !== i.stateVersion;
    const sc = supercellText(ps.supercellDim, ps.minImageDistance);
    dynamic = {
      id: "dynamic",
      title: "Dynamically stable",
      state: stale ? "stale" : ps.stable ? "pass" : "fail",
      value: ps.stable
        ? `min f ${formatFrequency(ps.minFrequencyThz, fu)}`
        : `${ps.imaginaryCount} imaginary · min ${formatFrequency(ps.minFrequencyThz, fu)}`,
      detail: [sc, convergenceText(ps.convergence, fu)].filter(Boolean).join(" · "),
      warning: ps.convergence && !ps.convergence.converged ? "supercell not converged: increase it before trusting the sign of soft modes" : null,
      prerequisite: prereq,
    };
  }

  // ---- mechanical stability
  const el = i.elastic;
  let mechanical: ScoreCheck;
  if (running(new Set(["COMPUTE_ELASTIC_TENSOR"]))) {
    mechanical = { id: "mechanical", title: "Mechanically stable", state: "running", value: "straining…", detail: null, warning: null, prerequisite: null };
  } else if (!el) {
    mechanical = { id: "mechanical", title: "Mechanically stable", state: "notrun", value: "—", detail: "elastic tensor (Born criteria)", warning: null, prerequisite: prereq };
  } else {
    const r = el.result;
    const minEig = r.eigenvalues_gpa.length ? Math.min(...r.eigenvalues_gpa) : null;
    mechanical = {
      id: "mechanical",
      title: "Mechanically stable",
      state: el.stateVersion !== i.stateVersion ? "stale" : r.born_stable ? "pass" : "fail",
      value: `min λ(C) ${minEig !== null ? minEig.toFixed(1) : "—"} GPa`,
      detail: `K ${r.k_vrh_gpa.toFixed(0)} · G ${r.g_vrh_gpa.toFixed(0)} GPa (VRH)${r.relaxed_ions ? "" : " · clamped-ion"}`,
      warning: Math.abs(r.reference_pressure_gpa) > 0.5 ? `reference cell at ${formatPressure(r.reference_pressure_gpa, pu)}` : null,
      prerequisite: prereq,
    };
  }

  // ---- thermodynamic stability
  const h = i.hull;
  let thermo: ScoreCheck;
  const hullValue = (e: number) => `E_hull ${e.toFixed(3)} eV/atom`;
  const hullDetail = (e: number) => (e <= 0 ? "on the hull" : e <= E_HULL_PASS_EV ? "near the hull (within uMLIP error)" : e <= E_HULL_METASTABLE_EV ? "metastable" : "above the hull");
  if (h.status === "busy") {
    thermo = { id: "thermo", title: "Thermodynamically stable", state: "running", value: "evaluating hull…", detail: null, warning: null, prerequisite: null };
  } else if (h.status === "ok") {
    const e = h.result.e_hull_ev;
    thermo = {
      id: "thermo",
      title: "Thermodynamically stable",
      state: h.stateVersion !== i.stateVersion ? "stale" : e <= E_HULL_PASS_EV ? "pass" : "fail",
      value: hullValue(e),
      detail: `${hullDetail(e)} · ${h.result.candidate_relaxed === false ? "candidate as given" : "relaxed candidate"}`,
      warning: h.result.candidate_relaxed === false ? "candidate scored without relaxation" : null,
      prerequisite: null,
    };
  } else if (h.status === "unavailable" || h.status === "error") {
    thermo = {
      id: "thermo",
      title: "Thermodynamically stable",
      state: "unavailable",
      value: h.status === "unavailable" ? "hull unavailable" : "hull failed",
      detail: h.message,
      warning: null,
      prerequisite: null,
    };
  } else if (i.crystal?.e_hull_ev != null) {
    const e = i.crystal.e_hull_ev;
    thermo = {
      id: "thermo",
      title: "Thermodynamically stable",
      state: e <= E_HULL_PASS_EV ? "pass" : "fail",
      value: hullValue(e),
      detail: hullDetail(e),
      warning: null,
      prerequisite: null,
    };
  } else {
    thermo = { id: "thermo", title: "Thermodynamically stable", state: "notrun", value: "—", detail: "E_hull vs Materials Project", warning: null, prerequisite: null };
  }

  return [relaxed, dynamic, mechanical, thermo];
}

/** One-line summary: "3 / 4 pass · 1 stale". */
export function scorecardSummary(checks: readonly ScoreCheck[]): { text: string; tone: "green" | "red" | "amber" | "slate" } {
  const n = (s: CheckState) => checks.filter((c) => c.state === s).length;
  const pass = n("pass");
  const fail = n("fail");
  const stale = n("stale");
  const parts = [`${pass}/${checks.length} ✓`];
  if (fail) parts.push(`${fail} fail`);
  if (stale) parts.push(`${stale} stale`);
  const tone = fail ? "red" : pass === checks.length ? "green" : stale ? "amber" : "slate";
  return { text: parts.join(" · "), tone };
}
