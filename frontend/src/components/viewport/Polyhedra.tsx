/**
 * Translucent coordination polyhedra (§5.7): the convex hull of each
 * polyhedron's ligands (LOCAL_ANALYSIS_DATA.polyhedra), coloured by the
 * centre element, drawn around every displayed copy of the centre atom (cell
 * layer; the 2×2×2 halo copies fainter, like the ghost atoms). Vertices follow
 * the transient frame buffers each frame (drag override, phonon animation),
 * so polyhedra move with the atoms; the face topology is rebuilt only when the
 * analysis or the display set changes. Ligand image = centre image + offset.
 */
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { useCrystalStore, type LocalAnalysisState } from "../../store/useCrystalStore";
import { cpkHex } from "../../utils/cpkColors";
import { convexHullFaces } from "../../utils/localStructure";
import type { Vec3 } from "../../utils/latticeMath";
import { getDisplaySet, type DrawnImage } from "./displaySet";
import { HALO_OPACITY } from "./sceneUtils";

interface Corner {
  atom: number;
  n0: number;
  n1: number;
  n2: number;
}
interface Layout {
  key: string;
  /** Triangles: 3 corners each. */
  tris: Corner[];
  triColor: number[];
  /** Edges: 2 corners each. */
  edges: Corner[];
  edgeColor: number[];
}

const _p: Vec3 = [0, 0, 0];
const MAX_FRAMEWORK_CN = 8;

function buildLayout(f: ReturnType<typeof useCrystalStore.getState>["frame"], la: LocalAnalysisState, images: DrawnImage[], key: string): Layout {
  const out: Layout = { key, tris: [], triColor: [], edges: [], edgeColor: [] };
  const byCenter = new Map<number, DrawnImage[]>();
  for (const im of images) {
    const l = byCenter.get(im.atom);
    if (l) l.push(im);
    else byCenter.set(im.atom, [im]);
  }
  const col = new THREE.Color();
  // Large cages (CN > 8, e.g. the perovskite A site) hide the framework polyhedra, so
  // they are drawn only when the structure has nothing smaller.
  const hasFramework = la.data.polyhedra.some((p) => p.ligand_indices.length >= 3 && p.ligand_indices.length <= MAX_FRAMEWORK_CN);
  for (const poly of la.data.polyhedra) {
    const ims = byCenter.get(poly.center_index);
    const n = poly.ligand_indices.length;
    if (!ims || n < 3 || poly.ligand_indices.some((i) => i >= f.n)) continue;
    if (hasFramework && n > MAX_FRAMEWORK_CN) continue;
    // hull topology from the home-image geometry
    const pts: Vec3[] = poly.ligand_indices.map((li, k) => {
      const o = poly.ligand_image_offsets[k] ?? [0, 0, 0];
      f.position(li, o[0], o[1], o[2], _p);
      return [_p[0], _p[1], _p[2]];
    });
    const faces = convexHullFaces(pts, 0.02);
    if (!faces.length) continue;
    col.set(cpkHex(f.numbers[poly.center_index] ?? 0));
    const edgeSet = new Set<string>();
    const edges: [number, number][] = [];
    for (const face of faces) {
      for (let k = 0; k < face.length; k++) {
        const a = face[k]!;
        const b = face[(k + 1) % face.length]!;
        const ek = a < b ? `${a}-${b}` : `${b}-${a}`;
        if (!edgeSet.has(ek)) {
          edgeSet.add(ek);
          edges.push([a, b]);
        }
      }
    }
    for (const im of ims) {
      const corner = (k: number): Corner => {
        const o = poly.ligand_image_offsets[k] ?? [0, 0, 0];
        return { atom: poly.ligand_indices[k]!, n0: im.n0 + o[0], n1: im.n1 + o[1], n2: im.n2 + o[2] };
      };
      for (const face of faces)
        for (let k = 1; k + 1 < face.length; k++) {
          out.tris.push(corner(face[0]!), corner(face[k]!), corner(face[k + 1]!));
          for (let r = 0; r < 3; r++) out.triColor.push(col.r, col.g, col.b);
        }
      for (const [a, b] of edges) {
        out.edges.push(corner(a), corner(b));
        for (let r = 0; r < 2; r++) out.edgeColor.push(col.r, col.g, col.b);
      }
    }
  }
  return out;
}

function PolyLayer({ ghost }: { ghost: boolean }) {
  const meshRef = useRef<THREE.Mesh>(null);
  const lineRef = useRef<THREE.LineSegments>(null);
  const layout = useRef<Layout | null>(null);
  const geo = useMemo(() => new THREE.BufferGeometry(), []);
  const lineGeo = useMemo(() => new THREE.BufferGeometry(), []);
  const mat = useMemo(
    () =>
      new THREE.MeshStandardMaterial({
        vertexColors: true,
        transparent: true,
        opacity: ghost ? HALO_OPACITY * 0.8 : 0.32,
        roughness: 0.4,
        metalness: 0.05,
        side: THREE.DoubleSide,
        depthWrite: false,
      }),
    [ghost],
  );
  const lineMat = useMemo(() => new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: ghost ? 0.25 : 0.7, depthWrite: false }), [ghost]);
  useEffect(
    () => () => {
      geo.dispose();
      lineGeo.dispose();
      mat.dispose();
      lineMat.dispose();
    },
    [geo, lineGeo, mat, lineMat],
  );

  useFrame(() => {
    const st = useCrystalStore.getState();
    const f = st.frame;
    const la = st.localAnalysis;
    const show = st.display.polyhedra && !!la && la.nAtoms === f.n && f.n > 0 && (!ghost || st.display.ghostHalo);
    if (meshRef.current) meshRef.current.visible = show;
    if (lineRef.current) lineRef.current.visible = show;
    if (!show || !la) return;
    const ds = getDisplaySet(f, st.display.ghostHalo);
    const key = `${ds.version}:${ds.halo}:${f.topologyVersion}`;
    if (!layout.current || layout.current.key !== key || (layout.current as Layout & { la?: unknown }).la !== la) {
      const images = ghost ? ds.ghost : ds.cell;
      const l = buildLayout(f, la, images, key);
      (l as Layout & { la?: unknown }).la = la;
      layout.current = l;
      geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(l.tris.length * 3), 3));
      geo.setAttribute("normal", new THREE.BufferAttribute(new Float32Array(l.tris.length * 3), 3));
      geo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(l.triColor), 3));
      lineGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(l.edges.length * 3), 3));
      lineGeo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(l.edgeColor), 3));
    }
    const l = layout.current;
    const pos = geo.getAttribute("position") as THREE.BufferAttribute;
    const arr = pos.array as Float32Array;
    l.tris.forEach((c, i) => {
      f.position(c.atom, c.n0, c.n1, c.n2, _p);
      arr[3 * i] = _p[0];
      arr[3 * i + 1] = _p[1];
      arr[3 * i + 2] = _p[2];
    });
    pos.needsUpdate = true;
    geo.computeVertexNormals();
    geo.computeBoundingSphere();
    const lp = lineGeo.getAttribute("position") as THREE.BufferAttribute;
    const la2 = lp.array as Float32Array;
    l.edges.forEach((c, i) => {
      f.position(c.atom, c.n0, c.n1, c.n2, _p);
      la2[3 * i] = _p[0];
      la2[3 * i + 1] = _p[1];
      la2[3 * i + 2] = _p[2];
    });
    lp.needsUpdate = true;
    lineGeo.computeBoundingSphere();
  });

  return (
    <>
      <mesh ref={meshRef} geometry={geo} material={mat} frustumCulled={false} raycast={() => null} renderOrder={2} />
      <lineSegments ref={lineRef} geometry={lineGeo} material={lineMat} frustumCulled={false} raycast={() => null} renderOrder={2} />
    </>
  );
}

export function Polyhedra() {
  return (
    <>
      <PolyLayer ghost={false} />
      <PolyLayer ghost />
    </>
  );
}
