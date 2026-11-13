/**
 * Phase C2 compare mode (§7.2): which structures can be compared (the current
 * one, the FOLLOW_INSTABILITY parent and every history entry) and the "Compare
 * with parent" shortcut. History entries whose payload the snapshot cache has
 * not seen are listed with `snapshot: null` and fetched on selection
 * (Phase D REQUEST_HISTORY_ENTRY, see `fetchEntry`).
 */
import { useCrystalStore } from "../store/useCrystalStore";
import { useUi } from "../store/uiStore";
import { historyKey, snapshots, type Snapshot } from "./snapshotCache";
import { commands } from "./wsClient";

export interface CompareOption {
  id: string;
  label: string;
  /** null: a history entry not cached yet (select it to fetch it). */
  snapshot: Snapshot | null;
  /** History entry (position + cache key), for fetching. */
  entry?: { position: number; key: string };
}

/** Selectable structures, newest first. */
export function compareOptions(): CompareOption[] {
  const st = useCrystalStore.getState();
  const out: CompareOption[] = [];
  const cur = snapshots.current({ crystal: st.frame.crystal, history: st.history });
  if (cur) out.push({ id: "current", label: `current · ${cur.crystal.formula} ${cur.crystal.spacegroup_symbol}`, snapshot: { ...cur, label: `current: ${cur.label}` } });
  if (snapshots.followParent) {
    const p = snapshots.followParent;
    out.push({ id: "follow-parent", label: `follow parent · ${p.crystal.formula} ${p.crystal.spacegroup_symbol}`, snapshot: p });
  }
  const entries = [...(st.history?.entries ?? [])].reverse();
  for (const e of entries) {
    const key = historyKey(e);
    out.push({ id: key, label: `${e.position + 1}. ${e.label} · ${e.formula} ${e.spacegroup_symbol}`, snapshot: snapshots.get(key), entry: { position: e.position, key } });
  }
  return out;
}

/** Ask the server for a history entry's structure; it lands in the snapshot cache (HISTORY_ENTRY). */
export function fetchEntry(position: number): boolean {
  return commands.requestHistoryEntry(position);
}

/** Phonons of a selection, re-read from the cache (they may have been computed after it was picked). */
export function phononOf(s: Snapshot | null) {
  if (!s) return null;
  return snapshots.get(s.key)?.phonon ?? s.phonon;
}

/** FollowBanner: parent (cached at send) vs the current structure (the product). */
export function compareWithFollowParent(): void {
  const st = useCrystalStore.getState();
  const b = snapshots.current({ crystal: st.frame.crystal, history: st.history });
  useUi.getState().openCompare(snapshots.followParent, b ? { ...b, label: `product: ${b.label}` } : null);
}
