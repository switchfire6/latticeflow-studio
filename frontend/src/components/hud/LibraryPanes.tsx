/**
 * Library-backed dock content:
 *   ResultsPane          the active project's structures as a sortable table of
 *                        the flattened `latest` summaries (empty columns hidden)
 *                        and an X/Y scatter (hover = formula, click = open);
 *   PhasePane            the hull response's phase diagram (protocol §8);
 *   PhononCompareControl "compare with…" for the dispersion pane (a stored
 *                        phonons result drawn dashed; only for matching k-paths);
 *   FromLibraryBadge     marks pane results fed back from the library.
 */
import { ArrowDown, ArrowUp, Calculator, GitCompare, Loader2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { evaluateHull } from "../../services/hullActions";
import { loadPhononOverlay, openStructure, refreshStructures } from "../../services/libraryActions";
import { useLibrary } from "../../store/libraryStore";
import { useCrystalStore, type CrystalStoreState, type DockPane } from "../../store/useCrystalStore";
import type { ResultKind } from "../../types/library";
import { columnTitle, flattenRows, formatCell, kpathMismatch, numericColumns, sortRows, visibleColumns } from "../../utils/library";
import { PhaseDiagramChart, PhaseDiagramLegend } from "../charts/PhaseDiagramChart";
import { ScatterChart, type ScatterPoint } from "../charts/ScatterChart";
import { Badge, Button, FormulaText, Segmented, cx } from "./ui";

// ------------------------------------------------------------------ "from library" badge
const PANE_RESULT: Partial<Record<DockPane, { kind: ResultKind; data: (s: CrystalStoreState) => unknown }>> = {
  dispersion: { kind: "phonons", data: (s) => s.phonon },
  elastic: { kind: "elastic", data: (s) => s.elastic?.result },
  eos: { kind: "eos", data: (s) => s.eos?.result },
  xrd: { kind: "diffraction", data: (s) => s.diffraction?.result },
  phase: { kind: "hull", data: (s) => (s.hull.status === "ok" ? s.hull.result : null) },
};

export function FromLibraryBadgeView({ kind, data }: { kind: ResultKind; data: unknown }) {
  const stored = useLibrary((s) => s.fromLibrary[kind]);
  if (!data || stored !== data) return null;
  return (
    <Badge tone="slate" title="Stored result loaded from the project library (not recomputed)">
      from library
    </Badge>
  );
}

export function FromLibraryBadge({ pane }: { pane: DockPane }) {
  const spec = PANE_RESULT[pane];
  const data = useCrystalStore((s) => (spec ? spec.data(s) : null));
  return spec ? <FromLibraryBadgeView kind={spec.kind} data={data} /> : null;
}

// ------------------------------------------------------------------ results table + scatter
const useCurrentStructureId = () => {
  const link = useLibrary((s) => s.link);
  const sv = useCrystalStore((s) => s.stateVersion);
  return link && link.stateVersion === sv ? link.structureId : null;
};

export function ResultsPane() {
  const rows = useLibrary((s) => s.structures);
  const project = useLibrary((s) => s.projects.find((p) => p.id === s.activeProjectId) ?? null);
  const currentId = useCurrentStructureId();
  const [view, setView] = useState<"table" | "scatter">("table");
  const [sort, setSort] = useState<{ key: string; dir: "asc" | "desc" }>({ key: "name", dir: "asc" });
  const flat = useMemo(() => flattenRows(rows), [rows]);
  const cols = useMemo(() => visibleColumns(flat), [flat]);
  const sorted = useMemo(() => sortRows(flat, sort.key, sort.dir), [flat, sort]);
  const numeric = numericColumns(cols);
  const [xKey, setXKey] = useState("e_hull_ev");
  const [yKey, setYKey] = useState("k_vrh_gpa");
  const xCol = numeric.find((c) => c.key === xKey) ?? numeric[0];
  const yCol = numeric.find((c) => c.key === yKey) ?? numeric[1] ?? numeric[0];

  useEffect(() => {
    void refreshStructures();
  }, []);

  const points: ScatterPoint[] = useMemo(() => {
    if (!xCol || !yCol) return [];
    return flat.flatMap((r) => {
      const x = r.values[xCol.key];
      const y = r.values[yCol.key];
      if (typeof x !== "number" || typeof y !== "number" || !Number.isFinite(x) || !Number.isFinite(y)) return [];
      return [{ id: r.id, x, y, lines: [`${r.values.formula} · ${r.values.spacegroup}`, `${r.values.name} (#${r.id}, ${r.values.origin})`, `${columnTitle(xCol)} ${formatCell(x, xCol)}`, `${columnTitle(yCol)} ${formatCell(y, yCol)}`] }];
    });
  }, [flat, xCol, yCol]);

  const header = (key: string) => setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  const sel = "rounded-ctl border border-line bg-field px-1 py-0.5 text-2xs text-fg outline-none focus:border-accent";

  return (
    <div className="flex h-full w-full min-w-0 flex-col gap-1.5" data-results-pane>
      <div className="flex shrink-0 items-center gap-2 text-2xs text-fg-3">
        <Segmented<"table" | "scatter">
          value={view}
          options={[
            { value: "table", label: "table" },
            { value: "scatter", label: "scatter" },
          ]}
          onChange={setView}
        />
        {view === "scatter" && numeric.length > 0 && (
          <>
            <label className="flex items-center gap-1">
              X
              <select className={sel} value={xCol?.key} onChange={(e) => setXKey(e.target.value)}>
                {numeric.map((c) => (
                  <option key={c.key} value={c.key}>
                    {columnTitle(c)}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1">
              Y
              <select className={sel} value={yCol?.key} onChange={(e) => setYKey(e.target.value)}>
                {numeric.map((c) => (
                  <option key={c.key} value={c.key}>
                    {columnTitle(c)}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
        <span className="ml-auto truncate">
          {project?.name ?? "—"} · {rows.length} structure{rows.length === 1 ? "" : "s"}
        </span>
      </div>
      {!rows.length ? (
        <div className="grid flex-1 place-items-center text-xs text-fg-4">The project has no structures yet.</div>
      ) : view === "table" ? (
        <div className="min-h-0 flex-1 overflow-auto rounded-ctl border border-line-soft">
          <table className="w-full border-collapse text-xs" data-results-table>
            <thead className="sticky top-0 z-10 bg-surface">
              <tr>
                {cols.map((c) => (
                  <th key={c.key} className={cx("border-b border-line px-2 py-1 font-medium whitespace-nowrap text-fg-3", c.kind === "number" ? "text-right" : "text-left")}>
                    <button type="button" className="inline-flex items-center gap-0.5 hover:text-fg" onClick={() => header(c.key)} title={`Sort by ${columnTitle(c)}`}>
                      {c.label}
                      {c.unit && <span className="font-normal text-fg-5">{c.unit}</span>}
                      {sort.key === c.key && (sort.dir === "asc" ? <ArrowUp size={9} /> : <ArrowDown size={9} />)}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => void openStructure(r.id)}
                  className={cx("cursor-pointer border-b border-line-soft", r.id === currentId ? "bg-accent-soft" : "hover:bg-hover")}
                  title="Open this structure"
                  data-row-id={r.id}
                >
                  {cols.map((c) => {
                    const v = r.values[c.key];
                    const bad = (c.key === "dynamically_stable" || c.key === "born_stable" || c.key === "relax_converged") && v === false;
                    return (
                      <td
                        key={c.key}
                        className={cx(
                          "px-2 py-[3px] whitespace-nowrap",
                          c.kind === "number" ? "text-right font-mono tabular-nums" : "text-left",
                          c.key === "name" ? "text-fg" : "text-fg-2",
                          bad && "text-bad",
                          v === true && "text-ok",
                        )}
                      >
                        {c.key === "formula" && typeof v === "string" ? <FormulaText formula={v} /> : formatCell(v, c)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : numeric.length < 2 ? (
        <div className="grid flex-1 place-items-center text-xs text-fg-4">The scatter needs at least two numeric columns.</div>
      ) : (
        <div className="relative min-h-0 flex-1">
          <ScatterChart points={points} xLabel={xCol ? columnTitle(xCol) : ""} yLabel={yCol ? columnTitle(yCol) : ""} currentId={currentId} onPick={(id) => void openStructure(id)} />
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ phase diagram
export function PhasePane() {
  const hull = useCrystalStore((s) => s.hull);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const pd = hull.status === "ok" ? hull.result.phase_diagram : null;
  if (!pd) {
    return (
      <div className="grid h-full w-full place-items-center text-center text-xs text-fg-4">
        <div className="space-y-2">
          <p>{hull.status === "busy" ? "Evaluating the hull…" : hull.status === "ok" ? "This hull result carries no phase-diagram data." : "Evaluate E_hull to draw the phase diagram."}</p>
          {hull.status !== "busy" && (
            <Button className="mx-auto" disabled={!hasCrystal} onClick={() => void evaluateHull()}>
              <Calculator size={12} /> Evaluate E_hull
            </Button>
          )}
          {hull.status === "busy" && <Loader2 size={14} className="mx-auto animate-spin" />}
        </div>
      </div>
    );
  }
  const stale = hull.status === "ok" && hull.stateVersion !== stateVersion;
  return (
    <div className="flex h-full w-full min-w-0 flex-col gap-1" data-phase-pane>
      <div className="flex shrink-0 items-center gap-2 text-2xs text-fg-3">
        <span className="font-mono text-fg-2">{pd.elements.join("–")}</span>
        <span>
          {pd.entries.filter((e) => e.on_hull).length} stable / {pd.entries.length} phases
        </span>
        {stale && <span className="text-warn">structure changed since</span>}
        <span className="ml-auto">
          <PhaseDiagramLegend />
        </span>
      </div>
      <div className="relative min-h-0 flex-1">
        <PhaseDiagramChart pd={pd} />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ dispersion compare
export function PhononCompareControl() {
  const phonon = useCrystalStore((s) => s.phonon);
  const rows = useLibrary((s) => s.structures);
  const overlay = useLibrary((s) => s.phononOverlay);
  const currentId = useCurrentStructureId();
  const current = rows.find((r) => r.id === currentId) ?? null;
  const candidates = rows.filter((r) => r.latest_result_ids.phonons !== undefined && r.id !== currentId);
  const parent = current?.parent_id != null ? candidates.find((r) => r.id === current.parent_id) : undefined;
  const ordered = parent ? [parent, ...candidates.filter((r) => r !== parent)] : candidates;
  const [choice, setChoice] = useState<number | null>(null);
  const picked = ordered.find((r) => r.id === choice) ?? ordered[0] ?? null;
  if (!phonon) return null;
  const on = !!overlay;
  const mismatch = overlay?.status === "ok" && overlay.data ? kpathMismatch(phonon, overlay.data) : null;
  const reason = !ordered.length ? "No other structure of this project has stored phonons" : null;
  const label = (r: (typeof ordered)[number]) => `${r.name}${r === parent ? " (parent)" : ` · ${r.origin}`}`;
  return (
    <span className="flex min-w-0 items-center justify-end gap-1.5 text-2xs" data-phonon-compare>
      {on && overlay?.status === "loading" && <Loader2 size={10} className="animate-spin text-fg-4" />}
      {on && (mismatch || overlay?.status === "error") && (
        <span className="truncate text-warn" title={mismatch ?? overlay?.error}>
          not drawn: {mismatch ?? overlay?.error}
        </span>
      )}
      {ordered.length > 0 && (
        <select
          className="max-w-[140px] min-w-0 rounded-ctl border border-line bg-field px-1 py-px text-2xs text-fg-2 outline-none focus:border-accent"
          value={picked?.id ?? ""}
          onChange={(e) => {
            const id = Number(e.target.value);
            setChoice(id);
            const r = ordered.find((x) => x.id === id);
            if (on && r) void loadPhononOverlay(r.id, label(r));
          }}
          aria-label="Compare with"
        >
          {ordered.map((r) => (
            <option key={r.id} value={r.id}>
              {label(r)}
            </option>
          ))}
        </select>
      )}
      <button
        type="button"
        disabled={!!reason}
        title={reason ?? (on ? "Hide the comparison" : "Draw the stored dispersion of the chosen structure (dashed); needs the same k-path")}
        onClick={() => {
          if (on) useLibrary.getState().setPhononOverlay(null);
          else if (picked) void loadPhononOverlay(picked.id, label(picked));
        }}
        className={cx("flex shrink-0 items-center gap-1 rounded-ctl px-1.5 py-px disabled:opacity-35", on ? "bg-accent-soft text-accent-fg" : "text-fg-3 hover:bg-hover hover:text-fg")}
      >
        <GitCompare size={11} /> compare
      </button>
    </span>
  );
}
