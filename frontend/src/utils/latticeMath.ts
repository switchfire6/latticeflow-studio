/**
 * Lattice math in the ASE / pymatgen row-vector convention (PROJECT_SPEC.md §4.1):
 * the rows of L are the lattice vectors a, b, c and  r = s · L  (r_j = Σ_i s_i L_ij).
 *
 * Matrices are flat, row-major length-9 arrays (Float64Array or number[]), so the
 * same helpers work on the transient render buffers and on plain JSON payloads.
 */

export type Vec3 = [number, number, number];
export type Mat3 = Float64Array | number[];

/** Flatten a 3x3 `lattice.matrix` (rows = a, b, c) into a row-major length-9 array. */
export function flattenMatrix(m: number[][], out: Float64Array = new Float64Array(9)): Float64Array {
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) out[i * 3 + j] = m[i]![j]!;
  return out;
}

export function det3(L: Mat3): number {
  return (
    L[0]! * (L[4]! * L[8]! - L[5]! * L[7]!) -
    L[1]! * (L[3]! * L[8]! - L[5]! * L[6]!) +
    L[2]! * (L[3]! * L[7]! - L[4]! * L[6]!)
  );
}

/** Inverse of a 3x3 row-major matrix. Throws on a singular lattice. */
export function invert3(L: Mat3, out: Float64Array = new Float64Array(9)): Float64Array {
  const d = det3(L);
  if (!Number.isFinite(d) || Math.abs(d) < 1e-12) throw new Error("singular lattice matrix");
  const [a, b, c, e, f, g, h, i, j] = [L[0]!, L[1]!, L[2]!, L[3]!, L[4]!, L[5]!, L[6]!, L[7]!, L[8]!];
  out[0] = (f * j - g * i) / d;
  out[1] = (c * i - b * j) / d;
  out[2] = (b * g - c * f) / d;
  out[3] = (g * h - e * j) / d;
  out[4] = (a * j - c * h) / d;
  out[5] = (c * e - a * g) / d;
  out[6] = (e * i - f * h) / d;
  out[7] = (b * h - a * i) / d;
  out[8] = (a * f - b * e) / d;
  return out;
}

/** r = s · L. `out` may alias `s`. */
export function fracToCart(s: ArrayLike<number>, L: Mat3, out: Vec3 | Float32Array | Float64Array = [0, 0, 0]) {
  const u = s[0]!, v = s[1]!, w = s[2]!;
  out[0] = u * L[0]! + v * L[3]! + w * L[6]!;
  out[1] = u * L[1]! + v * L[4]! + w * L[7]!;
  out[2] = u * L[2]! + v * L[5]! + w * L[8]!;
  return out;
}

/** s = r · L⁻¹ (pass the precomputed inverse). `out` may alias `r`. */
export function cartToFrac(r: ArrayLike<number>, Linv: Mat3, out: Vec3 | Float32Array | Float64Array = [0, 0, 0]) {
  return fracToCart(r, Linv, out);
}

/** Wrap a fractional coordinate into [0, 1). */
export function wrapFrac(x: number): number {
  const y = x - Math.floor(x);
  return y >= 1 ? 0 : y;
}

/** Fractional minimum image of a difference: (Δs + 0.5) mod 1 − 0.5, per component (§4.2). */
export function minimumImageFrac(ds: ArrayLike<number>, out: Vec3 = [0, 0, 0]): Vec3 {
  for (let k = 0; k < 3; k++) {
    const x = ds[k]! + 0.5;
    out[k] = x - Math.floor(x) - 0.5;
  }
  return out;
}

/**
 * Cartesian minimum-image vector of d under lattice L: the shortest d + n·L over
 * integer n. Starts from the fractional rounding and then checks the 26
 * neighbouring translations, so it is exact for skewed (triclinic) cells too.
 */
export function minimumImageCart(d: ArrayLike<number>, L: Mat3, Linv: Mat3, out: Vec3 = [0, 0, 0]): Vec3 {
  const s = cartToFrac(d, Linv, [0, 0, 0]);
  const base: Vec3 = [s[0] - Math.round(s[0]), s[1] - Math.round(s[1]), s[2] - Math.round(s[2])];
  let best = Infinity;
  const c: Vec3 = [0, 0, 0];
  for (let i = -1; i <= 1; i++)
    for (let j = -1; j <= 1; j++)
      for (let k = -1; k <= 1; k++) {
        fracToCart([base[0] + i, base[1] + j, base[2] + k], L, c);
        const n2 = c[0] * c[0] + c[1] * c[1] + c[2] * c[2];
        if (n2 < best - 1e-12) {
          best = n2;
          out[0] = c[0];
          out[1] = c[1];
          out[2] = c[2];
        }
      }
  return out;
}

/** n·L: the Cartesian translation of periodic image n = (n_a, n_b, n_c). */
export function imageTranslation(n: ArrayLike<number>, L: Mat3, out: Vec3 | Float32Array | Float64Array = [0, 0, 0]) {
  return fracToCart(n, L, out);
}

/**
 * Bond endpoint for a `BondEdge`: the target atom's periodic image,
 * r_target + n·L (protocol: bond vector = r_target + n·L − r_source).
 */
export function bondEndpoint(rTarget: ArrayLike<number>, imageOffset: ArrayLike<number>, L: Mat3, out: Vec3 = [0, 0, 0]): Vec3 {
  imageTranslation(imageOffset, L, out);
  out[0] += rTarget[0]!;
  out[1] += rTarget[1]!;
  out[2] += rTarget[2]!;
  return out;
}

/**
 * Integer image offsets of a supercell: every n with 0 <= n_k < dims_k, the
 * origin image (0, 0, 0) first. `supercellImages([2, 2, 2])` gives the 8
 * images of the 2x2x2 halo.
 */
export function supercellImages(dims: ArrayLike<number> = [2, 2, 2]): Vec3[] {
  const out: Vec3[] = [];
  for (let i = 0; i < dims[0]!; i++)
    for (let j = 0; j < dims[1]!; j++) for (let k = 0; k < dims[2]!; k++) out.push([i, j, k]);
  return out;
}

/** The 8 corners of the cell spanned by `scale`·(a, b, c), as fractional triples. */
export const CELL_CORNERS: readonly Vec3[] = supercellImages([2, 2, 2]);

/** The 12 cell edges as pairs of indices into CELL_CORNERS (a-, b-, c-parallel edges). */
export const CELL_EDGES: readonly (readonly [number, number])[] = (() => {
  const edges: [number, number][] = [];
  CELL_CORNERS.forEach((p, i) => {
    CELL_CORNERS.forEach((q, j) => {
      if (j <= i) return;
      const diff = Math.abs(p[0] - q[0]) + Math.abs(p[1] - q[1]) + Math.abs(p[2] - q[2]);
      if (diff === 1) edges.push([i, j]);
    });
  });
  return edges;
})();

/** Lattice parameters (a, b, c in Å; alpha, beta, gamma in degrees) -> row matrix (§4.1 orientation). */
export function matrixFromParameters(a: number, b: number, c: number, alpha: number, beta: number, gamma: number): number[][] {
  const r = Math.PI / 180;
  const ca = Math.cos(alpha * r), cb = Math.cos(beta * r), cg = Math.cos(gamma * r), sg = Math.sin(gamma * r);
  const cy = (ca - cb * cg) / sg;
  const cz = Math.sqrt(Math.max(0, 1 - cb * cb - cy * cy));
  return [
    [a, 0, 0],
    [b * cg, b * sg, 0],
    [c * cb, c * cy, c * cz],
  ];
}
