/** Periodic-image display set + bond segments, checked on the real SrTiO3 payload from the backend. */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { computeDisplaySet } from "../src/components/viewport/displaySet";
import { FrameBuffers } from "../src/store/frameBuffers";
import type { CrystalStatePayload, ServerMessage } from "../src/types/protocol";
import { makeCrystal } from "./fixtures";

const recorded: (ServerMessage & { _sent?: boolean })[] = JSON.parse(
  readFileSync(fileURLToPath(new URL("./fixtures/recorded-session.json", import.meta.url)), "utf8"),
);
const srtio3 = (recorded.find((m) => !m._sent && m.type === "SINGLE_POINT_UPDATE") as Extract<ServerMessage, { type: "SINGLE_POINT_UPDATE" }>)
  .payload.crystal;

function frameOf(c: CrystalStatePayload) {
  const f = new FrameBuffers();
  f.ingest(c, "single_point");
  return f;
}

const dist = (f: FrameBuffers, a: { atom: number; n0: number; n1: number; n2: number }, b: typeof a) => {
  const p: [number, number, number] = [0, 0, 0];
  const q: [number, number, number] = [0, 0, 0];
  f.position(a.atom, a.n0, a.n1, a.n2, p);
  f.position(b.atom, b.n0, b.n1, b.n2, q);
  return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
};

describe("display set on cubic SrTiO3 (Pm-3m, a = 3.905 Å)", () => {
  const f = frameOf(srtio3);
  const ds = computeDisplaySet(f, false);
  const count = (el: string) => ds.cell.filter((d) => srtio3.atoms[d.atom]!.element === el).length;

  it("draws the classic cell: Sr on 8 corners, Ti once, each face-centred O on both faces", () => {
    expect(count("Sr")).toBe(8);
    expect(count("Ti")).toBe(1);
    expect(count("O")).toBe(6);
    // home images first, in atom order (instance i < N is atom i)
    expect(ds.cell.slice(0, 5).map((d) => [d.atom, d.n0, d.n1, d.n2])).toEqual([0, 1, 2, 3, 4].map((i) => [i, 0, 0, 0]));
    expect(ds.ghost).toHaveLength(0);
  });

  it("draws every bond between drawn atoms in full, once, at the payload bond length, and no stubs", () => {
    expect(ds.bonds.every((b) => b.full)).toBe(true);
    const keys = ds.bonds.map((b) => {
      const ends = [`${b.from.atom}:${b.from.n0},${b.from.n1},${b.from.n2}`, `${b.to.atom}:${b.to.n0},${b.to.n1},${b.to.n2}`].sort();
      return ends.join("-");
    });
    expect(new Set(keys).size).toBe(keys.length);
    for (const b of ds.bonds) expect(dist(f, b.from, b.to)).toBeCloseTo(srtio3.bonds[b.bond]!.length, 4);
    // 6 Ti–O (the octahedron) + 24 Sr–O (each of the 8 corner Sr bonds to the 3 face-centre O of its corner)
    const tiO = ds.bonds.filter((b) => [b.from.atom, b.to.atom].some((a) => srtio3.atoms[a]!.element === "Ti"));
    expect(tiO).toHaveLength(6);
    expect(ds.bonds.length - tiO.length).toBe(24);
  });

  it("adds the ghost 2x2x2 halo without duplicating cell images", () => {
    const h = computeDisplaySet(f, true);
    expect(h.cell).toHaveLength(ds.cell.length);
    // 27 Sr lattice points in [0,2]^3, 8 Ti, 3 x 2 x 2 x 3 = 36 O sites
    expect(h.cell.length + h.ghost.length).toBe(27 + 8 + 36);
    const keys = [...h.cell, ...h.ghost].map((d) => `${d.atom}:${d.n0},${d.n1},${d.n2}`);
    expect(new Set(keys).size).toBe(keys.length);
  });
});

describe("display set on an interior atom bonded across the boundary", () => {
  it("draws half-bond stubs from both home atoms instead of a stick into empty space", () => {
    const c = makeCrystal(2, { elements: ["Na", "O"] });
    c.atoms[0]!.frac_coords = [0.9, 0.5, 0.5];
    c.atoms[0]!.cart_coords = [3.6, 2, 2];
    c.atoms[1]!.frac_coords = [0.1, 0.5, 0.5];
    c.atoms[1]!.cart_coords = [0.4, 2, 2];
    c.bonds = [{ source_index: 0, target_index: 1, image_offset: [1, 0, 0], length: 0.8, strain_ratio: 0, is_unstable: false }];
    const f = frameOf(c);
    const ds = computeDisplaySet(f, false);
    expect(ds.cell).toHaveLength(2);
    expect(ds.bonds).toHaveLength(2);
    expect(ds.bonds.every((b) => !b.full)).toBe(true);
    // each stub points at the partner's periodic image, 0.8 Å away
    for (const b of ds.bonds) expect(dist(f, b.from, b.to)).toBeCloseTo(0.8, 5);
    expect(ds.bonds.map((b) => [b.from.atom, b.to.atom, b.to.n0])).toEqual([
      [0, 1, 1],
      [1, 0, -1],
    ]);
  });
});
