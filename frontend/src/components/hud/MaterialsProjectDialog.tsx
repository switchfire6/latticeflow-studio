/**
 * Materials Project import (§4.3): search by formula ("SrTiO3"), chemical
 * system ("Sr-Ti-O") or mp-id ("mp-5229"); the result table lists id, formula,
 * space group, sites, MP's E_hull and stable / theoretical badges; Load fetches
 * the structure and sends LOAD_STRUCTURE {cif}. Missing MP_API_KEY (503),
 * no match (404) and an unreachable MP (502) get their own messages.
 */
import { Database, Loader2, Search, X } from "lucide-react";
import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../../services/apiClient";
import { classifyMpQuery, mpErrorView, type MpErrorView } from "../../services/mpActions";
import { commands } from "../../services/wsClient";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { MPEntry } from "../../types/protocol";
import { studioLimits } from "../../utils/limits";
import { Badge, Button, FormulaText, Panel, cx, inputCls } from "./ui";

const KIND_HINT = { "mp-id": "material id", chemsys: "chemical system", formula: "formula", invalid: "formula, Sr-Ti-O or mp-…" } as const;

export function MaterialsProjectDialog({ onClose }: { onClose(): void }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<MPEntry[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState<string | null>(null);
  const [err, setErr] = useState<MpErrorView | null>(null);
  const connected = useCrystalStore((s) => s.connection === "open");
  const { maxAtoms, phononMaxAtoms } = studioLimits(useCrystalStore((s) => s.health));
  const ctl = useRef<AbortController | null>(null);
  const kind = classifyMpQuery(q);

  const search = async () => {
    if (!q.trim()) return;
    ctl.current?.abort();
    const c = (ctl.current = new AbortController());
    setBusy(true);
    setErr(null);
    try {
      const r = await api.mpSearch(q, c.signal);
      const sorted = [...r.results].sort((a, b) => (a.energy_above_hull ?? Infinity) - (b.energy_above_hull ?? Infinity));
      setResults(sorted);
      if (!sorted.length) setErr({ title: "Nothing found", message: `No Materials Project entry matches “${q.trim()}”.`, tone: "slate" });
    } catch (e) {
      if ((e as { name?: string }).name === "AbortError") return;
      setResults(null);
      setErr(mpErrorView(e));
    } finally {
      if (ctl.current === c) setBusy(false);
    }
  };

  const load = async (id: string) => {
    setLoading(id);
    setErr(null);
    try {
      const r = await api.mpStructure(id);
      if (commands.loadCif(r.cif)) {
        useCrystalStore.getState().pushToast("info", "Loaded from Materials Project", `${r.entry.material_id} · ${r.entry.formula} · ${r.entry.spacegroup_symbol}`);
        onClose();
      }
    } catch (e) {
      setErr(mpErrorView(e));
    } finally {
      setLoading(null);
    }
  };

  return createPortal(
    <div className="fixed inset-0 z-[100] grid place-items-center bg-black/50" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <Panel floating className="flex max-h-[min(640px,calc(100vh-48px))] w-[640px] max-w-[calc(100vw-32px)] flex-col p-4">
        <div className="mb-2 flex items-center justify-between">
          <span className="flex items-center gap-1.5 text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase">
            <Database size={12} className="text-fg-3" /> Load from Materials Project
          </span>
          <button type="button" onClick={onClose} className="text-fg-4 hover:text-fg" aria-label="close">
            <X size={14} />
          </button>
        </div>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void search();
          }}
        >
          <input
            autoFocus
            className={cx(inputCls, "flex-1")}
            value={q}
            spellCheck={false}
            placeholder="SrTiO3 · Sr-Ti-O · mp-5229"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && onClose()}
          />
          <Button type="submit" variant="primary" disabled={!q.trim() || busy}>
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Search size={12} />} Search
          </Button>
        </form>
        <div className="mt-1 text-2xs text-fg-4">{q.trim() ? `searching by ${KIND_HINT[kind]}` : "polymorphs are listed by MP's E_hull (GGA/GGA+U), lowest first"}</div>
        {err && (
          <div
            className={cx(
              "mt-2 rounded-ctl border px-2 py-1.5 text-xs leading-snug",
              err.tone === "amber" ? "border-warn/40 bg-warn/10 text-warn" : err.tone === "red" ? "border-bad/40 bg-bad/10 text-bad" : "border-line bg-raised text-fg-2",
            )}
            data-mp-error
          >
            <span className="font-semibold">{err.title}.</span> {err.message}
          </div>
        )}
        {results && results.length > 0 && (
          <div className="mt-2 min-h-0 flex-1 overflow-y-auto rounded-ctl border border-line-soft">
            <table className="w-full font-mono text-xs tabular-nums" data-mp-results>
              <thead className="sticky top-0 bg-surface text-2xs text-fg-4">
                <tr>
                  <th className="px-2 py-1 text-left font-normal">id</th>
                  <th className="px-2 text-left font-normal">formula</th>
                  <th className="px-2 text-left font-normal">space group</th>
                  <th className="px-2 text-right font-normal">sites</th>
                  <th className="px-2 text-right font-normal" title="Energy above the MP convex hull (eV/atom)">E_hull</th>
                  <th className="px-2 font-normal" />
                  <th className="px-2 font-normal" />
                </tr>
              </thead>
              <tbody>
                {results.map((r) => (
                  <tr key={r.material_id} className="border-t border-line-soft text-fg hover:bg-hover">
                    <td className="px-2 py-1 text-fg">{r.material_id}</td>
                    <td className="px-2">
                      <FormulaText formula={r.formula} />
                    </td>
                    <td className="px-2">
                      {r.spacegroup_symbol} <span className="text-fg-4">#{r.spacegroup_number}</span>
                    </td>
                    <td className="px-2 text-right">{r.nsites}</td>
                    <td className="px-2 text-right">{r.energy_above_hull != null ? r.energy_above_hull.toFixed(3) : "—"}</td>
                    <td className="px-2">
                      <span className="flex gap-1">
                        {r.is_stable && <Badge tone="green">stable</Badge>}
                        {r.theoretical === true && <Badge tone="slate">theoretical</Badge>}
                        {r.theoretical === false && <Badge tone="cyan">ICSD</Badge>}
                      </span>
                    </td>
                    <td className="px-2 text-right">
                      <Button className="px-2 py-0.5 text-xs" disabled={!connected || loading !== null} onClick={() => void load(r.material_id)} title={r.nsites > maxAtoms ? `More than ${maxAtoms} sites: exceeds the studio atom limit` : r.nsites > phononMaxAtoms ? `More than ${phononMaxAtoms} sites: phonons will not run on this cell` : undefined}>
                        {loading === r.material_id ? <Loader2 size={11} className="animate-spin" /> : null} Load
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>,
    document.body,
  );
}

export function MaterialsProjectButton() {
  const [open, setOpen] = useState(false);
  const connected = useCrystalStore((s) => s.connection === "open");
  return (
    <>
      <Button className="w-full" disabled={!connected} onClick={() => setOpen(true)} title="Search Materials Project by formula, chemical system or mp-id">
        <Database size={13} /> Materials Project…
      </Button>
      {open && <MaterialsProjectDialog onClose={() => setOpen(false)} />}
    </>
  );
}
