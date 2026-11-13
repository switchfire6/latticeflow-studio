import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { availablePanes } from "../src/components/hud/BottomDock";
import { HullPopoverView } from "../src/components/hud/HullPopover";
import { createApiClient } from "../src/services/apiClient";
import { displayedEHull, evaluateHull, HULL_NO_KEY_HINT } from "../src/services/hullActions";
import { canFollow, followSelectedInstability, selectModeAt, type Send } from "../src/services/phononActions";
import { describeError, dispatchServerMessage } from "../src/services/wsClient";
import { createCrystalStore, type HullState } from "../src/store/useCrystalStore";
import type { ClientCommand, HullEvaluateResponse } from "../src/types/protocol";
import {
  classifyBands,
  classifyFrequency,
  formatKLabel,
  frequencyRange,
  isImaginarySegment,
  nearestMode,
  niceTicks,
  segmentBreaks,
} from "../src/utils/bandChart";
import { transitionText } from "../src/utils/followText";
import { loadJSON, saveJSON } from "../src/utils/persist";
import { makeCrystal, makeMode, makePhonon, phononDone, relaxFrame, spu, stopped } from "./fixtures";

// A small dispersion: 3 bands, 5 q-points, with a discontinuity between q2 and q3.
const D = [0, 0.1, 0.2, 0.2, 0.35];
const F = [
  [-2.0, 0.0, 5.0],
  [-1.0, 1.0, 5.5],
  [0.5, 2.0, 6.0],
  [-3.0, 1.5, 4.0],
  [-0.02, 2.5, 4.5],
];

describe("dispersion chart math", () => {
  it("formats k-path labels", () => {
    expect(formatKLabel("\\Gamma")).toBe("Γ");
    expect(formatKLabel("GAMMA")).toBe("Γ");
    expect(formatKLabel("X|K")).toBe("X|K");
    expect(formatKLabel("\\Gamma|R")).toBe("Γ|R");
    expect(formatKLabel("S_0")).toBe("S₀");
    expect(formatKLabel(null)).toBe("");
  });

  it("breaks the path at zero-length jumps", () => {
    expect(segmentBreaks(D)).toEqual([false, false, true, false]);
  });

  it("classifies modes and segments with F_TOL = 0.05 THz", () => {
    expect(classifyFrequency(-0.06)).toBe("imaginary");
    expect(classifyFrequency(-0.04)).toBe("zero");
    expect(classifyFrequency(0.03)).toBe("zero");
    expect(classifyFrequency(0.2)).toBe("real");
    expect(isImaginarySegment(-1, 2)).toBe(true);
    expect(isImaginarySegment(-0.02, 0.5)).toBe(false);
    const s = classifyBands(F);
    expect(s).toMatchObject({ nq: 5, nBands: 3, imaginaryBands: [0], minFrequency: -3, maxFrequency: 6 });
    expect(s.imaginaryPerQ).toEqual([1, 1, 0, 1, 0]);
  });

  it("picks the nearest q in x and the nearest band in frequency", () => {
    expect(nearestMode(D, F, 0.11, 0.8)).toEqual({ qIndex: 1, band: 1 });
    expect(nearestMode(D, F, 0.09, -1.2)).toEqual({ qIndex: 1, band: 0 });
    expect(nearestMode(D, F, -5, 100)).toEqual({ qIndex: 0, band: 2 });
    expect(nearestMode(D, F, 9, -9)).toEqual({ qIndex: 4, band: 0 });
    // at the discontinuity both q-points sharing x = 0.2 compete
    expect(nearestMode(D, F, 0.2, -2.9)).toEqual({ qIndex: 3, band: 0 });
    expect(nearestMode(D, F, 0.2, 0.6)).toEqual({ qIndex: 2, band: 0 });
    expect(nearestMode([], [], 0, 0)).toBeNull();
  });

  it("stays fast for 96 bands × 150 q-points", () => {
    const nq = 150;
    const d = Array.from({ length: nq }, (_, i) => i * 0.01);
    const f = Array.from({ length: nq }, (_, q) => Array.from({ length: 96 }, (_, b) => b * 0.2 - 1 + 0.1 * Math.sin(q)));
    const t0 = performance.now();
    for (let k = 0; k < 200; k++) nearestMode(d, f, (k % nq) * 0.01, 3.3);
    expect(performance.now() - t0).toBeLessThan(200);
    expect(nearestMode(d, f, 0.5, 3.31)?.band).toBe(22); // f = 0.2·b − 1 + 0.1·sin(50) ≈ 0.2·b − 1.026
  });

  it("builds axis ranges and ticks including zero", () => {
    expect(niceTicks(-3, 6, 5)).toEqual([-2, 0, 2, 4, 6]);
    const [lo, hi] = frequencyRange({ minFrequency: -3, maxFrequency: 6 });
    expect(lo).toBeLessThan(-3);
    expect(hi).toBeGreaterThan(6);
    expect(frequencyRange({ minFrequency: 0, maxFrequency: 4 })[0]).toBe(0);
  });
});

function phononStore() {
  const store = createCrystalStore();
  const sent: { type: ClientCommand; payload: unknown }[] = [];
  const send: Send = (type, payload) => {
    sent.push({ type, payload });
    return true;
  };
  dispatchServerMessage(store, spu(makeCrystal(5)));
  dispatchServerMessage(store, phononDone(makePhonon(5)));
  dispatchServerMessage(store, spu(makeCrystal(5, { actors: [2] })));
  return { store, sent, send };
}

describe("mode selection and Follow Instability", () => {
  it("uses the cached eigenvector of a labelled point, else REQUEST_EIGENMODE", () => {
    const { store, sent, send } = phononStore();
    // makePhonon caches q_index 0, bands 0, 1, 5
    expect(selectModeAt(0, 1, store, send)).toBe("cached");
    expect(store.getState().selectedEigenmode?.frequency_thz).toBe(-1.1);
    expect(store.getState().modeSelection).toEqual({ qIndex: 0, band: 1 });
    expect(sent).toHaveLength(0);

    expect(selectModeAt(1, 3, store, send)).toBe("requested");
    expect(sent).toEqual([{ type: "REQUEST_EIGENMODE", payload: { q_index: 1, band_index: 3 } }]);
    // selection is ahead of the loaded eigenvector until EIGENMODE_DATA arrives
    expect(store.getState().modeSelection).toEqual({ qIndex: 1, band: 3 });
    expect(store.getState().selectedEigenmode?.q_index).toBe(0);
    expect(canFollow(store.getState())).toBe(false);
    const mode = makeMode(5, { q_index: 1, band_index: 3, frequency_thz: -0.9 });
    dispatchServerMessage(store, { type: "EIGENMODE_DATA", payload: { eigenmode: mode } });
    expect(store.getState().selectedEigenmode?.q_index).toBe(1);
    expect(canFollow(store.getState())).toBe(true);
    expect(selectModeAt(99, 0, store, send)).toBe("unavailable");
  });

  it("only real modes cannot be followed", () => {
    const { store, send } = phononStore();
    selectModeAt(0, 5, store, send);
    expect(store.getState().selectedEigenmode?.is_imaginary).toBe(false);
    expect(canFollow(store.getState())).toBe(false);
    expect(followSelectedInstability(0.1, store, send)).toBe(false);
  });

  it("follows the selected imaginary mode and reports old → new space group", () => {
    const { store, sent, send } = phononStore();
    selectModeAt(0, 0, store, send); // R, −2.5 THz
    store.getState().setDock({ hidden: ["relaxation"], open: false });
    expect(followSelectedInstability(0.08, store, send)).toBe(true);
    expect(sent.at(-1)).toEqual({ type: "FOLLOW_INSTABILITY", payload: { q_index: 0, band_index: 0, amplitude: 0.08, fmax: 0.02, max_steps: 200 } });
    let s = store.getState();
    expect(s.followResult).toMatchObject({ status: "running", qLabel: "R", amplitude: 0.08, from: { spacegroupSymbol: "Pm-3m" } });
    expect(s.dock.open).toBe(true);
    expect(s.dock.hidden).not.toContain("relaxation");

    // the stream as the server sends it
    s.beginStream("FOLLOW_INSTABILITY");
    dispatchServerMessage(store, relaxFrame(0, makeCrystal(8, { energy: -7.8 })));
    expect(store.getState().phonon).toBeNull();
    const final = makeCrystal(8, { energy: -7.8144, maxForce: 0.012 });
    final.spacegroup_symbol = "P4mm";
    final.spacegroup_number = 99;
    dispatchServerMessage(store, relaxFrame(57, final, true, true));
    s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.followResult).toMatchObject({ status: "converged", steps: 57, to: { spacegroupSymbol: "P4mm", maxForce: 0.012 } });
    expect(transitionText(s.followResult!)).toBe("Pm-3m (#221) → P4mm (#99) · −14.4 meV/atom · −72.0 meV/f.u.");
  });

  it("marks a stopped or refused follow", () => {
    const { store, send } = phononStore();
    selectModeAt(0, 0, store, send);
    followSelectedInstability(0.1, store, send);
    store.getState().beginStream("FOLLOW_INSTABILITY");
    dispatchServerMessage(store, stopped("FOLLOW_INSTABILITY"));
    expect(store.getState().followResult?.status).toBe("stopped");

    const b = phononStore();
    selectModeAt(0, 0, b.store, b.send);
    followSelectedInstability(0.1, b.store, b.send);
    b.store.getState().beginStream("FOLLOW_INSTABILITY");
    dispatchServerMessage(b.store, { type: "ERROR", payload: { code: "INVALID_OPERATION", message: "q not commensurate", command: "FOLLOW_INSTABILITY" } });
    const s = b.store.getState();
    expect(s.followResult?.status).toBe("failed");
    expect(s.streams).toHaveLength(0);
    expect(s.toasts.at(-1)?.title).toBe("Pick a high-symmetry point");
  });

  it("maps follow errors to actionable messages", () => {
    const inv = describeError({ code: "INVALID_OPERATION", message: "x", command: "FOLLOW_INSTABILITY" });
    expect(inv.message).toMatch(/high-symmetry point/);
    const big = describeError({ code: "STRUCTURE_TOO_LARGE", message: "x", command: "FOLLOW_INSTABILITY" });
    expect(big.title).toBe("Supercell too large");
    expect(big.message).toMatch(/128-atom/);
    expect(describeError({ code: "COMPUTE_FAILED", message: "boom", command: null })).toEqual({ title: "Compute Failed", message: "boom" });
  });
});

// ------------------------------------------------------------------- hull
const HULL_OK: HullEvaluateResponse = {
  chemsys: "O-Sr-Ti",
  energy_per_atom: -8.018,
  e_hull_ev: 0.012,
  formation_energy_per_atom: -3.4,
  decomposition: { SrTiO3: 1 },
  references: [
    { material_id: "mp-5229", formula: "SrTiO3", energy_per_atom: -8.03 },
    { material_id: "mp-2858", formula: "TiO2", energy_per_atom: -8.9 },
  ],
  method: "uMLIP-relaxed MP references, candidate single point",
  umlip_backend: "mace-mp-0-small",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("E_hull", () => {
  const storeWithCrystal = () => {
    const store = createCrystalStore();
    dispatchServerMessage(store, spu(makeCrystal(5)));
    return store;
  };

  it("success: busy while pending, then ok with the result tied to the structure", async () => {
    const store = storeWithCrystal();
    let resolve!: (r: Response) => void;
    const fetchImpl = vi.fn(() => new Promise<Response>((r) => (resolve = r)));
    const client = createApiClient({ baseUrl: "/api", fetchImpl: fetchImpl as unknown as typeof fetch });
    const p = evaluateHull(store, client);
    expect(store.getState().hull.status).toBe("busy");
    expect(fetchImpl).toHaveBeenCalledWith("/api/hull/evaluate", expect.objectContaining({ method: "POST" }));
    const body = JSON.parse((fetchImpl.mock.calls[0] as unknown as [string, RequestInit])[1].body as string);
    expect(body.crystal.formula).toBe("SrTiO3");
    resolve(jsonResponse(200, HULL_OK));
    const st = await p;
    expect(st.status).toBe("ok");
    const s = store.getState();
    s.flushHud();
    expect(displayedEHull(null, s.hull, store.getState().stateVersion)).toEqual({ value: 0.012, stale: false });
    // a structure change makes it stale; the phonon actor-flag update does not
    dispatchServerMessage(store, phononDone(makePhonon(5)));
    dispatchServerMessage(store, spu(makeCrystal(5, { actors: [1] })));
    store.getState().flushHud();
    expect(displayedEHull(null, store.getState().hull, store.getState().stateVersion)?.stale).toBe(false);
    dispatchServerMessage(store, spu(makeCrystal(5, { move: { index: 1, cart: [1, 1, 1] } })));
    store.getState().flushHud();
    expect(displayedEHull(null, store.getState().hull, store.getState().stateVersion)?.stale).toBe(true);
    // a payload value always wins
    expect(displayedEHull(0.3, store.getState().hull, 0)).toEqual({ value: 0.3, stale: false });
  });

  it("503 HULL_UNAVAILABLE: tells the user to set MP_API_KEY", async () => {
    const store = storeWithCrystal();
    const client = createApiClient({
      baseUrl: "/api",
      fetchImpl: (async () => jsonResponse(503, { code: "HULL_UNAVAILABLE", message: "MP_API_KEY is not set" })) as unknown as typeof fetch,
    });
    const st = await evaluateHull(store, client);
    expect(st.status).toBe("unavailable");
    expect(st.status === "unavailable" && st.message).toContain(HULL_NO_KEY_HINT);
    expect(displayedEHull(null, st, 0)).toBeNull();
  });

  it("502 and network failures are errors", async () => {
    const store = storeWithCrystal();
    const c502 = createApiClient({
      baseUrl: "/api",
      fetchImpl: (async () => jsonResponse(502, { code: "HULL_UNAVAILABLE", message: "MP unreachable" })) as unknown as typeof fetch,
    });
    const a = await evaluateHull(store, c502);
    expect(a).toMatchObject({ status: "error", code: "HULL_UNAVAILABLE" });
    const cNet = createApiClient({ baseUrl: "/api", fetchImpl: (async () => Promise.reject(new TypeError("fetch failed"))) as unknown as typeof fetch });
    const b = await evaluateHull(store, cNet);
    expect(b).toMatchObject({ status: "error", code: "NETWORK" });
  });

  it("renders every popover state", () => {
    const render = (hull: HullState, stale = false) =>
      renderToStaticMarkup(createElement(HullPopoverView, { hull, now: 12_000, stale, onEvaluate: () => {}, onClose: () => {} }));
    expect(render({ status: "idle" })).toContain("Evaluate E_hull");
    const busy = render({ status: "busy", startedAt: 5_000, stateVersion: 1 });
    expect(busy).toContain("Evaluating… 7 s");
    expect(busy).toMatch(/disabled/);
    expect(render({ status: "unavailable", message: `${HULL_NO_KEY_HINT} (no key)`, stateVersion: 1 })).toContain("Set MP_API_KEY on the backend");
    expect(render({ status: "error", code: "HULL_UNAVAILABLE", message: "MP unreachable", stateVersion: 1 })).toContain("MP unreachable");
    const ok = render({ status: "ok", result: HULL_OK, stateVersion: 1 });
    expect(ok).toContain("0.012 eV/atom");
    expect(ok).toContain("O-Sr-Ti");
    expect(ok).toContain("2 reference phases");
    expect(ok).toContain("100.0 %");
    expect(ok).toContain(HULL_OK.method);
    expect(render({ status: "ok", result: HULL_OK, stateVersion: 1 }, true)).toContain("structure changed");
  });
});

// ------------------------------------------------------------------- dock
describe("dock", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows dispersion after a shake-test, g(r) always, relaxation during/after relax", () => {
    const base = { phonon: null, relaxHistory: [], streams: [], followResult: null };
    expect(availablePanes(base)).toEqual(["rdf"]);
    expect(availablePanes({ ...base, phonon: {} })).toEqual(["dispersion", "dos", "rdf"]);
    expect(availablePanes({ ...base, streams: [{ kind: "relaxation" }] })).toEqual(["rdf", "relaxation"]);
    expect(availablePanes({ ...base, relaxHistory: [1] })).toEqual(["rdf", "relaxation"]);
  });

  it("persists the layout in localStorage and survives broken storage", () => {
    const mem = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) });
    const a = createCrystalStore({ dockStorageKey: "dock" });
    a.getState().setDock({ open: false, height: 9999, hidden: ["rdf"] });
    expect(a.getState().dock.height).toBe(560); // clamped
    const b = createCrystalStore({ dockStorageKey: "dock" });
    expect(b.getState().dock).toMatchObject({ open: false, height: 560, hidden: ["rdf"] });

    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("quota");
      },
    });
    expect(() => saveJSON("x", { a: 1 })).not.toThrow();
    expect(loadJSON("x", { a: 2 })).toEqual({ a: 2 });
    const c = createCrystalStore({ dockStorageKey: "dock" });
    expect(c.getState().dock.open).toBe(true);
    expect(() => c.getState().setDock({ open: false })).not.toThrow();
  });
});
