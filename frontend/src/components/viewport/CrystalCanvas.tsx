/**
 * R3F canvas (§6.1): #07090E radial-gradient background, subtle grid floor,
 * depth fog, studio lighting (local Lightformer environment, no network),
 * OrbitControls, and post-processing (N8AO + selective Bloom with
 * luminanceThreshold 0.85 / intensity 0.65 + ACES tone mapping).
 *
 * Post-processing degrades gracefully: it can be toggled in the View panel,
 * forced with ?fx=0 / ?fx=1, and is switched off automatically when
 * drei's PerformanceMonitor reports a sustained frame-rate decline.
 *
 * Crystal frame → world: the crystal is rotated so its z axis points up
 * (world y) and translated each frame so the cell (or 2x2x2 halo) centre sits
 * at the origin. Inside `cellGroup` the coordinates are crystal Cartesian Å.
 */
import { Environment, Grid, Lightformer, OrbitControls, PerformanceMonitor } from "@react-three/drei";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Bloom, EffectComposer, N8AO, ToneMapping } from "@react-three/postprocessing";
import { ToneMappingMode } from "postprocessing";
import { useEffect, useMemo, useRef, type RefObject } from "react";
import * as THREE from "three";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { Vec3 } from "../../utils/latticeMath";
import { FlowTrails } from "./FlowTrails";
import { ForceVectors } from "./ForceVectors";
import { InstancedAtoms } from "./InstancedAtoms";
import { InstancedBonds } from "./InstancedBonds";
import { PhononDisplacement } from "./PhononDisplacement";
import { UnitCellBox } from "./UnitCellBox";
import { COLORS, createBackgroundTexture } from "./sceneUtils";

const FOV = 38;
const VIEW_DIR = new THREE.Vector3(1.0, 0.72, 1.35).normalize();

function SceneBackground() {
  const scene = useThree((s) => s.scene);
  useEffect(() => {
    const tex = createBackgroundTexture();
    scene.background = tex;
    scene.fog = new THREE.Fog(COLORS.background, 30, 90);
    return () => {
      scene.background = null;
      scene.fog = null;
      tex.dispose();
    };
  }, [scene]);
  return null;
}

/** Keeps the cell centred, fits the camera on new structures, and tracks fog + floor height. */
function CameraRig({ cellGroup, floor }: { cellGroup: RefObject<THREE.Group | null>; floor: RefObject<THREE.Object3D | null> }) {
  const camera = useThree((s) => s.camera) as THREE.PerspectiveCamera;
  const controls = useThree((s) => s.controls) as unknown as { target: THREE.Vector3; update(): void } | null;
  const scene = useThree((s) => s.scene);
  const ghostHalo = useCrystalStore((s) => s.display.ghostHalo);
  const fit = useRef({ key: "", from: new THREE.Vector3(), to: new THREE.Vector3(), t: 1, radius: 5 });
  const center: Vec3 = useMemo(() => [0, 0, 0], []);

  useFrame((_, dt) => {
    const f = useCrystalStore.getState().frame;
    const g = cellGroup.current;
    if (!g || f.n === 0) return;
    const scale = ghostHalo ? 2 : 1;
    f.cellCenter(center, scale);
    g.position.set(-center[0], -center[1], -center[2]);

    // floor below the lowest cell corner (crystal z is world up)
    const L = f.lattice;
    let minZ = 0;
    for (let i = 0; i < 2; i++)
      for (let j = 0; j < 2; j++)
        for (let k = 0; k < 2; k++) minZ = Math.min(minZ, scale * (i * L[2]! + j * L[5]! + k * L[8]!));
    if (floor.current) floor.current.position.y = minZ - center[2] - 1.2;

    const key = `${f.topologyVersion}:${scale}`;
    const s = fit.current;
    if (key !== s.key) {
      s.key = key;
      s.radius = Math.max(f.boundingRadius(scale), 2.5);
      // margin for atom radii / bond stubs, and the horizontal field of view on narrow viewports
      const aspect = Math.min(1, camera.aspect || 1);
      const dist = ((s.radius + 1.6) / Math.sin(THREE.MathUtils.degToRad(FOV / 2))) * (1.15 / Math.sqrt(aspect));
      s.from.copy(camera.position);
      s.to.copy(VIEW_DIR).multiplyScalar(dist);
      s.t = s.from.lengthSq() < 1e-6 || f.version <= 1 ? 1 : 0;
      if (s.t === 1) camera.position.copy(s.to);
      if (controls) {
        controls.target.set(0, 0, 0);
        controls.update();
      }
    }
    if (s.t < 1) {
      s.t = Math.min(1, s.t + dt / 0.6);
      const e = 1 - (1 - s.t) ** 3;
      camera.position.lerpVectors(s.from, s.to, e);
      controls?.update();
    }
    const fog = scene.fog as THREE.Fog | null;
    if (fog) {
      const d = camera.position.length();
      fog.near = Math.max(0.1, d - s.radius * 0.3);
      fog.far = d + s.radius * 3.2;
    }
  }, -1);
  return null;
}

function Effects() {
  return (
    <EffectComposer multisampling={4} enableNormalPass={false}>
      <N8AO aoRadius={1.1} distanceFalloff={0.9} intensity={2.4} quality="medium" halfRes color="#05070c" />
      <Bloom mipmapBlur luminanceThreshold={0.85} luminanceSmoothing={0.1} intensity={0.65} />
      <ToneMapping mode={ToneMappingMode.ACES_FILMIC} />
    </EffectComposer>
  );
}

function fxOverride(): boolean | null {
  const v = new URLSearchParams(globalThis.location?.search ?? "").get("fx");
  return v === "0" ? false : v === "1" ? true : null;
}

export function CrystalCanvas() {
  const postprocessing = useCrystalStore((s) => s.display.postprocessing);
  const cellGroup = useRef<THREE.Group>(null);
  const floor = useRef<THREE.Mesh>(null);
  const forced = useMemo(fxOverride, []);
  const fxOn = forced ?? postprocessing;

  return (
    <div className="absolute inset-0">
    <Canvas
      style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }}
      resize={{ debounce: 0, scroll: false }}
      dpr={[1, 2]}
      gl={{ antialias: true, powerPreference: "high-performance", stencil: false }}
      camera={{ position: [10, 7, 13], fov: FOV, near: 0.05, far: 400 }}
      onCreated={(state) => {
        if (import.meta.env.DEV) Object.assign((globalThis as unknown as { __latticeflow?: object }).__latticeflow ?? {}, { r3f: state });
      }}
      onPointerMissed={(e) => {
        if (e.button === 0) useCrystalStore.getState().selectAtom(null);
      }}
    >
      <SceneBackground />
      <ambientLight intensity={0.28} />
      <hemisphereLight args={["#bcd3ff", "#0a0d14", 0.45]} />
      <directionalLight position={[6, 12, 8]} intensity={1.6} color="#ffffff" />
      <directionalLight position={[-8, 4, -6]} intensity={0.55} color="#7dd3fc" />
      <Environment resolution={128} frames={1}>
        <Lightformer form="rect" intensity={2.2} position={[0, 6, 4]} scale={[10, 4, 1]} color="#ffffff" />
        <Lightformer form="rect" intensity={1.1} position={[-6, 2, -2]} rotation-y={Math.PI / 2} scale={[8, 3, 1]} color="#8ab4ff" />
        <Lightformer form="ring" intensity={0.9} position={[5, -1, -5]} scale={3} color="#22d3ee" />
      </Environment>

      <group rotation={[-Math.PI / 2, 0, 0]}>
        <group ref={cellGroup}>
          <PhononDisplacement />
          <UnitCellBox />
          <InstancedAtoms />
          <InstancedBonds />
          <ForceVectors />
          <FlowTrails />
        </group>
      </group>

      <Grid
        ref={floor}
        infiniteGrid
        cellSize={0.5}
        cellThickness={0.6}
        cellColor="#141c2a"
        sectionSize={2.5}
        sectionThickness={1}
        sectionColor="#1d2a40"
        fadeDistance={45}
        fadeStrength={2.2}
        followCamera={false}
      />

      <CameraRig cellGroup={cellGroup} floor={floor} />
      <OrbitControls makeDefault enableDamping dampingFactor={0.08} minDistance={1.5} maxDistance={150} />
      <PerformanceMonitor
        flipflops={2}
        onDecline={() => {
          const st = useCrystalStore.getState();
          if (forced === null && st.display.postprocessing) {
            st.setDisplay({ postprocessing: false });
            st.pushToast("info", "Post-processing off", "Frame rate was low, so bloom and ambient occlusion were disabled. Re-enable them in the View panel.");
          }
        }}
      />
      {fxOn && <Effects />}
    </Canvas>
    </div>
  );
}
