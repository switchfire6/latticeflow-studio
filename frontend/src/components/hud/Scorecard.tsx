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
import { useUi } from "../../store/uiStore";
import { committeeVerdict } from "../../utils/analysisC";
import { computeScorecard, scorecardSummary, type CheckId, type CheckState, type ScoreCheck } from "../../utils/scorecard";
import { Panel, cx } from "./ui";

const STATE_STYLE: Record<CheckState, { cls: string; label: string }> = {
  pass: { cls: "border-ok/50 bg-ok/15 text-ok", label: "pass" },
  fail: { cls: "border-bad/60 bg-bad/15 text-bad", label: "fail" },
  stale: { cls: "border-warn/50 bg-warn/10 text-warn", label: "stale" },
  notrun: { cls: "border-line bg-raised text-fg-4", label: "not run" },
  running: { cls: "border-accent/60 bg-accent-soft text-accent-fg", label: "running" },
  unavailable: { cls: "border-line bg-raised text-fg-4", label: "n/a" },
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
  dynamic: "Phonons",
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
  /** Phase C1: committee result for this structure (optional "Model agreement" line). */
  agreement?: { agree: boolean; text: string; stale: boolean } | null;
  onAgreement?(): void;
}

export function ScorecardView(p: ScorecardViewProps) {
  const sum = scorecardSummary(p.checks);
  const tone = { green: "text-ok", red: "text-bad", amber: "text-warn", slate: "text-fg-3" }[sum.tone];
  return (
    <Panel floating className="pointer-events-auto w-[310px] px-2.5 py-2">
      <button type="button" className="flex w-full items-center gap-1.5 px-0.5" onClick={p.onToggle} aria-expanded={!p.collapsed} data-scorecard-toggle>
        {p.collapsed ? <ChevronRight size={12} className="text-fg-4" /> : <ChevronDown size={12} className="text-fg-4" />}
        <ShieldQuestion size={13} className="text-fg-3" />
        <span className="text-2xs font-semibold tracking-[0.06em] whitespace-nowrap text-fg-3 uppercase">Stability</span>
        <span className={cx("ml-auto text-2xs whitespace-nowrap tabular-nums", tone)}>{sum.text}</span>
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
        <div className="mt-1.5 divide-y divide-line-soft" data-scorecard>
          {p.checks.map((c) => {
            const st = STATE_STYLE[c.state];
            const isRelax = c.id === "relaxed";
            const stop = isRelax && c.state === "running";
            return (
              <div key={c.id} className="py-1.5" data-check={c.id} data-state={c.state}>
                <div className="flex items-center gap-2">
                  <span className={cx("grid h-[18px] w-[18px] shrink-0 place-items-center rounded-full border", st.cls)} title={st.label}>
                    <StateIcon state={c.state} />
                  </span>
                  <div className="min-w-0 flex-1 leading-tight">
                    <div className="flex items-baseline gap-1.5">
                      <span className="text-xs font-medium text-fg">{c.title}</span>
                      <span className={cx("text-2xs font-medium", st.cls.split(" ").find((x) => x.startsWith("text-")))}>{st.label}</span>
                    </div>
                    <div className="truncate font-mono text-2xs text-fg-2" title={c.value}>
                      {c.value}
                    </div>
                    {c.detail && (
                      <div className="truncate text-2xs text-fg-4" title={c.detail}>
                        {c.detail}
                      </div>
                    )}
                    {c.warning && <div className="truncate text-2xs text-warn" title={c.warning}>⚠ {c.warning}</div>}
                  </div>
                  <button
                    type="button"
                    disabled={!p.canRun || (p.busy && !stop) || (c.state === "running" && !stop)}
                    onClick={() => p.onRun(c.id)}
                    title={stop ? "Stop the relaxation" : p.busy ? "Busy: wait for the running calculation" : `${RUN_LABEL[c.id]}${c.state === "pass" || c.state === "fail" || c.state === "stale" ? " (re-run)" : ""}`}
                    className={cx(
                      "grid h-6 w-6 shrink-0 place-items-center rounded-ctl transition-colors disabled:opacity-30",
                      stop ? "text-bad hover:bg-bad/15" : "text-fg-3 hover:bg-hover hover:text-fg",
                    )}
                    aria-label={stop ? "stop" : `run ${c.title}`}
                    data-run={c.id}
                  >
                    {stop ? <Square size={10} /> : <Play size={11} />}
                  </button>
                </div>
                {p.confirming === c.id && c.prerequisite && (
                  <div className="mt-1 rounded-ctl border border-warn/30 bg-warn/[0.07] p-1.5 text-xs text-warn" data-confirm>
                    <div className="mb-1">{c.prerequisite} — relax first?</div>
                    <div className="flex gap-1">
                      <button type="button" className="rounded-ctl border border-line-strong bg-raised px-1.5 py-0.5 text-fg hover:bg-hover" onClick={() => p.onConfirm("relax-then-run")}>
                        Relax, then {RUN_LABEL[c.id].toLowerCase()}
                      </button>
                      <button type="button" className="rounded-ctl border border-line px-1.5 py-0.5 text-fg-2 hover:bg-hover" onClick={() => p.onConfirm("run-anyway")}>
                        Run anyway
                      </button>
                      <button type="button" className="ml-auto px-1 text-fg-4 hover:text-fg" onClick={() => p.onConfirm("cancel")} aria-label="cancel">
                        <X size={11} />
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
          {p.agreement && (
            <button
              type="button"
              onClick={p.onAgreement}
              className="flex w-full items-center gap-2 py-1.5 text-left hover:bg-hover"
              data-check="agreement"
              title="Committee of models (Compare models); click for the table"
            >
              <span className={cx("grid h-[18px] w-[18px] shrink-0 place-items-center rounded-full border", STATE_STYLE[p.agreement.stale ? "stale" : p.agreement.agree ? "pass" : "fail"].cls)}>
                <StateIcon state={p.agreement.stale ? "stale" : p.agreement.agree ? "pass" : "fail"} />
              </span>
              <span className="min-w-0 flex-1 leading-tight">
                <span className="text-xs font-medium text-fg">Model agreement</span>
                <span className="block truncate font-mono text-2xs text-fg-2">{p.agreement.text}</span>
              </span>
            </button>
          )}
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
  const committee = useCrystalStore((s) => s.committee);
  const verdict = committee ? committeeVerdict(committee.result) : null;
  const agreement =
    committee && verdict
      ? {
          agree: !verdict.disagree,
          stale: committee.stateVersion !== stateVersion,
          text: verdict.disagree ? `models disagree: ${verdict.reasons.join("; ")}` : `${committee.result.entries.length} models agree · ΔV ${verdict.volumeSpreadPct != null ? verdict.volumeSpreadPct.toFixed(1) : "—"} %`,
        }
      : null;
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
      agreement={agreement}
      onAgreement={() => useUi.getState().openDialog("committee")}
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
