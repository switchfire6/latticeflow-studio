/**
 * Phase C2 compare mode (§7.2): a client-side cache of the CrystalStatePayload
 * last seen for each history entry. The server only ever sends the active
 * structure, so the cache records `frame.crystal` whenever a HISTORY_STATE
 * names the current entry (it follows every state change), and attaches the
 * phonon summary (lowest branch along the path) and DOS computed while that
 * entry is active. Entries are keyed by content (label, formula, atoms,
 * energy), like the bulk reference, because positions shift when the oldest
 * history entries are dropped; keys no longer in the history are pruned.
 *
 * FOLLOW_INSTABILITY additionally keeps its parent structure (with its
 * phonons) so "Compare with parent" works even after the history moved on.
 */
import { create } from "zustand";
import type { CrystalStatePayload, HistoryEntryInfo, HistoryState, KPathLabel, PhononAnalysisComplete, PhononDosData, ServerMessage } from "../types/protocol";

export interface PhononSnapshot {
  stable: boolean;
  minFrequencyThz: number;
  imaginaryCount: number;
  kpathDistances: number[];
  kpathLabels: KPathLabel[];
  /** Lowest branch at every q (THz, ascending order → index 0). */
  lowestBranch: number[];
  dos: { frequencies: number[]; total: number[] } | null;
}

export interface Snapshot {
  key: string;
  label: string;
  crystal: CrystalStatePayload;
  phonon: PhononSnapshot | null;
}

export function historyKey(e: Pick<HistoryEntryInfo, "label" | "formula" | "n_atoms" | "energy_per_atom">): string {
  return `${e.label}|${e.formula}|${e.n_atoms}|${e.energy_per_atom.toFixed(6)}`;
}

/** The payload really is the structure of this entry (formula, atom count and energy agree). */
export function payloadMatchesEntry(c: CrystalStatePayload, e: HistoryEntryInfo): boolean {
  return c.formula === e.formula && c.atoms.length === e.n_atoms && Math.abs(c.energy_per_atom - e.energy_per_atom) < 1e-5;
}

export function phononSnapshot(p: PhononAnalysisComplete, dos: PhononDosData | null = null): PhononSnapshot {
  return {
    stable: p.is_dynamically_stable,
    minFrequencyThz: p.min_frequency_thz,
    imaginaryCount: p.imaginary_mode_count,
    kpathDistances: p.kpath_distances.slice(),
    kpathLabels: p.kpath_labels.slice(),
    lowestBranch: p.frequencies_thz.map((row) => (row.length ? Math.min(...row) : Number.NaN)),
    dos: dos ? { frequencies: dos.frequencies_thz.slice(), total: dos.total_dos.slice() } : null,
  };
}

function currentEntry(h: HistoryState | null): HistoryEntryInfo | null {
  return h?.entries?.find((e) => e.position === h.position) ?? null;
}

export interface SnapshotSource {
  crystal: CrystalStatePayload | null;
  history: HistoryState | null;
}

export const SNAPSHOT_LIMIT = 60;

export class SnapshotCache {
  private map = new Map<string, Snapshot>();
  followParent: Snapshot | null = null;
  version = 0;
  constructor(private readonly onChange: () => void = () => {}) {}

  get(key: string): Snapshot | null {
    return this.map.get(key) ?? null;
  }

  /** The cached snapshot of a history entry, or null. */
  forEntry(e: HistoryEntryInfo): Snapshot | null {
    return this.map.get(historyKey(e)) ?? null;
  }

  get size(): number {
    return this.map.size;
  }

  private changed() {
    this.version++;
    this.onChange();
  }

  /** Snapshot of the current structure (cached one for the current entry, with its phonons, if it matches). */
  current(src: SnapshotSource): Snapshot | null {
    const c = src.crystal;
    if (!c) return null;
    const e = currentEntry(src.history);
    const cached = e ? this.forEntry(e) : null;
    if (cached && e && payloadMatchesEntry(c, e)) return { ...cached, crystal: c };
    return { key: "current", label: e?.label ?? "current", crystal: c, phonon: null };
  }

  /** Remember the parent of a FOLLOW_INSTABILITY (call before sending it). */
  setFollowParent(src: SnapshotSource, phonon: PhononAnalysisComplete | null, dos: PhononDosData | null): void {
    const cur = this.current(src);
    if (!cur) return;
    this.followParent = { ...cur, key: "follow-parent", label: `parent: ${cur.label}`, phonon: phonon ? phononSnapshot(phonon, dos) : cur.phonon };
    this.changed();
  }

  /** Call after every dispatched server message with the store's current state. */
  observe(msg: ServerMessage, src: SnapshotSource & { phonon: PhononAnalysisComplete | null; dos: PhononDosData | null }): void {
    if (msg.type === "HISTORY_STATE") {
      const h = msg.payload;
      const e = currentEntry(h);
      const keep = new Set((h.entries ?? []).map(historyKey));
      let changed = false;
      for (const k of [...this.map.keys()]) if (!keep.has(k)) (this.map.delete(k), (changed = true));
      if (e && src.crystal && payloadMatchesEntry(src.crystal, e)) {
        const key = historyKey(e);
        const prev = this.map.get(key);
        this.map.delete(key);
        this.map.set(key, { key, label: e.label, crystal: src.crystal, phonon: prev?.phonon ?? null });
        while (this.map.size > SNAPSHOT_LIMIT) this.map.delete(this.map.keys().next().value!);
        changed = true;
      }
      if (changed) this.changed();
      return;
    }
    if (msg.type === "PHONON_ANALYSIS_COMPLETE" || msg.type === "PHONON_DOS_DATA") {
      const e = currentEntry(src.history);
      const snap = e ? this.forEntry(e) : null;
      if (!snap || !src.phonon) return;
      snap.phonon = phononSnapshot(src.phonon, msg.type === "PHONON_DOS_DATA" ? msg.payload : src.dos);
      this.changed();
    }
  }

  clear(): void {
    this.map.clear();
    this.followParent = null;
    this.changed();
  }
}

/** Reactive version counter of the app cache (components re-render on change). */
export const useSnapshotVersion = create<{ v: number; bump(): void }>()((set) => ({ v: 0, bump: () => set((s) => ({ v: s.v + 1 })) }));

export const snapshots = new SnapshotCache(() => useSnapshotVersion.getState().bump());
