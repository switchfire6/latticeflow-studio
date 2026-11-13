/**
 * E_hull control for the telemetry bar: a button that runs
 * POST /api/hull/evaluate and a popover with the result (decomposition,
 * reference count, method), a busy state (cold hulls can take minutes) and
 * graceful HULL_UNAVAILABLE handling. `HullPopoverView` is pure (unit-tested
 * with server rendering).
 */
import { Calculator, Loader2, Triangle, X } from "lucide-react";
import { showPane } from "../../services/dockActions";
import { FromLibraryBadgeView } from "./LibraryPanes";
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
    <div className="w-[340px] rounded-panel border border-line bg-surface p-3 text-sm text-fg-2 shadow-lg shadow-black/40" data-hull-status={hull.status}>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase">Energy above hull (uMLIP)</span>
        <button type="button" onClick={onClose} className="text-fg-4 hover:text-fg" aria-label="close">
          <X size={13} />
        </button>
      </div>
      {hull.status === "idle" && (
        <p>Builds the convex hull of the chemical system from Materials Project reference phases, all re-relaxed with the studio's uMLIP; the candidate is relaxed the same way before scoring.</p>
      )}
      {hull.status === "busy" && (
        <p className="flex items-center gap-2 text-fg-2">
          <Loader2 size={13} className="animate-spin" /> Evaluating… {Math.max(0, Math.round((now - hull.startedAt) / 1000))} s (a cold hull relaxes up to 40 references and can take minutes)
        </p>
      )}
      {hull.status === "unavailable" && (
        <p className="text-warn">
          <span className="font-semibold">Hull lookup unavailable.</span> {hull.message}
        </p>
      )}
      {hull.status === "error" && (
        <p className="text-bad">
          <span className="font-semibold">{hull.code}:</span> {hull.message}
        </p>
      )}
      {hull.status === "ok" && (
        <div className="space-y-2">
          <div className="flex items-baseline justify-between font-mono">
            <span className="text-fg-3">{hull.result.chemsys}</span>
            <span className={cx("text-md font-semibold", hull.result.e_hull_ev <= 0.025 ? "text-ok" : "text-warn")}>
              {hull.result.e_hull_ev.toFixed(3)} eV/atom
            </span>
          </div>
          {stale && <p className="text-xs text-warn/80">The structure changed since this evaluation; re-run for the current one.</p>}
          <span className="flex items-center gap-1.5">
            <FallbackTag />
            <FromLibraryBadgeView kind="hull" data={hull.result} />
          </span>
          {hull.result.candidate_relaxed !== undefined && (
            <div className="flex items-baseline justify-between font-mono text-xs" data-hull-relaxed>
              <span className={hull.result.candidate_relaxed ? "text-ok/90" : "text-warn/90"}>
                {hull.result.candidate_relaxed ? "candidate relaxed" : "candidate scored as given"}
              </span>
              <span className="text-fg-3">
                E {hull.result.energy_per_atom.toFixed(3)}
                {hull.result.energy_per_atom_as_given != null && ` (as given ${hull.result.energy_per_atom_as_given.toFixed(3)})`} eV/atom
              </span>
            </div>
          )}
          <div>
            <div className="mb-1 text-xs font-medium text-fg-3">Decomposition</div>
            <ul className="space-y-0.5 font-mono text-xs">
              {Object.entries(hull.result.decomposition).map(([formula, frac]) => (
                <li key={formula} className="flex justify-between">
                  <FormulaText formula={formula} />
                  <span>{(frac * 100).toFixed(1)} %</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="font-mono text-xs text-fg-3">
            {hull.result.references.length} reference phase{hull.result.references.length === 1 ? "" : "s"} · {hull.result.umlip_backend}
            {hull.result.formation_energy_per_atom != null && ` · E_form ${hull.result.formation_energy_per_atom.toFixed(3)} eV/atom`}
          </div>
          {hull.result.phase_diagram && (
            <Button variant="subtle" className="w-full justify-start px-1.5 py-1 font-sans text-xs" onClick={() => showPane("phase")} data-show-phase>
              <Triangle size={11} /> Show the {hull.result.phase_diagram.elements.length === 3 ? "ternary" : hull.result.phase_diagram.elements.length === 2 ? "binary" : ""} phase diagram
            </Button>
          )}
          <p className="text-xs leading-snug text-fg-4">
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
        className="rounded-ctl p-0.5 text-fg-3 hover:bg-hover hover:text-fg disabled:opacity-30"
      >
        {hull.status === "busy" ? <Loader2 size={12} className="animate-spin text-fg-3" /> : <Calculator size={12} />}
      </button>
      {open && (
        <div className="absolute top-7 left-0 z-50">
          <HullPopoverView hull={hull} now={now} stale={stale} onEvaluate={() => void evaluateHull()} onClose={() => setOpen(false)} />
        </div>
      )}
    </span>
  );
}
