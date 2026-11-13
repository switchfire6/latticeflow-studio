/**
 * Triclinic unit-cell wireframe (§6.2): the 12 edges of the parallelepiped
 * spanned by the rows a, b, c of the live lattice matrix, rebuilt whenever the
 * frame buffers change (flow and relaxation deform it). The a, b, c edges from
 * the origin are coloured and labelled; with the halo on, the 2x2x2 supercell
 * outline is drawn faintly.
 */
import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { useCrystalStore } from "../../store/useCrystalStore";
import { CELL_CORNERS, CELL_EDGES } from "../../utils/latticeMath";

const AXIS_COLORS = [new THREE.Color("#f87171"), new THREE.Color("#4ade80"), new THREE.Color("#60a5fa")];
const EDGE_COLOR = new THREE.Color("#5eead4").multiplyScalar(0.75);

function fillEdges(pos: Float32Array, L: Float64Array, scale: number): void {
  let v = 0;
  for (const [i, j] of CELL_EDGES) {
    for (const c of [CELL_CORNERS[i]!, CELL_CORNERS[j]!]) {
      pos[v++] = scale * (c[0] * L[0]! + c[1] * L[3]! + c[2] * L[6]!);
      pos[v++] = scale * (c[0] * L[1]! + c[1] * L[4]! + c[2] * L[7]!);
      pos[v++] = scale * (c[0] * L[2]! + c[1] * L[5]! + c[2] * L[8]!);
    }
  }
}

export function UnitCellBox() {
  const visible = useCrystalStore((s) => s.display.unitCell);
  const ghostHalo = useCrystalStore((s) => s.display.ghostHalo);
  const cellRef = useRef<THREE.LineSegments>(null);
  const superRef = useRef<THREE.LineSegments>(null);
  const labelRefs = [useRef<THREE.Group>(null), useRef<THREE.Group>(null), useRef<THREE.Group>(null)];
  const lastVersion = useRef(-1);

  const { cellGeom, superGeom } = useMemo(() => {
    const cellGeom = new THREE.BufferGeometry();
    cellGeom.setAttribute("position", new THREE.BufferAttribute(new Float32Array(CELL_EDGES.length * 6), 3));
    const colors = new Float32Array(CELL_EDGES.length * 6);
    CELL_EDGES.forEach(([i, j], e) => {
      // an edge from the origin along a single axis gets that axis colour
      const axis = i === 0 ? CELL_CORNERS[j]!.findIndex((x) => x === 1) : -1;
      const c = axis >= 0 ? AXIS_COLORS[axis]! : EDGE_COLOR;
      colors.set([c.r, c.g, c.b, c.r, c.g, c.b], e * 6);
    });
    cellGeom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    const superGeom = new THREE.BufferGeometry();
    superGeom.setAttribute("position", new THREE.BufferAttribute(new Float32Array(CELL_EDGES.length * 6), 3));
    return { cellGeom, superGeom };
  }, []);
  const cellMat = useMemo(() => new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.9, toneMapped: false }), []);
  const superMat = useMemo(() => new THREE.LineBasicMaterial({ color: "#5eead4", transparent: true, opacity: 0.18, depthWrite: false }), []);
  useEffect(
    () => () => {
      cellGeom.dispose();
      superGeom.dispose();
      cellMat.dispose();
      superMat.dispose();
    },
    [cellGeom, superGeom, cellMat, superMat],
  );

  useFrame(() => {
    const f = useCrystalStore.getState().frame;
    const L = f.lattice;
    labelRefs.forEach((r, k) => {
      r.current?.position.set(L[3 * k]! * 1.06, L[3 * k + 1]! * 1.06, L[3 * k + 2]! * 1.06);
    });
    if (f.version === lastVersion.current) return;
    lastVersion.current = f.version;
    const p1 = cellGeom.getAttribute("position") as THREE.BufferAttribute;
    fillEdges(p1.array as Float32Array, L, 1);
    p1.needsUpdate = true;
    cellGeom.computeBoundingSphere();
    const p2 = superGeom.getAttribute("position") as THREE.BufferAttribute;
    fillEdges(p2.array as Float32Array, L, 2);
    p2.needsUpdate = true;
    superGeom.computeBoundingSphere();
  });

  const hasStructure = useCrystalStore((s) => s.crystal !== null);
  // Mount the drei <Html> labels one frame after the canvas: mounting their
  // React roots inside the canvas's first (StrictMode) commit logs
  // "Attempted to synchronously unmount a root while React was already rendering".
  const [labelsReady, setLabelsReady] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => setLabelsReady(true));
    return () => cancelAnimationFrame(id);
  }, []);
  if (!visible) return null;
  return (
    <>
      <lineSegments ref={cellRef} geometry={cellGeom} material={cellMat} raycast={() => null} />
      <lineSegments ref={superRef} geometry={superGeom} material={superMat} visible={ghostHalo} raycast={() => null} />
      {hasStructure &&
        labelsReady &&
        (["a", "b", "c"] as const).map((label, k) => (
          <group key={label} ref={labelRefs[k]}>
            <Html center zIndexRange={[10, 0]} style={{ pointerEvents: "none" }}>
              <span className="font-mono text-[11px] font-semibold italic" style={{ color: `#${AXIS_COLORS[k]!.getHexString()}` }}>
                {label}
              </span>
            </Html>
          </group>
        ))}
    </>
  );
}
