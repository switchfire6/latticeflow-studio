/**
 * Shared export menu of the dock panes (§4.2): CSV per chart, PNG at 2× DPR
 * (dark, as shown) and a white-background "publication" PNG; panes can add
 * their own items (e.g. the phonopy file of the dispersion pane).
 */
import { Download } from "lucide-react";
import { useEffect, useRef, useState, type RefObject } from "react";
import { currentFrequencyUnit, downloadCsv, exportPanePng, paneCsvFiles, pngFilename } from "../../services/chartExport";
import { useCrystalStore, type DockPane } from "../../store/useCrystalStore";
import { useLibrary } from "../../store/libraryStore";
import { cx } from "./ui";

export interface ExportItem {
  label: string;
  run(): void;
  disabled?: boolean;
  title?: string;
}

export function ExportMenu({ items, title = "Export" }: { items: ExportItem[]; title?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  const any = items.some((i) => !i.disabled);
  return (
    <span ref={ref} className="relative">
      <button
        type="button"
        title={title}
        aria-expanded={open}
        disabled={!any}
        onClick={() => setOpen((o) => !o)}
        className={cx("rounded-ctl p-0.5 text-fg-4 hover:text-fg disabled:opacity-30", open && "text-accent-fg")}
      >
        <Download size={12} />
      </button>
      {open && (
        <div className="absolute top-full right-0 z-50 mt-1 min-w-[190px] rounded-ctl border border-line bg-surface p-1 shadow-lg shadow-black/40" role="menu">
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              role="menuitem"
              disabled={it.disabled}
              title={it.title}
              onClick={() => {
                setOpen(false);
                it.run();
              }}
              className="block w-full rounded-ctl px-2 py-1 text-left text-xs text-fg-2 hover:bg-hover hover:text-fg disabled:opacity-35"
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}

/** Standard items for a pane: its CSV files, PNG and publication PNG of its canvases. */
export function usePaneExportItems(pane: DockPane, bodyRef: RefObject<HTMLElement | null>, extra: ExportItem[] = []): ExportItem[] {
  // Re-render when the pane's data changes so the menu enables / disables correctly.
  useLibrary((s) => (pane === "results" ? s.structures : null));
  useCrystalStore((s) =>
    pane === "phase" ? s.hull : pane === "dispersion" ? s.phonon : pane === "dos" ? s.dos : pane === "eos" ? s.eos : pane === "xrd" ? s.diffraction : pane === "elastic" ? s.elastic : pane === "relaxation" ? s.relaxHistory : pane === "local" ? s.localAnalysis : pane === "md" ? s.mdVersion : s.structureVersion,
  );
  const st = useCrystalStore.getState();
  const files = paneCsvFiles(pane, st, currentFrequencyUnit());
  const fail = () => st.pushToast("warning", "Nothing to export", "This pane has no chart to render yet.");
  const png = (publication: boolean): ExportItem => ({
    label: publication ? "PNG · publication (white, 2×)" : "PNG · dark (2×)",
    disabled: pane === "elastic",
    run: () => {
      const el = bodyRef.current;
      if (!el) return;
      void exportPanePng(el, pngFilename(pane, publication), publication).then((ok) => ok || fail());
    },
  });
  return [
    ...(files.length ? files.map((f) => ({ label: f.label, run: () => downloadCsv(f) })) : [{ label: "CSV", disabled: true, run: () => {} }]),
    png(true),
    png(false),
    ...extra,
  ];
}
