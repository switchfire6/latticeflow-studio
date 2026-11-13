/**
 * Chemical formula parsing for the generator form (§3.5): "SrTiO3",
 * "Li2MnO3", "Ca3(PO4)2", "[Co(NH3)6]Cl3". Element symbols are validated
 * against the studio's element table (Z ≤ 89, the MACE-MP-0 coverage).
 */
import { MAX_Z, elementBySymbol } from "./cpkColors";

export type Composition = Record<string, number>;

export interface ParsedFormula {
  ok: true;
  /** Element → count, in order of first appearance. */
  composition: Composition;
  /** Element symbols in order of first appearance. */
  elements: string[];
  atoms: number;
}
export interface FormulaError {
  ok: false;
  error: string;
}

const OPEN = "([{";
const CLOSE = ")]}";

/** Parse a formula. Counts must be positive integers (no fractional occupancies). */
export function parseFormula(input: string): ParsedFormula | FormulaError {
  const s = input.replace(/\s+/g, "");
  if (!s) return { ok: false, error: "empty formula" };
  const order: string[] = [];
  const stack: Map<string, number>[] = [new Map()];
  let i = 0;

  const readCount = (): number | FormulaError => {
    let j = i;
    while (j < s.length && /[0-9.]/.test(s[j]!)) j++;
    if (j === i) return 1;
    const raw = s.slice(i, j);
    i = j;
    if (!/^\d+$/.test(raw)) return { ok: false, error: `fractional count "${raw}" is not supported` };
    const n = Number.parseInt(raw, 10);
    if (n <= 0) return { ok: false, error: "counts must be positive" };
    return n;
  };

  while (i < s.length) {
    const ch = s[i]!;
    if (OPEN.includes(ch)) {
      stack.push(new Map());
      i++;
    } else if (CLOSE.includes(ch)) {
      if (stack.length < 2) return { ok: false, error: `unbalanced "${ch}"` };
      i++;
      const k = readCount();
      if (typeof k !== "number") return k;
      const group = stack.pop()!;
      const top = stack[stack.length - 1]!;
      for (const [el, n] of group) top.set(el, (top.get(el) ?? 0) + n * k);
    } else if (/[A-Z]/.test(ch)) {
      let sym = ch;
      i++;
      if (i < s.length && /[a-z]/.test(s[i]!)) {
        // two-letter symbol if it exists, else a one-letter symbol followed by junk
        const two = sym + s[i]!;
        if (elementBySymbol(two)) {
          sym = two;
          i++;
        }
      }
      const info = elementBySymbol(sym);
      if (!info) return { ok: false, error: `unknown element "${sym}${/[a-z]/.test(s[i] ?? "") ? s[i] : ""}"` };
      if (info.z > MAX_Z) return { ok: false, error: `${sym} (Z = ${info.z}) is outside the model's coverage (Z ≤ ${MAX_Z})` };
      const k = readCount();
      if (typeof k !== "number") return k;
      if (!order.includes(sym)) order.push(sym);
      const top = stack[stack.length - 1]!;
      top.set(sym, (top.get(sym) ?? 0) + k);
    } else if (/[a-z]/.test(ch)) {
      return { ok: false, error: `element symbols start with a capital letter ("${ch}")` };
    } else {
      return { ok: false, error: `unexpected "${ch}"` };
    }
  }
  if (stack.length !== 1) return { ok: false, error: "unbalanced parentheses" };
  const top = stack[0]!;
  const composition: Composition = {};
  let atoms = 0;
  for (const el of order) {
    const n = top.get(el) ?? 0;
    if (n > 0) {
      composition[el] = n;
      atoms += n;
    }
  }
  return { ok: true, composition, elements: Object.keys(composition), atoms };
}

/** Multiply every count by `units` (formula units per cell). */
export function scaleComposition(c: Composition, units: number): Composition {
  const k = Math.max(1, Math.round(units));
  const out: Composition = {};
  for (const [el, n] of Object.entries(c)) out[el] = n * k;
  return out;
}

function gcd(a: number, b: number): number {
  while (b) [a, b] = [b, a % b];
  return a;
}

/** Greatest common divisor of the counts (Z of the reduced formula). */
export function compositionGcd(counts: Iterable<number>): number {
  let g = 0;
  for (const n of counts) g = gcd(g, Math.round(n));
  return g || 1;
}

/** "Sr2Ti2O6" for display (order kept). */
export function compositionText(c: Composition): string {
  return Object.entries(c)
    .map(([el, n]) => (n === 1 ? el : `${el}${n}`))
    .join("");
}
