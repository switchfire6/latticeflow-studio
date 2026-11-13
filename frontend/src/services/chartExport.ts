/**
 * Data export of the dock panes (§4.2): CSV per chart (built by utils/csv.ts
 * from the store) and PNG of the pane's canvases at 2× DPR, optionally on a
 * white "publication" background (utils/pngExport.ts).
 */
import { useSettings } from "../store/settings";
import { useCrystalStore, type CrystalStoreState, type DockPane } from "../store/useCrystalStore";
import {
  diffractionCsv,
  dispersionCsv,
  dosCsv,
  elasticCsv,
  eosCsv,
  fileStem,
  rdfCsv,
  relaxationCsv,
  thermalCsv,
} from "../utils/csv";
import { downloadBlob, downloadText } from "../utils/download";
import { activeModel, modelBadgeText } from "../utils/models";
import { compositeCharts, type RenderedChart } from "../utils/pngExport";
import { chartRenderer } from "../components/charts/useCanvasChart";
import type { FrequencyUnit } from "../utils/units";

export interface CsvFile {
  label: string;
  filename: string;
  /** Built on demand (a dispersion table can be large). */
  build(): string;
}

/** Provenance comment lines for every export. */
export function provenance(st: Pick<CrystalStoreState, "crystal" | "model" | "health">): string[] {
  const c = st.crystal;
  const m = activeModel(st.model, st.health);
  return [
    c ? `${c.formula} ${c.spacegroup_symbol} (#${c.spacegroup_number}), ${c.atoms.length} atoms` : "no structure",
    `model ${m ? modelBadgeText(m) : "unknown"}; LatticeFlow Studio`,
  ];
}

/** CSV files a pane can export right now (empty when it has no data). */
export function paneCsvFiles(pane: DockPane, st: CrystalStoreState, unit: FrequencyUnit): CsvFile[] {
  const head = provenance(st);
  const f = st.crystal?.formula;
  switch (pane) {
    case "dispersion":
      return st.phonon ? [{ label: "CSV", filename: `${fileStem(f, "dispersion")}.csv`, build: () => dispersionCsv(st.phonon!, unit, head) }] : [];
    case "dos":
      return st.dos
        ? [
            { label: "CSV · DOS", filename: `${fileStem(f, "phonon_dos")}.csv`, build: () => dosCsv(st.dos!, unit, head) },
            { label: "CSV · thermodynamics", filename: `${fileStem(f, "thermal")}.csv`, build: () => thermalCsv(st.dos!, head) },
          ]
        : [];
    case "eos":
      return st.eos ? [{ label: "CSV", filename: `${fileStem(f, "eos")}.csv`, build: () => eosCsv(st.eos!.result, st.eos!.nAtoms, head) }] : [];
    case "rdf": {
      const c = st.frame.crystal;
      return c && c.rdf_r.length ? [{ label: "CSV", filename: `${fileStem(f, "rdf")}.csv`, build: () => rdfCsv(c.rdf_r, c.rdf_g, head) }] : [];
    }
    case "relaxation": {
      const h = st.session.relaxHistory;
      return h.length ? [{ label: "CSV", filename: `${fileStem(f, "relaxation")}.csv`, build: () => relaxationCsv(h, st.session.relaxFmax, head) }] : [];
    }
    case "xrd":
      return st.diffraction ? [{ label: "CSV · peaks", filename: `${fileStem(f, "xrd_peaks")}.csv`, build: () => diffractionCsv(st.diffraction!.result, head) }] : [];
    case "elastic":
      return st.elastic ? [{ label: "CSV · C_ij", filename: `${fileStem(f, "elastic")}.csv`, build: () => elasticCsv(st.elastic!.result, head) }] : [];
  }
}

export function downloadCsv(file: CsvFile): void {
  downloadText(file.build(), file.filename, "text/csv;charset=utf-8");
}

/** Render every chart canvas inside `root` at `dpr` and composite them into one PNG. */
export async function exportPanePng(root: HTMLElement, filename: string, publication: boolean, dpr = 2): Promise<boolean> {
  const canvases = [...root.querySelectorAll("canvas")].filter((c) => chartRenderer(c));
  if (!canvases.length) return false;
  const rects = canvases.map((c) => c.getBoundingClientRect());
  const left = Math.min(...rects.map((r) => r.left));
  const top = Math.min(...rects.map((r) => r.top));
  const right = Math.max(...rects.map((r) => r.right));
  const bottom = Math.max(...rects.map((r) => r.bottom));
  const charts: RenderedChart[] = canvases.map((c, i) => ({ canvas: chartRenderer(c)!(dpr), x: rects[i]!.left - left, y: rects[i]!.top - top }));
  const out = compositeCharts(charts, right - left, bottom - top, dpr, publication);
  const blob = await new Promise<Blob | null>((resolve) => out.toBlob(resolve, "image/png"));
  if (!blob) return false;
  downloadBlob(blob, filename);
  return true;
}

export function pngFilename(pane: DockPane, publication: boolean): string {
  return `${fileStem(useCrystalStore.getState().crystal?.formula, pane)}${publication ? "_publication" : ""}.png`;
}

export const currentFrequencyUnit = (): FrequencyUnit => useSettings.getState().units.frequency;
