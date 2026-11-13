/**
 * Which periodic images of which atoms are drawn.
 *
 *  * cell layer (opaque, pickable): every image (i, n), n ∈ {-1,0,1}^3, whose
 *    fractional position s_i + n lies in the closed unit cell [−ε, 1+ε]^3, so
 *    an atom on a face / edge / corner appears on all of them (the classic
 *    crystallographic picture: SrTiO₃ shows Sr on all 8 corners).
 *  * ghost layer (opacity 0.22, optional 2x2x2 halo): every further image
 *    with s_i + n ∈ [−ε, 2+ε]^3.
 *
 * Bonds (s, t, offset): for each drawn image (s, m) the partner is (t, m + offset).
 * If both ends are drawn the full bond is drawn once (from the source side).
 * Otherwise, if the drawn end is an interior atom (its only drawn copy is the
 * home image), a half-bond stub to the midpoint shows the bond leaving the
 * cell; atoms on the cell boundary get no stubs (their partners are reached
 * through their other copies), which keeps the classic picture uncluttered.
 *
 * Recomputed only when the structure version or the halo toggle changes.
 */
import type { FrameBuffers } from "../../store/frameBuffers";

export const BOUNDARY_EPS = 0.02;
const NMIN = -1;
const NMAX = 2;
const SPAN = NMAX - NMIN + 1; // 4 values per axis
const CODES = SPAN * SPAN * SPAN; // 64 image codes

export const LAYER_NONE = 0;
export const LAYER_CELL = 1;
export const LAYER_GHOST = 2;

export function imageCode(n0: number, n1: number, n2: number): number {
  if (n0 < NMIN || n0 > NMAX || n1 < NMIN || n1 > NMAX || n2 < NMIN || n2 > NMAX) return -1;
  return ((n0 - NMIN) * SPAN + (n1 - NMIN)) * SPAN + (n2 - NMIN);
}

export interface DrawnImage {
  atom: number;
  n0: number;
  n1: number;
  n2: number;
}

export interface BondSegment {
  bond: number;
  /** Drawn end the segment starts from. */
  from: DrawnImage;
  /** Image of the partner atom (may not be drawn). */
  to: DrawnImage;
  /** true: full bond to `to`; false: half-bond stub to the midpoint. */
  full: boolean;
  ghost: boolean;
}

export interface DisplaySet {
  version: number;
  halo: boolean;
  /** layer per (atom * 64 + imageCode) */
  layer: Uint8Array;
  cell: DrawnImage[];
  ghost: DrawnImage[];
  bonds: BondSegment[];
}

function inRange(x: number, hi: number): boolean {
  return x >= -BOUNDARY_EPS && x <= hi + BOUNDARY_EPS;
}

export function computeDisplaySet(f: FrameBuffers, halo: boolean): DisplaySet {
  const n = f.n;
  const layer = new Uint8Array(n * CODES);
  const cell: DrawnImage[] = [];
  const ghost: DrawnImage[] = [];
  const hi = halo ? 2 : 1;
  for (let i = 0; i < n; i++) {
    const s0 = f.frac[3 * i]!, s1 = f.frac[3 * i + 1]!, s2 = f.frac[3 * i + 2]!;
    for (let a = NMIN; a <= NMAX; a++) {
      if (!inRange(s0 + a, hi)) continue;
      for (let b = NMIN; b <= NMAX; b++) {
        if (!inRange(s1 + b, hi)) continue;
        for (let c = NMIN; c <= NMAX; c++) {
          if (!inRange(s2 + c, hi)) continue;
          const inCell = inRange(s0 + a, 1) && inRange(s1 + b, 1) && inRange(s2 + c, 1);
          const img = { atom: i, n0: a, n1: b, n2: c };
          // the home image (0,0,0) always belongs to the cell layer
          if (inCell || (a === 0 && b === 0 && c === 0)) {
            layer[i * CODES + imageCode(a, b, c)] = LAYER_CELL;
            if (a === 0 && b === 0 && c === 0) cell.unshift(img);
            else cell.push(img);
          } else {
            layer[i * CODES + imageCode(a, b, c)] = LAYER_GHOST;
            ghost.push(img);
          }
        }
      }
    }
  }
  // keep home images first, in atom order, so instance i < n is atom i
  cell.sort((x, y) => {
    const hx = x.n0 === 0 && x.n1 === 0 && x.n2 === 0 ? 0 : 1;
    const hy = y.n0 === 0 && y.n1 === 0 && y.n2 === 0 ? 0 : 1;
    return hx - hy || x.atom - y.atom;
  });

  const layerOf = (atom: number, n0: number, n1: number, n2: number) => {
    const code = imageCode(n0, n1, n2);
    return code < 0 ? LAYER_NONE : layer[atom * CODES + code]!;
  };

  const bonds: BondSegment[] = [];
  const drawn = [...cell, ...ghost];
  const byAtom: DrawnImage[][] = Array.from({ length: n }, () => []);
  for (const d of drawn) byAtom[d.atom]!.push(d);
  const cellCopies = new Uint16Array(n);
  for (const d of cell) cellCopies[d.atom]!++;
  const stubFrom = (m: DrawnImage) => m.n0 === 0 && m.n1 === 0 && m.n2 === 0 && cellCopies[m.atom] === 1;
  f.bonds.forEach((bd, k) => {
    const s = bd.source_index;
    const t = bd.target_index;
    if (s >= n || t >= n) return;
    const [o0, o1, o2] = bd.image_offset;
    // forward: from each drawn source image
    for (const m of byAtom[s]!) {
      const to = { atom: t, n0: m.n0 + o0, n1: m.n1 + o1, n2: m.n2 + o2 };
      const lt = layerOf(t, to.n0, to.n1, to.n2);
      const ls = layerOf(s, m.n0, m.n1, m.n2);
      if (lt === LAYER_NONE && !stubFrom(m)) continue;
      bonds.push({ bond: k, from: m, to, full: lt !== LAYER_NONE, ghost: ls === LAYER_GHOST || lt === LAYER_GHOST });
    }
    // backward: half-bonds from drawn target images whose source partner is not drawn
    for (const m of byAtom[t]!) {
      const to = { atom: s, n0: m.n0 - o0, n1: m.n1 - o1, n2: m.n2 - o2 };
      if (layerOf(s, to.n0, to.n1, to.n2) !== LAYER_NONE) continue; // already drawn in full
      if (!stubFrom(m)) continue;
      bonds.push({ bond: k, from: m, to, full: false, ghost: false });
    }
  });
  return { version: f.version, halo, layer, cell, ghost, bonds };
}

let cache: DisplaySet | null = null;
let cacheFrame: FrameBuffers | null = null;

/** Memoised per (frame buffers, structure version, halo). */
export function getDisplaySet(f: FrameBuffers, halo: boolean): DisplaySet {
  if (!cache || cacheFrame !== f || cache.version !== f.version || cache.halo !== halo) {
    cache = computeDisplaySet(f, halo);
    cacheFrame = f;
  }
  return cache;
}
