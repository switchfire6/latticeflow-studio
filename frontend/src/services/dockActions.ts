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
  elastic?: unknown;
  diffraction?: unknown;
  diffractionPending?: boolean;
  openedPanes?: readonly DockPane[];
  localAnalysis?: unknown;
  mdVersion?: number;
}): DockPane[] {
  const out: DockPane[] = [];
  const opened = s.openedPanes ?? [];
  if (s.phonon) out.push("dispersion");
  if (s.phonon || s.dos) out.push("dos");
  out.push("rdf");
  if (s.relaxHistory.length > 0 || s.streams.some((x) => x.kind === "relaxation") || s.followResult) out.push("relaxation");
  if (s.eos || s.eosPending) out.push("eos");
  if (s.elastic || s.streams.some((x) => x.kind === "elastic") || opened.includes("elastic")) out.push("elastic");
  if (s.diffraction || s.diffractionPending || opened.includes("xrd")) out.push("xrd");
  if (s.localAnalysis || opened.includes("local")) out.push("local");
  if ((s.mdVersion ?? 0) > 0 || s.streams.some((x) => x.kind === "md") || opened.includes("md")) out.push("md");
  if (opened.includes("phase")) out.push("phase");
  if (opened.includes("results")) out.push("results");
  return out;
}

/** Open the dock on `pane` (hides g(r) when the dock is already full); on-demand panes become available. */
export function showPane(pane: DockPane, store: CrystalStore = useCrystalStore): void {
  const s = store.getState();
  const visible = availablePanes(s).filter((p) => !s.dock.hidden.includes(p));
  s.openPane(pane, visible);
}
