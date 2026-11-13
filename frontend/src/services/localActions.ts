/**
 * Local-structure auto-refresh (§5.7): while the "Local structure" pane is
 * shown, or polyhedra / bond-valence colouring are on, REQUEST_LOCAL_ANALYSIS
 * is re-sent for every committed structure: never while a stream runs (flow,
 * relaxation, MD frames) or an atom is being dragged, debounced and at most
 * once per REFRESH_MIN_INTERVAL_MS, and only once per state version.
 */
import { useEffect } from "react";
import { useCrystalStore, type CrystalStoreState } from "../store/useCrystalStore";
import { availablePanes } from "./dockActions";
import { commands } from "./wsClient";

export const REFRESH_DEBOUNCE_MS = 400;
export const REFRESH_MIN_INTERVAL_MS = 1000;

/** Whether anything on screen needs the local analysis. */
export function localAnalysisWanted(s: CrystalStoreState): boolean {
  const paneShown = s.dock.open && !s.dock.hidden.includes("local") && availablePanes(s).includes("local");
  return paneShown || s.display.polyhedra || s.display.bondColoring === "valence";
}

/** The structure is committed (no stream running, no drag in flight) and not yet analysed. */
export function localRefreshDue(s: CrystalStoreState, lastRequested: number | null): boolean {
  if (s.connection !== "open" || !s.crystal || s.localPending) return false;
  if (s.session.streams.length > 0 || s.frame.drag !== null) return false;
  const v = s.frame.stateVersion;
  return s.localAnalysis?.stateVersion !== v && lastRequested !== v;
}

export function useLocalAnalysisAutoRefresh(): void {
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let lastAt = -Infinity;
    let lastVersion: number | null = null;
    const fire = () => {
      timer = null;
      const s = useCrystalStore.getState();
      if (!localAnalysisWanted(s) || !localRefreshDue(s, lastVersion)) return;
      const wait = lastAt + REFRESH_MIN_INTERVAL_MS - Date.now();
      if (wait > 0) {
        timer = setTimeout(fire, wait);
        return;
      }
      lastAt = Date.now();
      lastVersion = s.frame.stateVersion;
      commands.requestLocalAnalysis({ include_partial_rdf: true });
    };
    const check = () => {
      const s = useCrystalStore.getState();
      if (timer || !localAnalysisWanted(s) || !localRefreshDue(s, lastVersion)) return;
      timer = setTimeout(fire, REFRESH_DEBOUNCE_MS);
    };
    // A drag ends without a store change once its override reconciles: poll lightly too.
    const poll = setInterval(check, 500);
    const unsub = useCrystalStore.subscribe(check);
    check();
    return () => {
      unsub();
      clearInterval(poll);
      if (timer) clearTimeout(timer);
    };
  }, []);
}
