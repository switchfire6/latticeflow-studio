/** Phase D: fetched history entries in the compare cache, /api/health size limits, after_terminal. */
import { describe, expect, it } from "vitest";
import { SnapshotCache } from "../src/services/snapshotCache";
import { describeError, isPostTerminalError } from "../src/services/wsClient";
import type { CrystalStatePayload, ErrorPayload, HistoryEntryInfo, HistoryState } from "../src/types/protocol";
import { studioLimits } from "../src/utils/limits";
import { makeCrystal } from "./fixtures";

const info = (position: number, label: string, c: CrystalStatePayload): HistoryEntryInfo => ({
  position,
  label,
  formula: c.formula,
  n_atoms: c.atoms.length,
  energy_per_atom: c.energy_per_atom,
  spacegroup_symbol: c.spacegroup_symbol,
});
const hist = (position: number, entries: HistoryEntryInfo[]): HistoryState => ({ can_undo: position > 0, can_redo: false, position, length: entries.length, label: entries[position]!.label, entries });

describe("compare cache: HISTORY_ENTRY", () => {
  it("stores a fetched entry that matches its position, ignores a stale one", () => {
    const cache = new SnapshotCache();
    const c0 = makeCrystal(5);
    const c1 = makeCrystal(5, { energy: -7.9 });
    const h = hist(1, [info(0, "Load preset", c0), info(1, "Relax", c1)]);
    const src = { crystal: c1, history: h, phonon: null, dos: null };
    // stale reply: the entry at position 0 is no longer this structure
    cache.observe({ type: "HISTORY_ENTRY", payload: { position: 0, label: "Load preset", crystal: c1 } }, src);
    expect(cache.forEntry(h.entries![0]!)).toBeNull();
    cache.observe({ type: "HISTORY_ENTRY", payload: { position: 0, label: "Load preset", crystal: c0 } }, src);
    expect(cache.forEntry(h.entries![0]!)?.crystal).toBe(c0);
  });
});

describe("size limits from /api/health", () => {
  it("uses the reported limits, defaults otherwise, in the error hints too", () => {
    expect(studioLimits(null)).toEqual({ maxAtoms: 128, phononMaxAtoms: 64 });
    expect(studioLimits({ max_atoms: 256, phonon_max_atoms: null })).toEqual({ maxAtoms: 256, phononMaxAtoms: 64 });
    const p: ErrorPayload = { code: "STRUCTURE_TOO_LARGE", message: "", command: "RUN_PHONON_SHAKE_TEST" };
    expect(describeError(p, studioLimits({ max_atoms: 256, phonon_max_atoms: 96 })).message).toContain("at most 96 atoms");
  });

  it("after_terminal overrides the code-based fallback", () => {
    const p: ErrorPayload = { code: "COMPUTE_FAILED", message: "", command: "START_MD" };
    expect(isPostTerminalError(p)).toBe(false);
    expect(isPostTerminalError({ ...p, after_terminal: true })).toBe(true);
    expect(isPostTerminalError({ ...p, code: "RELAXATION_DIVERGED" })).toBe(true);
  });
});
