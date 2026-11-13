/**
 * Phase B: scorecard status logic, XRD parsing / broadening, band_order
 * branches, CSV builders, the relaxation-chart log axis, and the dispatcher /
 * client handling of the new server events.
 */
import { describe, expect, it } from "vitest";
import { StudioClient, dispatchServerMessage, type WebSocketLike } from "../src/services/wsClient";
import { DEFAULT_RELAX, badgeCriteria } from "../src/store/settings";
import { createCrystalStore, type ElasticState, type PhononSummary } from "../src/store/useCrystalStore";
import type { ClientCommand, CrystalStatePayload, ElasticTensorResult, PhononConvergenceResult, ServerMessage } from "../src/types/protocol";
import { branchOfBand, connectedFrequencies, gammaQIndex, logAxisTicks } from "../src/utils/bandChart";
import { dispersionCsv, elasticCsv, toCsv } from "../src/utils/csv";
import { broadenPattern, parsePattern, transformPattern } from "../src/utils/diffraction";
import { computeScorecard, type ScorecardInput } from "../src/utils/scorecard";
import { error, makeCrystal, makePhonon, phononDone, relaxFrame, spu, stopped } from "./fixtures";

const ELASTIC: ElasticTensorResult = {
  c_ij_gpa: Array.from({ length: 6 }, (_, i) => Array.from({ length: 6 }, (_, j) => (i === j ? (i < 3 ? 318 : 113) : i < 3 && j < 3 ? 102 : 0))),
  eigenvalues_gpa: [522, 216, 216, 113, 113, 113],
  born_stable: true,
  k_voigt_gpa: 174,
  k_reuss_gpa: 174,
  k_vrh_gpa: 174,
  g_voigt_gpa: 111,
  g_reuss_gpa: 111,
  g_vrh_gpa: 111,
  youngs_modulus_vrh_gpa: 275,
  poisson_ratio_vrh: 0.24,
  pugh_ratio: 0.64,
  universal_anisotropy: 0,
  debye_temperature_k: 540,
  relaxed_ions: true,
  max_strain: 0.01,
  reference_pressure_gpa: 0.1,
  fit_residual_gpa: 0.1,
  method: "stress-strain fit",
};

const CONV: PhononConvergenceResult = {
  supercell_dim_a: [2, 2, 2],
  supercell_dim_b: [3, 3, 3],
  min_image_distance_a: 7.8,
  min_image_distance_b: 11.7,
  min_frequency_thz_a: -2.6,
  min_frequency_thz_b: -2.5,
  max_abs_delta_thz: 0.06,
  rms_delta_thz: 0.02,
  per_label_max_delta_thz: { R: 0.06 },
  tolerance_thz: 0.1,
  converged: true,
};

// ------------------------------------------------------------------ scorecard
describe("stability scorecard", () => {
  const relaxed = (sigma = 0): CrystalStatePayload => {
    const c = makeCrystal(5, { maxForce: 0.001 });
    c.stress_gpa = [sigma, sigma, sigma, 0, 0, 0];
    return c;
  };
  const summary = (o: Partial<PhononSummary> = {}): PhononSummary => ({
    stable: true,
    minFrequencyThz: 0.1,
    imaginaryCount: 0,
    supercellDim: [3, 3, 3],
    minImageDistance: 11.7,
    cellMode: "primitive",
    stateVersion: 7,
    convergence: null,
    ...o,
  });
  const input = (o: Partial<ScorecardInput> = {}): ScorecardInput => ({
    crystal: relaxed(),
    criteria: badgeCriteria(DEFAULT_RELAX),
    stateVersion: 7,
    streams: [],
    phononSummary: null,
    elastic: null,
    hull: { status: "idle" },
    ...o,
  });
  const byId = (i: ScorecardInput) => Object.fromEntries(computeScorecard(i).map((c) => [c.id, c]));

  it("nothing run: relaxed passes, the rest are 'not run' without a prerequisite", () => {
    const c = byId(input());
    expect(c.relaxed!.state).toBe("pass");
    expect([c.dynamic!.state, c.mechanical!.state, c.thermo!.state]).toEqual(["notrun", "notrun", "notrun"]);
    expect(c.dynamic!.prerequisite).toBeNull();
  });

  it("a stressed cell fails 'relaxed' and offers relax-first for phonons and C_ij", () => {
    const c = byId(input({ crystal: relaxed(-5.1) }));
    expect(c.relaxed!.state).toBe("fail");
    expect(c.dynamic!.prerequisite).toBe("Cell under +5.10 GPa");
    expect(c.mechanical!.prerequisite).toBe("Cell under +5.10 GPa");
    expect(c.thermo!.prerequisite).toBeNull(); // the hull relaxes the candidate itself
  });

  it("pass / fail / stale / running from results stamped with the state version", () => {
    const elastic: ElasticState = { result: { ...ELASTIC, born_stable: false, eigenvalues_gpa: [-3, 1, 1, 1, 1, 1] }, stateVersion: 7 };
    const c = byId(
      input({
        phononSummary: summary({ stable: false, imaginaryCount: 3, minFrequencyThz: -2.5, convergence: { ...CONV, converged: false } }),
        elastic,
        hull: { status: "ok", stateVersion: 6, result: { e_hull_ev: 0.004 } as never },
      }),
    );
    expect(c.dynamic).toMatchObject({ state: "fail", value: "3 imaginary · min −2.50 THz" });
    expect(c.dynamic!.detail).toContain("3×3×3 supercell · d_min 11.7 Å");
    expect(c.dynamic!.warning).toContain("not converged");
    expect(c.mechanical!.state).toBe("fail");
    expect(c.thermo!.state).toBe("stale");
    expect(byId(input({ phononSummary: summary({ stateVersion: 3 }) })).dynamic!.state).toBe("stale");
    expect(byId(input({ streams: [{ command: "CHECK_PHONON_CONVERGENCE" }] })).dynamic!.state).toBe("running");
  });

  it("E_hull passes within the uMLIP tolerance; hull unavailable is 'n/a'", () => {
    const hull = (e: number) => byId(input({ hull: { status: "ok", stateVersion: 7, result: { e_hull_ev: e, candidate_relaxed: true } as never } })).thermo!;
    expect(hull(0.02).state).toBe("pass");
    expect(hull(0.08)).toMatchObject({ state: "fail", detail: "metastable · relaxed candidate" });
    expect(byId(input({ hull: { status: "unavailable", message: "no key", stateVersion: 7 } })).thermo!.state).toBe("unavailable");
  });
});

// ------------------------------------------------------------------ XRD
describe("diffraction helpers", () => {
  it("parses .xy / .xye / .csv with comments and headers, sorted by 2θ", () => {
    const r = parsePattern("# Cu Ka\n2theta,I\n30.0, 5\n10.0, 1\n20.0;3\n", "a.csv");
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.pattern.twoTheta).toEqual([10, 20, 30]);
    expect(r.pattern.intensity).toEqual([1, 3, 5]);
    expect(r.pattern.sigma).toBeNull();
    expect(r.skipped).toBe(1);
    const xye = parsePattern("10 1 0.1\n11 2 0.2\n");
    expect(xye.ok && xye.pattern.sigma).toEqual([0.1, 0.2]);
    expect(parsePattern("hello\nworld").ok).toBe(false);
  });

  it("normalises and shifts the overlay", () => {
    const t = transformPattern({ twoTheta: [10, 20], intensity: [50, 150] }, 0.5, 2);
    expect(t.x).toEqual([10.5, 20.5]);
    expect(t.y).toEqual([0, 200]);
  });

  it("broadening keeps isolated peak heights at the FWHM", () => {
    for (const shape of ["gaussian", "pseudo-voigt"] as const) {
      const p = broadenPattern([{ two_theta: 30, intensity: 100 }], [20, 40], 0.2, shape, 0.5, 0.01);
      const at = (x: number) => p.y[Math.round((x - 20) / 0.01)]!;
      expect(at(30)).toBeCloseTo(100, 6);
      expect(at(30.1)).toBeCloseTo(50, 1); // half maximum at ± FWHM/2
      expect(at(25)).toBeLessThan(0.5);
    }
  });
});

// ------------------------------------------------------------------ band order
describe("connected branches", () => {
  // two branches crossing between q1 and q2: ascending order swaps
  const freqs = [
    [1, 3],
    [1.8, 2.2],
    [2.2, 1.8].sort((a, b) => a - b),
    [1, 3],
  ];
  const order = [
    [0, 1],
    [0, 1],
    [1, 0],
    [1, 0],
  ];

  it("follows band_order through a crossing and maps a selection to its branch", () => {
    expect(connectedFrequencies(freqs, order).map((r) => r[0])).toEqual([1, 1.8, 2.2, 3]);
    expect(branchOfBand(order, 3, 0)).toBe(1); // ascending band 0 at q3 belongs to branch 1
    expect(branchOfBand(null, 3, 0)).toBe(0);
  });

  it("ignores an invalid band_order", () => {
    expect(connectedFrequencies(freqs, [[0, 0]])).toEqual(freqs);
  });

  it("finds Γ on the path", () => {
    expect(gammaQIndex([{ index: 4, label: "X" }, { index: 9, label: "\\Gamma" }])).toBe(9);
    expect(gammaQIndex([{ index: 0, label: "X" }])).toBeNull();
  });
});

// ------------------------------------------------------------------ CSV
describe("CSV export", () => {
  it("quotes fields and writes comments", () => {
    expect(toCsv(["a", "b"], [["x,y", 1.5], ['q"', null]], ["note"])).toBe('# note\na,b\n"x,y",1.5\n"q""",\n');
  });

  it("dispersion CSV is per q-point, connected branches, in the display unit", () => {
    const p = makePhonon(1);
    p.frequencies_thz = [
      [1, 3],
      [1, 3],
      [1, 3],
    ];
    p.band_order = [
      [0, 1],
      [1, 0],
      [1, 0],
    ];
    const lines = dispersionCsv(p, "cm-1").trim().split("\n").filter((l) => !l.startsWith("#"));
    expect(lines[0]).toBe("q_index,distance_inv_A,label,branch_0_cm-1,branch_1_cm-1");
    expect(lines[1]).toBe(`0,0,Γ,33.35640952,${+(3 * 33.35640952).toPrecision(10)}`);
    expect(lines[2]!.split(",")[3]).toBe(String(+(3 * 33.35640952).toPrecision(10)));
    expect(lines[3]!.split(",")[2]).toBe("R");
  });

  it("elastic CSV holds the 6×6 matrix and the derived moduli", () => {
    const csv = elasticCsv(ELASTIC);
    expect(csv).toContain("C1j,318,102,102,0,0,0");
    expect(csv).toContain("K_vrh_GPa,174");
    expect(csv).toContain("born_stable,true");
  });
});

// ------------------------------------------------------------------ log axis (relaxation chart)
describe("relaxation log axis", () => {
  it("clamps at 1e-12 and thins the decades to fit", () => {
    const a = logAxisTicks(1e-18, 1, 7);
    expect(a.lo).toBe(-12);
    expect(a.ticks.length).toBeLessThanOrEqual(7);
    expect(a.ticks.at(-1)).toBe(0);
    expect(new Set(a.ticks).size).toBe(a.ticks.length);
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

function client() {
  const store = createCrystalStore();
  const sock = new FakeSocket();
  const exports: string[] = [];
  const c = new StudioClient({ store, url: "ws://t", createSocket: () => sock, onPhononExport: (p) => exports.push(p.filename) });
  c.connect();
  sock.onopen?.({});
  const deliver = (...m: ServerMessage[]) => {
    for (const x of m) c.handle(x);
    store.getState().flushHud();
  };
  return { store, client: c, sock, deliver, exports };
}

describe("dispatcher: Phase B events", () => {
  it("convergence check: result, then the larger analysis keeps it; a new shake-test drops it", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5)));
    c.send("RUN_PHONON_SHAKE_TEST", {});
    deliver(phononDone(makePhonon(5)), spu(makeCrystal(5, { actors: [1] })));
    c.send("CHECK_PHONON_CONVERGENCE", {});
    expect(store.getState().streams[0]?.kind).toBe("phonon");
    deliver({ type: "PROGRESS", payload: { command: "CHECK_PHONON_CONVERGENCE", done: 2, total: 6, message: "displacements" } });
    expect(store.getState().streams[0]?.progress).toMatchObject({ done: 2, total: 6 });
    deliver({ type: "PHONON_CONVERGENCE_RESULT", payload: CONV }, phononDone(makePhonon(5)), spu(makeCrystal(5, { actors: [1] })));
    let s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.progress.CHECK_PHONON_CONVERGENCE).toBeUndefined();
    expect(s.convergence?.converged).toBe(true);
    expect(s.phononSummary?.convergence?.max_abs_delta_thz).toBe(0.06);
    c.send("RUN_PHONON_SHAKE_TEST", {});
    deliver(phononDone(makePhonon(5)));
    s = store.getState();
    expect(s.convergence).toBeNull();
    expect(s.phononSummary?.convergence).toBeNull();
  });

  it("elastic tensor: progress, result stamped with the state version, ERROR ends the stream", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5)));
    c.send("COMPUTE_ELASTIC_TENSOR", { max_strain: 0.01 });
    expect(store.getState().streams[0]?.kind).toBe("elastic");
    deliver({ type: "ELASTIC_TENSOR_RESULT", payload: ELASTIC });
    const s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.elastic).toMatchObject({ stateVersion: s.stateVersion });
    c.send("COMPUTE_ELASTIC_TENSOR", {});
    deliver(error("COMPUTE_FAILED", "COMPUTE_ELASTIC_TENSOR"));
    expect(store.getState().streams).toHaveLength(0);
    expect(store.getState().toasts.at(-1)?.title).toBe("Elastic tensor failed");
  });

  it("MODEL_CHANGED sets the session model, drops phonons and marks results stale; MODEL_UNAVAILABLE keeps the old one", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5)), phononDone(makePhonon(5)), spu(makeCrystal(5, { actors: [1] })), { type: "ELASTIC_TENSOR_RESULT", payload: ELASTIC });
    const v0 = store.getState().stateVersion;
    c.send("SET_MODEL", { model_id: "medium", dispersion: true });
    expect(store.getState().streams[0]?.kind).toBe("model");
    deliver({ type: "MODEL_CHANGED", payload: { model_id: "medium", backend_name: "mace-mp-0-medium", dispersion: true, device: "cuda", dtype: "float64" } });
    let s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.model?.backend_name).toBe("mace-mp-0-medium");
    expect(s.phonon).toBeNull();
    expect(s.stateVersion).toBeGreaterThan(v0);
    expect(s.elastic!.stateVersion).not.toBe(s.stateVersion);
    c.send("SET_MODEL", { model_id: "large" });
    deliver(error("MODEL_UNAVAILABLE", "SET_MODEL", "download failed"));
    s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.model?.model_id).toBe("medium");
    expect(s.toasts.at(-1)?.title).toBe("Model unavailable");
  });

  it("PHONON_EXPORT is handed to the download hook", () => {
    const { store, client: c, deliver, exports } = client();
    c.send("EXPORT_PHONON_DATA", {});
    store.getState().setPhononExportPending(true);
    deliver({ type: "PHONON_EXPORT", payload: { filename: "SrTiO3_phonopy_params.yaml", phonopy_yaml: "phonopy:\n" } });
    expect(exports).toEqual(["SrTiO3_phonopy_params.yaml"]);
    expect(store.getState().phononExportPending).toBe(false);
  });

  it("relax-then-run fires only after a converged relaxation", () => {
    const { store, client: c, deliver } = client();
    deliver(spu(makeCrystal(5)));
    let ran = 0;
    c.runAfterRelax("Shake-test", () => ran++, { fmax: 0.02 });
    deliver(relaxFrame(0, makeCrystal(5)), relaxFrame(5, makeCrystal(5), true, true));
    expect(ran).toBe(1);
    c.runAfterRelax("Shake-test", () => ran++, {});
    deliver(relaxFrame(0, makeCrystal(5)), stopped("START_RELAXATION"));
    expect(ran).toBe(1);
    expect(store.getState().toasts.at(-1)?.title).toBe("Shake-test not started");
  });

  it("the dispatcher ignores a stray ELASTIC result's stream bookkeeping", () => {
    const store = createCrystalStore();
    dispatchServerMessage(store, { type: "ELASTIC_TENSOR_RESULT", payload: ELASTIC });
    expect(store.getState().elastic?.result.k_vrh_gpa).toBe(174);
  });
});
