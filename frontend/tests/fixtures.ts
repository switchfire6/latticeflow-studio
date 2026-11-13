/** Synthetic protocol messages for store / dispatcher tests (shapes follow protocol.ts). */
import type {
  AtomSite,
  BondEdge,
  CrystalStatePayload,
  Eigenmode,
  PhononAnalysisComplete,
  ServerMessage,
  StreamStopped,
} from "../src/types/protocol";

export const A = 4; // cubic cell edge (Å)

export interface CrystalOpts {
  /** Override one atom's Cartesian position. */
  move?: { index: number; cart: [number, number, number] };
  elements?: string[];
  maxForce?: number;
  energy?: number;
  actors?: number[];
  formula?: string;
  a?: number;
}

const Z: Record<string, number> = { Sr: 38, Ti: 22, O: 8, Ba: 56, Si: 14, Na: 11 };

export function makeCrystal(n: number, o: CrystalOpts = {}): CrystalStatePayload {
  const a = o.a ?? A;
  const els = o.elements ?? ["Sr", "Ti", "O", "O", "O", "O", "O", "O"].slice(0, n);
  const atoms: AtomSite[] = Array.from({ length: n }, (_, i) => {
    const frac: [number, number, number] = [((i * 0.37) % 1 + 1) % 1, ((i * 0.21) % 1 + 1) % 1, ((i * 0.13) % 1 + 1) % 1];
    let cart: [number, number, number] = [frac[0] * a, frac[1] * a, frac[2] * a];
    if (o.move && o.move.index === i) cart = o.move.cart;
    const el = els[i] ?? "O";
    return {
      index: i,
      element: el,
      atomic_number: Z[el] ?? 8,
      frac_coords: [cart[0] / a, cart[1] / a, cart[2] / a],
      cart_coords: cart,
      force: [0.01 * i, 0, 0],
      force_norm: 0.01 * i,
      is_unstable_actor: o.actors?.includes(i) ?? false,
    };
  });
  const bonds: BondEdge[] =
    n >= 2
      ? [
          { source_index: 0, target_index: 1, image_offset: [0, 0, 0], length: 2.0, strain_ratio: -0.1, is_unstable: false },
          { source_index: 1, target_index: 0, image_offset: [1, 0, 0], length: 2.0, strain_ratio: 0.1, is_unstable: !!o.actors?.length },
        ]
      : [];
  return {
    formula: o.formula ?? "SrTiO3",
    spacegroup_symbol: "Pm-3m",
    spacegroup_number: 221,
    lattice: {
      a,
      b: a,
      c: a,
      alpha: 90,
      beta: 90,
      gamma: 90,
      matrix: [
        [a, 0, 0],
        [0, a, 0],
        [0, 0, a],
      ],
      volume: a ** 3,
    },
    atoms,
    bonds,
    energy_per_atom: o.energy ?? -7.8,
    max_force: o.maxForce ?? 0.01 * (n - 1),
    stress_gpa: [0, 0, 0, 0, 0, 0],
    bulk_modulus_gpa: null,
    e_hull_ev: null,
    rdf_r: [0.5, 0.55],
    rdf_g: [0, 0],
  };
}

export function makeMode(n: number, o: Partial<Eigenmode> = {}): Eigenmode {
  return {
    q_index: 0,
    q_label: "R",
    q_frac: [0.5, 0.5, 0.5],
    band_index: 0,
    frequency_thz: -2.5,
    is_imaginary: true,
    eigenvectors_real: Array.from({ length: n }, (_, i) => (i === 2 ? [1, 0, 0] : [0, 0, 0])),
    eigenvectors_imag: Array.from({ length: n }, () => [0, 0, 0]),
    ...o,
  };
}

export function makePhonon(n: number, stable = false): PhononAnalysisComplete {
  const modes = stable
    ? [makeMode(n, { frequency_thz: 0, is_imaginary: false, q_label: "\\Gamma", q_frac: [0, 0, 0] }), makeMode(n, { band_index: 3, frequency_thz: 4.2, is_imaginary: false })]
    : [makeMode(n, { band_index: 1, frequency_thz: -1.1 }), makeMode(n), makeMode(n, { band_index: 5, frequency_thz: 3.0, is_imaginary: false })];
  return {
    is_dynamically_stable: stable,
    min_frequency_thz: stable ? 0 : -2.5,
    imaginary_mode_count: stable ? 0 : 3,
    kpath_distances: [0, 0.1, 0.2],
    kpath_labels: [
      { index: 0, label: "\\Gamma" },
      { index: 2, label: "R" },
    ],
    frequencies_thz: [Array(3 * n).fill(1), Array(3 * n).fill(1), Array(3 * n).fill(1)],
    eigenmodes: modes,
  };
}

// message builders
export const spu = (c: CrystalStatePayload): ServerMessage => ({ type: "SINGLE_POINT_UPDATE", payload: { crystal: c } });
export const flowFrame = (step: number, total: number, c: CrystalStatePayload): ServerMessage => ({
  type: "FLOW_TRAJECTORY_FRAME",
  payload: { step, total_steps: total, t: total > 1 ? (step - 1) / (total - 1) : 1, crystal: c },
});
export const flowFinal = (c: CrystalStatePayload): ServerMessage => ({ type: "CRYSTAL_STATE_FINAL", payload: { crystal: c } });
export const relaxFrame = (step: number, c: CrystalStatePayload, final = false, converged = false): ServerMessage => ({
  type: "RELAXATION_FRAME",
  payload: { step, converged, final, crystal: c },
});
export const phononDone = (p: PhononAnalysisComplete): ServerMessage => ({ type: "PHONON_ANALYSIS_COMPLETE", payload: p });
export const stopped = (cmd: StreamStopped["stopped"]): ServerMessage => ({ type: "STREAM_STOPPED", payload: { stopped: cmd } });
export const error = (code: string, command: string | null, message = "boom"): ServerMessage =>
  ({ type: "ERROR", payload: { code, message, command } }) as ServerMessage;
