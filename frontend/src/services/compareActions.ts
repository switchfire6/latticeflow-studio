/**
 * Phase C2 compare mode (§7.2): which structures can be compared (the current
 * one, the FOLLOW_INSTABILITY parent and every history entry whose payload the
 * snapshot cache has seen) and the "Compare with parent" shortcut.
 */
import { useCrystalStore } from "../store/useCrystalStore";
import { useUi } from "../store/uiStore";
import { snapshots, type Snapshot } from "./snapshotCache";

export interface CompareOption {
  id: string;
  label: string;
  snapshot: Snapshot;
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
    const snap = snapshots.forEntry(e);
    if (!snap) continue;
    out.push({ id: snap.key, label: `${e.position + 1}. ${e.label} · ${e.formula} ${e.spacegroup_symbol}`, snapshot: snap });
  }
  return out;
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
