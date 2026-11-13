/**
 * Phase C1: pure helpers (Glazer text, polyhedron hull, bond-valence alignment,
 * supercell geometry, Arrhenius fit, committee verdict, history navigation)
 * and one dispatcher / client test per new event family.
 */
import { describe, expect, it } from "vitest";
import { StudioClient, type WebSocketLike } from "../src/services/wsClient";
import { SessionMemory } from "../src/services/sessionMemory";
import { createCrystalStore } from "../src/store/useCrystalStore";
import type { ClientCommand, CommitteeResult, LocalAnalysisData, MDFrame, ServerMessage } from "../src/types/protocol";
import { arrheniusFit, committeeVerdict, historySteps, K_B_EV, resolveHistoryRef, sciText } from "../src/utils/analysis";
import { alignIdeals, bondValenceDeviation, convexHullFaces, glazerDisplay, idealLengthMap } from "../src/utils/localStructure";
import { diagonalMatrix, previewSupercell, shortestPeriodicDistance, substitutionCount, SUPERCELL_PRESETS, suggestDiagonal } from "../src/utils/supercell";
import { error, makeCrystal, spu, stopped } from "./fixtures";

const cubic = (a: number) => [
  [a, 0, 0],
  [0, a, 0],
  [0, 0, a],
];

describe("local structure helpers", () => {
  it("Glazer symbols get superscripts", () => {
    expect(glazerDisplay("a0a0c-")).toBe("a⁰a⁰c⁻");
    expect(glazerDisplay("a+b-b-")).toBe("a⁺b⁻b⁻");
    expect(glazerDisplay(null)).toBe("—");
    expect(glazerDisplay("weird")).toBe("weird");
  });

  it("convex hull: octahedron 8 faces, tetrahedron 4, a flat square one polygon", () => {
    const oct: [number, number, number][] = [
      [1, 0, 0],
      [-1, 0, 0],
      [0, 1, 0],
      [0, -1, 0],
      [0, 0, 1],
      [0, 0, -1],
    ];
    const faces = convexHullFaces(oct);
    expect(faces).toHaveLength(8);
    expect(faces.every((f) => f.length === 3)).toBe(true);
    expect(convexHullFaces([[1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]])).toHaveLength(4);
    const sq = convexHullFaces([[1, 0, 0], [0, 1, 0], [-1, 0, 0], [0, -1, 0]]);
    expect(sq).toHaveLength(1);
    expect(sq[0]).toHaveLength(4);
    // a cube merges coplanar points: 6 quads
    const cube = [0, 1].flatMap((x) => [0, 1].flatMap((y) => [0, 1].map((z) => [x, y, z] as [number, number, number])));
    expect(convexHullFaces(cube).map((f) => f.length)).toEqual([4, 4, 4, 4, 4, 4]);
  });

  it("bond-valence ideals follow bonds by identity, not by index", () => {
    const c = makeCrystal(5);
    const map = idealLengthMap(c.bonds, [1.95, null]);
    const swapped = [c.bonds[1]!, c.bonds[0]!];
    const al = alignIdeals(swapped, map);
    expect(Number.isNaN(al[0]!)).toBe(true);
    expect(al[1]).toBeCloseTo(1.95);
    expect(bondValenceDeviation(2.0, 1.95)).toBeCloseTo(0.0256, 3);
    expect(bondValenceDeviation(2.0, null)).toBeNull();
    expect(idealLengthMap(c.bonds, [1.9]).size).toBe(0); // misaligned length: ignored
  });
});

describe("supercell geometry", () => {
  it("atom count, shortest periodic distance and the < 10 Å flag", () => {
    const L = cubic(3.9);
    expect(shortestPeriodicDistance(L)).toBeCloseTo(3.9);
    const p = previewSupercell(diagonalMatrix([2, 2, 2]), L, 5);
    expect(p).toMatchObject({ det: 8, nAtoms: 40, valid: true, small: true });
    expect(p.minDistance).toBeCloseTo(7.8);
    const r2 = previewSupercell(SUPERCELL_PRESETS.find((x) => x.id === "r2r2x2")!.matrix, L, 5);
    expect(r2.nAtoms).toBe(20);
    expect(r2.minDistance).toBeCloseTo(3.9 * Math.SQRT2);
    expect(previewSupercell(diagonalMatrix([3, 3, 3]), L, 5)).toMatchObject({ nAtoms: 135, valid: false, tooLarge: true });
    expect(previewSupercell([[1, 0, 0], [0, 1, 0], [0, 0, -1]], L, 5).valid).toBe(false);
    expect(suggestDiagonal(L, 5)).toEqual([2, 3, 3]); // 3×3×3 = 135 atoms > 128: one axis shrinks
  });

  it("substitution count uses Python rounding", () => {
    expect(substitutionCount(0.25, 8)).toBe(2);
    expect(substitutionCount(0.5, 5)).toBe(2); // round(2.5) = 2
    expect(substitutionCount(0.7, 5)).toBe(4); // round(3.5) = 4
  });
});

describe("Arrhenius fit, committee verdict, history steps", () => {
  it("recovers E_a and D0 from synthetic data", () => {
    const ea = 0.35;
    const d0 = 2e-3;
    const pts = [500, 700, 900, 1100].map((t) => ({ temperatureK: t, d: d0 * Math.exp(-ea / (K_B_EV * t)) }));
    const fit = arrheniusFit([...pts, { temperatureK: 300, d: null }])!;
    expect(fit.ea).toBeCloseTo(ea, 6);
    expect(fit.d0).toBeCloseTo(d0, 8);
    expect(fit.n).toBe(4);
    expect(arrheniusFit(pts.slice(0, 1))).toBeNull();
    expect(sciText(1.234e-5)).toBe("1.23 × 10⁻⁵");
  });

  it("flags a > 3 % volume spread or differing space groups", () => {
    const entry = (id: string, v: number, sg: string) => ({ model_id: id, backend_name: id, pressure_gpa: 0, max_force: 0, relaxed_volume_per_atom: v, relaxed_lattice_abc: null, relaxed_spacegroup: sg, relaxed_energy_per_atom: null });
    const r = (e: CommitteeResult["entries"], spread: Record<string, number> = {}): CommitteeResult => ({ entries: e, spread, method: "" });
    expect(committeeVerdict(r([entry("a", 12, "Pm-3m"), entry("b", 12.2, "Pm-3m")])).disagree).toBe(false);
    expect(committeeVerdict(r([entry("a", 12, "Pm-3m"), entry("b", 12.2, "Pm-3m")], { volume_percent: 4 })).disagree).toBe(true);
    const v = committeeVerdict(r([entry("a", 12, "Pm-3m"), entry("b", 12.1, "I4/mcm")]));
    expect(v.disagree).toBe(true);
    expect(v.reasons[0]).toContain("space groups differ");
  });

  it("history navigation and re-resolving a remembered entry", () => {
    expect(historySteps(5, 2)).toEqual({ command: "UNDO", count: 3 });
    expect(historySteps(1, 3)).toEqual({ command: "REDO", count: 2 });
    expect(historySteps(2, 2)).toBeNull();
    const e = (position: number, label: string) => ({ position, label, formula: "SrTiO3", n_atoms: 40, energy_per_atom: -7.9, spacegroup_symbol: "Pm-3m" });
    const ref = { position: 3, label: "Make supercell 2x2x2", formula: "SrTiO3", n_atoms: 40, energy_per_atom: -7.9 };
    // the oldest entry was dropped: the same entry is now at position 2
    expect(resolveHistoryRef([e(0, "Load"), e(1, "Relax"), e(2, "Make supercell 2x2x2")], ref)?.position).toBe(2);
  });
});

// ------------------------------------------------------------------ dispatcher + client
class FakeSocket implements WebSocketLike {
  readyState = 1;
  sent: { type: ClientCommand; payload: Record<string, unknown> }[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {}
}

function client(memory: SessionMemory | null = null) {
  const store = createCrystalStore();
  const sockets: FakeSocket[] = [];
  const c = new StudioClient({ store, url: "ws://t", memory, createSocket: () => sockets[sockets.push(new FakeSocket()) - 1]! });
  c.connect();
  sockets[0]!.onopen?.({});
  const deliver = (...m: ServerMessage[]) => {
    for (const x of m) c.handle(x);
    store.getState().flushHud();
  };
  return { store, client: c, sockets, sock: () => sockets.at(-1)!, deliver };
}

const LOCAL: LocalAnalysisData = {
  rdf_r: [1, 2],
  partial_rdf: { "O-Ti": [0, 1] },
  sites: [],
  bond_ideal_lengths: [1.95, 1.97],
  global_instability_index: 0.08,
  polyhedra: [],
  octahedral_tilts: { tilt_angles_deg: [0, 0, 5.1], phases: ["0", "0", "-"], glazer: "a0a0c-", pseudo_cubic_axes: [], method: "" },
  method: "BVS",
};

const mdFrame = (step: number, msd: number): ServerMessage => ({
  type: "MD_FRAME",
  payload: { step, time_ps: step / 1000, temperature_k: 300 + step, potential_energy_ev: -39, kinetic_energy_ev: 0.2, pressure_gpa: 0.1, volume: 60, msd_by_element: { O: msd }, crystal: makeCrystal(5) } satisfies MDFrame,
});

describe("dispatcher: Phase C1 events", () => {
  it("LOCAL_ANALYSIS_DATA keys the ideal lengths by the bonds on screen", () => {
    const { store, deliver } = client();
    deliver(spu(makeCrystal(5)));
    store.getState().setLocalPending(true);
    deliver({ type: "LOCAL_ANALYSIS_DATA", payload: LOCAL });
    const la = store.getState().localAnalysis!;
    expect(store.getState().localPending).toBe(false);
    expect(la.stateVersion).toBe(store.getState().stateVersion);
    expect(la.ideals.size).toBe(2);
  });

  it("formation / substitution / committee results end their streams", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5)));
    c.send("COMPUTE_FORMATION_ENERGY", { bulk_position: 0 });
    expect(store.getState().streams[0]?.kind).toBe("formation");
    deliver({ type: "PROGRESS", payload: { command: "COMPUTE_FORMATION_ENERGY", done: 1, total: 3, message: "references" } });
    deliver({
      type: "FORMATION_ENERGY_RESULT",
      payload: { defect_label: "V_O", delta_n: { O: -1 }, e_defect_ev: -300, e_bulk_ev: -310, chemical_potentials: { "O-rich": { O: -4.9 } }, formation_energies_ev: { "O-rich": 5.1 }, min_image_distance: 7.8, warnings: [], method: "" },
    });
    expect(store.getState().streams).toHaveLength(0);
    expect(store.getState().progress.COMPUTE_FORMATION_ENERGY).toBeUndefined();
    expect(store.getState().formation?.result.defect_label).toBe("V_O");

    c.send("SUBSTITUTE_FRACTION", { from_element: "Sr", to_element: "Ba", fraction: 1 });
    deliver({ type: "SUBSTITUTION_RESULT", payload: { from_element: "Sr", to_element: "Ba", n_substituted: 1, n_tried: 1, energies_per_atom: [-7.7], chosen_energy_per_atom: -7.7, method: "" } }, spu(makeCrystal(5)));
    expect(store.getState().streams).toHaveLength(0);
    expect(store.getState().substitution?.n_substituted).toBe(1);

    c.send("COMMITTEE_EVALUATE", { model_ids: ["small", "medium"] });
    deliver(error("MODEL_UNAVAILABLE", "COMMITTEE_EVALUATE", "no checkpoint"));
    expect(store.getState().streams).toHaveLength(0);
    expect(store.getState().toasts.at(-1)?.title).toBe("Model unavailable");
    c.send("COMMITTEE_EVALUATE", { model_ids: ["small", "medium"] });
    deliver({ type: "COMMITTEE_RESULT", payload: { entries: [], spread: {}, method: "" } });
    expect(store.getState().streams).toHaveLength(0);
    expect(store.getState().committee).not.toBeNull();
  });

  it("MD frames render like relaxation frames and feed the series; complete / stop end the stream", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5)));
    c.send("START_MD", { temperature_k: 600, n_steps: 100 });
    expect(store.getState().streams[0]?.kind).toBe("md");
    deliver(mdFrame(10, 0.1), mdFrame(20, 0.2));
    let s = store.getState();
    expect(s.crystalSource).toBe("md");
    expect(s.session.md.times).toEqual([0.01, 0.02]);
    expect(s.session.md.msd.O).toEqual([0.1, 0.2]);
    expect(s.session.md.epotPerAtom[0]).toBeCloseTo(-7.8);
    expect(s.streams[0]?.progress).toMatchObject({ step: 20, totalSteps: 100 });
    deliver({
      type: "MD_COMPLETE",
      payload: { n_steps: 100, time_ps: 0.1, mean_temperature_k: 600, mean_pressure_gpa: 0.1, mean_volume: 60, times_ps: [], msd_by_element: {}, diffusion_coefficients_cm2_s: { O: 1e-5 }, final_crystal: makeCrystal(5), method: "" },
    });
    s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.session.md.status).toBe("complete");
    expect(s.session.md.complete?.diffusion_coefficients_cm2_s.O).toBe(1e-5);
    c.send("START_MD", {});
    expect(store.getState().session.md.times).toHaveLength(0); // a new run starts a new series
    deliver(mdFrame(10, 0.1), stopped("START_MD"));
    expect(store.getState().streams).toHaveLength(0);
    expect(store.getState().session.md.status).toBe("stopped");
  });

  it("a disordered CIF is kept for 'Find ordered approximants'", () => {
    const { store, client: c, deliver } = client();
    c.send("LOAD_STRUCTURE", { cif: "data_x\n_occ 0.5" });
    deliver(error("DISORDERED_STRUCTURE", "LOAD_STRUCTURE", "partial occupancies"));
    const s = store.getState();
    expect(s.disordered).toEqual({ cif: "data_x\n_occ 0.5", message: "partial occupancies" });
    expect(s.toasts.at(-1)?.action?.kind).toBe("find-orderings");
  });

  it("restores the session's model after a reconnect, once the structure is back", () => {
    const memory = new SessionMemory({ storageKey: null });
    memory.remember(makeCrystal(5, { formula: "BaTiO3", elements: ["Ba", "Ti", "O", "O", "O"] }));
    memory.rememberModel({ model_id: "medium", dispersion: true });
    const { sock, deliver } = client(memory); // connected: restore armed
    deliver(spu(makeCrystal(5))); // the server's auto-loaded default -> LOAD_STRUCTURE {crystal}
    expect(sock().sent.map((m) => m.type)).toEqual(["LOAD_STRUCTURE"]);
    deliver(spu(makeCrystal(5, { formula: "BaTiO3", elements: ["Ba", "Ti", "O", "O", "O"] })));
    expect(sock().sent.at(-1)).toEqual({ type: "SET_MODEL", payload: { model_id: "medium", dispersion: true } });
    deliver({ type: "MODEL_CHANGED", payload: { model_id: "small", backend_name: "mace-mp-0-small", dispersion: false, device: "cpu", dtype: "float64" } });
    expect(memory.model).toEqual({ model_id: "small", dispersion: false });
  });
});
