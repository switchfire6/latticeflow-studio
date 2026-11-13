/**
 * Force arrows (§6.2.3): one instanced cone-arrow per atom along F_i, starting
 * at the atom surface, length ∝ log(1 + |F|), colour emerald (< 0.05 eV/Å) →
 * neon orange (> 0.5 eV/Å) with HDR emission so strong forces bloom.
 * Arrows follow the drag override and phonon displacement like the atoms.
 */
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { Vec3 } from "../../utils/latticeMath";
import { atomDisplayRadius } from "./InstancedAtoms";
import { createArrowGeometry, createEmissiveInstanceMaterial, forceArrowLength, forceColor, growCapacity, writeSegment } from "./sceneUtils";

const MIN_FORCE = 1e-3; // eV/Å; smaller forces draw no arrow
const _p: Vec3 = [0, 0, 0];
const _c = new THREE.Color();

export function ForceVectors() {
  const visible = useCrystalStore((s) => s.display.forceVectors);
  const spaceFilling = useCrystalStore((s) => s.display.representation === "space-filling");
  const [capacity, setCapacity] = useState(64);
  const ref = useRef<THREE.InstancedMesh>(null);
  const geometry = useMemo(() => createArrowGeometry(), []);
  const material = useMemo(() => createEmissiveInstanceMaterial(0.55, { roughness: 0.3, metalness: 0.1, toneMapped: false }), []);
  useEffect(
    () => () => {
      geometry.dispose();
      material.dispose();
    },
    [geometry, material],
  );

  useFrame(() => {
    const f = useCrystalStore.getState().frame;
    if (f.n > capacity) {
      setCapacity(growCapacity(f.n));
      return;
    }
    const mesh = ref.current;
    if (!mesh) return;
    mesh.visible = visible && f.n > 0;
    if (!mesh.visible) return;
    let k = 0;
    for (let i = 0; i < f.n; i++) {
      const fn = f.forceNorm[i]!;
      if (!(fn > MIN_FORCE)) continue;
      const fx = f.forces[3 * i]! / fn;
      const fy = f.forces[3 * i + 1]! / fn;
      const fz = f.forces[3 * i + 2]! / fn;
      const len = forceArrowLength(fn);
      const r0 = atomDisplayRadius(f.numbers[i]!, spaceFilling) * 0.92;
      f.position(i, 0, 0, 0, _p);
      writeSegment(mesh, k, _p[0] + fx * r0, _p[1] + fy * r0, _p[2] + fz * r0, fx * len, fy * len, fz * len, 1, false);
      mesh.setColorAt(k, forceColor(fn, _c));
      k++;
    }
    mesh.count = k;
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  });

  return <instancedMesh key={capacity} ref={ref} args={[geometry, material, capacity]} frustumCulled={false} raycast={() => null} />;
}
