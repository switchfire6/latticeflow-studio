import { describe, expect, it } from "vitest";
import {
  CELL_EDGES,
  bondEndpoint,
  cartToFrac,
  det3,
  flattenMatrix,
  fracToCart,
  invert3,
  matrixFromParameters,
  minimumImageCart,
  minimumImageFrac,
  supercellImages,
  wrapFrac,
  type Vec3,
} from "../src/utils/latticeMath";

// A genuinely triclinic cell (a != b != c, no right angles).
const TRICLINIC = matrixFromParameters(4.1, 5.3, 6.7, 78.0, 96.0, 108.0);
const L = flattenMatrix(TRICLINIC);
const Linv = invert3(L);

function close(a: ArrayLike<number>, b: ArrayLike<number>, tol = 1e-10) {
  for (let i = 0; i < a.length; i++) expect(Math.abs(a[i]! - b[i]!)).toBeLessThan(tol);
}

describe("lattice matrix", () => {
  it("follows the §4.1 lower-triangular orientation and reproduces the parameters", () => {
    expect(TRICLINIC[0]![1]).toBe(0);
    expect(TRICLINIC[0]![2]).toBe(0);
    expect(TRICLINIC[1]![2]).toBe(0);
    const len = (v: number[]) => Math.hypot(...v);
    const ang = (u: number[], v: number[]) =>
      (Math.acos((u[0]! * v[0]! + u[1]! * v[1]! + u[2]! * v[2]!) / (len(u) * len(v))) * 180) / Math.PI;
    const [a, b, c] = TRICLINIC as [number[], number[], number[]];
    expect(len(a)).toBeCloseTo(4.1, 10);
    expect(len(b)).toBeCloseTo(5.3, 10);
    expect(len(c)).toBeCloseTo(6.7, 10);
    expect(ang(b, c)).toBeCloseTo(78.0, 8); // alpha
    expect(ang(a, c)).toBeCloseTo(96.0, 8); // beta
    expect(ang(a, b)).toBeCloseTo(108.0, 8); // gamma
  });

  it("inverts: L · L⁻¹ = I", () => {
    const prod = new Array(9).fill(0);
    for (let i = 0; i < 3; i++)
      for (let j = 0; j < 3; j++) for (let k = 0; k < 3; k++) prod[i * 3 + j] += L[i * 3 + k]! * Linv[k * 3 + j]!;
    close(prod, [1, 0, 0, 0, 1, 0, 0, 0, 1], 1e-12);
    expect(det3(L)).toBeGreaterThan(0);
  });

  it("rejects a singular lattice", () => {
    expect(() => invert3([1, 0, 0, 2, 0, 0, 0, 0, 1])).toThrow();
  });
});

describe("frac <-> cart", () => {
  it("uses the row-vector convention r = s · L", () => {
    // s = (1, 0, 0) is exactly the first row, a.
    close(fracToCart([1, 0, 0], L), TRICLINIC[0]!);
    close(fracToCart([0, 0, 1], L), TRICLINIC[2]!);
    const s: Vec3 = [0.2, 0.5, 0.7];
    const expected = [0, 1, 2].map((j) => s[0] * TRICLINIC[0]![j]! + s[1] * TRICLINIC[1]![j]! + s[2] * TRICLINIC[2]![j]!);
    close(fracToCart(s, L), expected);
  });

  it("round-trips on a triclinic cell", () => {
    let seed = 7;
    const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647) * 2 - 0.5;
    for (let n = 0; n < 200; n++) {
      const s: Vec3 = [rnd(), rnd(), rnd()];
      const r = fracToCart(s, L, [0, 0, 0]);
      const back = cartToFrac(r, Linv, [0, 0, 0]);
      close(back, s, 1e-12);
      // and cart -> frac -> cart
      const r2 = fracToCart(cartToFrac(r, Linv), L);
      close(r2, r, 1e-12);
    }
  });

  it("wraps fractional coordinates into [0, 1)", () => {
    expect(wrapFrac(1.25)).toBeCloseTo(0.25);
    expect(wrapFrac(-0.25)).toBeCloseTo(0.75);
    expect(wrapFrac(1)).toBe(0);
    expect(wrapFrac(-1e-18)).toBeLessThan(1);
  });
});

describe("minimum image", () => {
  it("fractional: (Δs + 0.5) mod 1 − 0.5", () => {
    close(minimumImageFrac([0.9, -0.8, 0.3]), [-0.1, 0.2, 0.3], 1e-12);
    close(minimumImageFrac([2.45, -3.55, 0]), [0.45, 0.45, 0], 1e-12);
  });

  it("cartesian: finds the shortest lattice translate even in a skewed cell", () => {
    const d = fracToCart([0.93, -0.88, 1.6], L, [0, 0, 0]) as Vec3;
    const m = minimumImageCart(d, L, Linv);
    const mLen = Math.hypot(...m);
    // m differs from d by an integer lattice vector
    const n = cartToFrac([d[0] - m[0], d[1] - m[1], d[2] - m[2]], Linv, [0, 0, 0]);
    for (const x of n) expect(Math.abs(x - Math.round(x))).toBeLessThan(1e-9);
    // and no translate within ±2 cells is shorter
    for (let i = -2; i <= 2; i++)
      for (let j = -2; j <= 2; j++)
        for (let k = -2; k <= 2; k++) {
          const t = fracToCart([i, j, k], L, [0, 0, 0]);
          expect(Math.hypot(d[0] + t[0], d[1] + t[1], d[2] + t[2])).toBeGreaterThanOrEqual(mLen - 1e-9);
        }
  });

  it("is the identity for a short vector", () => {
    close(minimumImageCart([0.3, -0.2, 0.1], L, Linv), [0.3, -0.2, 0.1], 1e-12);
  });
});

describe("bond endpoints from image_offset", () => {
  it("is r_target + n · L", () => {
    const rT = fracToCart([0.1, 0.2, 0.3], L, [0, 0, 0]);
    const end = bondEndpoint(rT, [1, -1, 0], L);
    close(end, fracToCart([1.1, -0.8, 0.3], L));
  });

  it("reproduces the bond length of a cubic perovskite bond crossing the boundary", () => {
    // SrTiO3, a = 3.905: Ti at (½,½,½), O at (½,½,0); the Ti–O bond to the O of
    // the cell above has image_offset (0,0,1) and length a/2.
    const a = 3.905;
    const C = flattenMatrix([
      [a, 0, 0],
      [0, a, 0],
      [0, 0, a],
    ]);
    const ti = fracToCart([0.5, 0.5, 0.5], C, [0, 0, 0]);
    const o = fracToCart([0.5, 0.5, 0], C, [0, 0, 0]);
    const inCell = bondEndpoint(o, [0, 0, 0], C);
    const image = bondEndpoint(o, [0, 0, 1], C);
    expect(Math.hypot(inCell[0] - ti[0], inCell[1] - ti[1], inCell[2] - ti[2])).toBeCloseTo(a / 2, 12);
    expect(Math.hypot(image[0] - ti[0], image[1] - ti[1], image[2] - ti[2])).toBeCloseTo(a / 2, 12);
    expect(image[2]).toBeCloseTo(a, 12);
  });
});

describe("supercell images", () => {
  it("enumerates the 2x2x2 halo with the origin first", () => {
    const imgs = supercellImages([2, 2, 2]);
    expect(imgs).toHaveLength(8);
    expect(imgs[0]).toEqual([0, 0, 0]);
    expect(new Set(imgs.map((x) => x.join(","))).size).toBe(8);
    expect(supercellImages([3, 1, 2])).toHaveLength(6);
  });

  it("defines the 12 unit-cell edges", () => {
    expect(CELL_EDGES).toHaveLength(12);
  });
});
