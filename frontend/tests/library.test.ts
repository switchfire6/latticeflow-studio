import { describe, expect, it } from "vitest";
import type { StructureListRow } from "../src/types/library";
import { parsePattern } from "../src/utils/diffraction";
import { flattenRows, formatCell, kpathMismatch, lineageOrder, sortRows, visibleColumns } from "../src/utils/library";
import { buildMethodsText } from "../src/utils/methodsText";

const row = (id: number, parent: number | null, latest: StructureListRow["latest"]): StructureListRow => ({
  id,
  project_id: 1,
  name: `S${id}`,
  formula: "SrTiO3",
  n_atoms: 5,
  spacegroup_symbol: "Pm-3m",
  spacegroup_number: 221,
  content_hash: String(id),
  parent_id: parent,
  origin: parent ? "relax" : "import",
  notes: "",
  created_at: "",
  updated_at: "",
  latest,
  latest_result_ids: {},
});

describe("results table", () => {
  const rows = [
    row(1, null, { energy_per_atom_ev: -8.1, k_vrh_gpa: 170.2, born_stable: true, diffusion_Li_cm2_s: null }),
    row(2, 1, { energy_per_atom_ev: -8.2, min_frequency_thz: -2.3 }),
    row(3, null, {}),
  ];

  it("flattens latest summaries and hides empty columns", () => {
    const flat = flattenRows(rows);
    expect(flat[0]!.values).toMatchObject({ name: "S1", spacegroup: "Pm-3m (#221)", k_vrh_gpa: 170.2 });
    const keys = visibleColumns(flat).map((c) => c.key);
    expect(keys).toEqual(["name", "formula", "spacegroup", "origin", "energy_per_atom_ev", "min_frequency_thz", "k_vrh_gpa", "born_stable"]);
  });

  it("sorts with empty cells last and formats cells", () => {
    const flat = flattenRows(rows);
    expect(sortRows(flat, "energy_per_atom_ev", "asc").map((r) => r.id)).toEqual([2, 1, 3]);
    expect(sortRows(flat, "energy_per_atom_ev", "desc").map((r) => r.id)).toEqual([1, 2, 3]);
    const k = visibleColumns(flat).find((c) => c.key === "k_vrh_gpa")!;
    expect(formatCell(170.24, k)).toBe("170.2");
    expect(formatCell(null, k)).toBe("—");
  });

  it("orders the lineage depth-first and checks k-paths", () => {
    expect(lineageOrder(rows).map((i) => [i.row.id, i.depth])).toEqual([[1, 0], [2, 1], [3, 0]]);
    const a = { kpath_distances: [0, 1, 2], kpath_labels: [{ index: 0, label: "G" }, { index: 2, label: "X" }] };
    expect(kpathMismatch(a, a)).toBeNull();
    expect(kpathMismatch(a, { ...a, kpath_distances: [0, 1] })).toMatch(/sampling/);
  });
});

describe("experimental pattern parser", () => {
  it("reads two-column CSV with # comments", () => {
    const r = parsePattern("# exported\n2theta,I\n32.4,100\n22.8,40\n", "x.csv");
    expect(r.ok && r.pattern.twoTheta).toEqual([22.8, 32.4]);
    expect(r.ok && r.skipped).toBe(1);
  });
});

describe("methods text", () => {
  const model = { label: "MACE-MP-0 small", dtype: "float64" };
  it("describes only the results that exist, with citations", () => {
    const text = buildMethodsText({
      relax: { settings: { optimizer: "FIRE", fmax: 0.005, relax_cell: true, stress_tol_gpa: 0.05 }, steps: 35, converged: true, model },
      phonons: { settings: { displacement_distance: 0.01 }, supercell: [3, 3, 3], cellMode: "primitive", model },
    })!;
    expect(text).toContain("relaxed with FIRE");
    expect(text).toContain("max|F| < 0.005 eV/Å");
    expect(text).toContain("finite displacement (0.01 Å) in a 3×3×3 supercell");
    expect(text).toContain("MACE-MP-0 small (float64)");
    expect(text).toContain("phonopy (https://phonopy.github.io/phonopy/)");
    expect(text).not.toMatch(/elastic|hull|Materials Project/i);
    expect(buildMethodsText({})).toBeNull();
  });

  it("names the model per clause when models differ", () => {
    const text = buildMethodsText({
      eos: { settings: { strain_window: 0.03, n_points: 7 }, model },
      elastic: { settings: {}, model: { label: "MACE-MPA-0 medium", dispersion: true } },
    })!;
    expect(text).toContain("using MACE-MPA-0 medium (with D3(BJ) dispersion)");
    expect(text).toContain("±3 % in volume (7 points");
    expect(text).toContain("torch-dftd");
  });
});
