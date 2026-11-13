/**
 * Phase C1 workflows as modal dialogs (opened from the "Defects · disorder ·
 * models" sidebar card, the history list, toasts and the vacancy warning):
 *   supercell      MAKE_SUPERCELL (diagonal n×m×k, √2×√2×2 / 2×2×2 presets),
 *                  resulting atom count and shortest periodic distance (< 10 Å warned)
 *   add-atom       ADD_ATOM (element + fractional coordinates, nearest-atom check); Phase C2:
 *                  "pick in viewport" hides the dialog, the viewport (PlacementPlane) raycasts
 *                  the cell plane s_axis = depth with a ghost atom, a click fills u, v, w
 *   substitution   SUBSTITUTE_FRACTION (from → to, fraction → count, candidates, seed) + SUBSTITUTION_RESULT
 *   formation      COMPUTE_FORMATION_ENERGY against a bulk history entry + FORMATION_ENERGY_RESULT
 *   committee      COMMITTEE_EVALUATE with 2–4 models + COMMITTEE_RESULT and the disagreement flag
 *   ordering       POST /api/ordering for a CIF refused as DISORDERED_STRUCTURE, load a ranked candidate
 */
import { AlertTriangle, Atom, Boxes, Flame, GitCompare, Grid3x3, Layers, Loader2, MousePointerClick, Scale, Shuffle, Users, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ApiError, api } from "../../services/apiClient";
import { showPane } from "../../services/dockActions";
import { commands } from "../../services/wsClient";
import { useCrystalStore } from "../../store/useCrystalStore";
import { cancelPlacement, startPlacement } from "../../services/placementActions";
import { useUi, type DialogKind } from "../../store/uiStore";
import type { OrderingResponse } from "../../types/protocol";
import { committeeVerdict, resolveHistoryRef } from "../../utils/analysisC";
import { cpkHex, elementBySymbol, normalizeSymbol } from "../../utils/cpkColors";
import { flattenMatrix, invert3, minimumImageCart, type Vec3 } from "../../utils/latticeMath";
import { studioLimits } from "../../utils/limits";
import { SUPERCELL_PRESETS, diagonalMatrix, previewSupercell, shortestPeriodicDistance, substitutionCount, suggestDiagonal, DEFECT_MIN_DISTANCE, type IntMatrix } from "../../utils/supercell";
import { ProgressBar } from "./ElasticPane";
import { refreshModels } from "./ModelPicker";
import { PeriodicTablePicker } from "./PeriodicTablePicker";
import { Badge, Button, CollapsibleCard, FallbackTag, FormulaText, Panel, cx, inputCls } from "./ui";

const fx = (v: number | null | undefined, d = 3) => (v == null || !Number.isFinite(v) ? "—" : v.toFixed(d));

export function Modal({ title, width = 520, onClose, children }: { title: ReactNode; width?: number; onClose(): void; children: ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return createPortal(
    <div className="fixed inset-0 z-[100] grid place-items-center bg-black/50 backdrop-blur-[2px]" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <Panel className="max-h-[calc(100vh-32px)] max-w-[calc(100vw-32px)] overflow-y-auto p-4">
        <div style={{ width }} data-dialog>
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[10px] font-semibold tracking-[0.18em] text-slate-400 uppercase">{title}</span>
            <button type="button" onClick={onClose} className="text-slate-500 hover:text-slate-200" aria-label="close">
              <X size={14} />
            </button>
          </div>
          {children}
        </div>
      </Panel>
    </div>,
    document.body,
  );
}

export function Warn({ children }: { children: ReactNode }) {
  return (
    <p className="flex gap-1 rounded-md border border-amber-400/30 bg-amber-400/[0.07] px-2 py-1 text-[10.5px] leading-snug text-amber-200">
      <AlertTriangle size={11} className="mt-px shrink-0" />
      <span>{children}</span>
    </p>
  );
}

const close = () => useUi.getState().closeDialog();

/** Element text field with a periodic-table popover. */
function ElementField({ value, onChange, label }: { value: string; onChange(v: string): void; label: string }) {
  const [open, setOpen] = useState(false);
  const z = elementBySymbol(value)?.z;
  return (
    <label className="relative block">
      <div className="mb-0.5 text-[10px] text-slate-500">{label}</div>
      <div className="flex gap-1">
        <span className="relative flex-1">
          {z && <span className="absolute top-1/2 left-2 h-2 w-2 -translate-y-1/2 rounded-full" style={{ background: cpkHex(z) }} />}
          <input className={cx(inputCls, "pl-5", value && !z && "border-rose-500/60")} value={value} onChange={(e) => onChange(normalizeSymbol(e.target.value) ?? e.target.value)} spellCheck={false} />
        </span>
        <button type="button" className={cx("rounded-md border border-white/10 px-1.5 text-slate-400 hover:text-cyan-200", open && "text-cyan-300")} onClick={() => setOpen((o) => !o)} title="Periodic table">
          <Grid3x3 size={13} />
        </button>
      </div>
      {open && (
        <PeriodicTablePicker
          className="absolute top-full right-0 z-50 mt-1"
          current={value}
          onPick={(s) => {
            onChange(s);
            setOpen(false);
          }}
          onClose={() => setOpen(false)}
        />
      )}
    </label>
  );
}

// --------------------------------------------------------------------------- supercell
function MatrixInputs({ m, onChange }: { m: IntMatrix; onChange(m: IntMatrix): void }) {
  return (
    <div className="grid grid-cols-3 gap-1">
      {m.map((row, i) =>
        row.map((v, j) => (
          <input
            key={`${i}${j}`}
            aria-label={`M${i + 1}${j + 1}`}
            className="w-full rounded border border-white/10 bg-slate-900/80 px-1 py-0.5 text-center font-mono text-[11px] text-slate-100 outline-none focus:border-cyan-400/50"
            value={v}
            inputMode="numeric"
            onChange={(e) => {
              const n = Number.parseInt(e.target.value, 10);
              const next = m.map((r) => [...r]);
              next[i]![j] = Number.isFinite(n) ? Math.max(-8, Math.min(8, n)) : 0;
              onChange(next);
            }}
          />
        )),
      )}
    </div>
  );
}

export function SupercellDialog() {
  const crystal = useCrystalStore((s) => s.crystal);
  const connected = useCrystalStore((s) => s.connection === "open");
  const maxAtoms = useCrystalStore((s) => studioLimits(s.health).maxAtoms);
  const [m, setM] = useState<IntMatrix>(diagonalMatrix([2, 2, 2]));
  if (!crystal) return null;
  const L = crystal.lattice.matrix;
  const n = crystal.atoms.length;
  const pv = previewSupercell(m, L, n, maxAtoms);
  const d0 = shortestPeriodicDistance(L);
  const sugg = suggestDiagonal(L, n, DEFECT_MIN_DISTANCE, maxAtoms);
  return (
    <Modal title="Make supercell" onClose={close}>
      <div className="space-y-2.5 text-[11px] text-slate-300">
        <p className="text-slate-400">
          L′ = M·L (rows: new lattice vectors in units of a, b, c). Current cell: {n} atoms, shortest periodic distance {d0.toFixed(2)} Å.
        </p>
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-[10px] text-slate-500">presets</span>
          {SUPERCELL_PRESETS.map((p) => (
            <Button key={p.id} className="px-2 py-0.5 text-[10.5px]" title={p.title} onClick={() => setM(p.matrix.map((r) => [...r]))}>
              {p.label}
            </Button>
          ))}
          <Button className="px-2 py-0.5 text-[10.5px]" title={`Smallest diagonal cell with every vector ≥ ${DEFECT_MIN_DISTANCE} Å within ${maxAtoms} atoms`} onClick={() => setM(diagonalMatrix(sugg))}>
            suggest {sugg.join("×")}
          </Button>
        </div>
        <div className="grid grid-cols-[1fr_1fr] gap-3">
          <div>
            <div className="mb-0.5 text-[10px] text-slate-500">diagonal n × m × k</div>
            <div className="flex gap-1">
              {[0, 1, 2].map((i) => (
                <input
                  key={i}
                  aria-label={`n${i + 1}`}
                  className="w-12 rounded border border-white/10 bg-slate-900/80 px-1 py-0.5 text-center font-mono text-[11px] text-slate-100 outline-none"
                  value={m[i]![i]}
                  inputMode="numeric"
                  onChange={(e) => {
                    const v = Number.parseInt(e.target.value, 10);
                    const d = [m[0]![0]!, m[1]![1]!, m[2]![2]!];
                    d[i] = Number.isFinite(v) ? Math.max(1, Math.min(8, v)) : 1;
                    setM(diagonalMatrix(d));
                  }}
                />
              ))}
            </div>
          </div>
          <div>
            <div className="mb-0.5 text-[10px] text-slate-500">matrix M</div>
            <MatrixInputs m={m} onChange={setM} />
          </div>
        </div>
        <div className="rounded-md bg-white/[0.03] px-2 py-1 font-mono text-[11px]" data-supercell-preview>
          {pv.valid || pv.tooLarge ? (
            <>
              det M = {pv.det} → <span className={pv.tooLarge ? "text-rose-300" : "text-slate-100"}>{pv.nAtoms} atoms</span> · shortest periodic distance{" "}
              <span className={pv.small ? "text-amber-300" : "text-emerald-300"}>{pv.minDistance.toFixed(2)} Å</span>
            </>
          ) : (
            <span className="text-rose-300">{pv.error}</span>
          )}
        </div>
        {pv.tooLarge && <p className="text-[10.5px] text-rose-300">{pv.error}</p>}
        {pv.valid && pv.small && <Warn>Under {DEFECT_MIN_DISTANCE} Å a point defect still interacts noticeably with its periodic images; use a larger cell for formation energies.</Warn>}
        <div className="flex justify-end gap-2">
          <Button onClick={close}>Cancel</Button>
          <Button
            variant="primary"
            disabled={!connected || !pv.valid}
            onClick={() => {
              if (commands.makeSupercell(m)) close();
            }}
          >
            <Boxes size={12} /> Make supercell
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- add atom
/** Shortest distance (Å, minimum image) from a fractional position to any atom. */
export function nearestAtomDistance(frac: Vec3, matrix: number[][], atomsFrac: readonly (readonly number[])[]): number {
  const L = flattenMatrix(matrix);
  const Linv = invert3(L);
  let best = Infinity;
  for (const s of atomsFrac) {
    const d: Vec3 = [0, 0, 0];
    const ds = [frac[0] - s[0]!, frac[1] - s[1]!, frac[2] - s[2]!];
    for (let j = 0; j < 3; j++) d[j] = ds[0]! * L[j]! + ds[1]! * L[3 + j]! + ds[2]! * L[6 + j]!;
    const m = minimumImageCart(d, L, Linv);
    best = Math.min(best, Math.hypot(m[0], m[1], m[2]));
  }
  return best;
}

export function AddAtomDialog() {
  const crystal = useCrystalStore((s) => s.crystal);
  const selected = useCrystalStore((s) => s.selectedAtom);
  const connected = useCrystalStore((s) => s.connection === "open");
  // Phase C2: the fields live in the UI store so they survive "pick in viewport".
  const el = useUi((s) => s.addAtomDraft.element);
  const frac = useUi((s) => s.addAtomDraft.frac);
  const setEl = (element: string) => useUi.getState().setAddAtomDraft({ element });
  const setFrac = (f: [string, string, string]) => useUi.getState().setAddAtomDraft({ frac: f });
  const [axis, setAxis] = useState<0 | 1 | 2>(2);
  if (!crystal) return null;
  const f = frac.map((x) => Number.parseFloat(x)) as Vec3;
  const ok = f.every((x) => Number.isFinite(x));
  const elOk = (elementBySymbol(el)?.z ?? 99) <= 89;
  const dmin = ok ? nearestAtomDistance(f, crystal.lattice.matrix, crystal.atoms.map((a) => a.frac_coords)) : NaN;
  const dmin0 = shortestPeriodicDistance(crystal.lattice.matrix);
  const sel = selected !== null ? crystal.atoms[selected] : null;
  return (
    <Modal title="Add atom (interstitial / adsorbate)" width={440} onClose={close}>
      <div className="space-y-2.5 text-[11px] text-slate-300">
        <div className="grid grid-cols-[110px_1fr] gap-3">
          <ElementField label="element" value={el} onChange={setEl} />
          <div>
            <div className="mb-0.5 text-[10px] text-slate-500">fractional coordinates (u, v, w)</div>
            <div className="flex gap-1">
              {[0, 1, 2].map((i) => (
                <input
                  key={i}
                  aria-label={`frac ${i}`}
                  className={cx(inputCls, "text-center", !Number.isFinite(f[i]!) && "border-rose-500/60")}
                  value={frac[i]}
                  inputMode="decimal"
                  onChange={(e) => {
                    const n = [...frac] as [string, string, string];
                    n[i] = e.target.value;
                    setFrac(n);
                  }}
                />
              ))}
            </div>
          </div>
        </div>
        <div className="flex flex-wrap gap-1">
          {[
            ["cell centre", [0.5, 0.5, 0.5]],
            ["face centre (½,½,0)", [0.5, 0.5, 0]],
            ["edge centre (½,0,0)", [0.5, 0, 0]],
            ["(¼,¼,¼)", [0.25, 0.25, 0.25]],
          ].map(([label, v]) => (
            <Button key={label as string} className="px-1.5 py-0.5 text-[10px]" onClick={() => setFrac((v as number[]).map(String) as [string, string, string])}>
              {label as string}
            </Button>
          ))}
          {sel && (
            <Button className="px-1.5 py-0.5 text-[10px]" title="Start from the selected atom's position (then shift it)" onClick={() => setFrac(sel.frac_coords.map((x) => x.toFixed(4)) as [string, string, string])}>
              at selected #{selected}
            </Button>
          )}
        </div>
        <div className="rounded-md border border-white/[0.06] bg-white/[0.02] p-1.5" data-pick-viewport>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] text-slate-500">pick in viewport on the plane</span>
            <select className="rounded border border-white/10 bg-slate-900/80 px-1 py-0.5 font-mono text-[10.5px] text-slate-100 outline-none" value={axis} onChange={(e) => setAxis(Number(e.target.value) as 0 | 1 | 2)} aria-label="plane normal axis">
              <option value={0}>u (∥ b,c)</option>
              <option value={1}>v (∥ a,c)</option>
              <option value={2}>w (∥ a,b)</option>
            </select>
            <span className="font-mono text-[10.5px] text-slate-400">= {Number.isFinite(f[axis]!) ? f[axis]!.toFixed(3) : "—"}</span>
            <Button
              variant="primary"
              className="ml-auto px-2 py-0.5 text-[10.5px]"
              disabled={!Number.isFinite(f[axis]!)}
              title="Hide this dialog, then click on the cell plane at this depth (Esc cancels)"
              onClick={() => startPlacement(axis, f[axis]!)}
            >
              <MousePointerClick size={11} /> Pick in viewport
            </Button>
          </div>
          <p className="mt-0.5 text-[10px] leading-snug text-slate-500">
            The depth is the {"uvw"[axis]} field above; use "at selected" first for a plane through an atom.
          </p>
        </div>
        <div className="font-mono text-[10.5px] text-slate-400">
          nearest atom: <span className={dmin < 0.5 ? "text-rose-300" : dmin < 1.2 ? "text-amber-300" : "text-slate-100"}>{Number.isFinite(dmin) ? `${dmin.toFixed(2)} Å` : "—"}</span>
          {dmin < 0.5 && " (the server rejects < 0.5 Å)"}
        </div>
        {dmin0 < DEFECT_MIN_DISTANCE && <Warn>The cell's shortest periodic distance is {dmin0.toFixed(1)} Å: the interstitial interacts with its images. Make a supercell first for an isolated defect.</Warn>}
        <div className="flex justify-end gap-2">
          <Button onClick={() => useUi.getState().openDialog("supercell")}>Make supercell first</Button>
          <Button
            variant="primary"
            disabled={!connected || !ok || !elOk || dmin < 0.5}
            onClick={() => {
              if (commands.addAtom(el, f)) close();
            }}
          >
            <Atom size={12} /> Add {el}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/** Floating bar shown while picking (the modal is hidden so the viewport receives the clicks). */
export function PlacementBar() {
  const placement = useUi((s) => s.placement);
  const el = useUi((s) => s.addAtomDraft.element);
  useEffect(() => {
    if (!placement) return;
    const k = (e: KeyboardEvent) => e.key === "Escape" && cancelPlacement();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [placement]);
  if (!placement) return null;
  const h = placement.hover;
  return (
    <div className="pointer-events-none absolute bottom-16 left-1/2 z-40 -translate-x-1/2">
      <Panel className="pointer-events-auto flex items-center gap-2 px-3 py-1.5" >
        <MousePointerClick size={13} className="text-cyan-300" />
        <span className="font-mono text-[11px] text-slate-200" data-placement>
          Click on the plane {"uvw"[placement.axis]} = {placement.depth.toFixed(3)} to place {el}
          {h ? ` · (${h.map((x) => x.toFixed(3)).join(", ")})` : ""}
        </span>
        <Button className="px-2 py-0.5 text-[10.5px]" onClick={cancelPlacement}>
          Cancel
        </Button>
      </Panel>
    </div>
  );
}

// --------------------------------------------------------------------------- substitution
export function SubstitutionDialog() {
  const crystal = useCrystalStore((s) => s.crystal);
  const connected = useCrystalStore((s) => s.connection === "open");
  const running = useCrystalStore((s) => s.streams.find((x) => x.command === "SUBSTITUTE_FRACTION") ?? null);
  const busy = useCrystalStore((s) => s.streams.length > 0);
  const progress = useCrystalStore((s) => s.progress.SUBSTITUTE_FRACTION ?? null);
  const result = useCrystalStore((s) => s.substitution);
  const counts: Record<string, number> = {};
  for (const a of crystal?.atoms ?? []) counts[a.element] = (counts[a.element] ?? 0) + 1;
  const els = Object.keys(counts);
  const [from, setFrom] = useState(els[0] ?? "");
  const [to, setTo] = useState("Na");
  const [fraction, setFraction] = useState(0.25);
  const [cands, setCands] = useState(12);
  const [seed, setSeed] = useState("");
  if (!crystal) return null;
  const nFrom = counts[from] ?? 0;
  const k = substitutionCount(fraction, nFrom);
  const toOk = (elementBySymbol(to)?.z ?? 99) <= 89 && to !== from;
  const step = nFrom > 0 ? 1 / nFrom : 0.05;
  const energies = result?.energies_per_atom ?? [];
  return (
    <Modal title="Partial substitution" width={460} onClose={close}>
      <div className="space-y-2.5 text-[11px] text-slate-300">
        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <div className="mb-0.5 text-[10px] text-slate-500">replace</div>
            <select className={inputCls} value={from} onChange={(e) => setFrom(e.target.value)}>
              {els.map((e) => (
                <option key={e} value={e}>
                  {e} ({counts[e]} sites)
                </option>
              ))}
            </select>
          </label>
          <ElementField label="by" value={to} onChange={setTo} />
        </div>
        <label className="block">
          <div className="mb-1 flex justify-between text-[11px] text-slate-400">
            <span>fraction</span>
            <span className="font-mono text-slate-100 tabular-nums">
              {(fraction * 100).toFixed(1)} % → {k} of {nFrom} {from} → {to}
            </span>
          </div>
          <input type="range" className="lf-range w-full" min={step} max={1} step={step} value={fraction} onChange={(e) => setFraction(Number(e.target.value))} />
        </label>
        {k === 0 && <Warn>That rounds to zero atoms in this cell; increase the fraction or make a supercell.</Warn>}
        <div className="grid grid-cols-2 gap-3">
          <label className="block" title="Symmetry-distinct configurations tried (random if there are more); the lowest single-point energy is kept">
            <div className="mb-0.5 text-[10px] text-slate-500">candidates</div>
            <input className={inputCls} inputMode="numeric" value={cands} onChange={(e) => setCands(Math.max(1, Math.min(100, Number.parseInt(e.target.value, 10) || 1)))} />
          </label>
          <label className="block">
            <div className="mb-0.5 text-[10px] text-slate-500">seed (optional)</div>
            <input className={inputCls} inputMode="numeric" placeholder="random" value={seed} onChange={(e) => setSeed(e.target.value.replace(/[^\d]/g, ""))} />
          </label>
        </div>
        {running && <ProgressBar done={progress?.done} total={progress?.total} message={progress?.message ?? "ranking configurations…"} />}
        {result && !running && (
          <div className="rounded-md border border-emerald-400/25 bg-emerald-400/[0.05] px-2 py-1.5 font-mono text-[10.5px]" data-substitution-result>
            <div className="text-slate-100">
              {result.n_substituted} {result.from_element} → {result.to_element} · best of {result.n_tried} configurations
            </div>
            <div className="text-slate-400">
              chosen E = {result.chosen_energy_per_atom.toFixed(4)} eV/atom
              {energies.length > 1 && ` · spread ${((energies[energies.length - 1]! - energies[0]!) * 1000).toFixed(1)} meV/atom`}
            </div>
            <div className="text-slate-500">{result.method}</div>
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={close}>Close</Button>
          <Button
            variant={running ? "danger" : "primary"}
            disabled={!connected || (!running && (busy || !toOk || k === 0))}
            onClick={() => {
              if (running) commands.stop();
              else {
                useCrystalStore.getState().setSubstitution(null);
                commands.substitute({ from_element: from, to_element: to, fraction, n_candidates: cands, seed: seed ? Number.parseInt(seed, 10) : null });
              }
            }}
          >
            <Shuffle size={12} /> {running ? "Stop" : `Substitute ${k} ${from}`}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- formation energy
export function FormationDialog() {
  const history = useCrystalStore((s) => s.history);
  const crystal = useCrystalStore((s) => s.crystal);
  const connected = useCrystalStore((s) => s.connection === "open");
  const running = useCrystalStore((s) => s.streams.find((x) => x.command === "COMPUTE_FORMATION_ENERGY") ?? null);
  const busy = useCrystalStore((s) => s.streams.length > 0);
  const progress = useCrystalStore((s) => s.progress.COMPUTE_FORMATION_ENERGY ?? null);
  const formation = useCrystalStore((s) => s.formation);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const lastError = useCrystalStore((s) => (s.lastError?.command === "COMPUTE_FORMATION_ENERGY" ? s.lastError : null));
  const bulkRef = useUi((s) => s.bulkRef);
  const entries = history?.entries ?? [];
  const resolved = resolveHistoryRef(entries, bulkRef);
  const [pick, setPick] = useState<number | null>(null);
  const bulkPos = pick ?? resolved?.position ?? entries.find((e) => e.position !== history?.position)?.position ?? null;
  const bulk = entries.find((e) => e.position === bulkPos) ?? null;
  const r = formation?.result;
  const limits = r ? Object.keys(r.formation_energies_ev) : [];
  const muEls = r ? [...new Set(Object.values(r.chemical_potentials).flatMap((m) => Object.keys(m)))].sort() : [];
  return (
    <Modal title="Defect formation energy" width={560} onClose={close}>
      <div className="space-y-2.5 text-[11px] text-slate-300">
        <p className="text-slate-400">
          E_f = E_defect − E_bulk − Σ Δn_i μ_i for the current structure (the defect cell) against a bulk entry of the history. μ_i from the uMLIP-relaxed Materials Project phases of the chemical system, at each limit of the stability region.
        </p>
        <label className="block">
          <div className="mb-0.5 text-[10px] text-slate-500">bulk reference (history entry)</div>
          <select className={inputCls} value={bulkPos ?? ""} onChange={(e) => setPick(Number(e.target.value))} disabled={!entries.length}>
            {entries.map((e) => (
              <option key={e.position} value={e.position} disabled={e.position === history?.position}>
                {e.position + 1}. {e.label} — {e.formula}, {e.n_atoms} at, {e.spacegroup_symbol}
                {e.position === history?.position ? " (current)" : ""}
              </option>
            ))}
          </select>
        </label>
        {bulk && crystal && (
          <div className="font-mono text-[10.5px] text-slate-400">
            defect cell: <FormulaText formula={crystal.formula} /> {crystal.atoms.length} at · bulk: <FormulaText formula={bulk.formula} /> {bulk.n_atoms} at
            {Math.abs(crystal.atoms.length - bulk.n_atoms) > 2 && <span className="text-amber-300"> · atom counts differ by more than a point defect</span>}
          </div>
        )}
        {!entries.length && <Warn>No history entries yet: make the supercell (bulk) first, then create the defect.</Warn>}
        {running && <ProgressBar done={progress?.done} total={progress?.total} message={progress?.message ?? "relaxing reference phases…"} />}
        {lastError && !running && !r && <p className="text-[10.5px] text-rose-300">{lastError.message}</p>}
        {r && (
          <div className="space-y-1.5 rounded-md border border-white/[0.06] bg-white/[0.02] p-2" data-formation-result>
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="font-mono text-[15px] text-cyan-100">{r.defect_label}</span>
              {Object.entries(r.delta_n).map(([el, d]) => (
                <Badge key={el} tone={d < 0 ? "amber" : "cyan"}>
                  Δn({el}) = {d > 0 ? "+" : ""}
                  {d}
                </Badge>
              ))}
              {formation!.stateVersion !== stateVersion && <span className="font-mono text-[10px] text-amber-300">stale</span>}
              <FallbackTag compact />
            </div>
            <div className="font-mono text-[10.5px] text-slate-400">
              E_defect {r.e_defect_ev.toFixed(3)} eV · E_bulk {r.e_bulk_ev.toFixed(3)} eV · defect–image distance{" "}
              <span className={r.min_image_distance < DEFECT_MIN_DISTANCE ? "text-amber-300" : "text-emerald-300"}>{r.min_image_distance.toFixed(2)} Å</span>
            </div>
            <table className="w-full font-mono text-[10.5px] tabular-nums">
              <thead className="text-slate-500">
                <tr>
                  <th className="text-left font-normal">limit</th>
                  <th className="text-right font-normal">E_f (eV)</th>
                  {muEls.map((el) => (
                    <th key={el} className="text-right font-normal">
                      μ({el})
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {limits.map((l) => (
                  <tr key={l} className="border-t border-white/[0.04]">
                    <td className="text-slate-300">{l}</td>
                    <td className="text-right text-slate-50">{fx(r.formation_energies_ev[l], 3)}</td>
                    {muEls.map((el) => (
                      <td key={el} className="text-right text-slate-300">
                        {fx(r.chemical_potentials[l]?.[el], 3)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {r.warnings.map((w) => (
              <Warn key={w}>{w}</Warn>
            ))}
            <div className="text-[10px] text-slate-500">{r.method}</div>
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={close}>Close</Button>
          <Button
            variant={running ? "danger" : "primary"}
            disabled={!connected || (!running && (busy || bulkPos === null || bulkPos === history?.position))}
            onClick={() => {
              if (running) commands.stop();
              else if (bulkPos !== null) {
                if (bulk) useUi.getState().setBulkRef({ position: bulk.position, label: bulk.label, formula: bulk.formula, n_atoms: bulk.n_atoms, energy_per_atom: bulk.energy_per_atom });
                commands.computeFormationEnergy(bulkPos);
              }
            }}
          >
            <Scale size={12} /> {running ? "Stop" : r ? "Recompute" : "Compute E_f"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- committee
export function CommitteeDialog() {
  const models = useCrystalStore((s) => s.models);
  const modelsError = useCrystalStore((s) => s.modelsError);
  const connected = useCrystalStore((s) => s.connection === "open");
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const running = useCrystalStore((s) => s.streams.find((x) => x.command === "COMMITTEE_EVALUATE") ?? null);
  const busy = useCrystalStore((s) => s.streams.length > 0);
  const progress = useCrystalStore((s) => s.progress.COMMITTEE_EVALUATE ?? null);
  const committee = useCrystalStore((s) => s.committee);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const [sel, setSel] = useState<string[] | null>(null);
  const [relax, setRelax] = useState(true);
  const [d3, setD3] = useState(false);
  useEffect(() => {
    void refreshModels();
  }, []);
  const list = (models?.models ?? []).filter((m) => m.family === "mace");
  const chosen = sel ?? list.filter((m) => m.checkpoint_cached).slice(0, 4).map((m) => m.id);
  const toggle = (id: string) => setSel(chosen.includes(id) ? chosen.filter((x) => x !== id) : chosen.length < 4 ? [...chosen, id] : chosen);
  const r = committee?.result;
  const verdict = r ? committeeVerdict(r) : null;
  return (
    <Modal title="Compare models (committee)" width={600} onClose={close}>
      <div className="space-y-2.5 text-[11px] text-slate-300">
        <p className="text-slate-400">Evaluate the current structure with 2–4 models{relax ? " and relax it with each" : ""}. Spread between models is a practical uncertainty estimate. Absolute energies are not comparable across models (different references).</p>
        {!models ? (
          <p className={modelsError ? "text-rose-300" : "text-slate-500"}>{modelsError ? `Could not list models: ${modelsError}` : "loading models…"}</p>
        ) : (
          <div className="grid grid-cols-2 gap-1" data-committee-models>
            {list.map((m) => (
              <label key={m.id} className={cx("flex cursor-pointer items-center gap-1.5 rounded-md border px-2 py-1", chosen.includes(m.id) ? "border-cyan-400/40 bg-cyan-400/10" : "border-white/[0.06]")}>
                <input type="checkbox" className="accent-cyan-400" checked={chosen.includes(m.id)} onChange={() => toggle(m.id)} />
                <span className="min-w-0 flex-1 truncate text-slate-100">{m.label}</span>
                <span className={cx("font-mono text-[9.5px]", m.checkpoint_cached ? "text-slate-500" : "text-amber-300/90")}>{m.checkpoint_cached ? "cached" : "download"}</span>
              </label>
            ))}
          </div>
        )}
        <div className="flex gap-4">
          <label className="flex items-center gap-1.5">
            <input type="checkbox" className="accent-cyan-400" checked={relax} onChange={(e) => setRelax(e.target.checked)} /> relax with each model (positions + cell)
          </label>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" className="accent-cyan-400" checked={d3} onChange={(e) => setD3(e.target.checked)} /> D3 dispersion
          </label>
        </div>
        {running && <ProgressBar done={progress?.done} total={progress?.total} message={progress?.message ?? "evaluating…"} />}
        {r && verdict && (
          <div className="space-y-1.5" data-committee-result>
            {verdict.disagree ? (
              <div className="rounded-md border border-rose-500/50 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200" data-disagree>
                <AlertTriangle size={12} className="mr-1 inline" />
                Models disagree: {verdict.reasons.join("; ")}.
              </div>
            ) : (
              <div className="rounded-md border border-emerald-400/30 bg-emerald-400/[0.06] px-2 py-1 text-[11px] text-emerald-200">Models agree (volume spread {verdict.volumeSpreadPct != null ? `${verdict.volumeSpreadPct.toFixed(1)} %` : "n/a"}, same space group).</div>
            )}
            {committee!.stateVersion !== stateVersion && <span className="font-mono text-[10px] text-amber-300">structure changed since this comparison (stale)</span>}
            <table className="w-full font-mono text-[10.5px] tabular-nums">
              <thead className="text-slate-500">
                <tr>
                  <th className="text-left font-normal">model</th>
                  <th className="text-right font-normal">P (GPa)</th>
                  <th className="text-right font-normal">max |F|</th>
                  <th className="text-right font-normal">V/atom (Å³)</th>
                  <th className="text-right font-normal">a / b / c (Å)</th>
                  <th className="text-right font-normal">SG</th>
                </tr>
              </thead>
              <tbody>
                {r.entries.map((e) => (
                  <tr key={e.model_id} className="border-t border-white/[0.04]">
                    <td className="text-slate-100" title={e.backend_name}>
                      {e.model_id}
                    </td>
                    <td className="text-right">{fx(e.pressure_gpa, 2)}</td>
                    <td className="text-right">{fx(e.max_force, 3)}</td>
                    <td className="text-right">{fx(e.relaxed_volume_per_atom, 2)}</td>
                    <td className="text-right">{e.relaxed_lattice_abc ? e.relaxed_lattice_abc.map((x) => x.toFixed(3)).join(" / ") : "—"}</td>
                    <td className={cx("text-right", verdict.spacegroups.length > 1 && "text-rose-300")}>{e.relaxed_spacegroup ?? "—"}</td>
                  </tr>
                ))}
                <tr className="border-t border-white/10 text-slate-400">
                  <td>spread</td>
                  <td className="text-right">{fx(r.spread.pressure_gpa, 2)}</td>
                  <td className="text-right">{fx(r.spread.max_force, 3)}</td>
                  <td className={cx("text-right", verdict.volumeSpreadPct != null && verdict.volumeSpreadPct > 3 && "text-rose-300")}>{verdict.volumeSpreadPct != null ? `${verdict.volumeSpreadPct.toFixed(1)} %` : "—"}</td>
                  <td />
                  <td />
                </tr>
              </tbody>
            </table>
            <div className="text-[10px] text-slate-500">{r.method}</div>
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={close}>Close</Button>
          <Button
            variant={running ? "danger" : "primary"}
            disabled={!connected || !hasCrystal || (!running && (busy || chosen.length < 2))}
            onClick={() => (running ? commands.stop() : commands.committee({ model_ids: chosen, relax, dispersion: d3 }))}
          >
            <Users size={12} /> {running ? "Stop" : `Compare ${chosen.length} models`}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- ordering
export function OrderingDialog() {
  const disordered = useCrystalStore((s) => s.disordered);
  const model = useCrystalStore((s) => s.model);
  const connected = useCrystalStore((s) => s.connection === "open");
  const studioMaxAtoms = useCrystalStore((s) => studioLimits(s.health).maxAtoms);
  const [maxAtoms, setMaxAtoms] = useState(() => Math.min(64, studioMaxAtoms));
  const [nCand, setNCand] = useState(8);
  const [state, setState] = useState<{ status: "idle" | "busy" | "ok" | "error"; result?: OrderingResponse; error?: string }>({ status: "idle" });
  const run = async () => {
    if (!disordered) return;
    setState({ status: "busy" });
    try {
      const result = await api.ordering({ cif: disordered.cif, max_atoms: maxAtoms, n_candidates: nCand, model_id: model?.model_id ?? null, dispersion: model?.dispersion ?? false });
      setState({ status: "ok", result });
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.code === "STRUCTURE_TOO_LARGE"
            ? `${e.message} Raise the atom limit or use a smaller cell.`
            : e.code === "MODEL_UNAVAILABLE"
              ? `Model unavailable: ${e.message}`
              : `${e.code}: ${e.message}`
          : `Could not reach the backend: ${e instanceof Error ? e.message : String(e)}`;
      setState({ status: "error", error: msg });
    }
  };
  useEffect(() => {
    if (disordered) void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [disordered]);
  const res = state.result;
  return (
    <Modal title="Ordered approximants" width={540} onClose={close}>
      <div className="space-y-2.5 text-[11px] text-slate-300">
        {!disordered ? (
          <p className="text-slate-500">No disordered structure pending. Load a partially occupied CIF first.</p>
        ) : (
          <>
            <p className="text-slate-400">
              The structure has partial occupancies ({disordered.message}). Small-cell orderings are enumerated and ranked by uMLIP energy; load one to continue.
            </p>
            <div className="flex items-end gap-2">
              <label className="block w-28">
                <div className="mb-0.5 text-[10px] text-slate-500">max atoms</div>
                <input className={inputCls} inputMode="numeric" value={maxAtoms} onChange={(e) => setMaxAtoms(Math.max(1, Math.min(studioMaxAtoms, Number.parseInt(e.target.value, 10) || 1)))} />
              </label>
              <label className="block w-28">
                <div className="mb-0.5 text-[10px] text-slate-500">candidates</div>
                <input className={inputCls} inputMode="numeric" value={nCand} onChange={(e) => setNCand(Math.max(1, Math.min(50, Number.parseInt(e.target.value, 10) || 1)))} />
              </label>
              <Button disabled={state.status === "busy"} onClick={() => void run()}>
                {state.status === "busy" ? <Loader2 size={12} className="animate-spin" /> : <Layers size={12} />} Enumerate
              </Button>
            </div>
            {state.status === "busy" && <ProgressBar message="enumerating and ranking orderings…" />}
            {state.status === "error" && <p className="text-[10.5px] text-rose-300">{state.error}</p>}
            {res && state.status === "ok" && (
              <div data-ordering-result>
                <div className="mb-1 font-mono text-[10.5px] text-slate-400">
                  <FormulaText formula={res.disordered_formula} /> · {res.n_enumerated} orderings enumerated · {res.method}
                </div>
                <table className="w-full font-mono text-[10.5px] tabular-nums">
                  <thead className="text-slate-500">
                    <tr>
                      <th className="text-left font-normal">#</th>
                      <th className="text-left font-normal">formula</th>
                      <th className="text-right font-normal">atoms</th>
                      <th className="text-right font-normal">SG</th>
                      <th className="text-right font-normal">ΔE (meV/atom)</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {res.candidates.map((c, i) => (
                      <tr key={i} className="border-t border-white/[0.04]">
                        <td className="text-slate-500">{i + 1}</td>
                        <td className="text-slate-100">
                          <FormulaText formula={c.formula} />
                        </td>
                        <td className="text-right">{c.n_atoms}</td>
                        <td className="text-right">{c.spacegroup_symbol}</td>
                        <td className={cx("text-right", i === 0 ? "text-emerald-300" : "text-slate-200")}>{c.relative_energy_mev_per_atom.toFixed(1)}</td>
                        <td className="text-right">
                          <Button
                            className="px-1.5 py-0 text-[10px]"
                            disabled={!connected}
                            onClick={() => {
                              if (commands.loadCif(c.cif)) {
                                useCrystalStore.getState().setDisordered(null);
                                close();
                              }
                            }}
                          >
                            Load
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- root + sidebar card
const DIALOGS: Partial<Record<DialogKind, () => ReactNode>> = {
  supercell: () => <SupercellDialog />,
  "add-atom": () => <AddAtomDialog />,
  substitution: () => <SubstitutionDialog />,
  formation: () => <FormationDialog />,
  committee: () => <CommitteeDialog />,
  ordering: () => <OrderingDialog />,
};

export function PhaseCDialogs() {
  const dialog = useUi((s) => s.dialog);
  const render = dialog ? DIALOGS[dialog] : undefined;
  return render ? <>{render()}</> : null;
}

/** Sidebar card with the Phase C1 workflows. */
export function DefectsCard() {
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const connected = useCrystalStore((s) => s.connection === "open");
  const disordered = useCrystalStore((s) => s.disordered !== null);
  const open = (d: DialogKind) => useUi.getState().openDialog(d);
  const dis = !connected || !hasCrystal;
  return (
    <CollapsibleCard id="defects" title="Defects · disorder · models" defaultOpen={false}>
      <div className="grid grid-cols-2 gap-1.5">
        <Button disabled={dis} onClick={() => open("supercell")} title="n×m×k or √2×√2×2 supercell (MAKE_SUPERCELL)">
          <Boxes size={12} /> Supercell
        </Button>
        <Button disabled={dis} onClick={() => open("add-atom")} title="Interstitial / adsorbate (ADD_ATOM)">
          <Atom size={12} /> Add atom
        </Button>
        <Button disabled={dis} onClick={() => open("substitution")} title="Replace a fraction of one element, lowest-energy configuration">
          <Shuffle size={12} /> Substitute
        </Button>
        <Button disabled={dis} onClick={() => open("formation")} title="Defect formation energy vs a bulk history entry">
          <Scale size={12} /> Formation E
        </Button>
        <Button disabled={dis} onClick={() => showPane("local")} title="Partial g(r), bond valence, polyhedra, Glazer tilts">
          <Layers size={12} /> Local structure
        </Button>
        <Button disabled={dis} onClick={() => showPane("md")} title="Molecular dynamics, MSD, diffusion, Arrhenius scan">
          <Flame size={12} /> MD
        </Button>
        <Button disabled={dis} onClick={() => open("committee")} title="Evaluate the structure with 2–4 models (COMMITTEE_EVALUATE)">
          <Users size={12} /> Compare models
        </Button>
        <Button disabled={!hasCrystal} onClick={() => useUi.getState().openCompare()} title="Two structures side by side: symmetry, ΔE, lattice, phonons (POST /api/compare)">
          <GitCompare size={12} /> Compare structures
        </Button>
        {disordered && (
          <Button variant="warning" className="col-span-2" onClick={() => open("ordering")}>
            <Layers size={12} /> Find ordered approximants
          </Button>
        )}
      </div>
    </CollapsibleCard>
  );
}

