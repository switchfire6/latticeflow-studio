import { describe, expect, it } from "vitest";
import { STEP_ORDER, buildSteps } from "../src/services/jobActions";
import { allenDynesTc } from "../src/utils/superconductivity";

describe("Allen–Dynes T_c (SUPERCONDUCTIVITY_TIER0.md reference values)", () => {
  const cases: [number, number, number, number, number][] = [
    [1.0, 0.1, 300, 300, 21.950675143220174],
    [1.0, 0.1, 300, 450, 22.478294763352174],
    [0.5, 0.13, 300, 400, 2.248854551769687],
  ];
  it.each(cases)("λ=%s μ*=%s ω_log=%s ω̄₂=%s", (lam, mu, wl, w2, ref) => {
    const tc = allenDynesTc(lam, mu, wl, w2);
    expect(Math.abs(tc - ref) / ref).toBeLessThan(1e-6);
  });
  it("is zero when λ − μ*(1 + 0.62λ) ≤ 0", () => {
    expect(allenDynesTc(0.1, 0.13, 300, 300)).toBe(0);
  });
});

describe("SC proxies job step", () => {
  it("sits between phonons and follow and needs phonons", () => {
    expect(STEP_ORDER.slice(1, 4)).toEqual(["phonons", "superconductivity", "follow-if-unstable"]);
    const kinds = buildSteps(["follow-if-unstable", "superconductivity", "phonons", "relax"]).map((s) => s.kind);
    expect(kinds).toEqual(["relax", "phonons", "superconductivity", "follow"]);
    expect(buildSteps(["relax", "superconductivity"]).map((s) => s.kind)).toEqual(["relax"]);
  });
});
