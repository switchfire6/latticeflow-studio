/**
 * Top telemetry bar (§6.3): formula | spacegroup (#n), E/atom, max |F| and
 * pressure with the stress-aware RELAXED badge (§3.2), bulk modulus (with an
 * on-demand "Compute K", §3.3) and E_hull when known, the dynamical-stability
 * badge after a shake-test, and the connection / uMLIP model indicator
 * (lj-fallback is shown prominently and watermarks K / E_hull, §6.4; Phase B:
 * the badge shows the session's model and opens the model picker, §6.1).
 */
import { Atom, Gauge, Loader2 } from "lucide-react";
import type { ReactNode } from "react";
import { displayedEHull } from "../../services/hullActions";
import { showPane } from "../../services/dockActions";
import { commands } from "../../services/wsClient";
import { badgeCriteria, useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import { F_TOL_THZ, type CrystalStatePayload } from "../../types/protocol";
import { relaxationVerdict, type RelaxationVerdict } from "../../utils/structureInfo";
import { formatFrequency, formatPressure, type PressureUnit } from "../../utils/units";
import { HullControl } from "./HullPopover";
import { ModelBadge } from "./ModelPicker";
import { Badge, FallbackTag, FormulaText, cx } from "./ui";

export { FormulaText };
export { RELAXED_FMAX } from "../../utils/structureInfo";

function Readout({ label, children, className, clip = true, title }: { label: ReactNode; children: ReactNode; className?: string; clip?: boolean; title?: string }) {
  return (
    <div className={cx("flex min-w-0 flex-col justify-center border-l border-white/[0.06] px-2.5 first:border-l-0 2xl:px-4", className)} title={title}>
      <span className="flex items-center gap-2 truncate text-[9px] font-semibold tracking-[0.18em] text-slate-500 uppercase">{label}</span>
      <span className={cx("flex items-center gap-2 font-mono text-[13px] whitespace-nowrap text-slate-100 tabular-nums", clip && "truncate")}>{children}</span>
    </div>
  );
}

const VOIGT = ["xx", "yy", "zz", "yz", "xz", "xy"] as const;

/** Multi-line tooltip text: P and the Voigt stress tensor. */
export function stressTooltip(c: Pick<CrystalStatePayload, "stress_gpa">, v: RelaxationVerdict, unit: PressureUnit): string {
  const lines = [
    v.pressureGpa !== null ? `P = −tr(σ)/3 = ${formatPressure(v.pressureGpa, unit)} (+ = compressed)` : "no stress tensor",
    `σ (Voigt, ASE sign, + = tensile):`,
    ...VOIGT.map((k, i) => `  σ_${k} = ${formatPressure(c.stress_gpa[i] ?? NaN, unit, 3, { sign: false })}`),
  ];
  if (v.maxStressDeviation !== null) {
    lines.push(
      `max |σ_ij + P_ext δ_ij| = ${v.maxStressDeviation.toFixed(3)} GPa (tolerance ${v.criteria.stressTolGpa} GPa, P_ext = ${v.criteria.targetPressureGpa} GPa)`,
    );
  }
  lines.push(`max |F| = ${v.maxForce.toFixed(4)} eV/Å (fmax ${v.criteria.fmax})`);
  return lines.join("\n");
}

export function RelaxationBadge({ verdict }: { verdict: RelaxationVerdict }) {
  if (verdict.state === "relaxed") {
    return (
      <Badge tone="green" title="Forces and cell stress below tolerance">
        RELAXED
      </Badge>
    );
  }
  if (verdict.state === "stressed") {
    return (
      <Badge tone="amber" title="Atomic forces are converged but the cell is under stress: relax with 'relax cell' on">
        FORCES OK · CELL STRESSED
      </Badge>
    );
  }
  return (
    <Badge tone="amber" title={`max |F| ≥ ${verdict.criteria.fmax} eV/Å`}>
      UNRELAXED
    </Badge>
  );
}

function EHullReadout() {
  const payloadEHull = useCrystalStore((s) => s.crystal?.e_hull_ev ?? null);
  const hull = useCrystalStore((s) => s.hull);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const shown = displayedEHull(payloadEHull, hull, stateVersion);
  return (
    <Readout label="E hull" clip={false}>
      {shown ? (
        <span className={shown.stale ? "text-slate-500" : ""} title={shown.stale ? "computed for an earlier structure" : undefined}>
          {shown.value.toFixed(3)} eV/atom
        </span>
      ) : hull.status === "busy" ? (
        <span className="text-cyan-300">evaluating…</span>
      ) : hull.status === "unavailable" ? (
        <span className="text-amber-300/80" title={hull.message}>
          n/a
        </span>
      ) : (
        <span className="text-slate-500">—</span>
      )}
      {shown && <FallbackTag compact />}
      <HullControl />
    </Readout>
  );
}

/** Bulk modulus readout with an on-demand "Compute K" (COMPUTE_BULK_MODULUS) that opens the EOS pane. */
function BulkModulusReadout() {
  const k = useCrystalStore((s) => s.crystal?.bulk_modulus_gpa ?? null);
  const pending = useCrystalStore((s) => s.eosPending);
  const connected = useCrystalStore((s) => s.connection === "open");
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  return (
    <Readout label="Bulk Modulus K" clip={false}>
      {k != null ? <span>{k.toFixed(1)} GPa</span> : <span className="text-slate-500">—</span>}
      {k != null && <FallbackTag compact />}
      <button
        type="button"
        disabled={!connected || !hasCrystal || pending}
        title="Compute K from an E(V) fit (clamped-ion) and show the equation of state"
        onClick={() => {
          if (commands.computeBulkModulus()) showPane("eos");
        }}
        className="rounded p-0.5 text-slate-400 hover:bg-white/5 hover:text-cyan-200 disabled:opacity-30"
      >
        {pending ? <Loader2 size={12} className="animate-spin text-cyan-300" /> : <Gauge size={12} />}
      </button>
    </Readout>
  );
}

function StabilityBadge() {
  const phonon = useCrystalStore((s) => s.phonon);
  const unit = useSettings((s) => s.units.frequency);
  if (!phonon) return <span className="text-slate-500">not tested</span>;
  if (phonon.is_dynamically_stable) {
    return (
      <Badge tone="green" title={`min f = ${formatFrequency(phonon.min_frequency_thz, unit, 3)} (tolerance ${F_TOL_THZ} THz)`}>
        STABLE (All ω² &gt; 0)
      </Badge>
    );
  }
  const n = phonon.imaginary_mode_count;
  return (
    <Badge tone="red" pulse title="Imaginary modes: the structure is a saddle point of the potential-energy surface">
      UNSTABLE ({n} Imaginary Mode{n === 1 ? "" : "s"}: {formatFrequency(phonon.min_frequency_thz, unit)})
    </Badge>
  );
}

function ConnectionIndicator() {
  const connection = useCrystalStore((s) => s.connection);
  const attempt = useCrystalStore((s) => s.reconnectAttempt);
  const dot = connection === "open" ? "bg-emerald-400 shadow-[0_0_8px_#34d399]" : connection === "connecting" ? "bg-amber-400 animate-pulse" : "bg-rose-500";
  const label = connection === "open" ? "live" : connection === "connecting" ? "connecting" : attempt > 0 ? `retry #${attempt}` : "offline";
  return (
    <div className="flex shrink-0 items-center gap-3 pr-4">
      <ModelBadge />
      <span className="flex items-center gap-1.5 font-mono text-[11px] text-slate-400">
        <span className={cx("h-2 w-2 rounded-full", dot)} />
        {label}
      </span>
    </div>
  );
}

export function TelemetryHeader() {
  const crystal = useCrystalStore((s) => s.crystal);
  const relax = useSettings((s) => s.relax);
  const pUnit = useSettings((s) => s.units.pressure);
  const verdict = crystal ? relaxationVerdict(crystal, badgeCriteria(relax)) : null;
  return (
    <header className="relative z-30 flex h-14 items-stretch border-b border-white/[0.06] bg-slate-950/80 backdrop-blur-md">
      <div className="flex shrink-0 items-center gap-2.5 border-r border-white/[0.06] px-3 2xl:w-[320px] 2xl:px-4" title="LatticeFlow Studio">
        <div className="grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-cyan-400/30 to-rose-500/20 ring-1 ring-cyan-400/30">
          <Atom size={18} className="text-cyan-300" />
        </div>
        <div className="hidden leading-tight 2xl:block">
          <div className="text-sm font-semibold tracking-wide text-slate-100">LatticeFlow Studio</div>
          <div className="text-[10px] tracking-wider text-slate-500">inverse crystal &amp; phonon discovery</div>
        </div>
      </div>
      <div className="flex min-w-0 flex-1 items-stretch py-2">
        <Readout label="Formula · Symmetry">
          {crystal ? (
            <>
              <span className="font-semibold text-white">
                <FormulaText formula={crystal.formula} />
              </span>
              <span className="text-slate-500">|</span>
              <span>
                {crystal.spacegroup_symbol} <span className="text-slate-400">(#{crystal.spacegroup_number})</span>
              </span>
            </>
          ) : (
            <span className="text-slate-500">—</span>
          )}
        </Readout>
        <Readout label="uMLIP Energy">{crystal ? `${crystal.energy_per_atom.toFixed(3)} eV/atom` : "—"}</Readout>
        <Readout
          label={
            <>
              <span>Max |F| · P</span>
              {verdict && <RelaxationBadge verdict={verdict} />}
            </>
          }
          clip={false}
          className="shrink-0"
          title={crystal && verdict ? stressTooltip(crystal, verdict, pUnit) : undefined}
        >
          {crystal && verdict ? (
            <>
              <span>{crystal.max_force.toFixed(3)} eV/Å</span>
              {verdict.pressureGpa !== null && (
                <span className={verdict.state === "stressed" ? "text-amber-200" : "text-slate-300"} data-pressure>
                  {formatPressure(verdict.pressureGpa, pUnit, pUnit === "GPa" ? 2 : 1)}
                </span>
              )}
            </>
          ) : (
            "—"
          )}
        </Readout>
        <BulkModulusReadout />
        <EHullReadout />
        <Readout label="Dynamical Stability" className="min-w-[180px]">
          <StabilityBadge />
        </Readout>
      </div>
      <ConnectionIndicator />
    </header>
  );
}
