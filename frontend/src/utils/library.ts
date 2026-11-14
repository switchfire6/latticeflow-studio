/**
 * Pure helpers of the project library UI (unit-tested): the lineage order of
 * the structure list, the results table (flattened `latest` summaries,
 * protocol §6.2–§6.3: column definitions, empty-column hiding, sorting) and
 * the k-path check of the dispersion overlay.
 */
import type { PhononAnalysisComplete } from "../types/protocol";
import type { StructureListRow, SummaryValue } from "../types/library";

// ------------------------------------------------------------------ lineage
export interface LineageItem<T extends { id: number; parent_id: number | null }> {
  row: T;
  depth: number;
}

/** Depth-first order by id: each structure follows its parent (orphans and roots at depth 0). */
export function lineageOrder<T extends { id: number; parent_id: number | null }>(rows: readonly T[]): LineageItem<T>[] {
  const byId = new Map(rows.map((r) => [r.id, r]));
  const children = new Map<number, T[]>();
  const roots: T[] = [];
  for (const r of rows) {
    if (r.parent_id !== null && r.parent_id !== r.id && byId.has(r.parent_id)) {
      const list = children.get(r.parent_id) ?? [];
      list.push(r);
      children.set(r.parent_id, list);
    } else roots.push(r);
  }
  const out: LineageItem<T>[] = [];
  const seen = new Set<number>();
  const visit = (r: T, depth: number) => {
    if (seen.has(r.id)) return;
    seen.add(r.id);
    out.push({ row: r, depth });
    for (const c of (children.get(r.id) ?? []).sort((a, b) => a.id - b.id)) visit(c, depth + 1);
  };
  for (const r of [...roots].sort((a, b) => a.id - b.id)) visit(r, 0);
  for (const r of rows) visit(r, 0); // cycles (should not happen)
  return out;
}

// ------------------------------------------------------------------ table
export type ColumnKind = "number" | "bool" | "text";
export interface ColumnDef {
  key: string;
  label: string;
  unit?: string;
  kind: ColumnKind;
  digits?: number;
  /** Format in scientific notation (diffusion coefficients). */
  sci?: boolean;
}

/** Identity columns, always present. */
export const BASE_COLUMNS: readonly ColumnDef[] = [
  { key: "name", label: "Name", kind: "text" },
  { key: "formula", label: "Formula", kind: "text" },
  { key: "spacegroup", label: "Space group", kind: "text" },
  { key: "origin", label: "Origin", kind: "text" },
];

/** Known summary keys (§6.3) in display order. Unknown keys follow as generic columns. */
export const SUMMARY_COLUMNS: readonly ColumnDef[] = [
  { key: "energy_per_atom_ev", label: "E/atom", unit: "eV", kind: "number", digits: 4 },
  { key: "e_hull_ev", label: "E_hull", unit: "eV/atom", kind: "number", digits: 3 },
  { key: "formation_energy_ev", label: "E_form", unit: "eV/atom", kind: "number", digits: 3 },
  { key: "max_force_ev_a", label: "max |F|", unit: "eV/Å", kind: "number", digits: 3 },
  { key: "pressure_gpa", label: "P", unit: "GPa", kind: "number", digits: 2 },
  { key: "relax_converged", label: "Converged", kind: "bool" },
  { key: "steps", label: "Steps", kind: "number", digits: 0 },
  { key: "min_frequency_thz", label: "min ω", unit: "THz", kind: "number", digits: 2 },
  { key: "imaginary_mode_count", label: "Imaginary", kind: "number", digits: 0 },
  { key: "dynamically_stable", label: "Dyn. stable", kind: "bool" },
  { key: "supercell", label: "Supercell", kind: "text" },
  { key: "k_vrh_gpa", label: "K_VRH", unit: "GPa", kind: "number", digits: 1 },
  { key: "g_vrh_gpa", label: "G_VRH", unit: "GPa", kind: "number", digits: 1 },
  { key: "born_stable", label: "Born stable", kind: "bool" },
  { key: "k_gpa", label: "K_EOS", unit: "GPa", kind: "number", digits: 1 },
  { key: "v0", label: "V0", unit: "Å³", kind: "number", digits: 2 },
  { key: "n_peaks", label: "XRD peaks", kind: "number", digits: 0 },
  { key: "strongest_peak_two_theta", label: "Strongest 2θ", unit: "°", kind: "number", digits: 2 },
  { key: "temperature_k", label: "T (MD)", unit: "K", kind: "number", digits: 0 },
  // Superconductivity Tier 0 proxies (T_c,ref: Allen–Dynes at an assumed λ = 1, μ* = 0.10; a ranking scale, not a prediction).
  { key: "omega_log_k", label: "ω_log", unit: "K", kind: "number", digits: 0 },
  { key: "omega_2_k", label: "ω̄₂", unit: "K", kind: "number", digits: 0 },
  { key: "tc_ref_k", label: "T_c,ref", unit: "K", kind: "number", digits: 1 },
  { key: "light_element_fraction", label: "Light-el. frac.", kind: "number", digits: 2 },
  { key: "metallicity", label: "Metallicity", kind: "text" },
  { key: "sc_flags", label: "SC flags", kind: "text" },
  { key: "sc_gate_pass", label: "SC gate", kind: "bool" },
];

export interface TableRow {
  id: number;
  parentId: number | null;
  values: Record<string, SummaryValue>;
}

/** One row per structure: the identity fields plus the merged `latest` summaries. */
export function flattenRows(rows: readonly StructureListRow[]): TableRow[] {
  return rows.map((r) => ({
    id: r.id,
    parentId: r.parent_id,
    values: {
      ...r.latest,
      name: r.name,
      formula: r.formula,
      spacegroup: `${r.spacegroup_symbol} (#${r.spacegroup_number})`,
      origin: r.origin,
    },
  }));
}

function columnFor(key: string, sample: SummaryValue): ColumnDef {
  const diff = /^diffusion_(.+)_cm2_s$/.exec(key);
  if (diff) return { key, label: `D(${diff[1]})`, unit: "cm²/s", kind: "number", sci: true };
  const kind: ColumnKind = typeof sample === "boolean" ? "bool" : typeof sample === "number" ? "number" : "text";
  return { key, label: key.replace(/_/g, " "), kind, digits: 3 };
}

const isEmpty = (v: SummaryValue | undefined) => v === null || v === undefined || v === "";

/** Columns that hold at least one value: base columns, known summaries, then any other keys (alphabetical). */
export function visibleColumns(rows: readonly TableRow[]): ColumnDef[] {
  const known = new Set([...BASE_COLUMNS, ...SUMMARY_COLUMNS].map((c) => c.key));
  const has = (key: string) => rows.some((r) => !isEmpty(r.values[key]));
  const extra = new Map<string, SummaryValue>();
  for (const r of rows) for (const [k, v] of Object.entries(r.values)) if (!known.has(k) && !isEmpty(v) && !extra.has(k)) extra.set(k, v);
  return [
    ...BASE_COLUMNS.filter((c) => has(c.key)),
    ...SUMMARY_COLUMNS.filter((c) => has(c.key)),
    ...[...extra.keys()].sort().map((k) => columnFor(k, extra.get(k)!)),
  ];
}

/** Sort by a column; empty cells always last, ties by id. */
export function sortRows(rows: readonly TableRow[], key: string, dir: "asc" | "desc"): TableRow[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const va = a.values[key];
    const vb = b.values[key];
    const ea = isEmpty(va);
    const eb = isEmpty(vb);
    if (ea || eb) return ea === eb ? a.id - b.id : ea ? 1 : -1;
    let c: number;
    if (typeof va === "number" && typeof vb === "number") c = va - vb;
    else if (typeof va === "boolean" && typeof vb === "boolean") c = Number(va) - Number(vb);
    else c = String(va).localeCompare(String(vb), undefined, { numeric: true });
    return c === 0 ? a.id - b.id : sign * c;
  });
}

/** Display text of a cell ("—" when empty). */
export function formatCell(v: SummaryValue | undefined, col: ColumnDef): string {
  if (isEmpty(v)) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return "—";
    if (col.sci) return v.toExponential(2);
    return col.digits !== undefined ? v.toFixed(col.digits) : String(v);
  }
  return String(v);
}

export function columnTitle(col: ColumnDef): string {
  return col.unit ? `${col.label} (${col.unit})` : col.label;
}

/** Numeric columns (scatter axes). */
export function numericColumns(cols: readonly ColumnDef[]): ColumnDef[] {
  return cols.filter((c) => c.kind === "number");
}

// ------------------------------------------------------------------ dispersion overlay
/** Why two dispersions cannot share an axis, or null when their k-paths match (same labels at the same q indices). */
export function kpathMismatch(
  a: Pick<PhononAnalysisComplete, "kpath_labels" | "kpath_distances">,
  b: Pick<PhononAnalysisComplete, "kpath_labels" | "kpath_distances">,
): string | null {
  if (a.kpath_distances.length !== b.kpath_distances.length) {
    return `different k-path sampling (${a.kpath_distances.length} vs ${b.kpath_distances.length} q-points)`;
  }
  const la = a.kpath_labels.map((l) => `${l.index}:${l.label}`).join(",");
  const lb = b.kpath_labels.map((l) => `${l.index}:${l.label}`).join(",");
  if (la !== lb) return "different high-symmetry path";
  return null;
}
