/**
 * Stability scorecard (§7.1), the product's core question "is this crystal
 * real?", as a viewport overlay (top right): Relaxed, Dynamically stable,
 * Mechanically stable and Thermodynamically stable, each with its state, key
 * number and a run button. A run whose prerequisite is not met asks first
 * ("Cell under +5.10 GPa — relax first?") and offers Relax-then-run.
 * Status logic: utils/scorecard.ts (pure, tested); `ScorecardView` is pure.
 */
import { Check, ChevronDown, ChevronRight, CircleDashed, Clock, Loader2, Minus, Play, ShieldQuestion, Square, X } from "lucide-react";
import { useState } from "react";
import { showPane } from "../../services/dockActions";
import { evaluateHull } from "../../services/hullActions";
import { commands } from "../../services/wsClient";
import { badgeCriteria, useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import { computeScorecard, scorecardSummary, type CheckId, type CheckState, type ScoreCheck } from "../../utils/scorecard";
import { Panel, cx } from "./ui";

const STATE_STYLE: Record<CheckState, { cls: string; label: string }> = {
  pass: { cls: "border-emerald-400/50 bg-emerald-400/15 text-emerald-300", label: "pass" },
  fail: { cls: "border-rose-500/60 bg-rose-500/15 text-rose-300", label: "fail" },
  stale: { cls: "border-amber-400/50 bg-amber-400/10 text-amber-300", label: "stale" },
  notrun: { cls: "border-white/10 bg-white/[0.03] text-slate-500", label: "not run" },
  running: { cls: "border-cyan-400/50 bg-cyan-400/10 text-cyan-200", label: "running" },
  unavailable: { cls: "border-white/10 bg-white/[0.03] text-slate-500", label: "n/a" },
};

function StateIcon({ state }: { state: CheckState }) {
  const s = 11;
  switch (state) {
    case "pass":
      return <Check size={s} />;
    case "fail":
      return <X size={s} />;
    case "stale":
      return <Clock size={s} />;
    case "running":
      return <Loader2 size={s} className="animate-spin" />;
    case "unavailable":
      return <Minus size={s} />;
    default:
      return <CircleDashed size={s} />;
  }
}

const RUN_LABEL: Record<CheckId, string> = {
  relaxed: "Relax",
  dynamic: "Shake-test",
  mechanical: "Elastic tensor",
  thermo: "Evaluate E_hull",
};

export interface ScorecardViewProps {
  checks: ScoreCheck[];
  collapsed: boolean;
  /** A run is possible (connected, structure loaded). */
  canRun: boolean;
  /** Another stream is running (runs other than "stop" are disabled). */
  busy: boolean;
  confirming: CheckId | null;
  onToggle(): void;
  onRun(id: CheckId): void;
  onConfirm(choice: "relax-then-run" | "run-anyway" | "cancel"): void;
}

export function ScorecardView(p: ScorecardViewProps) {
  const sum = scorecardSummary(p.checks);
  const tone = { green: "text-emerald-300", red: "text-rose-300", amber: "text-amber-300", slate: "text-slate-400" }[sum.tone];
  return (
    <Panel className="pointer-events-auto w-[310px] p-2">
      <button type="button" className="flex w-full items-center gap-1.5 px-0.5" onClick={p.onToggle} aria-expanded={!p.collapsed} data-scorecard-toggle>
        {p.collapsed ? <ChevronRight size={12} className="text-slate-500" /> : <ChevronDown size={12} className="text-slate-500" />}
        <ShieldQuestion size={13} className="text-cyan-300" />
        <span className="text-[10px] font-semibold tracking-[0.1em] whitespace-nowrap text-slate-300 uppercase">Is this crystal real?</span>
        <span className={cx("ml-auto font-mono text-[10px] whitespace-nowrap", tone)}>{sum.text}</span>
        {p.collapsed && (
          <span className="ml-1 flex gap-0.5">
            {p.checks.map((c) => (
              <span key={c.id} className={cx("grid h-3.5 w-3.5 place-items-center rounded-full border", STATE_STYLE[c.state].cls)} title={`${c.title}: ${STATE_STYLE[c.state].label}`}>
                <StateIcon state={c.state} />
              </span>
            ))}
          </span>
        )}
      </button>
      {!p.collapsed && (
        <div className="mt-1.5 space-y-1" data-scorecard>
          {p.checks.map((c) => {
            const st = STATE_STYLE[c.state];
            const isRelax = c.id === "relaxed";
            const stop = isRelax && c.state === "running";
            return (
              <div key={c.id} className="rounded-lg border border-white/[0.05] bg-white/[0.015] px-1.5 py-1" data-check={c.id} data-state={c.state}>
                <div className="flex items-center gap-1.5">
                  <span className={cx("grid h-[18px] w-[18px] shrink-0 place-items-center rounded-full border", st.cls)} title={st.label}>
                    <StateIcon state={c.state} />
                  </span>
                  <div className="min-w-0 flex-1 leading-tight">
                    <div className="flex items-baseline gap-1.5">
                      <span className="text-[11px] font-medium text-slate-100">{c.title}</span>
                      <span className={cx("font-mono text-[9px] uppercase", st.cls.split(" ").find((x) => x.startsWith("text-")))}>{st.label}</span>
                    </div>
                    <div className="truncate font-mono text-[10px] text-slate-300" title={c.value}>
                      {c.value}
                    </div>
                    {c.detail && (
                      <div className="truncate font-mono text-[9.5px] text-slate-500" title={c.detail}>
                        {c.detail}
                      </div>
                    )}
                    {c.warning && <div className="truncate font-mono text-[9.5px] text-amber-300/90" title={c.warning}>⚠ {c.warning}</div>}
                  </div>
                  <button
                    type="button"
                    disabled={!p.canRun || (p.busy && !stop) || (c.state === "running" && !stop)}
                    onClick={() => p.onRun(c.id)}
                    title={stop ? "Stop the relaxation" : p.busy ? "Busy: wait for the running calculation" : `${RUN_LABEL[c.id]}${c.state === "pass" || c.state === "fail" || c.state === "stale" ? " (re-run)" : ""}`}
                    className={cx(
                      "grid h-6 w-6 shrink-0 place-items-center rounded-md border transition-colors disabled:opacity-30",
                      stop ? "border-rose-500/50 text-rose-300 hover:bg-rose-500/15" : "border-white/10 text-slate-300 hover:border-cyan-400/50 hover:text-cyan-100",
                    )}
                    aria-label={stop ? "stop" : `run ${c.title}`}
                    data-run={c.id}
                  >
                    {stop ? <Square size={10} /> : <Play size={11} />}
                  </button>
                </div>
                {p.confirming === c.id && c.prerequisite && (
                  <div className="mt-1 rounded-md border border-amber-400/30 bg-amber-400/[0.07] p-1.5 text-[10.5px] text-amber-100" data-confirm>
                    <div className="mb-1">{c.prerequisite} — relax first?</div>
                    <div className="flex gap-1">
                      <button type="button" className="rounded border border-cyan-400/50 bg-cyan-400/15 px-1.5 py-0.5 text-cyan-100 hover:bg-cyan-400/25" onClick={() => p.onConfirm("relax-then-run")}>
                        Relax, then {RUN_LABEL[c.id].toLowerCase()}
                      </button>
                      <button type="button" className="rounded border border-white/10 px-1.5 py-0.5 text-slate-300 hover:bg-white/5" onClick={() => p.onConfirm("run-anyway")}>
                        Run anyway
                      </button>
                      <button type="button" className="ml-auto px-1 text-slate-500 hover:text-slate-200" onClick={() => p.onConfirm("cancel")} aria-label="cancel">
                        <X size={11} />
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

/** Start the calculation behind a check. */
export function runCheck(id: CheckId): void {
  switch (id) {
    case "relaxed":
      commands.relax();
      return;
    case "dynamic":
      commands.runPhonons();
      return;
    case "mechanical":
      if (commands.computeElastic()) showPane("elastic");
      return;
    case "thermo":
      void evaluateHull();
      return;
  }
}

export function Scorecard() {
  const crystal = useCrystalStore((s) => s.crystal);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const streams = useCrystalStore((s) => s.streams);
  const phononSummary = useCrystalStore((s) => s.phononSummary);
  const elastic = useCrystalStore((s) => s.elastic);
  const hull = useCrystalStore((s) => s.hull);
  const connected = useCrystalStore((s) => s.connection === "open");
  const relax = useSettings((s) => s.relax);
  const units = useSettings((s) => s.units);
  const collapsed = useSettings((s) => !!s.collapsed.scorecard);
  const [confirming, setConfirming] = useState<CheckId | null>(null);
  const checks = computeScorecard({
    crystal,
    criteria: badgeCriteria(relax),
    stateVersion,
    streams,
    phononSummary,
    elastic,
    hull,
    frequencyUnit: units.frequency,
    pressureUnit: units.pressure,
  });
  const busy = streams.length > 0;
  return (
    <ScorecardView
      checks={checks}
      collapsed={collapsed}
      canRun={connected && !!crystal}
      busy={busy}
      confirming={confirming}
      onToggle={() => useSettings.getState().setCollapsed("scorecard", !collapsed)}
      onRun={(id) => {
        const c = checks.find((x) => x.id === id)!;
        if (id === "relaxed" && c.state === "running") {
          commands.stop();
          return;
        }
        if (c.prerequisite) {
          setConfirming(confirming === id ? null : id);
          return;
        }
        setConfirming(null);
        runCheck(id);
      }}
      onConfirm={(choice) => {
        const id = confirming;
        setConfirming(null);
        if (!id || choice === "cancel") return;
        if (choice === "run-anyway") runCheck(id);
        else commands.relaxThen(RUN_LABEL[id], () => runCheck(id));
      }}
    />
  );
}
