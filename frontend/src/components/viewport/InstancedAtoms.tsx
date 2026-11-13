/**
 * GPU-instanced atoms (§6.2.1).
 *
 *  * cell layer: one opaque InstancedMesh with every periodic image that lies
 *    in the closed unit cell (boundary atoms on all faces / edges / corners),
 *    home images first (see displaySet.ts);
 *  * ghost 2x2x2 halo (opacity 0.22, optional) on a second InstancedMesh;
 *  * Fresnel-rim overlays: cyan for hover / selection, pulsing crimson for the
 *    phonon unstable actors.
 *
 * Instance matrices are written in useFrame straight from the transient frame
 * buffers (positions include the drag override and the phonon displacement of
 * each image); React only re-renders for display toggles, hover and capacity growth.
 *
 * Drag (DRAG_ATOM mode): pointer-down on any drawn copy of an atom disables
 * OrbitControls, fixes a camera-facing plane through that copy and keeps the
 * grab offset; window pointermove events are raycast onto the plane, converted
 * into the cell frame, shifted back to the home image and fed to DragController
 * (optimistic override + 30 Hz PERTURB_OR_TRANSMUTE_ATOM, final position on release).
 * A press-and-release without motion selects the atom (opens the toolbar).
 */
import { Html } from "@react-three/drei";
import { useFrame, useThree, type ThreeEvent } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { DragController } from "../../services/dragController";
import { commands } from "../../services/wsClient";
import { useCrystalStore } from "../../store/useCrystalStore";
import { BALL_AND_STICK_SCALE, SPACE_FILLING_SCALE, cpkColor, covalentRadius, elementByZ } from "../../utils/cpkColors";
import type { Vec3 } from "../../utils/latticeMath";
import { getDisplaySet, type DrawnImage } from "./displaySet";
import { COLORS, HALO_OPACITY, createRimMaterial, growCapacity, writeScaleTranslate } from "./sceneUtils";

const DRAG_START_PX = 3;
const _p: Vec3 = [0, 0, 0];
const _color = new THREE.Color();
const SELECTED_TINT = new THREE.Color("#22D3EE").lerp(new THREE.Color(1, 1, 1), 0.45);

export function atomDisplayRadius(z: number, spaceFilling: boolean): number {
  return covalentRadius(z) * (spaceFilling ? SPACE_FILLING_SCALE : BALL_AND_STICK_SCALE);
}

interface OrbitLike {
  enabled: boolean;
}

export function InstancedAtoms() {
  const representation = useCrystalStore((s) => s.display.representation);
  const ghostHalo = useCrystalStore((s) => s.display.ghostHalo);
  const hoveredAtom = useCrystalStore((s) => s.hoveredAtom);
  const spaceFilling = representation === "space-filling";

  const [capacity, setCapacity] = useState({ cell: 64, ghost: 64 });
  const atomsRef = useRef<THREE.InstancedMesh>(null);
  const haloRef = useRef<THREE.InstancedMesh>(null);
  const rimRef = useRef<THREE.InstancedMesh>(null);
  const actorRimRef = useRef<THREE.InstancedMesh>(null);
  const badgeRef = useRef<THREE.Group>(null);
  /** The drawn copy under the pointer (badge position). */
  const hoverImage = useRef<DrawnImage | null>(null);

  const sphere = useMemo(() => new THREE.SphereGeometry(1, 36, 24), []);
  const atomMaterial = useMemo(
    () => new THREE.MeshStandardMaterial({ roughness: 0.3, metalness: 0.12, envMapIntensity: 1.0 }),
    [],
  );
  const haloMaterial = useMemo(
    () =>
      new THREE.MeshStandardMaterial({
        roughness: 0.5,
        metalness: 0.0,
        transparent: true,
        opacity: HALO_OPACITY,
        depthWrite: false,
      }),
    [],
  );
  const rimMaterial = useMemo(() => createRimMaterial(2.0, 2.2, 0), []);
  const actorRimMaterial = useMemo(() => createRimMaterial(1.3, 4.5, 1), []);
  useEffect(
    () => () => {
      sphere.dispose();
      atomMaterial.dispose();
      haloMaterial.dispose();
      rimMaterial.dispose();
      actorRimMaterial.dispose();
    },
    [sphere, atomMaterial, haloMaterial, rimMaterial, actorRimMaterial],
  );

  const camera = useThree((s) => s.camera);
  const gl = useThree((s) => s.gl);
  const controls = useThree((s) => s.controls) as unknown as OrbitLike | null;

  const drag = useMemo(
    () => new DragController(useCrystalStore.getState().frame, (i, cart) => commands.moveAtom(i, cart)),
    [],
  );
  const dragState = useRef<{ index: number; image: DrawnImage; cleanup: () => void } | null>(null);
  useEffect(() => () => dragState.current?.cleanup(), []);

  const lastColorKey = useRef("");

  useFrame(({ clock }) => {
    const st = useCrystalStore.getState();
    const f = st.frame;
    const ds = getDisplaySet(f, ghostHalo);
    const nCell = ds.cell.length;
    const nGhost = ghostHalo ? ds.ghost.length : 0;
    if (nCell > capacity.cell || nGhost > capacity.ghost) {
      setCapacity({ cell: growCapacity(nCell), ghost: growCapacity(nGhost) });
      return;
    }
    const atoms = atomsRef.current;
    const halo = haloRef.current;
    const rim = rimRef.current;
    const actorRim = actorRimRef.current;
    if (!atoms || !halo || !rim || !actorRim) return;

    // colours change only with the drawn set (topology / boundary membership) or a new mesh
    const colorKey = `${f.topologyVersion}:${ds.version}:${ghostHalo}:${capacity.cell}:${capacity.ghost}`;
    const recolor = colorKey !== lastColorKey.current;
    lastColorKey.current = colorKey;

    const arr = atoms.instanceMatrix.array as Float32Array;
    for (let k = 0; k < nCell; k++) {
      const d = ds.cell[k]!;
      f.position(d.atom, d.n0, d.n1, d.n2, _p);
      const z = f.numbers[d.atom]!;
      writeScaleTranslate(arr, k, _p[0], _p[1], _p[2], atomDisplayRadius(z, spaceFilling));
      if (recolor) atoms.setColorAt(k, _color.setHex(cpkColor(z)));
    }
    atoms.count = nCell;
    atoms.instanceMatrix.needsUpdate = true;
    if (recolor && atoms.instanceColor) atoms.instanceColor.needsUpdate = true;
    atoms.computeBoundingSphere(); // raycasting relies on it and the instances move

    if (nGhost > 0) {
      const harr = halo.instanceMatrix.array as Float32Array;
      for (let k = 0; k < nGhost; k++) {
        const d = ds.ghost[k]!;
        f.position(d.atom, d.n0, d.n1, d.n2, _p);
        const z = f.numbers[d.atom]!;
        writeScaleTranslate(harr, k, _p[0], _p[1], _p[2], atomDisplayRadius(z, spaceFilling));
        if (recolor) halo.setColorAt(k, _color.setHex(cpkColor(z)));
      }
      halo.instanceMatrix.needsUpdate = true;
      if (recolor && halo.instanceColor) halo.instanceColor.needsUpdate = true;
    }
    halo.count = nGhost;
    halo.visible = nGhost > 0;

    // hover / selection rims on every drawn copy of the atom (cyan; selected whiter)
    const rimArr = rim.instanceMatrix.array as Float32Array;
    const rimCap = rim.instanceMatrix.count;
    let r = 0;
    const hovered = st.hoveredAtom;
    const selected = st.selectedAtom;
    for (let k = 0; k < nCell && r < rimCap; k++) {
      const d = ds.cell[k]!;
      const isHover = d.atom === hovered;
      if (!isHover && d.atom !== selected) continue;
      f.position(d.atom, d.n0, d.n1, d.n2, _p);
      writeScaleTranslate(rimArr, r, _p[0], _p[1], _p[2], atomDisplayRadius(f.numbers[d.atom]!, spaceFilling) * 1.1);
      rim.setColorAt(r, isHover ? COLORS.cyan : SELECTED_TINT);
      r++;
    }
    rim.count = r;
    rim.instanceMatrix.needsUpdate = true;
    if (rim.instanceColor) rim.instanceColor.needsUpdate = true;

    // pulsing crimson rims on the atoms flagged by the phonon shake-test
    const aArr = actorRim.instanceMatrix.array as Float32Array;
    let a = 0;
    for (let k = 0; k < nCell; k++) {
      const d = ds.cell[k]!;
      if (!f.unstable[d.atom]) continue;
      f.position(d.atom, d.n0, d.n1, d.n2, _p);
      writeScaleTranslate(aArr, a, _p[0], _p[1], _p[2], atomDisplayRadius(f.numbers[d.atom]!, spaceFilling) * 1.4);
      actorRim.setColorAt(a, COLORS.crimson);
      a++;
    }
    actorRim.count = a;
    actorRim.instanceMatrix.needsUpdate = true;
    if (actorRim.instanceColor) actorRim.instanceColor.needsUpdate = true;
    actorRimMaterial.uniforms.uTime!.value = clock.elapsedTime;

    // floating badge follows the hovered (or dragged) copy
    const img = dragState.current?.image ?? hoverImage.current;
    if (badgeRef.current && img && img.atom < f.n) {
      f.position(img.atom, img.n0, img.n1, img.n2, _p);
      const rad = atomDisplayRadius(f.numbers[img.atom]!, spaceFilling);
      badgeRef.current.position.set(_p[0], _p[1], _p[2] + rad + 0.25);
    }
  });

  const imageOf = (instanceId: number | undefined): DrawnImage | null => {
    if (instanceId === undefined) return null;
    const ds = getDisplaySet(useCrystalStore.getState().frame, useCrystalStore.getState().display.ghostHalo);
    return ds.cell[instanceId] ?? null;
  };

  // ---------------------------------------------------------------- pointer handlers
  const onPointerMove = (e: ThreeEvent<PointerEvent>) => {
    if (dragState.current) return;
    const img = imageOf(e.instanceId);
    if (!img) return;
    e.stopPropagation();
    hoverImage.current = img;
    useCrystalStore.getState().setHoveredAtom(img.atom);
    gl.domElement.style.cursor = useCrystalStore.getState().toolMode === "DRAG_ATOM" ? "grab" : "pointer";
  };

  const onPointerOut = () => {
    if (dragState.current) return;
    hoverImage.current = null;
    useCrystalStore.getState().setHoveredAtom(null);
    gl.domElement.style.cursor = "";
  };

  const onClick = (e: ThreeEvent<MouseEvent>) => {
    const img = imageOf(e.instanceId);
    if (!img) return;
    e.stopPropagation();
    const st = useCrystalStore.getState();
    if (st.toolMode !== "DRAG_ATOM") st.selectAtom(img.atom);
  };

  const onPointerDown = (e: ThreeEvent<PointerEvent>) => {
    if (e.button !== 0) return;
    const img = imageOf(e.instanceId);
    const st = useCrystalStore.getState();
    if (!img || st.toolMode !== "DRAG_ATOM") return;
    e.stopPropagation();
    const mesh = atomsRef.current;
    const cellGroup = mesh?.parent;
    if (!mesh || !cellGroup) return;
    const index = img.atom;
    const f = st.frame;
    // home-cell position and the lattice translation of the grabbed copy
    const home: Vec3 = [0, 0, 0];
    f.basePosition(index, home);
    const L = f.lattice;
    const shift = new THREE.Vector3(
      img.n0 * L[0]! + img.n1 * L[3]! + img.n2 * L[6]!,
      img.n0 * L[1]! + img.n1 * L[4]! + img.n2 * L[7]!,
      img.n0 * L[2]! + img.n1 * L[5]! + img.n2 * L[8]!,
    );
    const atomWorld = cellGroup.localToWorld(new THREE.Vector3(home[0], home[1], home[2]).add(shift));
    const normal = camera.getWorldDirection(new THREE.Vector3()).negate();
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, atomWorld);
    const hit = new THREE.Vector3();
    const ray = new THREE.Ray(camera.position.clone(), e.point.clone().sub(camera.position).normalize());
    const offset = ray.intersectPlane(plane, hit) ? atomWorld.clone().sub(hit) : new THREE.Vector3();

    if (controls) controls.enabled = false;
    gl.domElement.style.cursor = "grabbing";
    drag.begin(index, home);
    st.setHoveredAtom(index);

    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const startX = e.nativeEvent.clientX;
    const startY = e.nativeEvent.clientY;
    let moved = false;

    const onMove = (ev: PointerEvent) => {
      if (!moved && Math.hypot(ev.clientX - startX, ev.clientY - startY) < DRAG_START_PX) return;
      moved = true;
      const rect = gl.domElement.getBoundingClientRect();
      ndc.set(((ev.clientX - rect.left) / rect.width) * 2 - 1, -((ev.clientY - rect.top) / rect.height) * 2 + 1);
      raycaster.setFromCamera(ndc, camera);
      if (!raycaster.ray.intersectPlane(plane, hit)) return;
      const local = cellGroup.worldToLocal(hit.clone().add(offset)).sub(shift);
      drag.move([local.x, local.y, local.z]);
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      dragState.current = null;
      if (controls) controls.enabled = true;
      gl.domElement.style.cursor = "";
    };
    function onUp() {
      cleanup();
      const wasDrag = drag.end();
      if (!wasDrag) useCrystalStore.getState().selectAtom(index);
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    dragState.current = { index, image: img, cleanup };
  };

  return (
    <>
      <instancedMesh
        key={`atoms-${capacity.cell}`}
        name="lf-atoms"
        ref={atomsRef}
        args={[sphere, atomMaterial, capacity.cell]}
        frustumCulled={false}
        onPointerMove={onPointerMove}
        onPointerOut={onPointerOut}
        onPointerDown={onPointerDown}
        onClick={onClick}
      />
      <instancedMesh
        key={`halo-${capacity.ghost}`}
        ref={haloRef}
        args={[sphere, haloMaterial, capacity.ghost]}
        frustumCulled={false}
        raycast={() => null}
        renderOrder={1}
      />
      <instancedMesh ref={rimRef} args={[sphere, rimMaterial, 32]} frustumCulled={false} raycast={() => null} renderOrder={3} />
      <instancedMesh
        key={`actors-${capacity.cell}`}
        ref={actorRimRef}
        args={[sphere, actorRimMaterial, capacity.cell]}
        frustumCulled={false}
        raycast={() => null}
        renderOrder={3}
      />
      <group ref={badgeRef}>{hoveredAtom !== null && <AtomBadge index={hoveredAtom} />}</group>
    </>
  );
}

function AtomBadge({ index }: { index: number }) {
  const atom = useCrystalStore((s) => s.crystal?.atoms[index] ?? null);
  if (!atom) return null;
  const el = elementByZ(atom.atomic_number);
  return (
    <Html center zIndexRange={[20, 0]} style={{ pointerEvents: "none" }}>
      <div className="whitespace-nowrap rounded-md border border-cyan-400/40 bg-slate-950/85 px-2 py-0.5 font-mono text-[11px] text-cyan-100 shadow-[0_0_12px_rgba(34,211,238,0.25)] backdrop-blur">
        #{index} <span className="font-semibold text-white">{el.symbol}</span> | |F| = {atom.force_norm.toFixed(2)} eV/Å
      </div>
    </Html>
  );
}
