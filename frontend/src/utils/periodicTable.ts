/**
 * Periodic-table layout (18 columns plus a detached f-block) for the element
 * pickers, and the group-neighbour suggestions used by the transmute toolbar.
 */
import { ELEMENTS, MAX_Z, elementBySymbol } from "./cpkColors";

export interface TableCell {
  z: number;
  symbol: string;
  /** 1-based grid row: 1..7 main table, 9..10 f-block (row 8 is a spacer). */
  row: number;
  /** 1-based grid column, 1..18 (f-block uses 3..17). */
  col: number;
  period: number;
  /** IUPAC group 1..18, or null for La–Lu / Ac (the f-block row). */
  group: number | null;
}

const PERIOD_END = [2, 10, 18, 36, 54, 86, 118];

function periodOf(z: number): number {
  return PERIOD_END.findIndex((end) => z <= end) + 1;
}

function layout(z: number): Omit<TableCell, "symbol"> {
  const period = periodOf(z);
  const start = period === 1 ? 1 : PERIOD_END[period - 2]! + 1;
  const k = z - start; // 0-based offset within the period
  let group: number | null;
  if (period === 1) group = k === 0 ? 1 : 18;
  else if (period <= 3) group = k < 2 ? k + 1 : k + 11;
  else if (period <= 5) group = k + 1;
  else if (k < 2) group = k + 1;
  else if (k <= 16) group = null; // La..Lu, Ac..Lr
  else group = k - 13;
  if (group === null) {
    return { z, period, group, row: period + 3, col: k + 1 }; // La -> col 3
  }
  return { z, period, group, row: period, col: group };
}

/** Only Z <= 89 is selectable (MACE-MP-0 coverage), so the table stops at Ac. */
export const TABLE_CELLS: readonly TableCell[] = ELEMENTS.filter((e) => e.z <= MAX_Z).map((e) => ({
  symbol: e.symbol,
  ...layout(e.z),
}));

const CELL_BY_SYMBOL = new Map(TABLE_CELLS.map((c) => [c.symbol, c]));

export function tableCell(symbol: string): TableCell | undefined {
  return CELL_BY_SYMBOL.get(symbol);
}

/**
 * Quick transmutation targets: elements in the same group (nearest periods
 * first, e.g. Na -> Li, K, Rb), then the horizontal neighbours Z ± 1.
 */
export function groupNeighbours(symbol: string, maxPerColumn = 3): { column: string[]; row: string[] } {
  const cell = CELL_BY_SYMBOL.get(symbol);
  const el = elementBySymbol(symbol);
  if (!cell || !el) return { column: [], row: [] };
  let column: string[] = [];
  if (cell.group !== null) {
    // H sits above the alkali metals but is not a sensible "group neighbour" for them.
    column = TABLE_CELLS.filter((c) => c.group === cell.group && c.symbol !== symbol && (c.symbol !== "H" || symbol === "He"))
      .sort((a, b) => Math.abs(a.period - cell.period) - Math.abs(b.period - cell.period) || a.period - b.period)
      .slice(0, maxPerColumn)
      .sort((a, b) => a.z - b.z)
      .map((c) => c.symbol);
  }
  const row = [el.z - 1, el.z + 1]
    .filter((z) => z >= 1 && z <= MAX_Z)
    .map((z) => ELEMENTS[z - 1]!.symbol)
    .filter((s) => !column.includes(s));
  return { column, row };
}
