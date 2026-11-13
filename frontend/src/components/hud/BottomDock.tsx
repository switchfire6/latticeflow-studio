/**
 * Collapsible split charts dock (§6.3). Panes:
 *   dispersion  after a shake-test (with the phonon control bar)
 *   g(r)        always
 *   relaxation  during and after a relaxation / Follow Instability
 *   dos         phonon DOS + thermodynamics (after PHONON_DOS_DATA, or with a Compute button after a shake-test)
 *   eos         equation of state (after "Compute K")
 * Panes computed with the LJ fallback carry an "analytic fallback" label (§6.4).
 * Collapsed state, height (drag the top edge), hidden panes and pane widths
 * (drag the dividers) are kept in localStorage (store.setDock, try/catch).
 */
import { ChevronDown, ChevronUp, X } from "lucide-react";
import { useRef, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { availablePanes } from "../../services/dockActions";
import { DOCK_PANES, useCrystalStore, type DockPane } from "../../store/useCrystalStore";
import { followStatusText, transitionText } from "../../utils/followText";
import { PhononBandChart } from "../charts/PhononBandChart";
import { RdfChart } from "../charts/RdfChart";
import { RelaxationCurve } from "../charts/RelaxationCurve";
import { DosPane } from "./DosPane";
import { EosPane } from "./EosPane";
import { PhononControlBar } from "./PhononControlBar";
import { FallbackTag, cx } from "./ui";

const TITLES: Record<DockPane, string> = {
  dispersion: "Phonon dispersion ω(q)",
  dos: "Phonon DOS · thermodynamics",
  rdf: "Radial distribution g(r)",
  relaxation: "Relaxation",
  eos: "Equation of state E(V)",
};
const TOGGLE_LABELS: Record<DockPane, string> = { dispersion: "dispersion", dos: "DOS", rdf: "g(r)", relaxation: "relaxation", eos: "EOS" };
/** Panes whose numbers come from the uMLIP and get the fallback watermark. */
const MODEL_PANES: ReadonlySet<DockPane> = new Set<DockPane>(["dispersion", "dos", "eos"]);

export { availablePanes };

function RelaxationHeader() {
  const follow = useCrystalStore((s) => s.followResult);
  const stream = useCrystalStore((s) => s.streams.find((x) => x.kind === "relaxation") ?? null);
  if (follow) {
    return (
      <span className="truncate font-mono text-[10.5px] normal-case tracking-normal">
        <span className="text-slate-400">follow {follow.qLabel ?? "q"} ν{follow.bandIndex}: </span>
        <span className={follow.to ? "text-emerald-300" : "text-slate-300"}>{transitionText(follow)}</span>
        <span className="text-slate-500"> · {followStatusText(follow)}</span>
      </span>
    );
  }
  if (stream?.progress.step != null) {
    return (
      <span className="font-mono text-[10.5px] normal-case tracking-normal text-slate-400">
        step {stream.progress.step} · max |F| {stream.progress.maxForce?.toFixed(3)} eV/Å
      </span>
    );
  }
  return null;
}

function Pane({ pane, weight, children, onHide }: { pane: DockPane; weight: number; children: ReactNode; onHide(): void }) {
  return (
    <div className="flex min-w-0 flex-col rounded-lg border border-white/[0.06] bg-white/[0.015]" style={{ flex: `${weight} 1 0` }} data-pane={pane}>
      <div className="flex h-7 shrink-0 items-center gap-2 px-2.5">
        <span className="shrink-0 text-[10px] font-semibold tracking-[0.16em] text-slate-400 uppercase">{TITLES[pane]}</span>
        <span className="min-w-0 flex-1 truncate">{pane === "relaxation" && <RelaxationHeader />}</span>
        {MODEL_PANES.has(pane) && <FallbackTag />}
        <button type="button" title="Hide pane" className="text-slate-600 hover:text-slate-300" onClick={onHide}>
          <X size={12} />
        </button>
      </div>
      <div className="flex min-h-0 flex-1 px-2 pb-2">
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
      className="relative shrink-0 border-t border-white/[0.06] bg-slate-950/85 backdrop-blur-md"
      style={{ height: dock.open ? dock.height : 32 }}
      data-dock-open={dock.open}
    >
      {dock.open && <div className="absolute -top-1 right-0 left-0 z-10 h-2 cursor-row-resize hover:bg-cyan-400/20" onPointerDown={startResize} title="Drag to resize" />}
      <div className="flex h-8 items-center gap-3 px-4">
        <button
          type="button"
          onClick={() => setDock({ open: !dock.open })}
          className="flex items-center gap-2 text-[10px] font-semibold tracking-[0.18em] text-slate-400 uppercase hover:text-slate-200"
        >
          {dock.open ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
          Charts dock
        </button>
        <div className="flex gap-1">
          {DOCK_PANES.map((p) => {
            const isAvail = available.split(",").includes(p);
            const shown = isAvail && !dock.hidden.includes(p);
            return (
              <button
                key={p}
                type="button"
                disabled={!isAvail}
                onClick={() => toggleHidden(p)}
                className={cx(
                  "rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors disabled:opacity-30",
                  shown ? "bg-cyan-400/15 text-cyan-200" : "text-slate-500 hover:text-slate-300",
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
                  className="mx-0.5 w-1.5 shrink-0 cursor-col-resize rounded hover:bg-cyan-400/25"
                  onPointerDown={(e) => startSplit(e, panes[i - 1]!, p)}
                  title="Drag to resize panes"
                />
              )}
              <Pane pane={p} weight={1} onHide={() => toggleHidden(p)}>
                {p === "dispersion" ? (
                  <div className="flex h-full w-full gap-3">
                    <PhononControlBar />
                    <div className="min-w-0 flex-1">
                      <PhononBandChart />
                    </div>
                  </div>
                ) : p === "rdf" ? (
                  <RdfChart />
                ) : p === "dos" ? (
                  <DosPane />
                ) : p === "eos" ? (
                  <EosPane />
                ) : (
                  <RelaxationCurve />
                )}
              </Pane>
            </div>
          ))}
          {panes.length === 0 && <div className="grid flex-1 place-items-center text-[11px] text-slate-500">All panes hidden. Use the toggles above.</div>}
        </div>
      )}
    </section>
  );
}
