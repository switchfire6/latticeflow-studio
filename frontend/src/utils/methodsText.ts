/**
 * "Copy methods text": a methods paragraph built from the settings and model
 * of the results that actually exist (pure, unit-tested), followed by the
 * software the README acknowledgements ask users to cite.
 */

export interface MethodsModel {
  /** Human label, e.g. "MACE-MP-0 small"; "lj-fallback" is described as the analytic fallback. */
  label: string;
  dtype?: string | null;
  dispersion?: boolean;
}

type Settings = Record<string, unknown>;

export interface MethodsInput {
  relax?: { settings: Settings; steps?: number | null; converged?: boolean | null; model: MethodsModel };
  phonons?: {
    settings: Settings;
    supercell?: readonly number[] | null;
    cellMode?: string | null;
    bandPathSource?: string | null;
    model: MethodsModel;
  };
  elastic?: { settings: Settings; model: MethodsModel };
  eos?: { settings: Settings; model: MethodsModel };
  hull?: { nReferences: number; chemsys: string; candidateRelaxed: boolean; model: MethodsModel };
  diffraction?: { kind: string; radiation: string | null; wavelength: number; range?: [number, number] | null };
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const bool = (v: unknown, d: boolean): boolean => (typeof v === "boolean" ? v : d);
const str = (v: unknown, d: string): string => (typeof v === "string" && v ? v : d);
const g = (v: number) => String(+v.toPrecision(3));

const RADIATION_LABEL: Record<string, string> = { CuKa: "Cu Kα", MoKa: "Mo Kα", CoKa: "Co Kα", CrKa: "Cr Kα", FeKa: "Fe Kα", AgKa: "Ag Kα" };

export function modelPhrase(m: MethodsModel): string {
  if (/lj-fallback/i.test(m.label)) return "an analytic Lennard-Jones fallback potential (not physical)";
  const extras = [m.dtype, m.dispersion ? "with D3(BJ) dispersion" : null].filter(Boolean);
  return `${m.label}${extras.length ? ` (${extras.join(", ")})` : ""}`;
}

const sameModel = (a: MethodsModel, b: MethodsModel) => a.label === b.label && !!a.dispersion === !!b.dispersion && (a.dtype ?? null) === (b.dtype ?? null);

function relaxSentence(r: NonNullable<MethodsInput["relax"]>): string {
  const s = r.settings;
  const fmax = num(s.fmax) ?? 0.02;
  if (s.follow === true || num(s.q_index) !== null || num(s.amplitude) !== null) {
    const q = typeof s.q_label === "string" && s.q_label ? `${s.q_label}-point ` : "";
    const band = num(s.band_index);
    const amp = num(s.amplitude) ?? 0.1;
    return `The distorted structure was obtained by freezing in the imaginary ${q}mode${band !== null ? ` ν${band}` : ""} (amplitude ${g(amp)} Å) in its commensurate supercell and relaxing with FIRE to max|F| < ${g(fmax)} eV/Å`;
  }
  const opt = str(s.optimizer, "FIRE");
  const cell = bool(s.relax_cell, true);
  const p = num(s.pressure_gpa) ?? 0;
  const tol = num(s.stress_tol_gpa);
  const parts = [`Structures were relaxed with ${opt} (${cell ? "atomic positions and cell" : "atomic positions, fixed cell"}${bool(s.fix_symmetry, false) ? ", symmetry constrained" : ""}${p !== 0 ? `, at ${g(p)} GPa` : ""}) to max|F| < ${g(fmax)} eV/Å`];
  if (cell && tol !== null) parts.push(` and a residual stress below ${g(tol)} GPa`);
  if (r.steps != null) parts.push(` (${r.steps} steps${r.converged === false ? ", not converged" : ""})`);
  return parts.join("");
}

function phononSentence(p: NonNullable<MethodsInput["phonons"]>): string {
  const s = p.settings;
  const disp = num(s.displacement_distance) ?? 0.01;
  const sc = p.supercell && p.supercell.length === 3 ? p.supercell : Array.isArray(s.supercell_dim) ? (s.supercell_dim as number[]) : null;
  const cellMode = p.cellMode ?? (typeof s.cell_mode === "string" ? s.cell_mode : null);
  const cell = cellMode === "primitive" ? " of the primitive cell" : cellMode === "as_displayed" ? " of the conventional (displayed) cell" : "";
  const path = p.bandPathSource ? `; the dispersion follows the ${/seek/i.test(p.bandPathSource) ? "SeeK-path" : p.bandPathSource} high-symmetry path` : "";
  return `phonons were computed by finite displacement (${g(disp)} Å) in a ${sc ? sc.join("×") : "periodic"} supercell${cell} with phonopy${path}`;
}

function elasticSentence(e: NonNullable<MethodsInput["elastic"]>): string {
  const s = e.settings;
  const strain = (num(s.max_strain) ?? 0.01) * 100;
  const n = num(s.n_strains) ?? 4;
  const ions = bool(s.relax_ions, true);
  return `the elastic tensor was obtained from linear stress–strain fits (${n} strains up to ±${g(strain)} %, ${ions ? `relaxed ions to max|F| < ${g(num(s.fmax) ?? 0.01)} eV/Å` : "clamped ions"}) with Voigt–Reuss–Hill averages`;
}

function eosSentence(e: NonNullable<MethodsInput["eos"]>): string {
  const s = e.settings;
  const w = (num(s.strain_window) ?? 0.02) * 100;
  const n = num(s.n_points) ?? 5;
  return `the bulk modulus was fitted to a third-order Birch–Murnaghan equation of state over ±${g(w)} % in volume (${n} points, clamped ions, fixed cell shape)`;
}

function hullSentence(h: NonNullable<MethodsInput["hull"]>): string {
  return `the energy above the convex hull was evaluated against ${h.nReferences} Materials Project reference phase${h.nReferences === 1 ? "" : "s"} of the ${h.chemsys} system, all re-relaxed with the same potential${h.candidateRelaxed ? ", after relaxing the candidate" : ""}`;
}

function diffractionSentence(d: NonNullable<MethodsInput["diffraction"]>): string {
  const rad = d.kind === "neutron" ? "neutron" : "X-ray";
  const src = d.kind !== "neutron" && d.radiation ? `${RADIATION_LABEL[d.radiation] ?? d.radiation}, ` : "";
  const range = d.range ? `, 2θ = ${g(d.range[0])}–${g(d.range[1])}°` : "";
  return `powder ${rad} diffraction patterns were simulated with pymatgen (${src}λ = ${d.wavelength.toFixed(4)} Å${range})`;
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

const CITATIONS = {
  mace: "MACE (https://github.com/ACEsuit/mace) and the MACE-MP-0 foundation models",
  phonopy: "phonopy (https://phonopy.github.io/phonopy/)",
  seekpath: "SeeK-path (https://github.com/giovannipizzi/seekpath)",
  spglib: "spglib (https://spglib.readthedocs.io/)",
  ase: "ASE (https://wiki.fysik.dtu.dk/ase/)",
  pymatgen: "pymatgen (https://pymatgen.org/)",
  mp: "the Materials Project (https://materialsproject.org/)",
  d3: "torch-dftd (https://github.com/pfnet-research/torch-dftd)",
} as const;

/** The methods paragraph plus a "Software:" line; null when nothing has been computed. */
export function buildMethodsText(input: MethodsInput): string | null {
  const parts: { text: string; model: MethodsModel | null }[] = [];
  if (input.relax) parts.push({ text: relaxSentence(input.relax), model: input.relax.model });
  if (input.phonons) parts.push({ text: phononSentence(input.phonons), model: input.phonons.model });
  if (input.elastic) parts.push({ text: elasticSentence(input.elastic), model: input.elastic.model });
  if (input.eos) parts.push({ text: eosSentence(input.eos), model: input.eos.model });
  if (input.hull) parts.push({ text: hullSentence(input.hull), model: input.hull.model });
  if (input.diffraction) parts.push({ text: diffractionSentence(input.diffraction), model: null });
  if (!parts.length) return null;

  const models = parts.map((p) => p.model).filter((m): m is MethodsModel => !!m);
  const oneModel = models.length > 0 && models.every((m) => sameModel(m, models[0]!));
  const sentences = parts.map((p) => (!oneModel && p.model ? `${p.text} using ${modelPhrase(p.model)}` : p.text));
  // Two clauses per sentence at most, joined with semicolons.
  const out: string[] = [];
  for (let i = 0; i < sentences.length; i += 2) {
    out.push(`${cap(sentences.slice(i, i + 2).join("; "))}.`);
  }
  if (oneModel) out.push(`Energies, forces and stresses were computed with the ${modelPhrase(models[0]!)} universal interatomic potential.`);

  const cite: string[] = [];
  const anyMace = models.some((m) => !/lj-fallback/i.test(m.label));
  if (anyMace) cite.push(CITATIONS.mace);
  if (input.relax || input.elastic || input.eos) cite.push(CITATIONS.ase);
  if (input.phonons) cite.push(CITATIONS.phonopy, CITATIONS.seekpath);
  cite.push(CITATIONS.spglib);
  if (input.elastic || input.diffraction) cite.push(CITATIONS.pymatgen);
  if (input.hull) cite.push(CITATIONS.mp);
  if (models.some((m) => m.dispersion)) cite.push(CITATIONS.d3);
  return `${out.join(" ")}\n\nSoftware: ${cite.join("; ")}.`;
}
