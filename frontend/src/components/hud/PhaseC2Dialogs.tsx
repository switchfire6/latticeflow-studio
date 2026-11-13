/**
 * Phase C2 (§7.2, §7.5) dialogs and panels:
 *   compare     two structures side by side (POST /api/compare): space groups,
 *               group–subgroup relation, StructureMatcher match + rms, ΔV, ΔE per
 *               atom / formula unit, a b c α β γ of both, and an overlay of their
 *               phonon DOS or lowest branch when both have phonons cached.
 *               Structures come from the snapshot cache (services/snapshotCache.ts):
 *               the current structure, the Follow-Instability parent and every
 *               history entry (uncached ones fetched with REQUEST_HISTORY_ENTRY).
 *   prototype   PROTOTYPE_SEARCH (ionic substitution into MP prototypes) with
 *               PROGRESS and the ranked PROTOTYPE_CANDIDATES; Load = LOAD_STRUCTURE {crystal}.
 * Plus the Structure card's "Match to Materials Project" (POST /api/mp/match)
 * and the generator's ranked GENERATION_CANDIDATES list.
 */
import { SERIES_COLORS } from "../../utils/chartData";
import { Database, Loader2, Search, Square } from "lucide-react";
import { useEffect, useState } from "react";
import { ApiError, api } from "../../services/apiClient";
import { compareOptions, fetchEntry, phononOf } from "../../services/compareActions";
import { MP_KEY_HINT, mpErrorView, type MpErrorView } from "../../services/mpActions";
import { useSnapshotVersion, type PhononSnapshot, type Snapshot } from "../../services/snapshotCache";
import { commands } from "../../services/wsClient";
import { useCrystalStore } from "../../store/useCrystalStore";
import { useUi } from "../../store/uiStore";
import type { CompareResponse, CrystalStatePayload, MPMatchResponse } from "../../types/protocol";
import { searchSummaryText, substitutionText } from "../../utils/analysisC2";
import { parseFormula } from "../../utils/formula";
import { sameStructure } from "../../utils/structureInfo";
import { LineChart, type LineSeries } from "../charts/LineChart";
import { ProgressBar } from "./ElasticPane";
import { Modal, Warn } from "./PhaseCDialogs";
import { Button, FormulaText, Segmented, Toggle, cx, inputCls } from "./ui";

const close = () => useUi.getState().closeDialog();
const [COLOR_A, COLOR_B] = SERIES_COLORS;
const fx = (v: number | null | undefined, d = 3) => (v == null || !Number.isFinite(v) ? "—" : v.toFixed(d));
const signed = (v: number | null | undefined, d = 1) => (v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v).toFixed(d)}`);

// --------------------------------------------------------------------------- compare
type CompareState = { status: "idle" | "busy" | "ok" | "error"; result: CompareResponse | null; error: string | null };

function SidePicker({ label, color, value, onChange }: { label: string; color: string; value: Snapshot | null; onChange(s: Snapshot | null): void }) {
  useSnapshotVersion((s) => s.v);
  useCrystalStore((s) => s.history);
  const opts = compareOptions();
  // An uncached history entry picked by the user: fetched, then selected once it lands in the cache.
  const [pending, setPending] = useState<string | null>(null);
  const pendingOpt = pending ? opts.find((o) => o.id === pending) : undefined;
  useEffect(() => {
    if (pending && pendingOpt?.snapshot) {
      setPending(null);
      onChange(pendingOpt.snapshot);
    } else if (pending && !pendingOpt) setPending(null); // the entry left the history
  }, [pending, pendingOpt, onChange]);
  useEffect(() => {
    if (!pending) return;
    // No reply (the fetch failed and an error toast was shown): stop showing "loading".
    const t = setTimeout(() => setPending(null), 8000);
    return () => clearTimeout(t);
  }, [pending]);
  const pick = (id: string) => {
    if (id === "__kept") return onChange(value);
    const o = opts.find((x) => x.id === id);
    if (o?.snapshot || !o?.entry) {
      setPending(null);
      return onChange(o?.snapshot ?? null);
    }
    if (fetchEntry(o.entry.position)) setPending(o.id);
  };
  const selectedId = pending ?? (value ? (opts.find((o) => o.snapshot && o.snapshot.key === value.key && sameStructure(o.snapshot.crystal, value.crystal))?.id ?? "__kept") : "");
  return (
    <label className="block min-w-0 flex-1">
      <div className="mb-0.5 flex items-center gap-1 text-2xs text-fg-4">
        <span className="h-2 w-2 rounded-full" style={{ background: color }} /> {label}
      </div>
      <select
        className={inputCls}
        value={selectedId}
        onChange={(e) => pick(e.target.value)}
        data-compare-side={label}
      >
        <option value="">choose a structure…</option>
        {value && selectedId === "__kept" && <option value="__kept">{value.label}</option>}
        {opts.map((o) => (
          <option key={o.id} value={o.id}>
            {o.label}
            {o.id === pending ? " (loading…)" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

function latticeRows(a: CrystalStatePayload, b: CrystalStatePayload) {
  const la = a.lattice;
  const lb = b.lattice;
  return (
    [
      ["a", la.a, lb.a, "Å"],
      ["b", la.b, lb.b, "Å"],
      ["c", la.c, lb.c, "Å"],
      ["α", la.alpha, lb.alpha, "°"],
      ["β", la.beta, lb.beta, "°"],
      ["γ", la.gamma, lb.gamma, "°"],
    ] as const
  ).map(([k, va, vb, unit]) => ({ k, va, vb, unit, delta: unit === "Å" ? ((vb - va) / va) * 100 : vb - va }));
}

function PhononOverlay({ a, b }: { a: PhononSnapshot; b: PhononSnapshot }) {
  const bothDos = !!a.dos && !!b.dos;
  const [mode, setMode] = useState<"dos" | "branch">(bothDos ? "dos" : "branch");
  const m = bothDos ? mode : "branch";
  const nA = useUi((s) => s.compare.a?.crystal.atoms.length ?? 1);
  const nB = useUi((s) => s.compare.b?.crystal.atoms.length ?? 1);
  let series: LineSeries[];
  if (m === "dos") {
    series = [
      { label: "A", xs: a.dos!.frequencies, ys: a.dos!.total.map((v) => v / nA), color: COLOR_A, width: 1.4 },
      { label: "B", xs: b.dos!.frequencies, ys: b.dos!.total.map((v) => v / nB), color: COLOR_B, width: 1.4 },
    ];
  } else {
    const norm = (p: PhononSnapshot) => {
      const max = p.kpathDistances.at(-1) || 1;
      return p.kpathDistances.map((d) => d / max);
    };
    series = [
      { label: "A", xs: norm(a), ys: a.lowestBranch, color: COLOR_A, width: 1.4 },
      { label: "B", xs: norm(b), ys: b.lowestBranch, color: COLOR_B, width: 1.4 },
    ];
  }
  return (
    <div className="rounded-ctl border border-line-soft p-1.5" data-compare-phonons>
      <div className="mb-1 flex items-center gap-2 text-2xs text-fg-4">
        <span>phonons</span>
        <Segmented<"dos" | "branch">
          value={m}
          options={[
            { value: "dos", label: "DOS / atom", disabled: !bothDos },
            { value: "branch", label: "lowest branch" },
          ]}
          onChange={setMode}
        />
        <span className="ml-auto font-mono">
          min f: <span style={{ color: COLOR_A }}>{a.minFrequencyThz.toFixed(2)}</span> / <span style={{ color: COLOR_B }}>{b.minFrequencyThz.toFixed(2)}</span> THz
        </span>
      </div>
      <div className="relative h-36">
        <LineChart
          series={series}
          xLabel={m === "dos" ? "f (THz)" : "path position (normalised)"}
          yLabel={m === "dos" ? "DOS (1/THz/atom)" : "f_min (THz)"}
          zeroLine={m === "branch"}
          shadeBelowX={m === "dos" ? 0 : undefined}
        />
      </div>
      {m === "branch" && <p className="mt-0.5 text-2xs text-fg-4">Each structure's own high-symmetry path, scaled to 0–1 (paths of different cells do not align point by point).</p>}
    </div>
  );
}

export function CompareDialog() {
  const sel = useUi((s) => s.compare);
  useSnapshotVersion((s) => s.v); // phonons may arrive later
  const [state, setState] = useState<CompareState>({ status: "idle", result: null, error: null });
  const { a, b } = sel;
  const key = a && b ? `${a.key}:${a.crystal.energy_per_atom}:${a.crystal.atoms.length}|${b.key}:${b.crystal.energy_per_atom}:${b.crystal.atoms.length}` : "";
  useEffect(() => {
    if (!a || !b) {
      setState({ status: "idle", result: null, error: null });
      return;
    }
    const ctl = new AbortController();
    setState({ status: "busy", result: null, error: null });
    api
      .compare({ crystal_a: a.crystal, crystal_b: b.crystal }, ctl.signal)
      .then((result) => setState({ status: "ok", result, error: null }))
      .catch((e: unknown) => {
        if (ctl.signal.aborted) return;
        setState({ status: "error", result: null, error: e instanceof ApiError ? `${e.code}: ${e.message}` : e instanceof Error ? e.message : String(e) });
      });
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const r = state.result;
  const pa = phononOf(a);
  const pb = phononOf(b);
  return (
    <Modal title="Compare structures" width={600} onClose={close}>
      <div className="space-y-2.5 text-xs text-fg-2" data-compare>
        <div className="flex gap-2">
          <SidePicker label="A" color={COLOR_A} value={a} onChange={(s) => useUi.getState().setCompare({ a: s })} />
          <SidePicker label="B" color={COLOR_B} value={b} onChange={(s) => useUi.getState().setCompare({ b: s })} />
        </div>
        <p className="text-2xs leading-snug text-fg-4">
          Every history entry can be picked (structures not seen yet are fetched from the server). ΔE assumes both energies come from the same model.
        </p>
        {state.status === "busy" && <ProgressBar message="comparing (StructureMatcher, symmetry)…" />}
        {state.status === "error" && <p className="text-xs text-bad">{state.error}</p>}
        {a && b && (
          <table className="w-full font-mono text-xs tabular-nums" data-compare-table>
            <thead className="text-fg-4">
              <tr>
                <th className="text-left font-normal" />
                <th className="text-right font-normal" style={{ color: COLOR_A }}>
                  A
                </th>
                <th className="text-right font-normal" style={{ color: COLOR_B }}>
                  B
                </th>
                <th className="text-right font-normal">B − A</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-t border-line-soft">
                <td className="text-fg-4">formula · atoms</td>
                <td className="text-right text-fg">
                  <FormulaText formula={a.crystal.formula} /> · {a.crystal.atoms.length}
                </td>
                <td className="text-right text-fg">
                  <FormulaText formula={b.crystal.formula} /> · {b.crystal.atoms.length}
                </td>
                <td className="text-right text-fg-3">{r ? (r.same_composition ? "same composition" : "different composition") : ""}</td>
              </tr>
              <tr className="border-t border-line-soft">
                <td className="text-fg-4">space group</td>
                <td className="text-right text-fg">{r?.spacegroup_a ?? `${a.crystal.spacegroup_symbol} (#${a.crystal.spacegroup_number})`}</td>
                <td className="text-right text-fg">{r?.spacegroup_b ?? `${b.crystal.spacegroup_symbol} (#${b.crystal.spacegroup_number})`}</td>
                <td className="text-right text-fg" data-group-subgroup>
                  {r?.group_subgroup ?? "—"}
                </td>
              </tr>
              <tr className="border-t border-line-soft">
                <td className="text-fg-4">structure match</td>
                <td colSpan={2} className="text-right">
                  {r ? <span className={r.structures_match ? "text-ok" : "text-warn"}>{r.structures_match ? "same structure" : "different structures"}</span> : "—"}
                </td>
                <td className="text-right text-fg-2">{r?.rms_displacement != null ? `rms ${r.rms_displacement.toFixed(3)}${r.max_displacement != null ? ` · max ${r.max_displacement.toFixed(3)}` : ""}` : "—"}</td>
              </tr>
              <tr className="border-t border-line-soft">
                <td className="text-fg-4">V / atom (Å³)</td>
                <td className="text-right">{fx(r?.volume_per_atom_a ?? a.crystal.lattice.volume / a.crystal.atoms.length, 3)}</td>
                <td className="text-right">{fx(r?.volume_per_atom_b ?? b.crystal.lattice.volume / b.crystal.atoms.length, 3)}</td>
                <td className="text-right text-fg">{r ? `${signed(r.volume_change_percent, 2)} %` : "—"}</td>
              </tr>
              <tr className="border-t border-line-soft">
                <td className="text-fg-4">E / atom (eV)</td>
                <td className="text-right">{fx(r?.energy_per_atom_a ?? a.crystal.energy_per_atom, 4)}</td>
                <td className="text-right">{fx(r?.energy_per_atom_b ?? b.crystal.energy_per_atom, 4)}</td>
                <td className={cx("text-right", r && r.energy_difference_mev_per_atom < 0 ? "text-ok" : "text-fg")} data-delta-e>
                  {r ? `${signed(r.energy_difference_mev_per_atom, 1)} meV/atom` : "—"}
                </td>
              </tr>
              <tr className="border-t border-line-soft">
                <td className="text-fg-4">ΔE per formula unit</td>
                <td colSpan={2} />
                <td className="text-right text-fg">{r?.energy_difference_mev_per_formula_unit != null ? `${signed(r.energy_difference_mev_per_formula_unit, 1)} meV/f.u.` : r ? "n/a (compositions differ)" : "—"}</td>
              </tr>
              {latticeRows(a.crystal, b.crystal).map((row) => (
                <tr key={row.k} className="border-t border-line-soft">
                  <td className="text-fg-4">
                    {row.k} ({row.unit})
                  </td>
                  <td className="text-right">{row.va.toFixed(row.unit === "Å" ? 4 : 2)}</td>
                  <td className="text-right">{row.vb.toFixed(row.unit === "Å" ? 4 : 2)}</td>
                  <td className="text-right text-fg-2">{row.unit === "Å" ? `${signed(row.delta, 2)} %` : `${signed(row.delta, 2)}°`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {r && <div className="text-2xs text-fg-4">{r.method}</div>}
        {a && b && pa && pb ? (
          <PhononOverlay a={pa} b={pb} />
        ) : a && b ? (
          <p className="text-2xs text-fg-4">Phonon overlay: run phonons on both structures (while each is active) to compare their DOS / lowest branch.</p>
        ) : null}
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- MP match (Structure card)
type MatchState = { status: "idle" | "busy" | "ok" | "error"; result: MPMatchResponse | null; error: MpErrorView | null; stateVersion: number };

export function MpMatchSection() {
  const crystal = useCrystalStore((s) => s.crystal);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const [st, setSt] = useState<MatchState>({ status: "idle", result: null, error: null, stateVersion: -1 });
  if (!crystal) return null;
  const stale = st.status === "ok" && st.stateVersion !== stateVersion;
  const run = async () => {
    const v = stateVersion;
    setSt({ status: "busy", result: null, error: null, stateVersion: v });
    try {
      const result = await api.mpMatch({ crystal });
      setSt({ status: "ok", result, error: null, stateVersion: v });
    } catch (e) {
      setSt({ status: "error", result: null, error: mpErrorView(e), stateVersion: v });
    }
  };
  const r = st.result;
  return (
    <div className="mt-2 border-t border-line-soft pt-2" data-mp-match>
      <Button className="w-full px-2 py-1 text-xs" disabled={st.status === "busy"} onClick={() => void run()} title="Which Materials Project entries of this formula match the structure (pymatgen StructureMatcher)?">
        {st.status === "busy" ? <Loader2 size={11} className="animate-spin" /> : <Database size={11} />} Match to Materials Project
      </Button>
      {st.status === "error" && st.error && (
        <p className={cx("mt-1 text-xs leading-snug", st.error.tone === "amber" ? "text-warn" : st.error.tone === "slate" ? "text-fg-3" : "text-bad")}>
          <span className="font-semibold">{st.error.title}.</span> {st.error.message}
        </p>
      )}
      {r && (
        <div className={cx("mt-1", stale && "opacity-50")}>
          <div className="font-mono text-2xs text-fg-4">
            <FormulaText formula={r.formula} /> · {r.n_searched} MP entries searched{stale ? " · stale (structure changed)" : ""}
          </div>
          {r.matches.length === 0 ? (
            <p className="text-xs text-fg-3">No match: this structure is not among the known MP phases of this formula.</p>
          ) : (
            <table className="mt-0.5 w-full font-mono text-xs tabular-nums">
              <thead className="text-fg-4">
                <tr>
                  <th className="text-left font-normal">mp-id</th>
                  <th className="text-left font-normal">SG</th>
                  <th className="text-right font-normal" title="MP's own (DFT) energy above hull">E_hull MP</th>
                  <th className="text-right font-normal">rms</th>
                </tr>
              </thead>
              <tbody>
                {r.matches.map((m) => (
                  <tr key={m.entry.material_id} className="border-t border-line-soft">
                    <td>
                      <a className="text-fg underline decoration-fg-5 underline-offset-2 hover:decoration-fg-3" href={`https://next-gen.materialsproject.org/materials/${m.entry.material_id}`} target="_blank" rel="noreferrer">
                        {m.entry.material_id}
                      </a>
                    </td>
                    <td className="text-fg">{m.entry.spacegroup_symbol}</td>
                    <td className="text-right text-fg">{m.entry.energy_above_hull != null ? `${(m.entry.energy_above_hull * 1000).toFixed(0)} meV` : "—"}</td>
                    <td className="text-right text-fg-3">{m.rms_displacement.toFixed(3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- generator candidates
export function GenerationCandidatesList() {
  const gc = useCrystalStore((s) => s.generationCandidates);
  const crystal = useCrystalStore((s) => s.crystal);
  const connected = useCrystalStore((s) => s.connection === "open");
  const busy = useCrystalStore((s) => s.streams.length > 0);
  if (!gc || gc.candidates.length === 0) return null;
  return (
    <div className="rounded-ctl border border-line-soft bg-raised p-1.5" data-generation-candidates>
      <div className="mb-1 text-2xs leading-snug text-fg-3" data-search-summary>
        {searchSummaryText(gc.summary, gc.candidates.length)}
      </div>
      <table className="w-full font-mono text-xs tabular-nums">
        <thead className="text-fg-4">
          <tr>
            <th className="text-left font-normal">#</th>
            <th className="text-right font-normal" title="Energy above the best candidate">ΔE meV/at</th>
            <th className="pl-2 text-left font-normal">SG · Wyckoff</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {gc.candidates.map((c) => {
            const active = !!crystal && sameStructure(crystal, c.crystal);
            return (
              <tr key={c.rank} className={cx("border-t border-line-soft", active && "bg-accent-soft")}>
                <td className="text-fg-4">{c.rank}</td>
                <td className={cx("text-right", c.rank === 1 ? "text-ok" : "text-fg")}>{c.relative_energy_mev_per_atom.toFixed(1)}</td>
                <td className="max-w-[150px] pl-2 leading-tight" title={`${c.spacegroup_symbol} (#${c.spacegroup_number}) · ${c.wyckoff_assignment}`}>
                  <div className="text-fg">{c.spacegroup_symbol}</div>
                  <div className="truncate text-2xs text-fg-4">{c.wyckoff_assignment}</div>
                </td>
                <td className="text-right">
                  {active ? (
                    <span className="text-2xs text-fg-3">active</span>
                  ) : (
                    <Button className="px-1.5 py-0 text-2xs" disabled={!connected || busy} onClick={() => commands.loadCrystal(c.crystal)}>
                      Load
                    </Button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------------------- prototype search
export function PrototypeDialog() {
  const connected = useCrystalStore((s) => s.connection === "open");
  const search = useCrystalStore((s) => s.prototypeSearch);
  const running = useCrystalStore((s) => s.streams.find((x) => x.command === "PROTOTYPE_SEARCH") ?? null);
  const busy = useCrystalStore((s) => s.streams.length > 0);
  const progress = useCrystalStore((s) => s.progress.PROTOTYPE_SEARCH ?? null);
  const [formula, setFormula] = useState("NaNbO3");
  const [maxCand, setMaxCand] = useState(8);
  const [relax, setRelax] = useState(true);
  const [eHull, setEHull] = useState(true);
  const parsed = parseFormula(formula);
  const res = search?.result ?? null;
  const err = search?.status === "error" ? search.error : null;
  const noKey = !!err && /MP_API_KEY/i.test(err.message);
  return (
    <Modal title="From known prototypes (ionic substitution)" width={600} onClose={close}>
      <div className="space-y-2.5 text-xs text-fg-2" data-prototype>
        <p className="text-fg-3">
          Known Materials Project structures of other chemistries are re-decorated with the target ions, ranked by the ionic-substitution probability (pymatgen SubstitutionPredictor), then relaxed and scored with the session model.
        </p>
        <div className="flex items-end gap-2">
          <label className="block flex-1">
            <div className="mb-0.5 text-2xs text-fg-4">target formula</div>
            <input className={cx(inputCls, formula.trim() !== "" && !parsed.ok && "border-bad/60")} value={formula} spellCheck={false} onChange={(e) => setFormula(e.target.value)} placeholder="NaNbO3, Li3PS4…" />
          </label>
          <label className="block w-24">
            <div className="mb-0.5 text-2xs text-fg-4">max candidates</div>
            <input className={inputCls} inputMode="numeric" value={maxCand} onChange={(e) => setMaxCand(Math.max(1, Math.min(30, Number.parseInt(e.target.value, 10) || 1)))} />
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Toggle label="relax each" checked={relax} onChange={setRelax} title="Relax positions + cell (fmax 0.05) before scoring" />
          <Toggle label="E_hull" checked={eHull} onChange={setEHull} title="E_hull of each candidate against the uMLIP reference hull" />
          <span className="ml-auto">
            {running ? (
              <Button variant="danger" disabled={running.stopping} onClick={() => commands.stop()}>
                <Square size={11} /> Stop
              </Button>
            ) : (
              <Button
                variant="primary"
                disabled={!connected || busy || !parsed.ok}
                onClick={() => {
                  if (parsed.ok) commands.prototypeSearch({ composition: parsed.composition, max_candidates: maxCand, relax, with_e_hull: eHull });
                }}
              >
                <Search size={12} /> Search prototypes
              </Button>
            )}
          </span>
        </div>
        {!parsed.ok && formula.trim() !== "" && <p className="text-xs text-bad">{parsed.error}</p>}
        {running && <ProgressBar done={progress?.done} total={progress?.total} message={progress?.message ?? "fetching prototypes…"} />}
        {err &&
          (noKey ? (
            <Warn>
              Materials Project not configured: {MP_KEY_HINT} ({err.message})
            </Warn>
          ) : (
            <p className="text-xs text-bad">
              {err.code}: {err.message}
            </p>
          ))}
        {search?.status === "stopped" && <p className="text-xs text-warn">Search stopped.</p>}
        {res && (
          <div data-prototype-result>
            <div className="mb-1 font-mono text-xs text-fg-3">
              <FormulaText formula={res.target_formula} /> · {res.n_sources} prototypes considered · {res.candidates.length} candidates · {res.method}
            </div>
            {res.candidates.length === 0 ? (
              <p className="text-fg-3">No substitution above the probability threshold.</p>
            ) : (
              <table className="w-full font-mono text-xs tabular-nums">
                <thead className="text-fg-4">
                  <tr>
                    <th className="text-left font-normal">#</th>
                    <th className="text-left font-normal">formula</th>
                    <th className="text-left font-normal">from</th>
                    <th className="text-left font-normal">substitutions</th>
                    <th className="text-right font-normal">p</th>
                    <th className="text-left font-normal pl-1.5">SG</th>
                    <th className="text-right font-normal">E_hull</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {res.candidates.map((c) => (
                    <tr key={`${c.rank}-${c.source_material_id}`} className="border-t border-line-soft">
                      <td className="text-fg-4">{c.rank}</td>
                      <td className="text-fg">
                        <FormulaText formula={c.formula} />
                      </td>
                      <td className="text-fg-2">
                        <a className="text-fg underline decoration-fg-5 underline-offset-2 hover:decoration-fg-3" href={`https://next-gen.materialsproject.org/materials/${c.source_material_id}`} target="_blank" rel="noreferrer">
                          {c.source_material_id}
                        </a>{" "}
                        <FormulaText formula={c.source_formula} />
                      </td>
                      <td className="text-fg-2">{substitutionText(c.substitutions)}</td>
                      <td className="text-right text-fg-2">{c.probability < 0.01 ? c.probability.toExponential(1) : c.probability.toFixed(3)}</td>
                      <td className="pl-1.5 text-fg">{c.spacegroup_symbol}</td>
                      <td className={cx("text-right", c.e_hull_ev != null && c.e_hull_ev * 1000 < 25 ? "text-ok" : "text-fg")} title={c.relaxed ? "relaxed" : "unrelaxed"}>
                        {c.e_hull_ev != null ? `${(c.e_hull_ev * 1000).toFixed(0)} meV` : "—"}
                        {!c.relaxed && <span className="text-warn">*</span>}
                      </td>
                      <td className="text-right">
                        <Button
                          className="px-1.5 py-0 text-2xs"
                          disabled={!connected || busy}
                          onClick={() => {
                            if (commands.loadCrystal(c.crystal)) close();
                          }}
                        >
                          Load
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {res.candidates.some((c) => !c.relaxed) && <p className="mt-0.5 text-2xs text-warn/90">* unrelaxed: E_hull includes strain energy.</p>}
          </div>
        )}
      </div>
    </Modal>
  );
}

export function PhaseC2Dialogs() {
  const dialog = useUi((s) => s.dialog);
  if (dialog === "compare") return <CompareDialog />;
  if (dialog === "prototype") return <PrototypeDialog />;
  return null;
}
