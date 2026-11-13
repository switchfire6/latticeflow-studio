/**
 * Flow trails (§6.2.4): additive, fading screen-space ribbons (three's fat
 * LineSegments2, 3 px) through the last 15 flow frames of every atom
 * (TrailRing in the frame buffers). The ring stores minimum-image-unwrapped
 * Cartesian points, so a wrapped atom does not streak across the cell; each
 * trail is shifted by the lattice vector that puts its head on the displayed
 * atom. Trails fade out over TRAIL_FADE_MS after the flow ends.
 */
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { LineMaterial } from "three/examples/jsm/lines/LineMaterial.js";
import { LineSegments2 } from "three/examples/jsm/lines/LineSegments2.js";
import { LineSegmentsGeometry } from "three/examples/jsm/lines/LineSegmentsGeometry.js";
import { TRAIL_LENGTH } from "../../store/frameBuffers";
import { useCrystalStore } from "../../store/useCrystalStore";
import { cpkColor } from "../../utils/cpkColors";
import type { Vec3 } from "../../utils/latticeMath";

const TRAIL_FADE_MS = 2500;
const TRAIL_WIDTH_PX = 3;
const _p: Vec3 = [0, 0, 0];
const _c = new THREE.Color();
const _tint = new THREE.Color("#7dd3fc");

export function FlowTrails() {
  const visible = useCrystalStore((s) => s.display.trails);
  const size = useThree((s) => s.size);
  const buffers = useRef<{ pos: Float32Array; col: Float32Array } | null>(null);

  const { line, geometry, material } = useMemo(() => {
    const geometry = new LineSegmentsGeometry();
    const material = new LineMaterial({
      vertexColors: true,
      linewidth: TRAIL_WIDTH_PX,
      worldUnits: false,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      toneMapped: false,
    });
    const line = new LineSegments2(geometry, material);
    line.frustumCulled = false;
    line.raycast = () => {};
    line.renderOrder = 2;
    return { line, geometry, material };
  }, []);
  useEffect(
    () => () => {
      geometry.dispose();
      material.dispose();
    },
    [geometry, material],
  );
  useEffect(() => {
    material.resolution.set(size.width, size.height);
  }, [material, size.width, size.height]);

  useFrame(() => {
    const f = useCrystalStore.getState().frame;
    const tr = f.trails;
    let fade = 1;
    if (tr.endedAt !== null) fade = 1 - (performance.now() - tr.endedAt) / TRAIL_FADE_MS;
    const show = visible && tr.count >= 2 && tr.n === f.n && fade > 0;
    line.visible = show;
    if (!show) return;

    const segs = tr.n * (TRAIL_LENGTH - 1);
    let b = buffers.current;
    if (!b || b.pos.length < segs * 6) {
      b = buffers.current = { pos: new Float32Array(segs * 6), col: new Float32Array(segs * 6) };
      geometry.setPositions(b.pos);
      geometry.setColors(b.col);
    }
    const P = b.pos;
    const C = b.col;
    const d = tr.data;
    let v = 0;
    const span = tr.count - 1;
    for (let i = 0; i < tr.n; i++) {
      const head = tr.frameOffset(0) + 3 * i;
      f.position(i, 0, 0, 0, _p);
      const ox = _p[0] - d[head]!;
      const oy = _p[1] - d[head + 1]!;
      const oz = _p[2] - d[head + 2]!;
      _c.setHex(cpkColor(f.numbers[i]!)).lerp(_tint, 0.35);
      for (let age = 0; age < span; age++) {
        const a = tr.frameOffset(age) + 3 * i;
        const e = tr.frameOffset(age + 1) + 3 * i;
        const wa = fade * 1.8 * (1 - age / span) ** 1.5;
        const wb = fade * 1.8 * (1 - (age + 1) / span) ** 1.5;
        P[v * 3] = d[a]! + ox; P[v * 3 + 1] = d[a + 1]! + oy; P[v * 3 + 2] = d[a + 2]! + oz;
        C[v * 3] = _c.r * wa; C[v * 3 + 1] = _c.g * wa; C[v * 3 + 2] = _c.b * wa;
        v++;
        P[v * 3] = d[e]! + ox; P[v * 3 + 1] = d[e + 1]! + oy; P[v * 3 + 2] = d[e + 2]! + oz;
        C[v * 3] = _c.r * wb; C[v * 3 + 1] = _c.g * wb; C[v * 3 + 2] = _c.b * wb;
        v++;
      }
    }
    geometry.instanceCount = v / 2;
    (geometry.getAttribute("instanceStart") as THREE.InterleavedBufferAttribute).data.needsUpdate = true;
    (geometry.getAttribute("instanceColorStart") as THREE.InterleavedBufferAttribute).data.needsUpdate = true;
  });

  return <primitive object={line} />;
}
