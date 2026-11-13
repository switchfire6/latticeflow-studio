import { describe, expect, it } from "vitest";
import { buildFlowPayload, DEFAULT_FORM } from "../src/components/hud/GeneratorSidebar";
import { atomicMass, cpkColor, covalentRadius, elementBySymbol, ELEMENTS, normalizeSymbol } from "../src/utils/cpkColors";
import { groupNeighbours, TABLE_CELLS } from "../src/utils/periodicTable";
import { amplitudePrefactor, computeModeField, DEFAULT_PHONON_PARAMS, imageDisplacement, pickDefaultEigenmode } from "../src/utils/phonon";
import { makeMode } from "./fixtures";

describe("phonon animation (§4.4 step 6)", () => {
  it("amplitude = A / (2π max(|f|, 0.5 THz)) · sqrt(kB T / m)", () => {
    // O (15.999 u), 300 K, f = 2 THz: sqrt(kT/m) = 394.9 m/s, /(2π·2e12) = 3.142e-11 m
    const u = amplitudePrefactor(2, 15.999, DEFAULT_PHONON_PARAMS);
    expect(u).toBeCloseTo(0.3142, 3);
    // floor at 0.5 THz, linear in A, ∝ sqrt(T), ∝ 1/sqrt(m)
    expect(amplitudePrefactor(0.01, 16, DEFAULT_PHONON_PARAMS)).toBeCloseTo(amplitudePrefactor(0.5, 16, DEFAULT_PHONON_PARAMS), 12);
    expect(amplitudePrefactor(-2, 16, DEFAULT_PHONON_PARAMS)).toBeCloseTo(amplitudePrefactor(2, 16, DEFAULT_PHONON_PARAMS), 12);
    const p2 = { ...DEFAULT_PHONON_PARAMS, amplitude: 3, temperatureK: 1200 };
    expect(amplitudePrefactor(2, 16, p2) / amplitudePrefactor(2, 16, DEFAULT_PHONON_PARAMS)).toBeCloseTo(6, 10);
    expect(amplitudePrefactor(2, 64, DEFAULT_PHONON_PARAMS) / amplitudePrefactor(2, 16, DEFAULT_PHONON_PARAMS)).toBeCloseTo(0.5, 10);
  });

  it("uses the q·R phase of each image: an R-point mode alternates sign between neighbouring cells", () => {
    const n = 3;
    const mode = makeMode(n, { q_frac: [0.5, 0.5, 0.5], frequency_thz: -2, eigenvectors_real: [[0, 0, 0], [0, 0, 0], [0, 0, 1]] });
    const frac = new Float32Array([0, 0, 0, 0.5, 0.5, 0.5, 0.5, 0.5, 0]);
    const masses = [87.62, 47.867, 15.999];
    const re = new Float32Array(3 * n);
    const im = new Float32Array(3 * n);
    expect(computeModeField(mode, frac, masses, n, 0, DEFAULT_PHONON_PARAMS, re, im)).toBe(true);
    const home = { 0: 0, 1: 0, 2: 0 };
    const next = { 0: 0, 1: 0, 2: 0 };
    imageDisplacement(re, im, 2, mode.q_frac, 0, 0, 0, home);
    imageDisplacement(re, im, 2, mode.q_frac, 1, 0, 0, next);
    expect(Math.abs(home[2])).toBeGreaterThan(0.05);
    expect(next[2]).toBeCloseTo(-home[2], 6);
    // two cells over: back in phase
    imageDisplacement(re, im, 2, mode.q_frac, 1, 1, 0, next);
    expect(next[2]).toBeCloseTo(home[2], 6);
    // atoms with zero eigenvector do not move
    imageDisplacement(re, im, 0, mode.q_frac, 1, 0, 0, next);
    expect(next[0]).toBeCloseTo(0, 12);
  });

  it("oscillates in time with period 1/(|f| · slow-motion)", () => {
    const mode = makeMode(1, { q_frac: [0, 0, 0], frequency_thz: 2, eigenvectors_real: [[1, 0, 0]] });
    const re = new Float32Array(3);
    const im = new Float32Array(3);
    const p = DEFAULT_PHONON_PARAMS;
    const period = 1 / (2 * p.slowMotionPsPerSecond); // wall seconds
    computeModeField(mode, [0, 0, 0], [16], 1, 0, p, re, im);
    const x0 = re[0]!;
    computeModeField(mode, [0, 0, 0], [16], 1, period / 2, p, re, im);
    expect(re[0]).toBeCloseTo(-x0, 6);
    computeModeField(mode, [0, 0, 0], [16], 1, period, p, re, im);
    expect(re[0]).toBeCloseTo(x0, 6);
  });

  it("rejects an eigenmode that does not match the structure", () => {
    const re = new Float32Array(6);
    expect(computeModeField(makeMode(3), new Float32Array(6), [1, 1], 2, 0, DEFAULT_PHONON_PARAMS, re, re)).toBe(false);
  });

  it("auto-selects the softest imaginary mode", () => {
    const modes = [makeMode(1, { frequency_thz: -1 }), makeMode(1, { frequency_thz: -3 }), makeMode(1, { frequency_thz: 5, is_imaginary: false })];
    expect(pickDefaultEigenmode(modes)?.frequency_thz).toBe(-3);
    expect(pickDefaultEigenmode([])).toBeNull();
  });
});

describe("element tables", () => {
  it("covers Z = 1..89 with ASE values", () => {
    expect(ELEMENTS).toHaveLength(89);
    expect(elementBySymbol("Ac")?.z).toBe(89);
    expect(cpkColor(8)).toBe(0xff0d0d); // Jmol O
    expect(covalentRadius(22)).toBeCloseTo(1.6, 6); // Cordero Ti
    expect(atomicMass(38)).toBeCloseTo(87.62, 2);
    expect(normalizeSymbol("ti")).toBe("Ti");
    expect(normalizeSymbol("Xx")).toBeNull();
  });

  it("lays out the periodic table and suggests group neighbours", () => {
    expect(TABLE_CELLS.find((c) => c.symbol === "Fe")).toMatchObject({ row: 4, col: 8 });
    expect(TABLE_CELLS.find((c) => c.symbol === "Hf")).toMatchObject({ row: 6, col: 4 });
    expect(TABLE_CELLS.find((c) => c.symbol === "Rn")).toMatchObject({ row: 6, col: 18 });
    expect(TABLE_CELLS.find((c) => c.symbol === "Ce")?.row).toBe(9);
    expect(groupNeighbours("Na").column).toEqual(["Li", "K", "Rb"]);
    expect(groupNeighbours("Ti").column).toEqual(["Zr", "Hf"]);
    expect(groupNeighbours("O").column.slice(0, 2)).toEqual(["S", "Se"]);
    expect(groupNeighbours("O").row).toEqual(["N", "F"]);
  });
});

describe("generator payload", () => {
  it("maps the form onto START_FLOW_GENERATION", () => {
    expect(buildFlowPayload(DEFAULT_FORM)).toEqual({
      elements: ["Sr", "Ti", "O"],
      num_atoms: 5,
      composition: { Sr: 1, Ti: 1, O: 3 },
      target_spacegroup: null,
      target_crystal_system: "cubic",
      target_bulk_modulus_gpa: null,
      steps: 60,
      physics_guidance_scale: 1.5,
      seed: null,
    });
    const p = buildFlowPayload({ ...DEFAULT_FORM, mode: "elements", symmetry: "number", spacegroup: 999, bulkEnabled: true, bulkModulus: 200, seed: "42" });
    expect(p.target_spacegroup).toBe(230);
    expect(p.target_crystal_system).toBeNull();
    expect(p.target_bulk_modulus_gpa).toBe(200);
    expect(p.seed).toBe(42);
  });
});
