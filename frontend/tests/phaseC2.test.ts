/**
 * Phase C2: Nernst–Einstein units, click-to-place ray / plane intersection,
 * the compare snapshot cache, and dispatcher tests for GENERATION_CANDIDATES
 * and the PROTOTYPE_SEARCH stream.
 */
import { describe, expect, it } from "vitest";
import { buildFlowPayload, DEFAULT_FORM } from "../src/components/hud/GeneratorSidebar";
import { SnapshotCache, historyKey } from "../src/services/snapshotCache";
import { StudioClient, type WebSocketLike } from "../src/services/wsClient";
import { createCrystalStore } from "../src/store/useCrystalStore";
import type { ClientCommand, CrystalStatePayload, GenerationCandidates, HistoryEntryInfo, HistoryState, PrototypeCandidates, ServerMessage } from "../src/types/protocol";
import { ionicConductivities, nernstEinsteinSigma, oxidationStatesFromSites, rayCellPlaneFrac, searchSummaryText, substitutionText } from "../src/utils/analysisC2";
import { flattenMatrix, invert3 } from "../src/utils/latticeMath";
import { error, flowFinal, makeCrystal, makePhonon, phononDone, spu, stopped } from "./fixtures";

describe("Nernst–Einstein conductivity", () => {
  it("σ = n q² D / (k_B T) in S/cm", () => {
    // 1 carrier in 1000 Å³ = 1e21 cm⁻³, z = +1, D = 1e-6 cm²/s, 300 K
    const s = nernstEinsteinSigma(1, 1000, 1, 1e-6, 300)!;
    const expected = (1e21 * 1.602176634e-19 ** 2 * 1e-6) / (1.380649e-23 * 300);
    expect(s).toBeCloseTo(expected, 12);
    expect(s).toBeCloseTo(6.197e-3, 5);
    expect(nernstEinsteinSigma(1, 1000, -2, 1e-6, 300)! / s).toBeCloseTo(4); // q²
    expect(nernstEinsteinSigma(2, 1000, 1, 1e-6, 600)! / s).toBeCloseTo(1); // n/T
    expect(nernstEinsteinSigma(1, 1000, 1, null, 300)).toBeNull();
    expect(nernstEinsteinSigma(1, 0, 1, 1e-6, 300)).toBeNull();
  });

  it("charges: bond-valence analysis first, common oxidation state otherwise", () => {
    const charges = oxidationStatesFromSites([
      { element: "Li", oxidation_state: 0.98 },
      { element: "Li", oxidation_state: 1.02 },
      { element: "S", oxidation_state: null },
    ]);
    expect(charges.Li).toBeCloseTo(1);
    const rows = ionicConductivities({ Li: 12, P: 4, S: 16 }, 800, { Li: 1e-6, S: null, Xx: 1e-6 }, 600, charges);
    const li = rows.find((r) => r.element === "Li")!;
    expect(li.chargeSource).toBe("analysis");
    expect(li.density).toBeCloseTo(12 / 800e-24);
    expect(li.sigma).toBeCloseTo(nernstEinsteinSigma(12, 800, 1, 1e-6, 600)!);
    const s = rows.find((r) => r.element === "S")!;
    expect(s).toMatchObject({ charge: -2, chargeSource: "guess", sigma: null }); // no D
    expect(rows.find((r) => r.element === "Xx")).toMatchObject({ charge: null, sigma: null });
  });
});

describe("click-to-place", () => {
  const L = flattenMatrix([
    [4, 0, 0],
    [0, 4, 0],
    [0, 0, 4],
  ]);
  const Linv = invert3(L);
  it("hits the plane w = depth inside the face", () => {
    const f = rayCellPlaneFrac([1, 3, 10], [0, 0, -1], Linv, 2, 0.5)!;
    expect(f[0]).toBeCloseTo(0.25);
    expect(f[1]).toBeCloseTo(0.75);
    expect(f[2]).toBe(0.5);
    const g = rayCellPlaneFrac([-5, 2, 2], [1, 0, 0], Linv, 0, 0.25)!; // u-plane from the side
    expect(g).toEqual([0.25, 0.5, 0.5]);
  });
  it("misses: outside the face, parallel, or behind the camera", () => {
    expect(rayCellPlaneFrac([9, 3, 10], [0, 0, -1], Linv, 2, 0.5)).toBeNull();
    expect(rayCellPlaneFrac([1, 1, 10], [1, 0, 0], Linv, 2, 0.5)).toBeNull();
    expect(rayCellPlaneFrac([1, 1, 10], [0, 0, 1], Linv, 2, 0.5)).toBeNull();
  });
});

describe("generator / prototype text", () => {
  it("search summary, substitutions and top_k", () => {
    const s = { spacegroup_number: 221, spacegroup_symbol: "Pm-3m", composition: { Sr: 1, Ti: 1, O: 3 }, n_wyckoff_combinations: 6, n_structures_screened: 120, n_structures_scored: 18 };
    expect(searchSummaryText(s, 5)).toBe("searched 6 Wyckoff assignments in Pm-3m (#221), screened 120, scored 18 → top 5");
    expect(substitutionText({ Ta: "Nb", K: "Na", O: "O" })).toBe("K→Na, Ta→Nb");
    expect(buildFlowPayload(DEFAULT_FORM).top_k).toBeUndefined();
    expect(buildFlowPayload({ ...DEFAULT_FORM, topK: 5 }).top_k).toBe(5);
  });
});

// ------------------------------------------------------------------ snapshot cache
const entry = (position: number, label: string, c: CrystalStatePayload): HistoryEntryInfo => ({
  position,
  label,
  formula: c.formula,
  n_atoms: c.atoms.length,
  energy_per_atom: c.energy_per_atom,
  spacegroup_symbol: c.spacegroup_symbol,
});
const hist = (position: number, entries: HistoryEntryInfo[]): HistoryState => ({ can_undo: position > 0, can_redo: position < entries.length - 1, position, length: entries.length, label: entries[position]!.label, entries });

describe("compare snapshot cache", () => {
  it("records the payload of each history entry and attaches phonons; prunes dropped entries", () => {
    const cache = new SnapshotCache();
    const c0 = makeCrystal(5);
    const c1 = makeCrystal(5, { energy: -7.9 });
    const e0 = entry(0, "Load preset", c0);
    const e1 = entry(1, "Relax", c1);
    let h = hist(0, [e0]);
    cache.observe({ type: "HISTORY_STATE", payload: h }, { crystal: c0, history: h, phonon: null, dos: null });
    h = hist(1, [e0, e1]);
    // a HISTORY_STATE whose current entry does not match the payload is not cached
    cache.observe({ type: "HISTORY_STATE", payload: h }, { crystal: c0, history: h, phonon: null, dos: null });
    expect(cache.forEntry(e1)).toBeNull();
    cache.observe({ type: "HISTORY_STATE", payload: h }, { crystal: c1, history: h, phonon: null, dos: null });
    expect(cache.forEntry(e0)?.crystal).toBe(c0);
    expect(cache.forEntry(e1)?.crystal).toBe(c1);
    const ph = makePhonon(5);
    ph.frequencies_thz = [[-2, 1], [0.5, 2], [1, 3]];
    cache.observe(phononDone(ph), { crystal: c1, history: h, phonon: ph, dos: null });
    expect(cache.forEntry(e1)?.phonon?.lowestBranch).toEqual([-2, 0.5, 1]);
    expect(cache.current({ crystal: c1, history: h })?.phonon?.minFrequencyThz).toBe(-2.5);
    // follow parent keeps its own copy
    cache.setFollowParent({ crystal: c1, history: h }, ph, null);
    expect(cache.followParent?.crystal).toBe(c1);
    // the oldest entry is dropped from the server history
    const h2 = hist(0, [{ ...e1, position: 0 }]);
    cache.observe({ type: "HISTORY_STATE", payload: h2 }, { crystal: c1, history: h2, phonon: ph, dos: null });
    expect(cache.get(historyKey(e0))).toBeNull();
    expect(cache.forEntry(e1)?.phonon).not.toBeNull();
  });
});

// ------------------------------------------------------------------ dispatcher
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

function client() {
  const store = createCrystalStore();
  const sock = new FakeSocket();
  const c = new StudioClient({ store, url: "ws://t", memory: null, createSocket: () => sock });
  c.connect();
  sock.onopen?.({});
  const deliver = (...m: ServerMessage[]) => {
    for (const x of m) c.handle(x);
    store.getState().flushHud();
  };
  return { store, client: c, deliver };
}

const candidates = (n: number): GenerationCandidates => ({
  summary: { spacegroup_number: 221, spacegroup_symbol: "Pm-3m", composition: { Sr: 1, Ti: 1, O: 3 }, n_wyckoff_combinations: 4, n_structures_screened: 40, n_structures_scored: 8 },
  candidates: Array.from({ length: n }, (_, i) => ({
    rank: i + 1,
    energy_per_atom: -7.8 + i * 0.01,
    relative_energy_mev_per_atom: i * 10,
    spacegroup_symbol: "Pm-3m",
    spacegroup_number: 221,
    wyckoff_assignment: "Sr 1a, Ti 1b, O 3c",
    crystal: makeCrystal(5, { energy: -7.8 + i * 0.01 }),
  })),
});

const protos: PrototypeCandidates = {
  target_formula: "NaNbO3",
  n_sources: 3,
  method: "SubstitutionPredictor",
  candidates: [
    { rank: 1, formula: "NaNbO3", source_material_id: "mp-3614", source_formula: "KTaO3", substitutions: { K: "Na", Ta: "Nb" }, probability: 0.02, spacegroup_symbol: "Pm-3m", energy_per_atom: -8, e_hull_ev: 0.01, relaxed: true, crystal: makeCrystal(5) },
  ],
};

describe("dispatcher: Phase C2 events", () => {
  it("GENERATION_CANDIDATES arrives before CRYSTAL_STATE_FINAL and is cleared by the next generation", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5)));
    c.send("START_FLOW_GENERATION", { elements: ["Sr", "Ti", "O"], top_k: 3 });
    deliver({ type: "GENERATION_CANDIDATES", payload: candidates(3) });
    expect(store.getState().streams).toHaveLength(1); // not terminal
    deliver(flowFinal(makeCrystal(5)));
    const s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.generationCandidates?.candidates.map((x) => x.rank)).toEqual([1, 2, 3]);
    c.send("START_FLOW_GENERATION", { elements: ["Sr", "Ti", "O"] });
    expect(store.getState().generationCandidates).toBeNull();
  });

  it("PROTOTYPE_SEARCH is a stream: PROGRESS, then PROTOTYPE_CANDIDATES ends it; stop and MP-key errors", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5)));
    c.send("PROTOTYPE_SEARCH", { composition: { Na: 1, Nb: 1, O: 3 } });
    expect(store.getState().streams[0]?.kind).toBe("prototype");
    expect(store.getState().prototypeSearch?.status).toBe("running");
    deliver({ type: "PROGRESS", payload: { command: "PROTOTYPE_SEARCH", done: 1, total: 3, message: "relaxing" } });
    expect(store.getState().streams[0]?.progress.done).toBe(1);
    deliver({ type: "PROTOTYPE_CANDIDATES", payload: protos });
    let s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.progress.PROTOTYPE_SEARCH).toBeUndefined();
    expect(s.prototypeSearch).toMatchObject({ status: "done", error: null });
    expect(s.prototypeSearch?.result?.candidates[0]?.source_material_id).toBe("mp-3614");
    expect(s.crystalSource).toBe("single_point"); // not state-changing

    c.send("PROTOTYPE_SEARCH", { composition: { Na: 1, Nb: 1, O: 3 } });
    deliver(stopped("PROTOTYPE_SEARCH"));
    expect(store.getState().streams).toHaveLength(0);
    expect(store.getState().prototypeSearch?.status).toBe("stopped");

    c.send("PROTOTYPE_SEARCH", { composition: { Na: 1, Nb: 1, O: 3 } });
    deliver(error("INVALID_OPERATION", "PROTOTYPE_SEARCH", "MP_API_KEY is not set"));
    s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.prototypeSearch).toMatchObject({ status: "error", error: { code: "INVALID_OPERATION" } });
    expect(s.toasts.at(-1)).toMatchObject({ level: "warning", title: "Prototype search unavailable" });
  });
});
