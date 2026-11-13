/**
 * E_hull control for the telemetry bar: a button that runs
 * POST /api/hull/evaluate and a popover with the result (decomposition,
 * reference count, method), a busy state (cold hulls can take minutes) and
 * graceful HULL_UNAVAILABLE handling. `HullPopoverView` is pure (unit-tested
 * with server rendering).
 */
import { Calculator, Loader2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { evaluateHull } from "../../services/hullActions";
import { useCrystalStore, type HullState } from "../../store/useCrystalStore";
import { Button, FallbackTag, FormulaText, cx } from "./ui";

export function HullPopoverView({
  hull,
  now,
  stale,
  onEvaluate,
  onClose,
}: {
  hull: HullState;
  now: number;
  stale: boolean;
  onEvaluate(): void;
  onClose(): void;
}) {
  return (
    <div className="w-[340px] rounded-xl border border-white/10 bg-slate-950/95 p-3 text-[11.5px] text-slate-300 shadow-[0_12px_48px_rgba(0,0,0,0.6)] backdrop-blur-md" data-hull-status={hull.status}>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[10px] font-semibold tracking-[0.18em] text-slate-400 uppercase">Energy above hull (uMLIP)</span>
        <button type="button" onClick={onClose} className="text-slate-500 hover:text-slate-200" aria-label="close">
          <X size={13} />
        </button>
      </div>
      {hull.status === "idle" && (
        <p>Builds the convex hull of the chemical system from Materials Project reference phases, all re-relaxed with the studio's uMLIP; the candidate is relaxed the same way before scoring.</p>
      )}
      {hull.status === "busy" && (
        <p className="flex items-center gap-2 text-cyan-200">
          <Loader2 size={13} className="animate-spin" /> Evaluating… {Math.max(0, Math.round((now - hull.startedAt) / 1000))} s (a cold hull relaxes up to 40 references and can take minutes)
        </p>
      )}
      {hull.status === "unavailable" && (
        <p className="text-amber-300">
          <span className="font-semibold">Hull lookup unavailable.</span> {hull.message}
        </p>
      )}
      {hull.status === "error" && (
        <p className="text-rose-300">
          <span className="font-semibold">{hull.code}:</span> {hull.message}
        </p>
      )}
      {hull.status === "ok" && (
        <div className="space-y-2">
          <div className="flex items-baseline justify-between font-mono">
            <span className="text-slate-400">{hull.result.chemsys}</span>
            <span className={cx("text-[15px] font-semibold", hull.result.e_hull_ev <= 0.025 ? "text-emerald-300" : "text-amber-300")}>
              {hull.result.e_hull_ev.toFixed(3)} eV/atom
            </span>
          </div>
          {stale && <p className="text-[10.5px] text-amber-300/80">The structure changed since this evaluation; re-run for the current one.</p>}
          <FallbackTag />
          {hull.result.candidate_relaxed !== undefined && (
            <div className="flex items-baseline justify-between font-mono text-[10.5px]" data-hull-relaxed>
              <span className={hull.result.candidate_relaxed ? "text-emerald-300/90" : "text-amber-300/90"}>
                {hull.result.candidate_relaxed ? "candidate relaxed" : "candidate scored as given"}
              </span>
              <span className="text-slate-400">
                E {hull.result.energy_per_atom.toFixed(3)}
                {hull.result.energy_per_atom_as_given != null && ` (as given ${hull.result.energy_per_atom_as_given.toFixed(3)})`} eV/atom
              </span>
            </div>
          )}
          <div>
            <div className="mb-1 text-[10px] tracking-wider text-slate-500 uppercase">Decomposition</div>
            <ul className="space-y-0.5 font-mono text-[11px]">
              {Object.entries(hull.result.decomposition).map(([formula, frac]) => (
                <li key={formula} className="flex justify-between">
                  <FormulaText formula={formula} />
                  <span>{(frac * 100).toFixed(1)} %</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="font-mono text-[10.5px] text-slate-400">
            {hull.result.references.length} reference phase{hull.result.references.length === 1 ? "" : "s"} · {hull.result.umlip_backend}
            {hull.result.formation_energy_per_atom != null && ` · E_form ${hull.result.formation_energy_per_atom.toFixed(3)} eV/atom`}
          </div>
          <p className="text-[10.5px] leading-snug text-slate-500">
            {hull.result.method}
            {hull.result.candidate_relaxed && !/relax/i.test(hull.result.method) ? " · candidate relaxed (FIRE + cell) before scoring" : ""}
          </p>
        </div>
      )}
      <Button variant="ghost" className="mt-3 w-full" disabled={hull.status === "busy"} onClick={onEvaluate}>
        {hull.status === "busy" ? <Loader2 size={12} className="animate-spin" /> : <Calculator size={12} />}
        {hull.status === "ok" || hull.status === "error" || hull.status === "unavailable" ? "Re-evaluate" : "Evaluate E_hull"}
      </Button>
    </div>
  );
}

export function HullControl() {
  const hull = useCrystalStore((s) => s.hull);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (hull.status !== "busy") return;
    const h = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(h);
  }, [hull.status]);
  const stale = hull.status !== "idle" && hull.status !== "busy" && hull.stateVersion !== stateVersion;

  return (
    <span className="relative">
      <button
        type="button"
        disabled={!hasCrystal}
        title="Evaluate the energy above the convex hull"
        onClick={() => {
          setOpen(true);
          if (hull.status === "idle" || stale) void evaluateHull();
        }}
        className="rounded p-0.5 text-slate-400 hover:bg-white/5 hover:text-cyan-200 disabled:opacity-30"
      >
        {hull.status === "busy" ? <Loader2 size={12} className="animate-spin text-cyan-300" /> : <Calculator size={12} />}
      </button>
      {open && (
        <div className="absolute top-7 left-0 z-50">
          <HullPopoverView hull={hull} now={now} stale={stale} onEvaluate={() => void evaluateHull()} onClose={() => setOpen(false)} />
        </div>
      )}
    </span>
  );
}
