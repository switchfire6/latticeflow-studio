/**
 * Overlays drawn on top of the viewport: tool-mode switcher, display toggles,
 * stream progress (flow k/K, relaxation step + max |F|, phonon running) with a
 * Stop button, and the active-eigenmode pill.
 */
import { Eye, GitBranch, Hand, Loader2, Redo2, Replace, Square, Undo2, Waves, X } from "lucide-react";
import { followStatusText, transitionText } from "../../utils/followText";
import { commands } from "../../services/wsClient";
import { useSettings } from "../../store/settings";
import { selectActiveStream, useCrystalStore, type ToolMode } from "../../store/useCrystalStore";
import { FREQUENCY_UNITS, formatFrequency, frequencyUnitLabel, type FrequencyUnit, type PressureUnit } from "../../utils/units";
import { Button, Panel, Segmented, Toggle, cx } from "./ui";

const MODES: { mode: ToolMode; label: string; icon: typeof Eye; hint: string }[] = [
  { mode: "VIEW", label: "View", icon: Eye, hint: "Orbit and click atoms to inspect them" },
  { mode: "DRAG_ATOM", label: "Drag", icon: Hand, hint: "Grab an atom and pull it; forces update live" },
  { mode: "TRANSMUTE", label: "Transmute", icon: Replace, hint: "Click an atom to change its element" },
  { mode: "PHONON_SHAKE", label: "Phonon", icon: Waves, hint: "Animate the selected phonon eigenmode" },
];

/** Undo / redo (server history, HISTORY_STATE); the tooltip names the current entry. */
export function HistoryButtons() {
  const history = useCrystalStore((s) => s.history);
  const connected = useCrystalStore((s) => s.connection === "open");
  const label = history?.label ? `current: ${history.label}` : "";
  const pos = history ? ` (${history.position + 1}/${history.length})` : "";
  const cls = "flex items-center rounded-lg px-2 py-1.5 text-slate-400 transition-colors hover:bg-white/5 hover:text-slate-200 disabled:opacity-30";
  return (
    <>
      <button type="button" className={cls} disabled={!connected || !history?.can_undo} onClick={() => commands.undo()} title={`Undo (Ctrl+Z)${pos}${label ? ` · ${label}` : ""}`} aria-label="undo">
        <Undo2 size={13} />
      </button>
      <button type="button" className={cls} disabled={!connected || !history?.can_redo} onClick={() => commands.redo()} title={`Redo (Ctrl+Shift+Z / Ctrl+Y)${pos}${label ? ` · ${label}` : ""}`} aria-label="redo">
        <Redo2 size={13} />
      </button>
    </>
  );
}

export function ToolModeBar() {
  const toolMode = useCrystalStore((s) => s.toolMode);
  const hasPhonon = useCrystalStore((s) => s.phonon !== null);
  return (
    <Panel className="pointer-events-auto flex gap-1 p-1">
      <HistoryButtons />
      <span className="mx-0.5 w-px self-stretch bg-white/10" />
      {MODES.map(({ mode, label, icon: Icon, hint }) => {
        const disabled = mode === "PHONON_SHAKE" && !hasPhonon;
        return (
          <button
            key={mode}
            type="button"
            title={disabled ? "Run the phonon shake-test first" : hint}
            disabled={disabled}
            onClick={() => useCrystalStore.getState().setToolMode(mode)}
            className={cx(
              "flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px] font-medium transition-colors disabled:opacity-35",
              toolMode === mode ? "bg-cyan-400/20 text-cyan-100 ring-1 ring-cyan-400/40" : "text-slate-400 hover:bg-white/5 hover:text-slate-200",
            )}
          >
            <Icon size={13} />
            {label}
          </button>
        );
      })}
    </Panel>
  );
}

export function DisplayPanel() {
  const d = useCrystalStore((s) => s.display);
  const setDisplay = useCrystalStore((s) => s.setDisplay);
  return (
    <Panel className="pointer-events-auto flex flex-wrap items-center gap-1 p-1.5">
      <Toggle
        label={d.representation === "ball-and-stick" ? "Ball & stick" : "Space-filling"}
        checked
        title="Toggle ball-and-stick (0.45 × covalent radius) / space-filling (1.0 ×)"
        onChange={() => setDisplay({ representation: d.representation === "ball-and-stick" ? "space-filling" : "ball-and-stick" })}
      />
      <Toggle label="2×2×2 halo" checked={d.ghostHalo} onChange={(ghostHalo) => setDisplay({ ghostHalo })} />
      <Toggle label="Bonds" checked={d.bonds} onChange={(bonds) => setDisplay({ bonds })} />
      <Toggle label="Forces" checked={d.forceVectors} onChange={(forceVectors) => setDisplay({ forceVectors })} />
      <Toggle label="Trails" checked={d.trails} onChange={(trails) => setDisplay({ trails })} />
      <Toggle label="Cell" checked={d.unitCell} onChange={(unitCell) => setDisplay({ unitCell })} />
      <Toggle
        label="FX"
        title="Post-processing (bloom + ambient occlusion). Turn off on low-end GPUs."
        checked={d.postprocessing}
        onChange={(postprocessing) => setDisplay({ postprocessing })}
      />
      <UnitsToggle />
    </Panel>
  );
}

/** Display units: phonon frequency and pressure (persisted). */
export function UnitsToggle() {
  const units = useSettings((s) => s.units);
  const setUnits = useSettings((s) => s.setUnits);
  return (
    <span className="flex items-center gap-1 border-l border-white/10 pl-1.5" data-units>
      <Segmented<FrequencyUnit>
        value={units.frequency}
        title="Phonon frequency unit"
        options={FREQUENCY_UNITS.map((u) => ({ value: u, label: frequencyUnitLabel(u) }))}
        onChange={(frequency) => setUnits({ frequency })}
      />
      <Segmented<PressureUnit>
        value={units.pressure}
        title="Pressure unit"
        options={[
          { value: "GPa", label: "GPa" },
          { value: "kbar", label: "kbar" },
        ]}
        onChange={(pressure) => setUnits({ pressure })}
      />
    </span>
  );
}

export function StreamProgress() {
  const stream = useCrystalStore(selectActiveStream);
  const supercell = useCrystalStore((s) => s.session.phononSupercell);
  if (!stream) return null;
  const p = stream.progress;
  let title: string;
  let detail: string;
  let frac: number | null = null;
  if (stream.kind === "flow") {
    title = "Riemannian flow";
    detail = p.step !== null && p.totalSteps !== null ? `step ${p.step}/${p.totalSteps} · t = ${(p.t ?? 0).toFixed(2)}` : "sampling prior…";
    if (p.step !== null && p.totalSteps) frac = p.step / p.totalSteps;
    if (p.step !== null && p.step === p.totalSteps) detail += " · finalising (CrystalNN, K)";
  } else if (stream.kind === "relaxation") {
    title = stream.command === "FOLLOW_INSTABILITY" ? "Following instability" : "FIRE relaxation";
    detail =
      p.step !== null
        ? `step ${p.step} · max |F| = ${(p.maxForce ?? 0).toFixed(3)} eV/Å`
        : "starting…";
  } else {
    title = "Phonon shake-test";
    detail = `${supercell.join("×")} supercell · finite displacements`;
  }
  return (
    <Panel className="pointer-events-auto flex min-w-[320px] items-center gap-3 px-3 py-2">
      <Loader2 size={15} className="shrink-0 animate-spin text-cyan-300" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-[12px] font-semibold text-slate-100">{title}</span>
          <span className="font-mono text-[11px] text-slate-400 tabular-nums">{detail}</span>
        </div>
        <div className="mt-1 h-1 overflow-hidden rounded-full bg-white/5">
          {frac !== null ? (
            <div className="h-full rounded-full bg-gradient-to-r from-cyan-400 to-sky-400 transition-[width] duration-100" style={{ width: `${Math.round(frac * 100)}%` }} />
          ) : (
            <div className="lf-indeterminate h-full w-1/3 rounded-full bg-cyan-400/70" />
          )}
        </div>
      </div>
      <Button variant="danger" className="px-2 py-1" disabled={stream.stopping} onClick={() => commands.stop()}>
        <Square size={11} /> {stream.stopping ? "Stopping" : "Stop"}
      </Button>
    </Panel>
  );
}

/** "Pm-3m (#221) → I4/mcm (#140) · −14.4 meV/atom" after Follow Instability. */
export function FollowBanner() {
  const r = useCrystalStore((s) => s.followResult);
  const unit = useSettings((s) => s.units.frequency);
  if (!r) return null;
  const done = r.status === "converged" || r.status === "max_steps";
  return (
    <Panel className={cx("pointer-events-auto flex items-center gap-2.5 px-3 py-2", done && "border-emerald-400/30")}>
      {r.status === "running" ? <Loader2 size={14} className="animate-spin text-rose-300" /> : <GitBranch size={14} className={done ? "text-emerald-300" : "text-amber-300"} />}
      <div className="leading-tight">
        <div className="font-mono text-[12px] text-slate-100">{transitionText(r)}</div>
        <div className="font-mono text-[10px] text-slate-400">
          Follow Instability · {r.qLabel ?? "q"} ν{r.bandIndex} ({formatFrequency(r.frequencyThz, unit)}), {r.amplitude} Å · {followStatusText(r)}
        </div>
      </div>
      <button type="button" className="ml-1 text-slate-500 hover:text-slate-200" onClick={() => useCrystalStore.getState().setFollowResult(null)} aria-label="dismiss">
        <X size={12} />
      </button>
    </Panel>
  );
}

export function EigenmodePill() {
  const mode = useCrystalStore((s) => s.selectedEigenmode);
  const toolMode = useCrystalStore((s) => s.toolMode);
  const unit = useSettings((s) => s.units.frequency);
  if (!mode) return null;
  const label = mode.q_label ? mode.q_label.replace("\\Gamma", "Γ").replace("GAMMA", "Γ") : `q = (${mode.q_frac.map((x) => x.toFixed(2)).join(", ")})`;
  return (
    <Panel className="pointer-events-auto flex items-center gap-2 px-3 py-1.5">
      <Waves size={13} className={mode.is_imaginary ? "text-rose-400" : "text-cyan-300"} />
      <span className="font-mono text-[11px] text-slate-200">
        {label} · band {mode.band_index} · f = <span className={mode.is_imaginary ? "text-rose-300" : ""}>{formatFrequency(mode.frequency_thz, unit)}</span>
      </span>
      {toolMode !== "PHONON_SHAKE" && <span className="text-[10px] text-slate-500">(paused: Phonon mode animates)</span>}
      <button
        type="button"
        className="ml-1 rounded px-1 text-[10px] text-slate-400 hover:bg-white/5 hover:text-slate-200"
        onClick={() => useCrystalStore.getState().selectEigenmode(null)}
        title="Stop the eigenmode animation"
      >
        <X size={11} />
      </button>
    </Panel>
  );
}
