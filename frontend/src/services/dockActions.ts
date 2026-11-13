/** Dock pane availability and "show this pane" (making room at narrow widths). */
import { useCrystalStore, type CrystalStore, type DockPane } from "../store/useCrystalStore";

/** Which panes have content right now. */
export function availablePanes(s: {
  phonon: unknown;
  relaxHistory: unknown[];
  streams: { kind: string }[];
  followResult: unknown;
  dos?: unknown;
  eos?: unknown;
  eosPending?: boolean;
}): DockPane[] {
  const out: DockPane[] = [];
  if (s.phonon) out.push("dispersion");
  if (s.phonon || s.dos) out.push("dos");
  out.push("rdf");
  if (s.relaxHistory.length > 0 || s.streams.some((x) => x.kind === "relaxation") || s.followResult) out.push("relaxation");
  if (s.eos || s.eosPending) out.push("eos");
  return out;
}

/** Open the dock on `pane` (hides g(r) when the dock is already full). */
export function showPane(pane: DockPane, store: CrystalStore = useCrystalStore): void {
  const s = store.getState();
  const visible = availablePanes(s).filter((p) => !s.dock.hidden.includes(p));
  s.revealPane(pane, visible);
}
