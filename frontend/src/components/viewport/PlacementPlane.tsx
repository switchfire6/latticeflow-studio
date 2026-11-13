/**
 * Phase C2 click-to-place (§7.3): while the Add-atom dialog is in "pick in
 * viewport" mode, a translucent plane s_axis = depth spans the cell face and
 * the pointer ray is intersected with it analytically (utils/analysisC2.ts
 * rayCellPlaneFrac, so atoms in front never block the pick). A ghost atom
 * previews the hit; a click (no drag) commits the fractional coordinates
 * and reopens the dialog. The tool mode is VIEW while picking (no atom drags).
 */
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { useCrystalStore, type ToolMode } from "../../store/useCrystalStore";
import { useUi } from "../../store/uiStore";
import { rayCellPlaneFrac } from "../../utils/analysisC2";
import { cpkHex, elementBySymbol } from "../../utils/cpkColors";
import type { Vec3 } from "../../utils/latticeMath";
import { commitPlacement } from "../../services/placementActions";

const CLICK_SLOP_PX = 5;

export function PlacementPlane() {
  const placement = useUi((s) => s.placement);
  return placement ? <PlacementActive axis={placement.axis} depth={placement.depth} /> : null;
}

function PlacementActive({ axis, depth }: { axis: 0 | 1 | 2; depth: number }) {
  const group = useRef<THREE.Group>(null);
  const ghost = useRef<THREE.Mesh>(null);
  const gl = useThree((s) => s.gl);
  const camera = useThree((s) => s.camera);
  const element = useUi((s) => s.addAtomDraft.element);
  const structureVersion = useCrystalStore((s) => s.structureVersion);

  const geom = useMemo(() => {
    const L = useCrystalStore.getState().frame.lattice;
    const [i, j] = ([0, 1, 2] as const).filter((k) => k !== axis) as [number, number];
    const v = (k: number): Vec3 => [L[3 * k]!, L[3 * k + 1]!, L[3 * k + 2]!];
    const o = v(axis).map((x) => x * depth) as Vec3;
    const a = v(i);
    const b = v(j);
    const corner = (s: number, t: number) => [o[0] + s * a[0] + t * b[0], o[1] + s * a[1] + t * b[1], o[2] + s * a[2] + t * b[2]];
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array([...corner(0, 0), ...corner(1, 0), ...corner(1, 1), ...corner(0, 1)]), 3));
    g.setIndex([0, 1, 2, 0, 2, 3]);
    return g;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [axis, depth, structureVersion]);
  const edges = useMemo(() => new THREE.EdgesGeometry(geom), [geom]);
  useEffect(
    () => () => {
      geom.dispose();
      edges.dispose();
    },
    [geom, edges],
  );

  // VIEW mode while picking (DRAG_ATOM would grab atoms under the pointer); restored afterwards.
  useEffect(() => {
    const st = useCrystalStore.getState();
    const prev: ToolMode = st.toolMode;
    if (prev !== "VIEW") st.setToolMode("VIEW");
    return () => {
      const now = useCrystalStore.getState();
      if (now.toolMode === "VIEW" && prev !== "VIEW") now.setToolMode(prev);
    };
  }, []);

  useEffect(() => {
    const el = gl.domElement;
    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    let down: { x: number; y: number } | null = null;
    const hit = (e: PointerEvent): Vec3 | null => {
      const g = group.current;
      if (!g) return null;
      const r = el.getBoundingClientRect();
      ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      raycaster.setFromCamera(ndc, camera);
      g.updateWorldMatrix(true, false);
      const o = g.worldToLocal(raycaster.ray.origin.clone());
      const p = g.worldToLocal(raycaster.ray.origin.clone().add(raycaster.ray.direction));
      const d = p.sub(o);
      return rayCellPlaneFrac([o.x, o.y, o.z], [d.x, d.y, d.z], useCrystalStore.getState().frame.latticeInv, axis, depth);
    };
    const move = (e: PointerEvent) => {
      const f = hit(e);
      const ui = useUi.getState();
      const cur = ui.placement;
      if (cur) ui.setPlacement({ ...cur, hover: f });
      el.style.cursor = f ? "crosshair" : "";
    };
    const pdown = (e: PointerEvent) => {
      down = e.button === 0 ? { x: e.clientX, y: e.clientY } : null;
    };
    const pup = (e: PointerEvent) => {
      if (!down || e.button !== 0) return;
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
      down = null;
      if (moved > CLICK_SLOP_PX) return; // an orbit drag, not a pick
      const f = hit(e);
      if (f) commitPlacement(f);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerdown", pdown);
    el.addEventListener("pointerup", pup);
    return () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerdown", pdown);
      el.removeEventListener("pointerup", pup);
      el.style.cursor = "";
    };
  }, [gl, camera, axis, depth]);

  const tmp = useMemo(() => new THREE.Vector3(), []);
  useFrame(() => {
    const m = ghost.current;
    if (!m) return;
    const h = useUi.getState().placement?.hover ?? null;
    m.visible = !!h;
    if (!h) return;
    const L = useCrystalStore.getState().frame.lattice;
    tmp.set(
      h[0] * L[0]! + h[1] * L[3]! + h[2] * L[6]!,
      h[0] * L[1]! + h[1] * L[4]! + h[2] * L[7]!,
      h[0] * L[2]! + h[1] * L[5]! + h[2] * L[8]!,
    );
    m.position.copy(tmp);
  });

  const info = elementBySymbol(element);
  const radius = Math.max(0.3, (info?.covalentRadius ?? 0.8) * 0.55);
  const color = info ? cpkHex(info.z) : "#e2e8f0";
  return (
    <group ref={group}>
      <mesh geometry={geom} raycast={() => null} renderOrder={5}>
        <meshBasicMaterial color="#22d3ee" transparent opacity={0.1} side={THREE.DoubleSide} depthWrite={false} toneMapped={false} />
      </mesh>
      <lineSegments geometry={edges} raycast={() => null}>
        <lineBasicMaterial color="#67e8f9" transparent opacity={0.75} toneMapped={false} />
      </lineSegments>
      <mesh ref={ghost} visible={false} raycast={() => null} renderOrder={6}>
        <sphereGeometry args={[radius, 24, 16]} />
        <meshStandardMaterial color={color} transparent opacity={0.55} emissive={color} emissiveIntensity={0.25} depthWrite={false} />
      </mesh>
    </group>
  );
}
