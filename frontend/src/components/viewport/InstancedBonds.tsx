/**
 * Instanced cylinder bonds (§6.2.2) built from `BondEdge.image_offset`.
 *
 * For every drawn image (s, m) of a bond's source atom the partner is image
 * m + image_offset of the target, r_t + (m + n)·L. If that image is drawn too
 * the full bond is drawn once; otherwise each drawn end gets a half-bond stub
 * to the midpoint (the mirror half of the boundary-crossing bond), so no stick
 * ends in empty space (see displaySet.ts). Endpoints include the drag override
 * and the phonon displacement of the exact image they belong to.
 *
 * Colour: brushed metallic slate at equilibrium, a diverging blue (compressed)
 * → amber (stretched) map by strain_ratio, and bonds flagged is_unstable pulse
 * emissive crimson #FF2A54 (HDR, so they bloom).
 */
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { Vec3 } from "../../utils/latticeMath";
import { getDisplaySet } from "./displaySet";
import { COLORS, HALO_OPACITY, growCapacity, strainColor, writeSegment } from "./sceneUtils";

export const BOND_RADIUS = 0.1;

const _a: Vec3 = [0, 0, 0];
const _b: Vec3 = [0, 0, 0];
const _c = new THREE.Color();

export function InstancedBonds() {
  const visible = useCrystalStore((s) => s.display.bonds);
  const ghostHalo = useCrystalStore((s) => s.display.ghostHalo);
  const spaceFilling = useCrystalStore((s) => s.display.representation === "space-filling");

  const [capacity, setCapacity] = useState(128);
  const bondsRef = useRef<THREE.InstancedMesh>(null);
  const unstableRef = useRef<THREE.InstancedMesh>(null);
  const haloRef = useRef<THREE.InstancedMesh>(null);

  const cylinder = useMemo(() => new THREE.CylinderGeometry(1, 1, 1, 16, 1, false), []);
  const material = useMemo(
    () => new THREE.MeshStandardMaterial({ roughness: 0.26, metalness: 0.7, envMapIntensity: 1.2 }),
    [],
  );
  const unstableMaterial = useMemo(() => new THREE.MeshBasicMaterial({ color: COLORS.crimson.clone(), toneMapped: false }), []);
  const haloMaterial = useMemo(
    () =>
      new THREE.MeshStandardMaterial({
        roughness: 0.5,
        metalness: 0.3,
        transparent: true,
        opacity: HALO_OPACITY,
        depthWrite: false,
      }),
    [],
  );
  useEffect(
    () => () => {
      cylinder.dispose();
      material.dispose();
      unstableMaterial.dispose();
      haloMaterial.dispose();
    },
    [cylinder, material, unstableMaterial, haloMaterial],
  );

  useFrame(({ clock }) => {
    const f = useCrystalStore.getState().frame;
    const ds = getDisplaySet(f, ghostHalo);
    const segs = ds.bonds;
    if (segs.length > capacity) {
      setCapacity(growCapacity(segs.length, 128));
      return;
    }
    const mesh = bondsRef.current;
    const unstable = unstableRef.current;
    const halo = haloRef.current;
    if (!mesh || !unstable || !halo) return;
    const show = visible && !spaceFilling && f.n > 0;
    mesh.visible = show;
    unstable.visible = show;
    halo.visible = show && ghostHalo;
    if (!show) return;

    const bonds = f.bonds;
    let k = 0; // normal
    let u = 0; // unstable
    let h = 0; // ghost
    for (const sgm of segs) {
      const bond = bonds[sgm.bond];
      if (!bond) continue;
      if (sgm.ghost && !ghostHalo) continue;
      f.position(sgm.from.atom, sgm.from.n0, sgm.from.n1, sgm.from.n2, _a);
      f.position(sgm.to.atom, sgm.to.n0, sgm.to.n1, sgm.to.n2, _b);
      let dx = _b[0] - _a[0];
      let dy = _b[1] - _a[1];
      let dz = _b[2] - _a[2];
      if (!sgm.full) {
        dx *= 0.5;
        dy *= 0.5;
        dz *= 0.5;
      }
      if (sgm.ghost) {
        writeSegment(halo, h, _a[0], _a[1], _a[2], dx, dy, dz, BOND_RADIUS, true);
        halo.setColorAt(h, bond.is_unstable ? COLORS.crimson : strainColor(bond.strain_ratio, _c));
        h++;
      } else if (bond.is_unstable) {
        writeSegment(unstable, u++, _a[0], _a[1], _a[2], dx, dy, dz, BOND_RADIUS * 1.15, true);
      } else {
        writeSegment(mesh, k, _a[0], _a[1], _a[2], dx, dy, dz, BOND_RADIUS, true);
        mesh.setColorAt(k, strainColor(bond.strain_ratio, _c));
        k++;
      }
    }
    mesh.count = k;
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    unstable.count = u;
    unstable.instanceMatrix.needsUpdate = true;
    halo.count = h;
    halo.instanceMatrix.needsUpdate = true;
    if (halo.instanceColor) halo.instanceColor.needsUpdate = true;
    const pulse = 0.5 + 0.5 * Math.sin(clock.elapsedTime * 5.0);
    unstableMaterial.color.copy(COLORS.crimson).multiplyScalar(1.0 + 1.8 * pulse);
  });

  return (
    <>
      <instancedMesh key={`b-${capacity}`} ref={bondsRef} args={[cylinder, material, capacity]} frustumCulled={false} raycast={() => null} />
      <instancedMesh key={`u-${capacity}`} ref={unstableRef} args={[cylinder, unstableMaterial, capacity]} frustumCulled={false} raycast={() => null} />
      <instancedMesh
        key={`h-${capacity}`}
        ref={haloRef}
        args={[cylinder, haloMaterial, capacity]}
        frustumCulled={false}
        raycast={() => null}
        renderOrder={1}
      />
    </>
  );
}
