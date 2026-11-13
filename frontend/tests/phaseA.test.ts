/**
 * Phase A (IMPROVEMENT_SUGGESTIONS.md §3–§6): pure helpers, store reducers,
 * dispatcher handling of the new server events, session restore, settings and
 * the pure views.
 */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { buildFlowPayload, DEFAULT_FORM, maxFormulaUnits, previewForm } from "../src/components/hud/GeneratorSidebar";
import { EosReadouts, CLAMPED_ION_NOTE } from "../src/components/hud/EosPane";
import { imaginaryDosWarning } from "../src/components/hud/DosPane";
import { HullPopoverView } from "../src/components/hud/HullPopover";
import { exportOptions } from "../src/components/hud/ImportExport";
import { StructureInfoView } from "../src/components/hud/StructurePanel";
import { RelaxationBadge, stressTooltip } from "../src/components/hud/TelemetryHeader";
import { createApiClient, exportRequestBody } from "../src/services/apiClient";
import { availablePanes, showPane } from "../src/services/dockActions";
import { importStructureFile, importStructureText } from "../src/services/importActions";
import { SessionMemory, type StorageLike } from "../src/services/sessionMemory";
import {
  MAX_RESTORE_FAILURES,
  SESSION_RESTORED_TITLE,
  StudioClient,
  describeError,
  dispatchServerMessage,
  type WebSocketLike,
} from "../src/services/wsClient";
import { badgeCriteria, createSettingsStore, DEFAULT_RELAX, normalizeRelax, phononPayload, relaxPayload } from "../src/store/settings";
import { createCrystalStore, DEFAULT_DOCK, normalizeDock, type EosState } from "../src/store/useCrystalStore";
import type { BulkModulusResult, ClientCommand, CrystalStatePayload, HullEvaluateResponse, PhononDosData, ServerMessage } from "../src/types/protocol";
import { dosInUnit, eosPoints, extent, integrate, interpAt, SERIES_COLORS } from "../src/utils/chartData";
import { compositionGcd, compositionText, parseFormula, scaleComposition } from "../src/utils/formula";
import { shortcutFor } from "../src/utils/shortcuts";
import { formatFromFilename, hasFiles, sniffFormat } from "../src/utils/structureFile";
import {
  crystalSystemFromNumber,
  densityOf,
  formulaUnitsOf,
  latticeDelta,
  maxStressDeviation,
  phononWarning,
  pressureFromStress,
  pressureOf,
  relaxationVerdict,
  sameStructure,
  suggestSupercell,
} from "../src/utils/structureInfo";
import { convertFrequency, formatEnergyDelta, formatFrequency, formatPressure, normalizeUnits, THZ_TO_CM1, THZ_TO_MEV } from "../src/utils/units";
import { error, makeCrystal, makePhonon, phononDone, relaxFrame, spu, stopped } from "./fixtures";

// ------------------------------------------------------------------ helpers
/** A cubic SrTiO3-like cell under hydrostatic stress σ (GPa, ASE sign). */
function stressed(sigma: number, o: { maxForce?: number; shear?: number } = {}): CrystalStatePayload {
  const c = makeCrystal(5, { maxForce: o.maxForce ?? 0 });
  c.stress_gpa = [sigma, sigma, sigma, o.shear ?? 0, 0, 0];
  return c;
}

const history = (can_undo: boolean, can_redo: boolean, label = "Load preset"): ServerMessage => ({
  type: "HISTORY_STATE",
  payload: { can_undo, can_redo, position: 0, length: 1, label },
});

const BULK: BulkModulusResult = {
  k_gpa: 185.2,
  k_stress_gpa: 183.9,
  v0: 59.55,
  pressure_gpa: 5.1,
  v_eq: 61.4,
  k_eq_gpa: 161.0,
  k_eq_prime: 4.4,
  extrapolated: true,
  volumes: [60.7, 58.4, 59.55],
  energies_ev: [-39.9, -39.92, -39.95],
  pressures_gpa: [3.0, 7.3, 5.1],
  method: "clamped-ion, fixed shape, BM-3 fit over ±2 %",
};

const DOS: PhononDosData = {
  mesh: [8, 8, 8],
  frequencies_thz: [-1, 0, 1, 2, 3],
  total_dos: [0, 1, 2, 1, 0],
  projected_dos: { O: [0, 0.6, 1.2, 0.6, 0], Sr: [0, 0.2, 0.4, 0.2, 0], Ti: [0, 0.2, 0.4, 0.2, 0] },
  thermal: {
    temperatures_k: [0, 300, 600],
    free_energy_ev_per_atom: [0.05, 0.01, -0.06],
    entropy_kb_per_atom: [0, 2, 3],
    heat_capacity_kb_per_atom: [0, 2.5, 2.9],
    zero_point_energy_ev_per_atom: 0.05,
  },
  imaginary_fraction: 0.02,
};

class FakeSocket implements WebSocketLike {
  readyState = 0;
  sent: { type: ClientCommand; payload: Record<string, unknown> }[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = 3;
    this.onclose?.({});
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  deliver(msg: ServerMessage) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  drop() {
    this.readyState = 3;
    this.onclose?.({});
  }
}

function client(opts: { memory?: SessionMemory | null; autoDos?: boolean } = {}) {
  const store = createCrystalStore();
  const sockets: FakeSocket[] = [];
  const c = new StudioClient({
    store,
    url: "ws://t",
    createSocket: () => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    },
    random: () => 1,
    memory: opts.memory ?? null,
    autoDos: () => opts.autoDos ?? false,
  });
  c.connect();
  sockets[0]!.open();
  const sock = () => sockets[sockets.length - 1]!;
  const deliver = (...m: ServerMessage[]) => {
    for (const x of m) sock().deliver(x);
    store.getState().flushHud();
  };
  return { store, client: c, sockets, sock, deliver };
}

function memStorage(): StorageLike & { map: Map<string, string> } {
  const map = new Map<string, string>();
  return { map, getItem: (k) => map.get(k) ?? null, setItem: (k, v) => void map.set(k, v), removeItem: (k) => void map.delete(k) };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ------------------------------------------------------------------ units
describe("units", () => {
  it("converts THz to cm⁻¹ and meV, GPa to kbar", () => {
    expect(THZ_TO_CM1).toBeCloseTo(33.35641, 4);
    expect(THZ_TO_MEV).toBeCloseTo(4.135667, 5);
    expect(convertFrequency(1, "THz")).toBe(1);
    expect(convertFrequency(-2.5, "cm-1")).toBeCloseTo(-83.39, 2);
    expect(formatFrequency(-2.5, "THz")).toBe("−2.50 THz");
    expect(formatFrequency(-2.5, "cm-1")).toBe("−83 cm⁻¹");
    expect(formatFrequency(15.5, "meV")).toBe("64.1 meV");
    expect(formatPressure(5.1, "GPa")).toBe("+5.10 GPa");
    expect(formatPressure(5.1, "kbar")).toBe("+51.0 kbar");
    expect(formatPressure(-0.3, "GPa", 2, { sign: false })).toBe("−0.30 GPa");
    expect(formatPressure(0.0001, "GPa")).toBe("0.00 GPa");
  });

  it("formats ΔE per atom and per formula unit", () => {
    expect(formatEnergyDelta(-0.0144, 5)).toBe("−14.4 meV/atom · −72.0 meV/f.u.");
    expect(formatEnergyDelta(0.002, 1)).toBe("+2.0 meV/atom");
    expect(formatEnergyDelta(0.002, null)).toBe("+2.0 meV/atom");
  });

  it("sanitises stored preferences", () => {
    expect(normalizeUnits({ frequency: "cm-1", pressure: "kbar" })).toEqual({ frequency: "cm-1", pressure: "kbar" });
    expect(normalizeUnits({ frequency: "Hz" as never })).toEqual({ frequency: "THz", pressure: "GPa" });
    expect(normalizeUnits(null)).toEqual({ frequency: "THz", pressure: "GPa" });
  });
});

// ------------------------------------------------------------------ formula
describe("formula parser", () => {
  const comp = (f: string) => {
    const p = parseFormula(f);
    return p.ok ? p.composition : p.error;
  };
  it("parses plain formulas in order of appearance", () => {
    expect(comp("SrTiO3")).toEqual({ Sr: 1, Ti: 1, O: 3 });
    expect(Object.keys(comp("Li2MnO3") as object)).toEqual(["Li", "Mn", "O"]);
    expect(comp("Li2MnO3")).toEqual({ Li: 2, Mn: 1, O: 3 });
    expect(comp(" Li3 P S4 ")).toEqual({ Li: 3, P: 1, S: 4 });
    expect(comp("CO")).toEqual({ C: 1, O: 1 });
    expect(comp("Co")).toEqual({ Co: 1 });
    expect(comp("NaCl")).toEqual({ Na: 1, Cl: 1 });
    expect(comp("SiO2Si")).toEqual({ Si: 2, O: 2 });
  });

  it("handles nested parentheses and brackets", () => {
    expect(comp("Ca3(PO4)2")).toEqual({ Ca: 3, P: 2, O: 8 });
    expect(comp("[Co(NH3)6]Cl3")).toEqual({ Co: 1, N: 6, H: 18, Cl: 3 });
    const p = parseFormula("Ca3(PO4)2");
    expect(p.ok && p.atoms).toBe(13);
  });

  it("reports errors", () => {
    expect(comp("")).toMatch(/empty/);
    expect(comp("Xx2")).toMatch(/unknown element/);
    expect(comp("sr")).toMatch(/capital/);
    expect(comp("Ca3(PO4")).toMatch(/unbalanced/);
    expect(comp("CaPO4)2")).toMatch(/unbalanced/);
    expect(comp("Li0.5CoO2")).toMatch(/fractional/);
    expect(comp("Fe0")).toMatch(/positive/);
    expect(comp("Th")).toMatch(/unknown element/); // beyond Z = 89 coverage
    expect(comp("Si-O")).toMatch(/unexpected/);
  });

  it("scales, reduces and prints compositions", () => {
    expect(scaleComposition({ Sr: 1, Ti: 1, O: 3 }, 2)).toEqual({ Sr: 2, Ti: 2, O: 6 });
    expect(compositionText({ Sr: 2, Ti: 2, O: 6 })).toBe("Sr2Ti2O6");
    expect(compositionGcd([8, 8, 24])).toBe(8);
    expect(compositionGcd([3, 1, 4])).toBe(1);
    expect(compositionGcd([])).toBe(1);
  });
});

// ------------------------------------------------------------------ structure info
describe("stress-aware relaxation verdict", () => {
  it("computes pressure from the Voigt stress (+ = compressed)", () => {
    expect(pressureFromStress([-5.1, -5.1, -5.1, 0, 0, 0])).toBeCloseTo(5.1);
    expect(pressureFromStress([])).toBeNull();
    expect(pressureOf({ stress_gpa: [-1, -1, -1, 0, 0, 0], pressure_gpa: 2 })).toBe(2);
    expect(pressureOf({ stress_gpa: [-1, -2, -3, 0, 0, 0] })).toBeCloseTo(2);
    expect(maxStressDeviation([-5, -5, -5, 0.3, 0, 0], 5)).toBeCloseTo(0.3);
    expect(maxStressDeviation([1, 1], 0)).toBeNull();
  });

  it("is tri-state: relaxed / forces ok but cell stressed / unrelaxed", () => {
    expect(relaxationVerdict(stressed(-0.02)).state).toBe("relaxed");
    const st = relaxationVerdict(stressed(-5.1));
    expect(st.state).toBe("stressed");
    expect(st.pressureGpa).toBeCloseTo(5.1);
    expect(st.maxStressDeviation).toBeCloseTo(5.1);
    expect(relaxationVerdict(stressed(-0.02, { shear: 0.4 })).state).toBe("stressed");
    expect(relaxationVerdict(stressed(-5.1, { maxForce: 1.3 })).state).toBe("unrelaxed");
    expect(relaxationVerdict(stressed(0, { maxForce: 0.02 })).state).toBe("unrelaxed"); // strict <
    // relaxed at an external pressure: σ = −P_ext I
    expect(relaxationVerdict(stressed(-5.0), { targetPressureGpa: 5 }).state).toBe("relaxed");
    expect(relaxationVerdict(stressed(0), { targetPressureGpa: 5 }).state).toBe("stressed");
    // payload without a full stress tensor: forces only
    const noStress = makeCrystal(5, { maxForce: 0 });
    noStress.stress_gpa = [];
    expect(relaxationVerdict(noStress).state).toBe("relaxed");
  });

  it("uses the same test for the phonon warning, with a clear message", () => {
    expect(phononWarning(relaxationVerdict(stressed(0)))).toBeNull();
    expect(phononWarning(relaxationVerdict(stressed(-5.1)))).toMatch(/^cell under \+5\.1 GPa — relax the cell first/);
    expect(phononWarning(relaxationVerdict(stressed(-5.1), { targetPressureGpa: 2 }))).toMatch(/\+5\.1 GPa \(target \+2\.0 GPa\)/);
    expect(phononWarning(relaxationVerdict(stressed(0, { shear: 0.5 })))).toMatch(/shear/);
    expect(phononWarning(relaxationVerdict(stressed(0, { maxForce: 0.3 })))).toMatch(/max \|F\| = 0\.300/);
  });

  it("derives the badge criteria from the relaxation settings", () => {
    expect(badgeCriteria(DEFAULT_RELAX)).toEqual({ fmax: 0.02, stressTolGpa: 0.1, targetPressureGpa: 0 });
    expect(badgeCriteria({ ...DEFAULT_RELAX, stressTolGpa: 0.5, pressureGpa: 3 })).toEqual({ fmax: 0.02, stressTolGpa: 0.5, targetPressureGpa: 3 });
  });
});

describe("structure fallbacks and changes", () => {
  it("density, crystal system and Z", () => {
    const c = makeCrystal(5, { a: 3.905 });
    c.atoms = c.atoms.map((a, i) => ({ ...a, element: ["Sr", "Ti", "O", "O", "O"][i]!, atomic_number: [38, 22, 8, 8, 8][i]! }));
    expect(densityOf(c)).toBeCloseTo(5.12, 2); // SrTiO3 at a = 3.905 Å
    expect(densityOf({ ...c, density_g_cm3: 4.2 })).toBe(4.2);
    expect([1, 2, 3, 15, 16, 74, 75, 142, 143, 167, 168, 194, 195, 230].map(crystalSystemFromNumber)).toEqual([
      "triclinic", "triclinic", "monoclinic", "monoclinic", "orthorhombic", "orthorhombic", "tetragonal", "tetragonal",
      "trigonal", "trigonal", "hexagonal", "hexagonal", "cubic", "cubic",
    ]);
    expect(crystalSystemFromNumber(0)).toBeNull();
    expect(formulaUnitsOf(c)).toBe(1);
    const big = makeCrystal(8, { elements: ["Si", "Si", "Si", "Si", "Si", "Si", "Si", "Si"] });
    expect(formulaUnitsOf(big)).toBe(8);
    expect(formulaUnitsOf({ ...big, formula_units: 2 })).toBe(2);
  });

  it("lattice changes are relative, and only V/atom for a different cell", () => {
    const ref = { lattice: { a: 3.905, b: 3.905, c: 3.905, volume: 3.905 ** 3 }, nAtoms: 5 };
    const d = latticeDelta(ref, { lattice: { a: 3.944, b: 3.944, c: 3.944, volume: 3.944 ** 3 }, nAtoms: 5 });
    expect(d.a).toBeCloseTo(1.0, 1);
    expect(d.volume).toBeCloseTo(3.03, 1);
    const sc = latticeDelta(ref, { lattice: { a: 7.8, b: 7.8, c: 7.8, volume: 8 * 3.905 ** 3 * 0.99 }, nAtoms: 40 });
    expect(sc.a).toBeNull();
    expect(sc.volume).toBeNull();
    expect(sc.volumePerAtom).toBeCloseTo(-1, 5);
  });

  it("suggests the supercell: n_i·|a_i| ≥ 10 Å within ~500 atoms", () => {
    expect(suggestSupercell([3.905, 3.905, 3.905], 5)).toEqual([3, 3, 3]);
    expect(suggestSupercell([5.431, 5.431, 5.431], 8)).toEqual([2, 2, 2]);
    expect(suggestSupercell([10.5, 10.5, 10.5], 40)).toEqual([1, 1, 1]);
    expect(suggestSupercell([3, 3, 12], 2)).toEqual([4, 4, 1]);
    // 64 atoms in a 4 Å cube: 3x3x3 = 1728 > 500, shrink the longest supercell vectors first
    const n = suggestSupercell([4, 4, 4], 64);
    expect(n[0] * n[1] * n[2] * 64).toBeLessThanOrEqual(500);
    expect(n.every((x) => x >= 1)).toBe(true);
    expect(suggestSupercell([2, 2, 2], 600)).toEqual([1, 1, 1]); // cannot shrink below 1
  });

  it("compares structures atom by atom", () => {
    const a = makeCrystal(5);
    expect(sameStructure(a, makeCrystal(5))).toBe(true);
    expect(sameStructure(a, makeCrystal(5, { move: { index: 1, cart: [1, 1, 1] } }))).toBe(false);
    expect(sameStructure(a, makeCrystal(5, { a: 4.1 }))).toBe(false);
    expect(sameStructure(a, makeCrystal(4))).toBe(false);
    expect(sameStructure(a, null)).toBe(false);
  });
});

// ------------------------------------------------------------------ files
describe("structure files", () => {
  it("maps file names to text formats", () => {
    expect(formatFromFilename("SrTiO3.cif")).toBe("cif");
    expect(formatFromFilename("POSCAR")).toBe("poscar");
    expect(formatFromFilename("CONTCAR")).toBe("poscar");
    expect(formatFromFilename("POSCAR_relaxed")).toBe("poscar");
    expect(formatFromFilename("C:\\runs\\x\\BaTiO3.vasp")).toBe("poscar");
    expect(formatFromFilename("traj.extxyz")).toBe("extxyz");
    expect(formatFromFilename("frame.XYZ")).toBe("extxyz");
    expect(formatFromFilename("structure.txt")).toBe("auto");
  });

  it("sniffs pasted text and detects file drags", () => {
    expect(sniffFormat("data_SrTiO3\n_cell_length_a 3.905")).toBe("cif");
    expect(sniffFormat('5\nLattice="3.9 0 0 0 3.9 0 0 0 3.9" Properties=species:S:1:pos:R:3\nSr 0 0 0')).toBe("extxyz");
    expect(sniffFormat("SrTiO3\n1.0\n3.9 0 0\n")).toBe("auto");
    expect(hasFiles(["text/plain", "Files"])).toBe(true);
    expect(hasFiles(["text/plain"])).toBe(false);
    expect(hasFiles(null)).toBe(false);
  });

  it("imports a file as LOAD_STRUCTURE text, refusing empty and huge files", async () => {
    const sent: [string, string][] = [];
    const notes: string[] = [];
    const load = (t: string, f: string) => (sent.push([t, f]), true);
    const notify = (_l: string, title: string) => void notes.push(title);
    const file = (name: string, text: string, size = text.length) => ({ name, size, text: async () => text });
    expect(await importStructureFile(file("CONTCAR", "Si\n1.0\n..."), load, notify)).toBe("poscar");
    expect(await importStructureFile(file("x.dat", "data_x\n_cell_length_a 4"), load, notify)).toBe("cif"); // sniffed
    expect(await importStructureFile(file("e.cif", "   "), load, notify)).toBeNull();
    expect(await importStructureFile(file("big.cif", "x", 6e6), load, notify)).toBeNull();
    expect(sent.map((s) => s[1])).toEqual(["poscar", "cif"]);
    expect(notes).toContain("Empty file");
    expect(notes).toContain("File too large");
    expect(importStructureText("data_y", "auto", load)).toBe("cif");
    expect(importStructureText("Si\n1.0", "poscar", load)).toBe("poscar");
    expect(importStructureText("  ", "auto", load)).toBeNull();
  });
});

// ------------------------------------------------------------------ settings
describe("settings", () => {
  it("builds START_RELAXATION and RUN_PHONON_SHAKE_TEST payloads", () => {
    expect(relaxPayload(DEFAULT_RELAX)).toEqual({
      fmax: 0.02,
      max_steps: 100,
      relax_cell: true,
      fix_symmetry: false,
      pressure_gpa: 0,
      stress_tol_gpa: 0.05,
      optimizer: "FIRE",
    });
    expect(relaxPayload({ ...DEFAULT_RELAX, relaxCell: false }).stress_tol_gpa).toBeNull();
    expect(phononPayload({ supercell: null, displacement: 0.01, autoDos: true }, [3.905, 3.905, 3.905], 5)).toEqual({
      supercell_dim: [3, 3, 3],
      displacement_distance: 0.01,
    });
    expect(phononPayload({ supercell: [2, 2, 1], displacement: 0.02, autoDos: true }, [3.9, 3.9, 3.9], 5).supercell_dim).toEqual([2, 2, 1]);
  });

  it("sanitises and persists (surviving broken storage)", () => {
    expect(normalizeRelax({ fmax: -1, maxSteps: 1e9, optimizer: "SGD" as never })).toMatchObject({ fmax: 0.0001, maxSteps: 5000, optimizer: "FIRE" });
    const mem = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) });
    const a = createSettingsStore({ storageKey: "s" });
    a.getState().setRelax({ optimizer: "BFGS", pressureGpa: 2 });
    a.getState().setUnits({ frequency: "cm-1" });
    a.getState().setPhonon({ supercell: [3, 3, 2] });
    a.getState().setCollapsed("structure-info", true);
    const b = createSettingsStore({ storageKey: "s" });
    expect(b.getState().relax).toMatchObject({ optimizer: "BFGS", pressureGpa: 2, fmax: 0.02 });
    expect(b.getState().units.frequency).toBe("cm-1");
    expect(b.getState().phonon.supercell).toEqual([3, 3, 2]);
    expect(b.getState().collapsed["structure-info"]).toBe(true);
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("quota");
      },
    });
    const c = createSettingsStore({ storageKey: "s" });
    expect(c.getState().relax).toEqual(DEFAULT_RELAX);
    expect(() => c.getState().setRelax({ fmax: 0.01 })).not.toThrow();
  });
});

// ------------------------------------------------------------------ store
describe("store (Phase A)", () => {
  it("merges an old dock layout with the new panes", () => {
    const d = normalizeDock({ open: false, height: 250, hidden: ["rdf", "nope" as never], weights: { dispersion: 2, rdf: 1, relaxation: 1 } as never });
    expect(d).toMatchObject({ open: false, height: 250, hidden: ["rdf", "dos"] }); // legacy layout: DOS hidden like a fresh default
    expect(d.weights).toEqual({ ...DEFAULT_DOCK.weights, dispersion: 2, rdf: 1, relaxation: 1 });
    expect(normalizeDock(null)).toEqual(DEFAULT_DOCK);
    expect(normalizeDock({ ...DEFAULT_DOCK, hidden: [] }).hidden).toEqual([]); // a current layout keeps the user's choice
  });

  it("showPane opens a pane and hides g(r) when the dock is full", () => {
    const store = createCrystalStore();
    dispatchServerMessage(store, spu(makeCrystal(5)));
    dispatchServerMessage(store, phononDone(makePhonon(5)));
    store.getState().pushRelaxPoint(1, { step: 0, energyPerAtom: -7, maxForce: 0.1 });
    store.getState().flushHud();
    expect(availablePanes(store.getState())).toEqual(["dispersion", "dos", "rdf", "relaxation"]);
    expect(store.getState().dock.hidden).toEqual(["dos"]);
    showPane("dos", store); // dispersion, rdf, relaxation already visible
    expect(store.getState().dock.hidden).toEqual(["rdf"]);
    const roomy = createCrystalStore();
    dispatchServerMessage(roomy, spu(makeCrystal(5)));
    roomy.getState().setEosPending(true);
    showPane("eos", roomy); // only g(r) visible: nothing to hide
    expect(roomy.getState().dock.hidden).toEqual(["dos"]);
  });

  it("patchCrystal does not count as a structure change; revealPane opens a hidden pane", () => {
    const store = createCrystalStore();
    dispatchServerMessage(store, spu(makeCrystal(5)));
    store.getState().flushHud();
    const v = store.getState().stateVersion;
    store.getState().patchCrystal({ bulk_modulus_gpa: 100 });
    expect(store.getState().stateVersion).toBe(v);
    expect(store.getState().crystal?.bulk_modulus_gpa).toBe(100);
    store.getState().setDock({ open: false, hidden: ["eos"] });
    store.getState().revealPane("eos");
    expect(store.getState().dock).toMatchObject({ open: true, hidden: [] });
  });

  it("toasts can carry a serializable action", () => {
    const store = createCrystalStore();
    store.getState().pushToast("info", "t", "m", { kind: "continue-relaxation", label: "Continue relaxation" });
    expect(store.getState().toasts[0]?.action?.kind).toBe("continue-relaxation");
  });
});

// ------------------------------------------------------------------ dispatcher
describe("dispatcher: Phase A events", () => {
  it("HISTORY_STATE drives undo/redo availability; reset on reconnect", () => {
    const { store, deliver, sockets } = client();
    deliver(spu(makeCrystal(5)), history(true, false, "Drag atom 2"));
    expect(store.getState().history).toMatchObject({ can_undo: true, can_redo: false, label: "Drag atom 2" });
    sockets[0]!.drop();
    expect(store.getState().history).toBeNull();
  });

  it("UNDO/REDO are plain commands; 'nothing to undo' is an info toast", () => {
    const { store, client: c, sock, deliver } = client();
    deliver(spu(makeCrystal(5)));
    c.send("UNDO", {});
    expect(sock().sent.at(-1)?.type).toBe("UNDO");
    expect(store.getState().streams).toHaveLength(0);
    deliver(error("INVALID_OPERATION", "UNDO", "nothing to undo"));
    expect(store.getState().toasts.at(-1)).toMatchObject({ level: "info", title: "Nothing to undo" });
    expect(describeError({ code: "INVALID_OPERATION", message: "", command: "REDO" }).title).toBe("Nothing to redo");
  });

  it("UNDO during a stream: STREAM_STOPPED, SINGLE_POINT_UPDATE, HISTORY_STATE", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5)));
    c.send("START_RELAXATION", {});
    deliver(relaxFrame(0, makeCrystal(5)));
    c.send("UNDO", {});
    deliver(stopped("START_RELAXATION"), spu(makeCrystal(5, { a: 4.2 })), history(false, true));
    const s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.crystal?.lattice.a).toBe(4.2);
    expect(s.history?.can_redo).toBe(true);
  });

  it("BULK_MODULUS_RESULT patches K and stores the EOS, stale after a structure change", () => {
    const store = createCrystalStore();
    dispatchServerMessage(store, spu(makeCrystal(5)));
    store.getState().setEosPending(true);
    dispatchServerMessage(store, { type: "BULK_MODULUS_RESULT", payload: BULK });
    store.getState().flushHud();
    let s = store.getState();
    expect(s.eosPending).toBe(false);
    expect(s.crystal?.bulk_modulus_gpa).toBe(185.2);
    expect(s.eos?.result.k_eq_gpa).toBe(161);
    expect(s.eos?.nAtoms).toBe(5);
    expect(s.eos?.stateVersion).toBe(s.stateVersion);
    // the phonon actor-flag update keeps it fresh, a drag makes it stale
    dispatchServerMessage(store, phononDone(makePhonon(5)));
    dispatchServerMessage(store, spu(makeCrystal(5, { actors: [1] })));
    store.getState().flushHud();
    expect(store.getState().eos?.stateVersion).toBe(store.getState().stateVersion);
    dispatchServerMessage(store, spu(makeCrystal(5, { move: { index: 1, cart: [1, 1, 1] } })));
    store.getState().flushHud();
    s = store.getState();
    expect(s.eos!.stateVersion).not.toBe(s.stateVersion);
  });

  it("a connection starts with SINGLE_POINT_UPDATE then HISTORY_STATE (Load preset srtio3)", () => {
    const { store, deliver } = client();
    deliver(spu(makeCrystal(5)), history(false, false, "Load preset srtio3"));
    expect(store.getState().history).toMatchObject({ can_undo: false, label: "Load preset srtio3" });
    expect(store.getState().crystal?.formula).toBe("SrTiO3");
  });

  it("a BULK_MODULUS_RESULT with k_gpa = null clears K and keeps the reason", () => {
    const store = createCrystalStore();
    const c = makeCrystal(5);
    c.bulk_modulus_gpa = 120;
    dispatchServerMessage(store, spu(c));
    dispatchServerMessage(store, { type: "BULK_MODULUS_RESULT", payload: { ...BULK, k_gpa: null, method: "fit not meaningful: E(V) not convex" } });
    store.getState().flushHud();
    expect(store.getState().crystal?.bulk_modulus_gpa).toBeNull();
    const eos: EosState = store.getState().eos!;
    const html = renderToStaticMarkup(createElement(EosReadouts, { eos, stale: false, pressureUnit: "GPa" }));
    expect(html).toContain("not convex");
  });

  it("explains INVALID_ELEMENT on load", () => {
    expect(describeError({ code: "INVALID_ELEMENT", message: "Pu (Z=94)", command: "LOAD_STRUCTURE" })).toMatchObject({ title: "Element not supported" });
  });

  it("COMPUTE_BULK_MODULUS / REQUEST_PHONON_DOS errors clear the pending flags", () => {
    const store = createCrystalStore();
    store.getState().setEosPending(true);
    store.getState().setDosPending(true);
    dispatchServerMessage(store, error("COMPUTE_FAILED", "COMPUTE_BULK_MODULUS", "fit failed"));
    dispatchServerMessage(store, error("NO_PHONON_ANALYSIS", "REQUEST_PHONON_DOS"));
    const s = store.getState();
    expect(s.eosPending).toBe(false);
    expect(s.dosPending).toBe(false);
    expect(s.toasts.map((t) => t.title)).toEqual(["Bulk modulus failed", "Phonons out of date"]);
  });

  it("PHONON_DOS_DATA is kept with its analysis and dropped with it", () => {
    const store = createCrystalStore();
    dispatchServerMessage(store, spu(makeCrystal(5)));
    dispatchServerMessage(store, { type: "PHONON_DOS_DATA", payload: DOS }); // no analysis: ignored
    expect(store.getState().dos).toBeNull();
    dispatchServerMessage(store, phononDone(makePhonon(5)));
    dispatchServerMessage(store, spu(makeCrystal(5, { actors: [2] })));
    store.getState().setDosPending(true);
    dispatchServerMessage(store, { type: "PHONON_DOS_DATA", payload: DOS });
    expect(store.getState().dos?.mesh).toEqual([8, 8, 8]);
    expect(store.getState().dosPending).toBe(false);
    dispatchServerMessage(store, spu(makeCrystal(5, { move: { index: 0, cart: [0.5, 0, 0] } })));
    expect(store.getState().dos).toBeNull();
  });

  it("remembers where a relaxation started (Δa) and clears it on the next edit", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5, { a: 3.905 })));
    c.send("START_RELAXATION", {});
    deliver(relaxFrame(0, makeCrystal(5, { a: 3.905 })), relaxFrame(5, makeCrystal(5, { a: 3.944 }), true, true));
    const ref = store.getState().latticeRef;
    expect(ref).toMatchObject({ kind: "relaxation", nAtoms: 5, lattice: { a: 3.905 } });
    deliver(spu(makeCrystal(5, { a: 3.944, move: { index: 1, cart: [1, 1, 1] } })));
    expect(store.getState().latticeRef).toBeNull();
  });

  it("Follow Instability compares against the parent cell", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5, { a: 3.9 })));
    c.send("FOLLOW_INSTABILITY", { q_index: 0, band_index: 0 });
    deliver(relaxFrame(0, makeCrystal(8, { a: 7.8 })));
    expect(store.getState().latticeRef).toMatchObject({ kind: "follow", nAtoms: 5, lattice: { a: 3.9 } });
  });

  it("an unconverged relaxation offers 'Continue relaxation' until the structure changes", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5)));
    c.send("START_RELAXATION", {});
    deliver(relaxFrame(0, makeCrystal(5)), relaxFrame(100, makeCrystal(5, { maxForce: 0.2 }), true, false));
    let s = store.getState();
    expect(s.relaxUnconverged).toEqual({ steps: 100, maxForce: 0.2 });
    expect(s.toasts.at(-1)?.action).toEqual({ kind: "continue-relaxation", label: "Continue relaxation" });
    c.send("START_RELAXATION", {});
    deliver(relaxFrame(0, makeCrystal(5)));
    expect(store.getState().relaxUnconverged).toBeNull();
    deliver(relaxFrame(10, makeCrystal(5), true, true));
    s = store.getState();
    expect(s.relaxUnconverged).toBeNull();
    // a user stop is not "unconverged"
    c.send("START_RELAXATION", {});
    c.send("STOP_STREAM", {});
    deliver(relaxFrame(0, makeCrystal(5)), relaxFrame(3, makeCrystal(5), true, false));
    expect(store.getState().relaxUnconverged).toBeNull();
  });

  it("records the phonon supercell for the progress text", () => {
    const { store, client: c } = client();
    c.send("RUN_PHONON_SHAKE_TEST", { supercell_dim: [3, 3, 3] });
    expect(store.getState().session.phononSupercell).toEqual([3, 3, 3]);
  });
});

// ------------------------------------------------------------------ client hooks
describe("StudioClient: auto DOS", () => {
  it("requests the DOS right after PHONON_ANALYSIS_COMPLETE when enabled", () => {
    const on = client({ autoDos: true });
    on.deliver(spu(makeCrystal(5)));
    on.client.send("RUN_PHONON_SHAKE_TEST", {});
    on.deliver(phononDone(makePhonon(5)));
    expect(on.sock().sent.at(-1)).toEqual({ type: "REQUEST_PHONON_DOS", payload: {} });
    expect(on.store.getState().dosPending).toBe(true);
    on.deliver(spu(makeCrystal(5, { actors: [2] })), { type: "PHONON_DOS_DATA", payload: DOS });
    expect(on.store.getState().phonon).not.toBeNull(); // the flagged update still does not invalidate
    expect(on.store.getState().dos).not.toBeNull();

    const off = client({ autoDos: false });
    off.client.send("RUN_PHONON_SHAKE_TEST", {});
    off.deliver(phononDone(makePhonon(5)));
    expect(off.sock().sent.map((m) => m.type)).toEqual(["RUN_PHONON_SHAKE_TEST"]);
  });
});

describe("session restore", () => {
  const user = () => makeCrystal(5, { a: 4.2, formula: "BaTiO3" });
  const dflt = () => makeCrystal(5);

  function withMemory(storage = memStorage()) {
    const memory = new SessionMemory({ storageKey: "sess", storage, saveDelayMs: 10 });
    return { memory, storage, ...client({ memory }) };
  }

  it("remembers committed structures and re-sends the last one after a reconnect", () => {
    vi.useFakeTimers();
    const { memory, store, sockets, deliver, sock } = withMemory();
    deliver(spu(dflt()), history(false, false)); // first connect, nothing remembered
    expect(sock().sent).toHaveLength(0);
    deliver(spu(user())); // user loads BaTiO3
    expect(memory.crystal?.formula).toBe("BaTiO3");
    sockets[0]!.drop();
    vi.advanceTimersByTime(1000);
    sock().open();
    deliver(spu(dflt()));
    expect(sock().sent).toEqual([{ type: "LOAD_STRUCTURE", payload: { crystal: expect.objectContaining({ formula: "BaTiO3" }) } }]);
    expect(memory.crystal?.formula).toBe("BaTiO3"); // the default auto-load is not remembered
    deliver(history(true, false), spu(user()), history(true, false));
    expect(store.getState().crystal?.formula).toBe("BaTiO3");
    expect(store.getState().toasts.at(-1)?.title).toBe(SESSION_RESTORED_TITLE);
    expect(store.getState().toasts.at(-1)?.message).toMatch(/phonons need re-running/);
    // no loop: later updates do not re-send
    deliver(spu(user()));
    expect(sock().sent).toHaveLength(1);
  });

  it("does nothing when the remembered structure is the default", () => {
    vi.useFakeTimers();
    const { sockets, deliver, sock } = withMemory();
    deliver(spu(dflt()));
    sockets[0]!.drop();
    vi.advanceTimersByTime(1000);
    sock().open();
    deliver(spu(dflt()));
    expect(sock().sent).toHaveLength(0);
  });

  it("does not fight a user who loads a preset right after connecting", () => {
    vi.useFakeTimers();
    const { memory, client: c, sockets, deliver, sock } = withMemory();
    deliver(spu(user()));
    sockets[0]!.drop();
    vi.advanceTimersByTime(1000);
    sock().open();
    c.send("LOAD_STRUCTURE", { preset_id: "si" }); // before the auto-load arrives
    deliver(spu(dflt()), spu(makeCrystal(8, { formula: "Si" })));
    expect(sock().sent.map((m) => m.type)).toEqual(["LOAD_STRUCTURE"]);
    expect(sock().sent[0]!.payload).toEqual({ preset_id: "si" });
    expect(memory.crystal?.formula).toBe("Si");
  });

  it("a refused restore is dropped instead of retried", () => {
    vi.useFakeTimers();
    const { memory, store, sockets, deliver, sock } = withMemory();
    deliver(spu(user()));
    sockets[0]!.drop();
    vi.advanceTimersByTime(1000);
    sock().open();
    deliver(spu(dflt()), error("INVALID_STRUCTURE", "LOAD_STRUCTURE", "bad cell"));
    expect(store.getState().toasts.some((t) => t.title === "Session not restored")).toBe(true);
    expect(memory.crystal?.formula).toBe("SrTiO3"); // adopted the server's structure
    sock().drop();
    vi.advanceTimersByTime(5000);
    sock().open();
    deliver(spu(dflt()));
    expect(sock().sent).toHaveLength(0);
  });

  it(`gives up after ${MAX_RESTORE_FAILURES} connections die during the restore`, () => {
    vi.useFakeTimers();
    const { memory, sockets, deliver, sock } = withMemory();
    deliver(spu(user()));
    sockets[0]!.drop();
    for (let i = 0; i < MAX_RESTORE_FAILURES; i++) {
      vi.advanceTimersByTime(20_000);
      sock().open();
      deliver(spu(dflt()));
      expect(sock().sent.map((m) => m.type)).toEqual(["LOAD_STRUCTURE"]);
      sock().drop();
    }
    expect(memory.crystal).toBeNull();
  });

  it("persists across page reloads (localStorage) and survives broken storage", () => {
    vi.useFakeTimers();
    const storage = memStorage();
    const a = new SessionMemory({ storageKey: "k", storage, saveDelayMs: 50 });
    a.remember(user());
    expect(storage.map.size).toBe(0); // debounced
    vi.advanceTimersByTime(60);
    expect(new SessionMemory({ storageKey: "k", storage }).crystal?.formula).toBe("BaTiO3");
    storage.setItem("k", "{not json");
    expect(new SessionMemory({ storageKey: "k", storage }).crystal).toBeNull();
    const broken: StorageLike = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("quota");
      },
    };
    const b = new SessionMemory({ storageKey: "k", storage: broken });
    expect(() => {
      b.remember(user());
      b.flush();
      b.forget();
    }).not.toThrow();
  });
});

// ------------------------------------------------------------------ REST
describe("REST: export and hull", () => {
  it("builds export bodies (symmetrised CIF by default, extxyz as displayed)", () => {
    const c = makeCrystal(2);
    expect(exportRequestBody(c, { format: "cif" })).toMatchObject({ format: "cif", symmetrize: true, cell: "as_is" });
    expect(exportRequestBody(c, exportOptions("cif-p1", "primitive"))).toMatchObject({ format: "cif", symmetrize: false, cell: "primitive" });
    expect(exportRequestBody(c, exportOptions("cif-sym", "conventional", 0.05))).toMatchObject({ symmetrize: true, symprec: 0.05, cell: "conventional" });
    const x = exportRequestBody(c, exportOptions("extxyz", "conventional"));
    expect(x).toMatchObject({ format: "extxyz", cell: "as_is" });
    expect("symmetrize" in x).toBe(false);
    expect(exportRequestBody(c, exportOptions("poscar", "primitive"))).toMatchObject({ format: "poscar", cell: "primitive" });
  });

  it("sends the export options and the hull relax flag", async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      calls.push({ url, body: JSON.parse(init.body as string) });
      if (url.endsWith("/hull/evaluate")) return new Response(JSON.stringify({}), { status: 200 });
      return new Response("data_x", { status: 200, headers: { "Content-Disposition": 'attachment; filename="SrTiO3_Pm-3m.cif"' } });
    }) as unknown as typeof fetch;
    const api = createApiClient({ baseUrl: "/api", fetchImpl });
    const out = await api.exportStructure(makeCrystal(5), exportOptions("cif-sym", "as_is"));
    expect(out.filename).toBe("SrTiO3_Pm-3m.cif");
    expect(calls[0]!.body).toMatchObject({ format: "cif", symmetrize: true, cell: "as_is" });
    await api.hullEvaluate(makeCrystal(5));
    expect(calls[1]!.body).toMatchObject({ relax: true });
  });
});

// ------------------------------------------------------------------ charts
describe("chart data", () => {
  it("EOS points are sorted, per atom and relative to the minimum", () => {
    const pts = eosPoints(BULK, 5);
    expect(pts.map((p) => p.volume)).toEqual([58.4, 59.55, 60.7]);
    expect(pts[1]!.dEmeVPerAtom).toBe(0);
    expect(pts[0]!.dEmeVPerAtom).toBeCloseTo(6, 6); // 30 meV / 5 atoms
    expect(pts[0]!.pressureGpa).toBe(7.3);
    expect(eosPoints({ volumes: [], energies_ev: [], pressures_gpa: [] }, 5)).toEqual([]);
  });

  it("DOS unit conversion preserves the number of states", () => {
    const thz = dosInUnit(DOS, "THz", ["Sr", "Ti", "O"]);
    const cm = dosInUnit(DOS, "cm-1", ["Sr", "Ti", "O"]);
    expect(thz.projected.map((p) => p.element)).toEqual(["Sr", "Ti", "O"]);
    expect(thz.projected.map((p) => p.color)).toEqual(SERIES_COLORS.slice(0, 3));
    expect(integrate(cm.frequencies, cm.total)).toBeCloseTo(integrate(thz.frequencies, thz.total), 9);
    expect(cm.frequencies[2]).toBeCloseTo(THZ_TO_CM1);
  });

  it("interpolates and pads extents", () => {
    expect(interpAt([0, 300, 600], [0, 2, 3], 150)).toBe(1);
    expect(interpAt([0, 300], [0, 2], -5)).toBe(0);
    expect(interpAt([], [], 1)).toBeNull();
    expect(extent([1, 3], 0)).toEqual([1, 3]);
    expect(extent([2, 2], 0)[0]).toBeLessThan(2);
    expect(extent([1, 3], 0, 0)).toEqual([0, 3]);
  });

  it("warns when imaginary modes are excluded from the thermal properties", () => {
    expect(imaginaryDosWarning(DOS)).toMatch(/2\.0 % of the mesh modes are imaginary/);
    expect(imaginaryDosWarning({ imaginary_fraction: 0 })).toBeNull();
  });
});

// ------------------------------------------------------------------ generator
describe("generator form", () => {
  it("formula mode fills composition and the element list", () => {
    const p = buildFlowPayload({ ...DEFAULT_FORM, formula: "Ca3(PO4)2", formulaUnits: 2 });
    expect(p).toMatchObject({ elements: ["Ca", "P", "O"], num_atoms: 26, composition: { Ca: 6, P: 4, O: 16 } });
    const e = buildFlowPayload({ ...DEFAULT_FORM, mode: "elements", elements: ["Li", "O"], numAtoms: 48 });
    expect(e.composition).toBeUndefined();
    expect(e).toMatchObject({ elements: ["Li", "O"], num_atoms: 48 });
  });

  it("previews the cell and enforces the 64-atom limit", () => {
    expect(previewForm({ ...DEFAULT_FORM, formulaUnits: 2 })).toMatchObject({ ok: true, composition: "Sr2Ti2O6", atoms: 10 });
    expect(previewForm({ ...DEFAULT_FORM, formula: "Li2MnO3", formulaUnits: 11 })).toMatchObject({ ok: false, atoms: 66 });
    expect(previewForm({ ...DEFAULT_FORM, formula: "Qq" }).ok).toBe(false);
    expect(previewForm({ ...DEFAULT_FORM, mode: "elements", elements: [] }).ok).toBe(false);
    expect(maxFormulaUnits("SrTiO3")).toBe(12);
    expect(maxFormulaUnits("bad")).toBe(1);
  });
});

// ------------------------------------------------------------------ shortcuts
describe("keyboard shortcuts", () => {
  const ev = (key: string, mods: Partial<{ ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; altKey: boolean }> = {}, tagName = "DIV") => ({
    key,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    altKey: false,
    defaultPrevented: false,
    target: { tagName, isContentEditable: false } as unknown as EventTarget,
    ...mods,
  });
  it("maps Esc, Ctrl+Z, Ctrl+Shift+Z, Ctrl+Y; ignores typing", () => {
    expect(shortcutFor(ev("Escape"))).toBe("stop");
    expect(shortcutFor(ev("z", { ctrlKey: true }))).toBe("undo");
    expect(shortcutFor(ev("Z", { ctrlKey: true, shiftKey: true }))).toBe("redo");
    expect(shortcutFor(ev("y", { ctrlKey: true }))).toBe("redo");
    expect(shortcutFor(ev("z", { metaKey: true }))).toBe("undo");
    expect(shortcutFor(ev("z"))).toBeNull();
    expect(shortcutFor(ev("z", { ctrlKey: true }, "INPUT"))).toBeNull();
    expect(shortcutFor(ev("Escape", {}, "TEXTAREA"))).toBeNull();
    expect(shortcutFor(ev("z", { ctrlKey: true, altKey: true }))).toBeNull();
  });
});

// ------------------------------------------------------------------ views
describe("views", () => {
  const render = (el: Parameters<typeof renderToStaticMarkup>[0]) => renderToStaticMarkup(el);

  it("RelaxationBadge shows the three states", () => {
    expect(render(createElement(RelaxationBadge, { verdict: relaxationVerdict(stressed(0)) }))).toContain("Relaxed");
    expect(render(createElement(RelaxationBadge, { verdict: relaxationVerdict(stressed(-5.1)) }))).toContain("Forces OK · cell stressed");
    expect(render(createElement(RelaxationBadge, { verdict: relaxationVerdict(stressed(0, { maxForce: 1 })) }))).toContain("Unrelaxed");
  });

  it("the stress tooltip lists P and the Voigt tensor", () => {
    const c = stressed(-5.1);
    const t = stressTooltip(c, relaxationVerdict(c), "kbar");
    expect(t).toContain("P = −tr(σ)/3 = +51.0 kbar");
    expect(t).toContain("σ_xx = −51.00 kbar");
    expect(t).toContain("σ_yz = 0.00 kbar");
  });

  it("the Structure card shows cell, symmetry, stress, deltas and sites", () => {
    const c = stressed(-5.1);
    c.atoms[0]!.wyckoff = "1a";
    c.symprec = 0.01;
    const html = render(
      createElement(StructureInfoView, {
        crystal: c,
        criteria: badgeCriteria(DEFAULT_RELAX),
        pressureUnit: "GPa",
        latticeRef: { lattice: { a: 3.96, b: 3.96, c: 3.96, volume: 3.96 ** 3 }, nAtoms: 5, kind: "relaxation", streamId: 1 },
        eos: null,
        eosStale: false,
        eosPending: false,
        canCompute: true,
        onComputeK: () => {},
      }),
    );
    expect(html).toContain("4.0000 Å"); // a
    expect(html).toContain("90.00°");
    expect(html).toContain("Pm-3m (#221)");
    expect(html).toContain("cubic");
    expect(html).toContain("0.01 Å");
    expect(html).toContain("+5.10 GPa");
    expect(html).toContain("Forces OK · cell stressed");
    expect(html).toContain("Change since the relaxation start");
    expect(html).toContain("+1.01 %"); // Δa = 4.0/3.96 − 1
    expect(html).toContain("1a");
    expect(html).toContain("Sites (5)");
    expect(html).toContain("Compute K");
  });

  it("EOS readouts flag an extrapolated V_eq and label the method as clamped-ion", () => {
    const eos: EosState = { result: BULK, stateVersion: 1, nAtoms: 5 };
    const html = render(createElement(EosReadouts, { eos, stale: true, pressureUnit: "GPa" }));
    expect(html).toContain("185.2 GPa");
    expect(html).toContain("161.0 GPa");
    expect(html).toContain("4.40");
    expect(html).toContain("V_eq (extrapolated)");
    expect(html).toContain(BULK.method);
    expect(html).toContain(CLAMPED_ION_NOTE.slice(0, 20));
    expect(html).toContain("recompute");
  });

  it("the hull popover reports the relaxed candidate and the as-given energy", () => {
    const result: HullEvaluateResponse = {
      chemsys: "O-Sr-Ti",
      energy_per_atom: -8.03,
      e_hull_ev: 0.004,
      formation_energy_per_atom: null,
      decomposition: { SrTiO3: 1 },
      references: [],
      method: "uMLIP-relaxed MP references",
      umlip_backend: "mace-mp-0-small",
      candidate_relaxed: true,
      energy_per_atom_as_given: -8.0,
    };
    const html = render(createElement(HullPopoverView, { hull: { status: "ok", result, stateVersion: 1 }, now: 0, stale: false, onEvaluate: () => {}, onClose: () => {} }));
    expect(html).toContain("candidate relaxed");
    expect(html).toContain("as given -8.000");
    expect(html).toContain("uMLIP-relaxed MP references");
  });

  it("results are watermarked under the LJ fallback", async () => {
    const { FallbackTagView, FALLBACK_LABEL } = await import("../src/components/hud/ui");
    expect(render(createElement(FallbackTagView, { active: false }))).toBe("");
    expect(render(createElement(FallbackTagView, { active: true }))).toContain(FALLBACK_LABEL);
    expect(render(createElement(FallbackTagView, { active: true, compact: true }))).toContain("LJ fallback");
  });
});

