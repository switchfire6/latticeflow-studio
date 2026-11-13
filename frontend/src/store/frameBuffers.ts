/**
 * Transient, mutable render buffers (never React state).
 *
 * Every structure the server sends is copied into flat typed arrays here at
 * once, and `version` is bumped. The viewport components read these buffers
 * inside `useFrame` (via `useCrystalStore.getState().frame`) and re-upload
 * their instance matrices when `version` changes or an animation (drag,
 * phonon) is running, so ~30 Hz flow frames and 60 FPS phonon animation never
 * re-render a React tree. The HUD gets a throttled copy through zustand state.
 */
import type { BondEdge, CrystalStatePayload } from "../types/protocol";
import { atomicMass } from "../utils/cpkColors";
import { flattenMatrix, invert3, minimumImageCart, type Vec3 } from "../utils/latticeMath";
import { imageDisplacement } from "../utils/phonon";

export type CrystalSource = "single_point" | "phonon_flags" | "flow" | "flow_final" | "relaxation";

export interface DragOverride {
  index: number;
  /** Optimistic Cartesian position of the dragged atom (Å, cell frame). */
  pos: Float64Array;
  /** True while the pointer is down; false once released and waiting for the server to catch up. */
  active: boolean;
  releasedAt: number | null;
}

/** How close (Å) a server position must be to the released drag position to drop the override. */
export const DRAG_RECONCILE_TOL = 0.02;
/** After this long without a matching server echo the override is dropped anyway. */
export const DRAG_RECONCILE_TIMEOUT_MS = 1500;

export const TRAIL_LENGTH = 15;

/**
 * Ring buffer of the last TRAIL_LENGTH Cartesian frames per atom, stored
 * *unwrapped*: each new point is the previous point plus the minimum-image
 * displacement, so an atom crossing the cell boundary does not produce a streak.
 * At render time the trail is shifted by a lattice vector so its head sits on
 * the (wrapped) atom.
 */
export class TrailRing {
  readonly capacity: number;
  n = 0;
  count = 0;
  head = -1;
  data = new Float32Array(0);
  version = 0;
  /** Stream id the trail belongs to (a new flow starts a new trail). */
  streamId: number | null = null;
  /** performance.now() when the owning flow ended (trails fade out), or null while live. */
  endedAt: number | null = null;

  constructor(capacity = TRAIL_LENGTH) {
    this.capacity = capacity;
  }

  reset(n = 0, streamId: number | null = null): void {
    this.n = n;
    this.count = 0;
    this.head = -1;
    this.data = new Float32Array(this.capacity * n * 3);
    this.streamId = streamId;
    this.endedAt = null;
    this.version++;
  }

  push(positions: ArrayLike<number>, n: number, L: Float64Array, Linv: Float64Array): void {
    if (n !== this.n) this.reset(n, this.streamId);
    const slot = (this.head + 1) % this.capacity;
    const dst = slot * n * 3;
    if (this.count === 0) {
      for (let k = 0; k < 3 * n; k++) this.data[dst + k] = positions[k]!;
    } else {
      const src = this.head * n * 3;
      const d: Vec3 = [0, 0, 0];
      const m: Vec3 = [0, 0, 0];
      for (let i = 0; i < n; i++) {
        const o = 3 * i;
        d[0] = positions[o]! - this.data[src + o]!;
        d[1] = positions[o + 1]! - this.data[src + o + 1]!;
        d[2] = positions[o + 2]! - this.data[src + o + 2]!;
        minimumImageCart(d, L, Linv, m);
        this.data[dst + o] = this.data[src + o]! + m[0];
        this.data[dst + o + 1] = this.data[src + o + 1]! + m[1];
        this.data[dst + o + 2] = this.data[src + o + 2]! + m[2];
      }
    }
    this.head = slot;
    this.count = Math.min(this.count + 1, this.capacity);
    this.version++;
  }

  /** Offset into `data` of the frame `age` steps back (0 = newest). */
  frameOffset(age: number): number {
    const slot = (this.head - age + this.capacity * 2) % this.capacity;
    return slot * this.n * 3;
  }
}

export interface PhononField {
  active: boolean;
  /** Per-atom complex displacement c_κ (Å), see utils/phonon.ts. */
  re: Float32Array;
  im: Float32Array;
  q: Vec3;
}

export class FrameBuffers {
  /** Bumped on every structure update. */
  version = 0;
  /** Bumped when the atom count or species change (instance colours, camera fit). */
  topologyVersion = 0;
  /** Bumped on every structure *change* (not on the phonon actor-flag update, which re-sends the same structure). */
  stateVersion = 0;
  crystal: CrystalStatePayload | null = null;
  source: CrystalSource = "single_point";
  receivedAt = 0;

  n = 0;
  lattice = new Float64Array([1, 0, 0, 0, 1, 0, 0, 0, 1]);
  latticeInv = new Float64Array([1, 0, 0, 0, 1, 0, 0, 0, 1]);
  positions = new Float32Array(0); // N*3 Cartesian (Å), as sent by the server
  frac = new Float32Array(0); // N*3 fractional
  forces = new Float32Array(0); // N*3 eV/Å
  forceNorm = new Float32Array(0); // N
  numbers = new Uint8Array(0); // Z
  masses = new Float32Array(0); // amu
  unstable = new Uint8Array(0); // is_unstable_actor
  bonds: BondEdge[] = [];

  drag: DragOverride | null = null;
  phonon: PhononField = { active: false, re: new Float32Array(0), im: new Float32Array(0), q: [0, 0, 0] };
  trails = new TrailRing();

  /** Copy a payload into the typed buffers. Returns true when the topology changed. */
  ingest(crystal: CrystalStatePayload, source: CrystalSource, now = Date.now()): boolean {
    const atoms = crystal.atoms;
    const n = atoms.length;
    let topologyChanged = n !== this.n;
    if (topologyChanged) {
      this.n = n;
      this.positions = new Float32Array(3 * n);
      this.frac = new Float32Array(3 * n);
      this.forces = new Float32Array(3 * n);
      this.forceNorm = new Float32Array(n);
      this.numbers = new Uint8Array(n);
      this.masses = new Float32Array(n);
      this.unstable = new Uint8Array(n);
      this.phonon = { active: false, re: new Float32Array(3 * n), im: new Float32Array(3 * n), q: [0, 0, 0] };
    }
    for (let i = 0; i < n; i++) {
      const a = atoms[i]!;
      const o = 3 * i;
      if (this.numbers[i] !== a.atomic_number) {
        topologyChanged = true;
        this.numbers[i] = a.atomic_number;
        this.masses[i] = atomicMass(a.atomic_number);
      }
      this.positions[o] = a.cart_coords[0];
      this.positions[o + 1] = a.cart_coords[1];
      this.positions[o + 2] = a.cart_coords[2];
      this.frac[o] = a.frac_coords[0];
      this.frac[o + 1] = a.frac_coords[1];
      this.frac[o + 2] = a.frac_coords[2];
      this.forces[o] = a.force[0];
      this.forces[o + 1] = a.force[1];
      this.forces[o + 2] = a.force[2];
      this.forceNorm[i] = a.force_norm;
      this.unstable[i] = a.is_unstable_actor ? 1 : 0;
    }
    flattenMatrix(crystal.lattice.matrix, this.lattice);
    try {
      invert3(this.lattice, this.latticeInv);
    } catch {
      /* keep the previous inverse for a degenerate frame */
    }
    this.bonds = crystal.bonds;
    this.crystal = crystal;
    this.source = source;
    this.receivedAt = now;
    this.version++;
    if (source !== "phonon_flags") this.stateVersion++;
    if (topologyChanged) {
      this.topologyVersion++;
      this.drag = null;
    } else {
      this.reconcileDrag(now);
    }
    return topologyChanged;
  }

  /** Drop a released drag override once the server has echoed the final position (or after a timeout). */
  private reconcileDrag(now: number): void {
    const d = this.drag;
    if (!d || d.active) return;
    if (d.index >= this.n) {
      this.drag = null;
      return;
    }
    const o = 3 * d.index;
    const diff: Vec3 = [this.positions[o]! - d.pos[0]!, this.positions[o + 1]! - d.pos[1]!, this.positions[o + 2]! - d.pos[2]!];
    const m = minimumImageCart(diff, this.lattice, this.latticeInv);
    const dist = Math.hypot(m[0], m[1], m[2]);
    if (dist < DRAG_RECONCILE_TOL || (d.releasedAt !== null && now - d.releasedAt > DRAG_RECONCILE_TIMEOUT_MS)) {
      this.drag = null;
    }
  }

  /** Base Cartesian position of atom i in the home cell (drag override applied, no phonon). */
  basePosition(i: number, out: Vec3 | Float32Array): void {
    const d = this.drag;
    if (d && d.index === i) {
      out[0] = d.pos[0]!;
      out[1] = d.pos[1]!;
      out[2] = d.pos[2]!;
      return;
    }
    const o = 3 * i;
    out[0] = this.positions[o]!;
    out[1] = this.positions[o + 1]!;
    out[2] = this.positions[o + 2]!;
  }

  private readonly _disp: Vec3 = [0, 0, 0];

  /**
   * Displayed Cartesian position of atom i in periodic image (n0, n1, n2):
   * base + n·L + phonon displacement of that image.
   */
  position(i: number, n0: number, n1: number, n2: number, out: Vec3 | Float32Array): void {
    this.basePosition(i, out);
    const L = this.lattice;
    if (n0 !== 0 || n1 !== 0 || n2 !== 0) {
      out[0] += n0 * L[0]! + n1 * L[3]! + n2 * L[6]!;
      out[1] += n0 * L[1]! + n1 * L[4]! + n2 * L[7]!;
      out[2] += n0 * L[2]! + n1 * L[5]! + n2 * L[8]!;
    }
    const ph = this.phonon;
    if (ph.active) {
      imageDisplacement(ph.re, ph.im, i, ph.q, n0, n1, n2, this._disp);
      out[0] += this._disp[0];
      out[1] += this._disp[1];
      out[2] += this._disp[2];
    }
  }

  /** Geometric centre of the cell (or of `scale`·cell, for the 2x2x2 halo). */
  cellCenter(out: Vec3, scale = 1): Vec3 {
    const L = this.lattice;
    const h = 0.5 * scale;
    out[0] = h * (L[0]! + L[3]! + L[6]!);
    out[1] = h * (L[1]! + L[4]! + L[7]!);
    out[2] = h * (L[2]! + L[5]! + L[8]!);
    return out;
  }

  /** Radius of the sphere enclosing the cell (or the scaled supercell). */
  boundingRadius(scale = 1): number {
    const L = this.lattice;
    const c: Vec3 = this.cellCenter([0, 0, 0], scale);
    let r = 0;
    for (let i = 0; i < 2; i++)
      for (let j = 0; j < 2; j++)
        for (let k = 0; k < 2; k++) {
          const x = scale * (i * L[0]! + j * L[3]! + k * L[6]!) - c[0];
          const y = scale * (i * L[1]! + j * L[4]! + k * L[7]!) - c[1];
          const z = scale * (i * L[2]! + j * L[5]! + k * L[8]!) - c[2];
          r = Math.max(r, Math.hypot(x, y, z));
        }
    return r;
  }
}
