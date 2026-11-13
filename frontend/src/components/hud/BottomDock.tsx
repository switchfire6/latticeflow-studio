/**
 * Collapsible split charts dock (§6.3). Panes:
 *   dispersion  after a shake-test (with the phonon control bar)
 *   g(r)        always
 *   relaxation  during and after a relaxation / Follow Instability
 *   dos         phonon DOS + thermodynamics (after PHONON_DOS_DATA, or with a Compute button after a shake-test)
 *   eos         equation of state (after "Compute K")
 *   elastic     elastic tensor C_ij (Phase B, on demand)
 *   xrd         simulated powder diffraction (Phase B, on demand)
 *   local       local structure: partial g(r), BVS, polyhedra, Glazer tilts (Phase C1, on demand)
 *   md          molecular dynamics + Arrhenius scan (Phase C1, on demand)
 * Every pane header carries an export menu (CSV / PNG / publication PNG, §4.2).
 * Panes computed with the LJ fallback carry an "analytic fallback" label (§6.4).
 * Collapsed state, height (drag the top edge), hidden panes and pane widths
 * (drag the dividers) are kept in localStorage (store.setDock, try/catch).
 */
import { ChevronDown, ChevronUp, X } from "lucide-react";
import { useRef, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { availablePanes, showPane } from "../../services/dockActions";
import { commands } from "../../services/wsClient";
import { DOCK_PANES, ON_DEMAND_PANES, useCrystalStore, type DockPane } from "../../store/useCrystalStore";
import { followStatusText, transitionText } from "../../utils/followText";
import { PhononBandChart } from "../charts/PhononBandChart";
import { RdfChart } from "../charts/RdfChart";
import { RelaxationCurve } from "../charts/RelaxationCurve";
import { DiffractionPane } from "./DiffractionPane";
import { DosPane } from "./DosPane";
import { ElasticPane } from "./ElasticPane";
import { EosPane } from "./EosPane";
import { LocalStructurePane } from "./LocalStructurePane";
import { MdPane } from "./MdPane";
import { FromLibraryBadge, PhasePane, PhononCompareControl, ResultsPane } from "./LibraryPanes";
import { ExportMenu, usePaneExportItems, type ExportItem } from "./ExportMenu";
import { PhononControlBar } from "./PhononControlBar";
import { PhononInfoStrip } from "./PhononInfoStrip";
import { FallbackTag, cx } from "./ui";

const TITLES: Record<DockPane, string> = {
  dispersion: "Phonon dispersion ω(q)",
  dos: "Phonon DOS · thermodynamics",
  rdf: "Radial distribution g(r)",
  relaxation: "Relaxation",
  eos: "Equation of state E(V)",
  elastic: "Elastic tensor C_ij",
  xrd: "Powder diffraction",
  local: "Local structure",
  md: "Molecular dynamics",
  phase: "Phase diagram",
  results: "Project results",
};
const TOGGLE_LABELS: Record<DockPane, string> = {
  dispersion: "dispersion",
  dos: "DOS",
  rdf: "g(r)",
  relaxation: "relaxation",
  eos: "EOS",
  elastic: "elastic",
  xrd: "XRD",
  local: "local",
  md: "MD",
  phase: "phase diagram",
  results: "results",
};
/** Panes whose numbers come from the uMLIP and get the fallback watermark. */
const MODEL_PANES: ReadonlySet<DockPane> = new Set<DockPane>(["dispersion", "dos", "eos", "elastic", "md", "phase"]);

function PaneExport({ pane, bodyRef }: { pane: DockPane; bodyRef: React.RefObject<HTMLDivElement | null> }) {
  const exportPending = useCrystalStore((s) => s.phononExportPending);
  const hasPhonon = useCrystalStore((s) => s.phonon !== null);
  const extra: ExportItem[] =
    pane === "dispersion"
      ? [
          {
            label: exportPending ? "phonopy file (preparing…)" : "phonopy file (phonopy_params.yaml)",
            disabled: !hasPhonon || exportPending,
            title: "Force constants + structure for continuing in phonopy",
            run: () => void commands.exportPhonons(),
          },
        ]
      : [];
  const items = usePaneExportItems(pane, bodyRef, extra);
  return <ExportMenu items={items} title={`Export ${TITLES[pane].toLowerCase()}`} />;
}

export { availablePanes };

function RelaxationHeader() {
  const follow = useCrystalStore((s) => s.followResult);
  const stream = useCrystalStore((s) => s.streams.find((x) => x.kind === "relaxation") ?? null);
  if (follow) {
    return (
      <span className="truncate font-mono text-xs normal-case tracking-normal">
        <span className="text-fg-3">follow {follow.qLabel ?? "q"} ν{follow.bandIndex}: </span>
        <span className={follow.to ? "text-ok" : "text-fg-2"}>{transitionText(follow)}</span>
        <span className="text-fg-4"> · {followStatusText(follow)}</span>
      </span>
    );
  }
  if (stream?.progress.step != null) {
    return (
      <span className="font-mono text-xs normal-case tracking-normal text-fg-3">
        step {stream.progress.step} · max |F| {stream.progress.maxForce?.toFixed(3)} eV/Å
      </span>
    );
  }
  return null;
}

function Pane({ pane, weight, children, onHide }: { pane: DockPane; weight: number; children: ReactNode; onHide(): void }) {
  const bodyRef = useRef<HTMLDivElement>(null);
  return (
    <div className="flex min-w-0 flex-col rounded-panel border border-line bg-surface" style={{ flex: `${weight} 1 0` }} data-pane={pane}>
      <div className="flex h-7 shrink-0 items-center gap-2 px-2.5">
        <span className="shrink-0 text-xs font-semibold text-fg-2">{TITLES[pane]}</span>
        <span className="min-w-0 flex-1 truncate">
          {pane === "relaxation" && <RelaxationHeader />}
          {pane === "dispersion" && <PhononCompareControl />}
        </span>
        <FromLibraryBadge pane={pane} />
        {MODEL_PANES.has(pane) && <FallbackTag />}
        <PaneExport pane={pane} bodyRef={bodyRef} />
        <button type="button" title="Hide pane" className="text-fg-5 hover:text-fg-2" onClick={onHide}>
          <X size={12} />
        </button>
      </div>
      <div ref={bodyRef} className="flex min-h-0 flex-1 px-2 pb-2">
        {children}
      </div>
    </div>
  );
}

export function BottomDock() {
  const dock = useCrystalStore((s) => s.dock);
  const setDock = useCrystalStore((s) => s.setDock);
  const available = useCrystalStore((s) => availablePanes(s).join(","));
  const panes = (available.split(",") as DockPane[]).filter((p) => !dock.hidden.includes(p));
  const bodyRef = useRef<HTMLDivElement>(null);
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);

  const startResize = (e: ReactPointerEvent) => {
    e.preventDefault();
    const y0 = e.clientY;
    const h0 = dock.height;
    const move = (ev: PointerEvent) => setDock({ height: h0 + (y0 - ev.clientY), open: true });
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const startSplit = (e: ReactPointerEvent, left: DockPane, right: DockPane) => {
    e.preventDefault();
    const width = bodyRef.current?.getBoundingClientRect().width ?? 1000;
    const total = panes.reduce((a, p) => a + dock.weights[p], 0);
    const wl = dock.weights[left];
    const wr = dock.weights[right];
    const x0 = e.clientX;
    const move = (ev: PointerEvent) => {
      const dw = ((ev.clientX - x0) / width) * total;
      const l = Math.max(0.25, Math.min(wl + wr - 0.25, wl + dw));
      setDock({ weights: { ...useCrystalStore.getState().dock.weights, [left]: l, [right]: wl + wr - l } });
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const toggleHidden = (p: DockPane) =>
    setDock({ hidden: dock.hidden.includes(p) ? dock.hidden.filter((x) => x !== p) : [...dock.hidden, p], open: true });

  return (
    <section
      className="relative shrink-0 border-t border-line bg-canvas"
      style={{ height: dock.open ? dock.height : 32 }}
      data-dock-open={dock.open}
    >
      {dock.open && <div className="absolute -top-1 right-0 left-0 z-10 h-2 cursor-row-resize hover:bg-hover" onPointerDown={startResize} title="Drag to resize" />}
      <div className="flex h-8 items-center gap-3 px-4">
        <button
          type="button"
          onClick={() => setDock({ open: !dock.open })}
          className="flex items-center gap-2 text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase hover:text-fg"
        >
          {dock.open ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
          Charts
        </button>
        <div className="flex gap-1">
          {DOCK_PANES.map((p) => {
            const isAvail = available.split(",").includes(p);
            const shown = isAvail && !dock.hidden.includes(p);
            // On-demand panes (elastic, XRD) can be opened from the toggle once a structure exists.
            const onDemand = !isAvail && ON_DEMAND_PANES.includes(p) && hasCrystal;
            return (
              <button
                key={p}
                type="button"
                disabled={!isAvail && !onDemand}
                title={onDemand ? `Open the ${TITLES[p].toLowerCase()} pane` : undefined}
                onClick={() => (onDemand ? showPane(p) : toggleHidden(p))}
                className={cx(
                  "rounded-ctl px-2 py-0.5 text-xs transition-colors disabled:opacity-30",
                  shown ? "bg-accent-soft text-accent-fg" : "text-fg-3 hover:bg-hover hover:text-fg",
                )}
              >
                {TOGGLE_LABELS[p]}
              </button>
            );
          })}
        </div>
      </div>
      {dock.open && (
        <div ref={bodyRef} className="flex h-[calc(100%-2rem)] gap-0 px-3 pb-2">
          {panes.map((p, i) => (
            <div key={p} className="flex min-w-0" style={{ flex: `${dock.weights[p]} 1 0` }}>
              {i > 0 && (
                <div
                  className="mx-0.5 w-1.5 shrink-0 cursor-col-resize rounded-ctl hover:bg-hover"
                  onPointerDown={(e) => startSplit(e, panes[i - 1]!, p)}
                  title="Drag to resize panes"
                />
              )}
              <Pane pane={p} weight={1} onHide={() => toggleHidden(p)}>
                {p === "dispersion" ? (
                  <div className="flex h-full w-full gap-3">
                    <PhononControlBar />
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <PhononInfoStrip />
                      <div className="relative min-h-0 flex-1">
                        <PhononBandChart />
                      </div>
                    </div>
                  </div>
                ) : p === "rdf" ? (
                  <RdfChart />
                ) : p === "dos" ? (
                  <DosPane />
                ) : p === "eos" ? (
                  <EosPane />
                ) : p === "elastic" ? (
                  <ElasticPane />
                ) : p === "xrd" ? (
                  <DiffractionPane />
                ) : p === "local" ? (
                  <LocalStructurePane />
                ) : p === "md" ? (
                  <MdPane />
                ) : p === "phase" ? (
                  <PhasePane />
                ) : p === "results" ? (
                  <ResultsPane />
                ) : (
                  <RelaxationCurve />
                )}
              </Pane>
            </div>
          ))}
          {panes.length === 0 && <div className="grid flex-1 place-items-center text-xs text-fg-4">All panes hidden. Use the toggles above.</div>}
        </div>
      )}
    </section>
  );
}
