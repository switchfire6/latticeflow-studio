/**
 * Shared three.js helpers for the viewport: allocation-free instance-matrix
 * writers, colour maps (§6.2) and custom materials (Fresnel rim, emissive
 * instance colours).
 */
import * as THREE from "three";

export const HALO_OPACITY = 0.22;

export const COLORS = {
  background: "#07090E",
  cyan: new THREE.Color("#22D3EE"),
  crimson: new THREE.Color("#FF2A54"),
  slate: new THREE.Color("#8792A6"),
  compressed: new THREE.Color("#3B82F6"),
  stretched: new THREE.Color("#F5A524"),
  emerald: new THREE.Color("#10B981"),
  neonOrange: new THREE.Color("#FF6A1A"),
};

/** Next capacity (power of two, at least `min`) for growing instance buffers. */
export function growCapacity(needed: number, min = 64): number {
  let c = min;
  while (c < needed) c *= 2;
  return c;
}

/** Write a uniformly scaled translation into an instance matrix array (column-major). */
export function writeScaleTranslate(arr: Float32Array | number[], i: number, x: number, y: number, z: number, s: number): void {
  const o = i * 16;
  arr[o] = s; arr[o + 1] = 0; arr[o + 2] = 0; arr[o + 3] = 0;
  arr[o + 4] = 0; arr[o + 5] = s; arr[o + 6] = 0; arr[o + 7] = 0;
  arr[o + 8] = 0; arr[o + 9] = 0; arr[o + 10] = s; arr[o + 11] = 0;
  arr[o + 12] = x; arr[o + 13] = y; arr[o + 14] = z; arr[o + 15] = 1;
}

const _a = new THREE.Vector3();
const _dir = new THREE.Vector3();
const _q = new THREE.Quaternion();
const _s = new THREE.Vector3();
const _m = new THREE.Matrix4();
const UP = new THREE.Vector3(0, 1, 0);

/**
 * Matrix of a unit Y-aligned primitive stretched from `from` along `dir` with
 * `length`, cross-section `radius`. With `centered` the primitive spans y∈[-0.5, 0.5]
 * (a cylinder centred on the segment); otherwise y∈[0, 1] (an arrow).
 * Returns false for a degenerate direction.
 */
export function writeSegment(
  mesh: THREE.InstancedMesh,
  i: number,
  fx: number, fy: number, fz: number,
  dx: number, dy: number, dz: number,
  radius: number,
  centered: boolean,
): boolean {
  const len = Math.hypot(dx, dy, dz);
  if (!(len > 1e-6)) {
    _m.makeScale(0, 0, 0);
    mesh.setMatrixAt(i, _m);
    return false;
  }
  _dir.set(dx / len, dy / len, dz / len);
  _q.setFromUnitVectors(UP, _dir);
  if (centered) _a.set(fx + dx * 0.5, fy + dy * 0.5, fz + dz * 0.5);
  else _a.set(fx, fy, fz);
  _s.set(radius, len, radius);
  _m.compose(_a, _q, _s);
  mesh.setMatrixAt(i, _m);
  return true;
}

/** Diverging strain colormap: blue (compressed) <- slate (equilibrium) -> amber (stretched). */
export const STRAIN_DEADBAND = 0.04;
export const STRAIN_SATURATION = 0.2;
export function strainColor(strain: number, out: THREE.Color): THREE.Color {
  const t = Math.min(1, Math.max(0, (Math.abs(strain) - STRAIN_DEADBAND) / (STRAIN_SATURATION - STRAIN_DEADBAND)));
  return out.copy(COLORS.slate).lerp(strain < 0 ? COLORS.compressed : COLORS.stretched, t);
}

/** Force colour: emerald (|F| <= 0.05) -> neon orange (|F| >= 0.5), log-interpolated, HDR-boosted for bloom. */
export function forceColor(fnorm: number, out: THREE.Color): THREE.Color {
  const t = Math.min(1, Math.max(0, Math.log(Math.max(fnorm, 1e-9) / 0.05) / Math.log(10)));
  out.copy(COLORS.emerald).lerp(COLORS.neonOrange, t);
  return out.multiplyScalar(1 + 1.6 * t * t);
}

/**
 * Arrow length (Å) ∝ log(1 + |F|) (§6.2), capped at FORCE_ARROW_MAX so the
 * unphysical forces of flow priors (10²–10³ eV/Å) do not span the screen; the
 * cap only bites above ~17 eV/Å.
 */
export const FORCE_ARROW_SCALE = 1.25;
export const FORCE_ARROW_MAX = 3.6;
export function forceArrowLength(fnorm: number): number {
  return Math.min(FORCE_ARROW_MAX, FORCE_ARROW_SCALE * Math.log1p(fnorm));
}

/** Unit arrow along +Y from y = 0 to y = 1 (shaft + cone). */
export function createArrowGeometry(): THREE.BufferGeometry {
  const shaft = new THREE.CylinderGeometry(0.035, 0.035, 0.72, 10, 1);
  shaft.translate(0, 0.36, 0);
  const head = new THREE.ConeGeometry(0.1, 0.28, 14, 1);
  head.translate(0, 0.86, 0);
  const merged = mergeIndexed([shaft, head]);
  shaft.dispose();
  head.dispose();
  return merged;
}

/** Minimal merge of indexed geometries with position + normal (avoids pulling in examples/jsm). */
function mergeIndexed(geoms: THREE.BufferGeometry[]): THREE.BufferGeometry {
  let vCount = 0;
  let iCount = 0;
  for (const g of geoms) {
    vCount += g.getAttribute("position").count;
    iCount += g.getIndex()!.count;
  }
  const pos = new Float32Array(vCount * 3);
  const nor = new Float32Array(vCount * 3);
  const idx = new Uint32Array(iCount);
  let vo = 0;
  let io = 0;
  for (const g of geoms) {
    const p = g.getAttribute("position");
    const n = g.getAttribute("normal");
    const ix = g.getIndex()!;
    pos.set(p.array as Float32Array, vo * 3);
    nor.set(n.array as Float32Array, vo * 3);
    for (let k = 0; k < ix.count; k++) idx[io + k] = ix.getX(k) + vo;
    vo += p.count;
    io += ix.count;
  }
  const out = new THREE.BufferGeometry();
  out.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  out.setAttribute("normal", new THREE.BufferAttribute(nor, 3));
  out.setIndex(new THREE.BufferAttribute(idx, 1));
  out.computeBoundingSphere();
  return out;
}

/**
 * Additive Fresnel rim for instanced spheres: glow ∝ (1 − |n·v|)^power, tinted
 * by the per-instance colour; `uPulse` = 1 makes it pulse (unstable actors).
 */
export function createRimMaterial(power = 2.2, intensity = 1.6, pulse = 0): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    uniforms: {
      uPower: { value: power },
      uIntensity: { value: intensity },
      uTime: { value: 0 },
      uPulse: { value: pulse },
    },
    vertexShader: /* glsl */ `
      varying vec3 vN;
      varying vec3 vV;
      varying vec3 vTint;
      void main() {
        #ifdef USE_INSTANCING
          mat4 im = instanceMatrix;
        #else
          mat4 im = mat4(1.0);
        #endif
        vec4 mv = modelViewMatrix * im * vec4(position, 1.0);
        vN = normalize(normalMatrix * mat3(im) * normal);
        vV = normalize(-mv.xyz);
        #ifdef USE_INSTANCING_COLOR
          vTint = instanceColor;
        #else
          vTint = vec3(0.13, 0.83, 0.93);
        #endif
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: /* glsl */ `
      uniform float uPower;
      uniform float uIntensity;
      uniform float uTime;
      uniform float uPulse;
      varying vec3 vN;
      varying vec3 vV;
      varying vec3 vTint;
      void main() {
        float f = pow(1.0 - abs(dot(normalize(vN), normalize(vV))), uPower);
        float p = mix(1.0, 0.45 + 0.55 * (0.5 + 0.5 * sin(uTime * 5.0)), uPulse);
        vec3 c = vTint * uIntensity * p * f;
        gl_FragColor = vec4(c, clamp(f * p, 0.0, 1.0));
      }`,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    toneMapped: false,
  });
}

/**
 * MeshStandardMaterial whose per-instance colour also drives emission, so
 * HDR instance colours (> 1) cross the bloom threshold (force arrows, unstable bonds).
 */
export function createEmissiveInstanceMaterial(emissiveGain: number, params: THREE.MeshStandardMaterialParameters = {}) {
  const mat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.35, metalness: 0.2, ...params });
  const uniforms = { uEmissiveGain: { value: emissiveGain } };
  mat.onBeforeCompile = (shader) => {
    shader.uniforms.uEmissiveGain = uniforms.uEmissiveGain;
    shader.fragmentShader = shader.fragmentShader
      .replace("#include <common>", "#include <common>\nuniform float uEmissiveGain;")
      .replace(
        "#include <emissivemap_fragment>",
        "#include <emissivemap_fragment>\n#if defined( USE_COLOR ) || defined( USE_INSTANCING_COLOR )\n  totalEmissiveRadiance += vColor.rgb * uEmissiveGain;\n#endif",
      );
  };
  mat.customProgramCacheKey = () => `emissive-instance-${emissiveGain}`;
  return Object.assign(mat, { emissiveUniforms: uniforms });
}

/** Radial-gradient background texture (#07090E edges), used as scene.background so it survives post-processing. */
export function createBackgroundTexture(): THREE.Texture {
  const size = 512;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  const g = ctx.createRadialGradient(size * 0.5, size * 0.42, 0, size * 0.5, size * 0.5, size * 0.72);
  g.addColorStop(0, "#151c2b");
  g.addColorStop(0.45, "#0c111b");
  g.addColorStop(1, COLORS.background);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}
