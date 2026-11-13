/**
 * Floating contextual toolbar (§6.3), shown when an atom is selected:
 * transmute quick-buttons (group neighbours, then Z ± 1) plus a full periodic
 * table, "all equivalent sites", vacancy creation, FIRE relaxation (Stop while
 * running) and the phonon shake-test (warns when the structure is unrelaxed).
 */
import { Activity, Grid3x3, Trash2, X } from "lucide-react";
import { useState } from "react";
import { commands } from "../../services/wsClient";
import { useCrystalStore } from "../../store/useCrystalStore";
import { useUi } from "../../store/uiStore";
import { DEFECT_MIN_DISTANCE, shortestPeriodicDistance } from "../../utils/supercell";
import { cpkHex, elementBySymbol, elementByZ } from "../../utils/cpkColors";
import { groupNeighbours } from "../../utils/periodicTable";
import { PeriodicTablePicker } from "./PeriodicTablePicker";
import { StructureActions } from "./StructureActions";
import { Button, Panel, cx } from "./ui";

export { StructureActions };

function ElementChip({ symbol, onClick, current }: { symbol: string; onClick(): void; current?: boolean }) {
  const z = elementBySymbol(symbol)?.z ?? 0;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={current}
      className={cx(
        "flex h-8 min-w-9 items-center justify-center gap-1 rounded-ctl border px-1.5 text-sm transition-colors",
        current ? "border-line-strong bg-hover text-fg" : "border-line bg-raised text-fg hover:border-line-strong hover:bg-hover",
      )}
      title={current ? "current element" : `Change to ${symbol}`}
    >
      <span className="h-2 w-2 rounded-full" style={{ background: cpkHex(z) }} />
      {symbol}
    </button>
  );
}

export function TransmuteToolbar() {
  const selected = useCrystalStore((s) => s.selectedAtom);
  const atom = useCrystalStore((s) => (s.selectedAtom !== null ? (s.crystal?.atoms[s.selectedAtom] ?? null) : null));
  const connected = useCrystalStore((s) => s.connection === "open");
  const [allEquivalent, setAllEquivalent] = useState(false);
  const [tableOpen, setTableOpen] = useState(false);
  const [vacancyWarn, setVacancyWarn] = useState(false);
  const matrix = useCrystalStore((s) => s.crystal?.lattice.matrix ?? null);
  const sameElement = useCrystalStore((s) => (atom ? (s.crystal?.atoms.filter((a) => a.element === atom.element).length ?? 0) : 0));
  if (selected === null || !atom) return null;
  const dmin = matrix ? shortestPeriodicDistance(matrix) : Infinity;
  const createVacancy = () => {
    if (dmin < DEFECT_MIN_DISTANCE && !vacancyWarn) {
      setVacancyWarn(true);
      return;
    }
    setVacancyWarn(false);
    commands.deleteAtom(selected);
  };

  const el = elementByZ(atom.atomic_number);
  const { column, row } = groupNeighbours(el.symbol);
  const transmute = (sym: string) => {
    if (sym === el.symbol) return;
    commands.transmute(selected, sym, allEquivalent);
  };

  return (
    <Panel floating className="pointer-events-auto absolute top-3 right-3 z-20 w-[300px] p-3">
      <div className="mb-2 flex items-start justify-between">
        <div>
          <div className="text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase">Selected atom</div>
          <div className="mt-0.5 flex items-center gap-2 font-mono text-sm text-fg">
            <span className="h-3 w-3 rounded-full ring-1 ring-white/30" style={{ background: cpkHex(atom.atomic_number) }} />#{selected} {el.symbol}
            <span className="text-xs text-fg-3">|F| = {atom.force_norm.toFixed(3)} eV/Å</span>
          </div>
          <div className="mt-0.5 font-mono text-xs text-fg-4">
            frac ({atom.frac_coords.map((x) => x.toFixed(3)).join(", ")}){atom.wyckoff ? ` · Wyckoff ${atom.wyckoff}` : ""}
          </div>
        </div>
        <button type="button" className="rounded-ctl p-1 text-fg-4 hover:bg-hover hover:text-fg" onClick={() => useCrystalStore.getState().selectAtom(null)}>
          <X size={14} />
        </button>
      </div>

      <div className="mb-1 flex items-center justify-between text-xs text-fg-3">
        <span>Change element</span>
        <span className="relative">
          <button
            type="button"
            className={cx("flex items-center gap-1 rounded-ctl px-1.5 py-0.5 hover:bg-hover hover:text-fg", tableOpen && "text-fg-3")}
            onClick={() => setTableOpen((o) => !o)}
          >
            <Grid3x3 size={12} /> all elements
          </button>
          {tableOpen && (
            <PeriodicTablePicker
              className="absolute top-full right-0 mt-1"
              current={el.symbol}
              onPick={(s) => {
                setTableOpen(false);
                transmute(s);
              }}
              onClose={() => setTableOpen(false)}
            />
          )}
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <ElementChip symbol={el.symbol} current onClick={() => {}} />
        {column.map((s) => (
          <ElementChip key={s} symbol={s} onClick={() => transmute(s)} />
        ))}
        {row.length > 0 && <span className="mx-0.5 w-px self-stretch bg-hover" />}
        {row.map((s) => (
          <ElementChip key={s} symbol={s} onClick={() => transmute(s)} />
        ))}
      </div>
      <label className="mt-2 flex cursor-pointer items-center gap-2 text-xs text-fg-2">
        <input type="checkbox" className="accent-accent" checked={allEquivalent} onChange={(e) => setAllEquivalent(e.target.checked)} />
        Apply to all symmetry-equivalent sites
      </label>

      <div className="mt-3 grid grid-cols-1 gap-2 border-t border-line-soft pt-3">
        <Button variant="ghost" disabled={!connected} onClick={createVacancy}>
          <Trash2 size={13} /> Create Vacancy Defect
        </Button>
        {vacancyWarn && (
          <div className="rounded-ctl border border-warn/35 bg-warn/[0.07] p-2 text-xs leading-snug text-warn" data-vacancy-warning>
            <div className="mb-1.5">
              Shortest periodic distance {dmin.toFixed(1)} Å (&lt; {DEFECT_MIN_DISTANCE} Å): the vacancy interacts with its images — this is {(100 / Math.max(1, sameElement)).toFixed(0)} % of the {el.symbol} sites, not a point defect.
            </div>
            <div className="flex gap-1">
              <button
                type="button"
                className="rounded-ctl border border-line-strong bg-raised px-1.5 py-0.5 text-fg hover:bg-hover"
                onClick={() => {
                  setVacancyWarn(false);
                  useUi.getState().openDialog("supercell");
                }}
              >
                Make supercell first
              </button>
              <button type="button" className="rounded-ctl border border-line px-1.5 py-0.5 text-fg-2 hover:bg-hover" onClick={createVacancy}>
                Create anyway
              </button>
              <button type="button" className="ml-auto px-1 text-fg-4 hover:text-fg" onClick={() => setVacancyWarn(false)} aria-label="cancel">
                <X size={11} />
              </button>
            </div>
          </div>
        )}
        <StructureActions />
      </div>
      <div className="mt-2 flex items-center gap-1 text-2xs text-fg-4">
        <Activity size={10} /> Drag atoms in Drag mode; forces update live.
      </div>
    </Panel>
  );
}
