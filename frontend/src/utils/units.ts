/**
 * Display units (IMPROVEMENT_SUGGESTIONS.md §3.6). The wire protocol is always
 * THz / GPa / eV; these helpers convert for display only.
 *
 *   1 THz = 33.35641 cm⁻¹ (1e12 / c, c = 2.99792458e10 cm/s)
 *   1 THz = 4.135667 meV  (h · 1e12 Hz, h = 4.135667696e-15 eV·s)
 *   1 GPa = 10 kbar
 */

export type FrequencyUnit = "THz" | "cm-1" | "meV";
export type PressureUnit = "GPa" | "kbar";

export interface UnitPrefs {
  frequency: FrequencyUnit;
  pressure: PressureUnit;
}

export const DEFAULT_UNITS: UnitPrefs = { frequency: "THz", pressure: "GPa" };

export const THZ_TO_CM1 = 1e12 / 2.99792458e10; // 33.35641
export const THZ_TO_MEV = 4.135667696; // meV per THz
export const GPA_TO_KBAR = 10;

export const FREQUENCY_UNITS: readonly FrequencyUnit[] = ["THz", "cm-1", "meV"];
export const PRESSURE_UNITS: readonly PressureUnit[] = ["GPa", "kbar"];

/** Multiplicative factor THz → unit. */
export function frequencyFactor(unit: FrequencyUnit): number {
  return unit === "cm-1" ? THZ_TO_CM1 : unit === "meV" ? THZ_TO_MEV : 1;
}

export function convertFrequency(thz: number, unit: FrequencyUnit): number {
  return thz * frequencyFactor(unit);
}

/** Display label of a frequency unit ("cm⁻¹"). */
export function frequencyUnitLabel(unit: FrequencyUnit): string {
  return unit === "cm-1" ? "cm⁻¹" : unit;
}

/** Decimals that give ~the same resolution as 0.01 THz in each unit. */
function frequencyDecimals(unit: FrequencyUnit, digitsThz: number): number {
  if (unit === "cm-1") return Math.max(0, digitsThz - 2);
  if (unit === "meV") return Math.max(0, digitsThz - 1);
  return digitsThz;
}

/** "−2.50 THz", "−83 cm⁻¹", "−10.3 meV". `digits` is the THz precision. */
export function formatFrequency(thz: number, unit: FrequencyUnit, digits = 2, withUnit = true): string {
  const v = convertFrequency(thz, unit);
  const s = signed(v, frequencyDecimals(unit, digits));
  return withUnit ? `${s} ${frequencyUnitLabel(unit)}` : s;
}

export function convertPressure(gpa: number, unit: PressureUnit): number {
  return unit === "kbar" ? gpa * GPA_TO_KBAR : gpa;
}

/** "+5.10 GPa" / "+51.0 kbar" (explicit sign: + = compressed). */
export function formatPressure(gpa: number, unit: PressureUnit, digits = 2, opts: { sign?: boolean } = {}): string {
  const v = convertPressure(gpa, unit);
  const d = unit === "kbar" ? Math.max(0, digits - 1) : digits;
  const body = opts.sign === false ? signed(v, d) : withPlus(v, d);
  return `${body} ${unit}`;
}

/** ΔE per atom and per formula unit, e.g. "−14.4 meV/atom · −72.0 meV/f.u.". */
export function formatEnergyDelta(deltaEvPerAtom: number, atomsPerFormulaUnit: number | null): string {
  const perAtom = deltaEvPerAtom * 1000;
  const a = `${signed(perAtom, 1, true)} meV/atom`;
  if (!atomsPerFormulaUnit || atomsPerFormulaUnit <= 1) return a;
  return `${a} · ${signed(perAtom * atomsPerFormulaUnit, 1, true)} meV/f.u.`;
}

/** Unicode minus, optional explicit plus. */
function signed(v: number, decimals: number, plus = false): string {
  const r = Number(v.toFixed(decimals));
  if (r === 0) return (0).toFixed(decimals);
  const abs = Math.abs(v).toFixed(decimals);
  return v < 0 ? `−${abs}` : plus ? `+${abs}` : abs;
}

function withPlus(v: number, decimals: number): string {
  return signed(v, decimals, true);
}

/** Sanitise a stored preference object. */
export function normalizeUnits(u: Partial<UnitPrefs> | null | undefined): UnitPrefs {
  return {
    frequency: u && FREQUENCY_UNITS.includes(u.frequency as FrequencyUnit) ? (u.frequency as FrequencyUnit) : DEFAULT_UNITS.frequency,
    pressure: u && PRESSURE_UNITS.includes(u.pressure as PressureUnit) ? (u.pressure as PressureUnit) : DEFAULT_UNITS.pressure,
  };
}
