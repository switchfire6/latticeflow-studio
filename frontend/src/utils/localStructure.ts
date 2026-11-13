/**
 * Local-structure helpers (IMPROVEMENT_SUGGESTIONS.md §5.7), pure and unit-tested:
 * Glazer notation with superscripts, bond-valence deviation of a bond, the
 * alignment of LOCAL_ANALYSIS_DATA.bond_ideal_lengths with the bonds on
 * screen, BVS deviation tones and the convex hull of a coordination polyhedron.
 */
import type { BondEdge, LocalAnalysisData } from "../types/protocol";
import type { Vec3 } from "./latticeMath";

const SUP: Record<string, string> = { "0": "⁰", "+": "⁺", "-": "⁻", "−": "⁻" };

/** "a0a0c-" -> "a⁰a⁰c⁻" (unparseable input is returned unchanged). */
export function glazerDisplay(glazer: string | null | undefined): string {
  if (!glazer) return "—";
  const parts = [...glazer.matchAll(/([a-cA-C])\s*([0+\-−])/g)];
  if (parts.length !== 3) return glazer;
  return parts.map((m) => `${m[1]!.toLowerCase()}${SUP[m[2]!]}`).join("");
}

/** Identity of a bond across payloads (source, target, image offset). */
export function bondKey(b: Pick<BondEdge, "source_index" | "target_index" | "image_offset">): string {
  return `${b.source_index}:${b.target_index}:${b.image_offset[0]},${b.image_offset[1]},${b.image_offset[2]}`;
}

/** key -> ideal length from an analysis aligned with `bonds` (the payload it was computed for). */
export function idealLengthMap(bonds: readonly BondEdge[], ideals: LocalAnalysisData["bond_ideal_lengths"]): Map<string, number> {
  const m = new Map<string, number>();
  if (!ideals || ideals.length !== bonds.length) return m;
  bonds.forEach((b, i) => {
    const v = ideals[i];
    if (v != null && v > 0) m.set(bondKey(b), v);
  });
  return m;
}

/** Ideal lengths aligned with `bonds` (NaN where unknown). */
export function alignIdeals(bonds: readonly BondEdge[], map: Map<string, number>): Float32Array {
  const out = new Float32Array(bonds.length);
  bonds.forEach((b, i) => (out[i] = map.get(bondKey(b)) ?? Number.NaN));
  return out;
}

/** Bond-valence deviation (length − ideal) / ideal; null when the ideal is unknown. */
export function bondValenceDeviation(length: number, ideal: number | null | undefined): number | null {
  if (ideal == null || !(ideal > 0) || !Number.isFinite(length)) return null;
  return (length - ideal) / ideal;
}

/** |BVS| vs |oxidation state| tone: ≤ 0.1 v.u. fine, ≤ 0.25 notable, above strained. */
export function bvsTone(bvs: number | null, ox: number | null): "ok" | "warn" | "bad" | "none" {
  if (bvs == null || ox == null || ox === 0) return "none";
  const d = Math.abs(Math.abs(bvs) - Math.abs(ox));
  return d <= 0.1 ? "ok" : d <= 0.25 ? "warn" : "bad";
}

/** GII reading: < 0.1 v.u. unstrained, < 0.2 typical of stable structures, above strained / unstable. */
export function giiVerdict(gii: number | null): { text: string; tone: "green" | "amber" | "red" | "slate" } {
  if (gii == null) return { text: "n/a", tone: "slate" };
  if (gii < 0.1) return { text: "unstrained", tone: "green" };
  if (gii < 0.2) return { text: "typical", tone: "green" };
  if (gii < 0.4) return { text: "strained", tone: "amber" };
  return { text: "highly strained", tone: "red" };
}

/** Partial-RDF series keys, element pairs first by appearance. */
export function partialPairs(partial: LocalAnalysisData["partial_rdf"]): string[] {
  return partial ? Object.keys(partial).sort() : [];
}

// --------------------------------------------------------------------------- //
// Convex hull of a few points (coordination polyhedra, ≤ ~16 ligands)
// --------------------------------------------------------------------------- //
const sub = (a: Vec3, b: Vec3): Vec3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a: Vec3, b: Vec3): Vec3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a: Vec3, b: Vec3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

/**
 * Faces of the convex hull as index loops, counter-clockwise seen from outside.
 * Coplanar points of a face are merged into one polygon; a flat point set
 * (square-planar, trigonal-planar) yields a single polygon. Brute force O(n⁴).
 */
export function convexHullFaces(pts: readonly Vec3[], eps = 1e-3): number[][] {
  const n = pts.length;
  if (n < 3) return [];
  const c: Vec3 = [0, 0, 0];
  for (const p of pts) (c[0] += p[0] / n), (c[1] += p[1] / n), (c[2] += p[2] / n);
  let scale = 0;
  for (const p of pts) scale = Math.max(scale, Math.hypot(...sub(p, c)));
  const tol = eps * Math.max(1, scale);
  const faces: number[][] = [];
  const seen = new Set<string>();
  for (let i = 0; i < n; i++)
    for (let j = i + 1; j < n; j++)
      for (let k = j + 1; k < n; k++) {
        let nrm = cross(sub(pts[j]!, pts[i]!), sub(pts[k]!, pts[i]!));
        const len = Math.hypot(...nrm);
        if (len < tol * tol) continue;
        nrm = [nrm[0] / len, nrm[1] / len, nrm[2] / len];
        let pos = 0;
        let neg = 0;
        const on: number[] = [];
        for (let m = 0; m < n; m++) {
          const d = dot(nrm, sub(pts[m]!, pts[i]!));
          if (d > tol) pos++;
          else if (d < -tol) neg++;
          else on.push(m);
        }
        if (pos && neg) continue;
        const key = on.join(",");
        if (seen.has(key)) continue;
        seen.add(key);
        // outward normal: away from the centroid (flat sets: keep as is)
        if (pos > 0) nrm = [-nrm[0], -nrm[1], -nrm[2]];
        // order the coplanar points around their centroid
        const fc: Vec3 = [0, 0, 0];
        for (const m of on) (fc[0] += pts[m]![0] / on.length), (fc[1] += pts[m]![1] / on.length), (fc[2] += pts[m]![2] / on.length);
        const u0 = sub(pts[on[0]!]!, fc);
        const ul = Math.hypot(...u0) || 1;
        const u: Vec3 = [u0[0] / ul, u0[1] / ul, u0[2] / ul];
        const v = cross(nrm, u);
        const loop = [...on].sort((a, b) => {
          const da = sub(pts[a]!, fc);
          const db = sub(pts[b]!, fc);
          return Math.atan2(dot(da, v), dot(da, u)) - Math.atan2(dot(db, v), dot(db, u));
        });
        faces.push(loop);
      }
  return faces;
}
