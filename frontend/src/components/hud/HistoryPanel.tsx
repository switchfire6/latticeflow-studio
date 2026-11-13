/**
 * History card (Phase C1): every HISTORY_STATE entry (label, formula, atoms,
 * space group, energy) with the current one highlighted. Clicking an entry
 * walks there with repeated UNDO / REDO (no GOTO command exists); "bulk"
 * marks an entry as the bulk reference of the formation-energy workflow.
 */
import { History, Pin } from "lucide-react";
import { commands } from "../../services/wsClient";
import { useCrystalStore } from "../../store/useCrystalStore";
import { useUi } from "../../store/uiStore";
import { resolveHistoryRef } from "../../utils/analysisC";
import { CollapsibleCard, FormulaText, cx } from "./ui";

export function HistoryPanel() {
  const history = useCrystalStore((s) => s.history);
  const connected = useCrystalStore((s) => s.connection === "open");
  const busy = useCrystalStore((s) => s.streams.length > 0);
  const bulkRef = useUi((s) => s.bulkRef);
  const entries = history?.entries ?? [];
  const bulk = resolveHistoryRef(entries, bulkRef);
  return (
    <CollapsibleCard id="history" title={`History${history ? ` · ${history.position + 1}/${history.length}` : ""}`} right={<History size={12} className="text-cyan-300/70" />} defaultOpen={false}>
      {entries.length === 0 ? (
        <p className="text-[11px] text-slate-500">{history ? `Current: ${history.label}` : "No history yet."}</p>
      ) : (
        <ol className="max-h-[220px] space-y-0.5 overflow-y-auto pr-0.5" data-history>
          {[...entries].reverse().map((e) => {
            const current = e.position === history!.position;
            const isBulk = bulk?.position === e.position;
            return (
              <li key={e.position} className={cx("group flex items-center gap-1 rounded-md border px-1.5 py-1", current ? "border-cyan-400/40 bg-cyan-400/10" : "border-transparent hover:border-white/10 hover:bg-white/[0.03]", e.position > history!.position && "opacity-60")}>
                <button
                  type="button"
                  className="min-w-0 flex-1 text-left disabled:cursor-default"
                  disabled={current || !connected || busy}
                  title={current ? "Current entry" : busy ? "Wait for the running calculation" : `Go to this entry (${e.position < history!.position ? "undo" : "redo"} ×${Math.abs(e.position - history!.position)})`}
                  onClick={() => commands.gotoHistory(e.position)}
                  data-history-entry={e.position}
                >
                  <div className="truncate text-[11px] text-slate-200">
                    <span className="mr-1 font-mono text-[9.5px] text-slate-500">{e.position + 1}</span>
                    {e.label}
                  </div>
                  <div className="truncate font-mono text-[10px] text-slate-400">
                    <FormulaText formula={e.formula} /> · {e.n_atoms} at · {e.spacegroup_symbol} · {e.energy_per_atom.toFixed(3)} eV/at
                  </div>
                </button>
                <button
                  type="button"
                  title={isBulk ? "Bulk reference for the formation energy" : "Use as the bulk reference for defect formation energies"}
                  className={cx("shrink-0 rounded p-0.5", isBulk ? "text-amber-300" : "text-slate-600 opacity-0 group-hover:opacity-100 hover:text-amber-200")}
                  onClick={() => {
                    useUi.getState().setBulkRef({ position: e.position, label: e.label, formula: e.formula, n_atoms: e.n_atoms, energy_per_atom: e.energy_per_atom });
                    useUi.getState().openDialog("formation");
                  }}
                  aria-label="use as bulk reference"
                >
                  <Pin size={11} />
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </CollapsibleCard>
  );
}
