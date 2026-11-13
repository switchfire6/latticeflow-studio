/**
 * CSV export for every chart (IMPROVEMENT_SUGGESTIONS.md §4.2), pure and
 * unit-tested. Values are written in the display unit named in the header;
 * comment lines (`#`) carry provenance (formula, model, settings).
 */
import type {
  BulkModulusResult,
  DiffractionResponse,
  ElasticTensorResult,
  PhononAnalysisComplete,
  PhaseDiagramData,
  PhononDosData,
} from "../types/protocol";
import { columnTitle, type ColumnDef, type TableRow } from "./library";
import { connectedFrequencies, formatKLabel } from "./bandChart";
import { frequencyFactor, frequencyUnitLabel, type FrequencyUnit } from "./units";

type Cell = string | number | null | undefined;

/** RFC 4180 field quoting. */
export function csvField(v: Cell): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return Number.isFinite(v) ? String(+v.toPrecision(10)) : "";
  return /[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
}

export function toCsv(header: readonly string[], rows: readonly (readonly Cell[])[], comments: readonly string[] = []): string {
  const lines = [...comments.map((c) => `# ${c}`), header.map(csvField).join(","), ...rows.map((r) => r.map(csvField).join(","))];
  return lines.join("\n") + "\n";
}

const unitTxt = (u: FrequencyUnit) => (u === "cm-1" ? "cm-1" : u);

/** Dispersion: one row per q-point, one column per (connected) branch. */
export function dispersionCsv(p: PhononAnalysisComplete, unit: FrequencyUnit = "THz", comments: string[] = []): string {
  const k = frequencyFactor(unit);
  const rows = connectedFrequencies(p.frequencies_thz, p.band_order);
  const nb = rows[0]?.length ?? 0;
  const labels = new Map(p.kpath_labels.map((l) => [l.index, formatKLabel(l.label)]));
  const header = ["q_index", "distance_inv_A", "label", ...Array.from({ length: nb }, (_, b) => `branch_${b}_${unitTxt(unit)}`)];
  const extra = [
    `phonon dispersion (${p.band_order ? "connected branches" : "ascending bands"}), frequencies in ${frequencyUnitLabel(unit)}; negative = imaginary`,
    p.cell_mode ? `cell mode ${p.cell_mode}${p.supercell_dim ? `, supercell ${p.supercell_dim.join("x")}` : ""}` : "",
  ].filter(Boolean);
  return toCsv(
    header,
    rows.map((r, q) => [q, p.kpath_distances[q], labels.get(q) ?? "", ...r.map((f) => f * k)]),
    [...comments, ...extra],
  );
}

export function dosCsv(d: PhononDosData, unit: FrequencyUnit = "THz", comments: string[] = []): string {
  const k = frequencyFactor(unit);
  const els = Object.keys(d.projected_dos);
  return toCsv(
    [`frequency_${unitTxt(unit)}`, `total_states_per_${unitTxt(unit)}_cell`, ...els.map((e) => `pdos_${e}`)],
    d.frequencies_thz.map((f, i) => [f * k, (d.total_dos[i] ?? NaN) / k, ...els.map((e) => (d.projected_dos[e]?.[i] ?? NaN) / k)]),
    [...comments, `phonon DOS, mesh ${d.mesh.join("x")}`],
  );
}

export function thermalCsv(d: PhononDosData, comments: string[] = []): string {
  const t = d.thermal;
  return toCsv(
    ["T_K", "F_eV_per_atom", "S_kB_per_atom", "Cv_kB_per_atom"],
    t.temperatures_k.map((T, i) => [T, t.free_energy_ev_per_atom[i], t.entropy_kb_per_atom[i], t.heat_capacity_kb_per_atom[i]]),
    [...comments, `harmonic thermodynamics; ZPE ${t.zero_point_energy_ev_per_atom} eV/atom`],
  );
}

export function eosCsv(r: BulkModulusResult, nAtoms: number, comments: string[] = []): string {
  const order = r.volumes.map((_, i) => i).sort((a, b) => r.volumes[a]! - r.volumes[b]!);
  return toCsv(
    ["V_A3", "E_eV", "E_eV_per_atom", "P_GPa"],
    order.map((i) => [r.volumes[i], r.energies_ev[i], (r.energies_ev[i] ?? NaN) / Math.max(1, nAtoms), r.pressures_gpa[i]]),
    [...comments, r.method, `K(V0) ${r.k_gpa ?? "n/a"} GPa, B0 ${r.k_eq_gpa ?? "n/a"} GPa, B0' ${r.k_eq_prime ?? "n/a"}, V_eq ${r.v_eq ?? "n/a"} A3`],
  );
}

export function rdfCsv(r: readonly number[], g: readonly number[], comments: string[] = []): string {
  return toCsv(["r_A", "g_r"], r.map((x, i) => [x, g[i]]), comments);
}

export function relaxationCsv(hist: readonly { step: number; energyPerAtom: number; maxForce: number }[], fmax: number, comments: string[] = []): string {
  return toCsv(["step", "E_eV_per_atom", "max_force_eV_per_A"], hist.map((p) => [p.step, p.energyPerAtom, p.maxForce]), [...comments, `fmax target ${fmax} eV/A`]);
}

export function diffractionCsv(d: DiffractionResponse, comments: string[] = []): string {
  return toCsv(
    ["two_theta_deg", "intensity", "d_spacing_A", "hkl", "multiplicity"],
    d.peaks.map((p) => [p.two_theta, p.intensity, p.d_spacing, p.hkls.map((h) => h.hkl.join(" ")).join("; "), p.hkls.map((h) => h.multiplicity).join("; ")]),
    [...comments, `${d.kind} pattern, ${d.radiation ?? "custom"} lambda = ${d.wavelength_angstrom} A`],
  );
}

export function profileCsv(x: readonly number[], y: readonly number[], label = "intensity", comments: string[] = []): string {
  return toCsv(["two_theta_deg", label], x.map((v, i) => [v, y[i]]), comments);
}

export function elasticCsv(r: ElasticTensorResult, comments: string[] = []): string {
  const rows: Cell[][] = r.c_ij_gpa.map((row, i) => [`C${i + 1}j`, ...row]);
  rows.push([]);
  const kv: [string, Cell][] = [
    ["eigenvalues_GPa", r.eigenvalues_gpa.map((e) => +e.toPrecision(6)).join(" ")],
    ["born_stable", String(r.born_stable)],
    ["K_voigt_GPa", r.k_voigt_gpa],
    ["K_reuss_GPa", r.k_reuss_gpa],
    ["K_vrh_GPa", r.k_vrh_gpa],
    ["G_voigt_GPa", r.g_voigt_gpa],
    ["G_reuss_GPa", r.g_reuss_gpa],
    ["G_vrh_GPa", r.g_vrh_gpa],
    ["E_vrh_GPa", r.youngs_modulus_vrh_gpa],
    ["poisson_vrh", r.poisson_ratio_vrh],
    ["pugh_ratio", r.pugh_ratio],
    ["universal_anisotropy", r.universal_anisotropy],
    ["debye_temperature_K", r.debye_temperature_k],
    ["reference_pressure_GPa", r.reference_pressure_gpa],
    ["fit_residual_GPa", r.fit_residual_gpa],
    ["relaxed_ions", String(r.relaxed_ions)],
    ["max_strain", r.max_strain],
  ];
  for (const [k, v] of kv) rows.push([k, v]);
  return toCsv(["C_ij_GPa", "j=1", "j=2", "j=3", "j=4", "j=5", "j=6"], rows, [...comments, r.method]);
}

/** Safe file stem from a formula and a suffix ("SrTiO3_dispersion"). */
export function fileStem(formula: string | null | undefined, what: string): string {
  const f = (formula || "structure").replace(/[^A-Za-z0-9_-]+/g, "");
  return `${f || "structure"}_${what}`;
}

/** Results table of a project: one row per structure, the visible columns (raw values, units in the header). */
export function resultsTableCsv(rows: readonly TableRow[], cols: readonly ColumnDef[], comments: string[] = []): string {
  return toCsv(
    ["structure_id", ...cols.map(columnTitle)],
    rows.map((r) => [r.id, ...cols.map((c) => { const v = r.values[c.key]; return typeof v === "boolean" ? String(v) : v; })]),
    comments,
  );
}

/** Phase-diagram points (protocol §8): the reference phases, then the evaluated structure. */
export function phaseDiagramCsv(pd: PhaseDiagramData, comments: string[] = []): string {
  const pts = [...pd.entries.map((e) => ({ ...e, role: "reference" })), { ...pd.candidate, role: "candidate" }];
  return toCsv(
    ["role", "material_id", "formula", ...pd.elements.map((e) => `x_${e}`), "formation_energy_eV_per_atom", "e_above_hull_eV_per_atom", "on_hull"],
    pts.map((p) => [p.role, p.material_id ?? "", p.formula, ...pd.elements.map((e) => p.composition[e] ?? 0), p.formation_energy_per_atom, p.e_above_hull, String(p.on_hull)]),
    [...comments, `phase diagram ${pd.elements.join("-")}; hull facets ${pd.facets.map((f) => f.join("-")).join(" ")}`],
  );
}
