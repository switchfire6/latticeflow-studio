/**
 * Overlays drawn on top of the viewport: tool-mode switcher, display toggles,
 * stream progress (flow k/K, relaxation step + max |F|, phonon running) with a
 * Stop button, and the active-eigenmode pill.
 */
import { Eye, GitBranch, GitCompare, Hand, Loader2, Redo2, Replace, SlidersHorizontal, Square, Undo2, Waves, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { followStatusText, transitionText } from "../../utils/followText";
import { compareWithFollowParent } from "../../services/compareActions";
import { commands } from "../../services/wsClient";
import { useSettings } from "../../store/settings";
import { selectActiveStream, useCrystalStore, type BondColoring, type ToolMode } from "../../store/useCrystalStore";
import { FREQUENCY_UNITS, formatFrequency, frequencyUnitLabel, type FrequencyUnit, type PressureUnit } from "../../utils/units";
import { Button, Panel, Segmented, Toggle, cx } from "./ui";

const MODES: { mode: ToolMode; label: string; icon: typeof Eye; hint: string }[] = [
  { mode: "VIEW", label: "View", icon: Eye, hint: "Orbit and click atoms to inspect them" },
  { mode: "DRAG_ATOM", label: "Drag", icon: Hand, hint: "Grab an atom and pull it; forces update live" },
  { mode: "TRANSMUTE", label: "Edit", icon: Replace, hint: "Click an atom to change its element" },
  { mode: "PHONON_SHAKE", label: "Phonon", icon: Waves, hint: "Animate the selected phonon eigenmode" },
];

/** Undo / redo (server history, HISTORY_STATE); the tooltip names the current entry. */
export function HistoryButtons() {
  const history = useCrystalStore((s) => s.history);
  const connected = useCrystalStore((s) => s.connection === "open");
  const label = history?.label ? `current: ${history.label}` : "";
  const pos = history ? ` (${history.position + 1}/${history.length})` : "";
  const cls = "flex items-center rounded-ctl px-2 py-1 text-fg-3 transition-colors hover:bg-hover hover:text-fg disabled:opacity-30";
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
    <Panel floating className="pointer-events-auto flex gap-0.5 p-1">
      <HistoryButtons />
      <GroupRule />
      {MODES.map(({ mode, label, icon: Icon, hint }) => {
        const disabled = mode === "PHONON_SHAKE" && !hasPhonon;
        return (
          <button
            key={mode}
            type="button"
            title={disabled ? "Run phonons first" : hint}
            disabled={disabled}
            onClick={() => useCrystalStore.getState().setToolMode(mode)}
            className={cx(
              "flex items-center gap-1.5 rounded-ctl px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-35",
              toolMode === mode ? "bg-accent-soft text-accent-fg" : "text-fg-3 hover:bg-hover hover:text-fg",
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

/** Thin divider between control groups. */
function GroupRule() {
  return <span className="mx-1 w-px self-stretch bg-line" />;
}

export function DisplayPanel() {
  const d = useCrystalStore((s) => s.display);
  const setDisplay = useCrystalStore((s) => s.setDisplay);
  return (
    <Panel floating className="pointer-events-auto flex items-center gap-0.5 p-1">
      <Segmented<"ball-and-stick" | "space-filling">
        value={d.representation}
        title="Ball-and-stick (0.45 × covalent radius) or space-filling (1.0 ×)"
        options={[
          { value: "ball-and-stick", label: "Ball & stick" },
          { value: "space-filling", label: "Space-filling" },
        ]}
        onChange={(representation) => setDisplay({ representation })}
      />
      <GroupRule />
      <Toggle label="Cell" checked={d.unitCell} onChange={(unitCell) => setDisplay({ unitCell })} />
      <Toggle label="Bonds" checked={d.bonds} onChange={(bonds) => setDisplay({ bonds })} />
      {d.bonds && (
        <Segmented<BondColoring>
          value={d.bondColoring}
          title="Bond colour: covalent-radius strain, or bond-valence deviation (length − ideal)/ideal from the local analysis (blue short · orange long)"
          options={[
            { value: "strain", label: "strain", title: "(length − r_cov sum) / r_cov sum: ionic bonds read as compressed" },
            { value: "valence", label: "BV", title: "Bond-valence deviation (length − ideal) / ideal: physically meaningful for ionic bonds" },
          ]}
          onChange={(bondColoring) => setDisplay({ bondColoring })}
        />
      )}
      <Toggle label="Polyhedra" title="Translucent coordination polyhedra (from the local analysis), coloured by the centre element" checked={d.polyhedra} onChange={(polyhedra) => setDisplay({ polyhedra })} />
      <GroupRule />
      <Toggle label="Forces" checked={d.forceVectors} onChange={(forceVectors) => setDisplay({ forceVectors })} />
      <Toggle label="Trails" checked={d.trails} onChange={(trails) => setDisplay({ trails })} />
      <Toggle label="2×2×2 halo" title="Periodic images around the cell" checked={d.ghostHalo} onChange={(ghostHalo) => setDisplay({ ghostHalo })} />
      <GroupRule />
      <DisplayMenu />
    </Panel>
  );
}

/** Rarely changed settings (post-processing, units) behind a small popover. */
function DisplayMenu() {
  const d = useCrystalStore((s) => s.display);
  const setDisplay = useCrystalStore((s) => s.setDisplay);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-label="display settings"
        aria-expanded={open}
        title="Rendering and units"
        onClick={() => setOpen((o) => !o)}
        className={cx("flex items-center rounded-ctl px-1.5 py-1 transition-colors hover:bg-hover hover:text-fg", open ? "bg-accent-soft text-accent-fg" : "text-fg-3")}
      >
        <SlidersHorizontal size={13} />
      </button>
      {open && (
        <Panel floating className="absolute bottom-full left-0 mb-2 grid w-max gap-2 p-2.5 text-xs text-fg-3">
          <div className="flex items-center justify-between gap-4">
            <span title="Post-processing (bloom + ambient occlusion). Turn off on low-end GPUs.">Bloom &amp; AO</span>
            <Toggle
              label={d.postprocessing ? "on" : "off"}
              title="Post-processing (bloom + ambient occlusion). Turn off on low-end GPUs."
              checked={d.postprocessing}
              onChange={(postprocessing) => setDisplay({ postprocessing })}
            />
          </div>
          <UnitsToggle />
        </Panel>
      )}
    </div>
  );
}

/** Display units: phonon frequency and pressure (persisted). */
export function UnitsToggle() {
  const units = useSettings((s) => s.units);
  const setUnits = useSettings((s) => s.setUnits);
  return (
    <div className="grid gap-2" data-units>
      <div className="flex items-center justify-between gap-4">
        <span>Frequency</span>
        <Segmented<FrequencyUnit>
          value={units.frequency}
          title="Phonon frequency unit"
          options={FREQUENCY_UNITS.map((u) => ({ value: u, label: frequencyUnitLabel(u) }))}
          onChange={(frequency) => setUnits({ frequency })}
        />
      </div>
      <div className="flex items-center justify-between gap-4">
        <span>Pressure</span>
        <Segmented<PressureUnit>
          value={units.pressure}
          title="Pressure unit"
          options={[
            { value: "GPa", label: "GPa" },
            { value: "kbar", label: "kbar" },
          ]}
          onChange={(pressure) => setUnits({ pressure })}
        />
      </div>
    </div>
  );
}

export function StreamProgress() {
  const stream = useCrystalStore(selectActiveStream);
  const supercell = useCrystalStore((s) => s.session.phononSupercell);
  const minLength = useCrystalStore((s) => s.session.phononMinLength);
  if (!stream) return null;
  const p = stream.progress;
  let title: string;
  let detail: string;
  let frac: number | null = null;
  if (stream.kind === "flow") {
    title = "Symmetric candidate search";
    detail = p.step !== null && p.totalSteps !== null ? `step ${p.step}/${p.totalSteps} · t = ${(p.t ?? 0).toFixed(2)}` : "sampling prior…";
    if (p.step !== null && p.totalSteps) frac = p.step / p.totalSteps;
    if (p.step !== null && p.step === p.totalSteps) detail += " · finalising (CrystalNN, K)";
  } else if (stream.kind === "relaxation") {
    title = stream.command === "FOLLOW_INSTABILITY" ? "Following instability" : "Relaxation";
    detail =
      p.step !== null
        ? `step ${p.step} · max |F| = ${(p.maxForce ?? 0).toFixed(3)} eV/Å`
        : "starting…";
  } else if (stream.command === "CHECK_PHONON_CONVERGENCE") {
    title = "Phonon convergence check";
    detail = p.message || "next larger supercell";
  } else if (stream.kind === "elastic") {
    title = "Elastic tensor";
    detail = p.message || "straining the cell";
  } else if (stream.kind === "model") {
    title = "Switching model";
    detail = p.message || "loading the checkpoint";
  } else if (stream.kind === "md") {
    title = "Molecular dynamics";
    detail = p.step !== null ? `step ${p.step}${p.totalSteps ? `/${p.totalSteps}` : ""}` : "thermalising…";
    if (p.step !== null && p.totalSteps) frac = Math.min(1, p.step / p.totalSteps);
  } else if (stream.kind === "formation") {
    title = "Defect formation energy";
    detail = p.message || "evaluating bulk, defect and references";
  } else if (stream.kind === "substitution") {
    title = "Partial substitution";
    detail = p.message || "ranking configurations";
  } else if (stream.kind === "committee") {
    title = "Model committee";
    detail = p.message || "evaluating with each model";
  } else if (stream.kind === "prototype") {
    title = "Prototype search";
    detail = p.message || "fetching Materials Project prototypes";
  } else {
    title = "Phonons";
    detail = p.message || `${minLength != null ? `auto (≥ ${minLength} Å)` : supercell.join("×")} supercell · finite displacements`;
  }
  // Phase B: PROGRESS events give a determinate bar for long streams.
  if (frac === null && p.done != null && p.total) frac = Math.min(1, p.done / p.total);
  if (p.done != null && p.total && stream.kind !== "flow") detail += ` · ${p.done}/${p.total}`;
  return (
    <Panel floating className="pointer-events-auto flex min-w-[320px] items-center gap-3 px-3 py-2">
      <Loader2 size={15} className="shrink-0 animate-spin text-fg-3" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-sm font-semibold text-fg">{title}</span>
          <span className="font-mono text-xs text-fg-3 tabular-nums">{detail}</span>
        </div>
        <div className="mt-1 h-1 overflow-hidden rounded-full bg-raised">
          {frac !== null ? (
            <div className="h-full rounded-full bg-accent transition-[width] duration-100" style={{ width: `${Math.round(frac * 100)}%` }} />
          ) : (
            <div className="lf-indeterminate h-full w-1/3 rounded-full bg-accent" />
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
    <Panel floating className={cx("pointer-events-auto flex items-center gap-2.5 px-3 py-2", done && "border-ok/30")}>
      {r.status === "running" ? <Loader2 size={14} className="animate-spin text-bad" /> : <GitBranch size={14} className={done ? "text-ok" : "text-warn"} />}
      <div className="leading-tight">
        <div className="font-mono text-sm text-fg">{transitionText(r)}</div>
        <div className="font-mono text-2xs text-fg-3">
          Follow instability · {r.qLabel ?? "q"} ν{r.bandIndex} ({formatFrequency(r.frequencyThz, unit)}), {r.amplitude} Å · {followStatusText(r)}
        </div>
        {done && (
          <button
            type="button"
            className="mt-1 inline-flex items-center gap-1 rounded-ctl border border-line-strong bg-raised px-1.5 py-0.5 text-xs text-fg hover:bg-hover"
            onClick={compareWithFollowParent}
            title="Open the compare panel: parent vs product (space groups, ΔE, lattice, phonons)"
            data-compare-parent
          >
            <GitCompare size={11} /> Compare with parent
          </button>
        )}
      </div>
      <button type="button" className="ml-1 text-fg-4 hover:text-fg" onClick={() => useCrystalStore.getState().setFollowResult(null)} aria-label="dismiss">
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
    <Panel floating className="pointer-events-auto flex items-center gap-2 px-3 py-1.5">
      <Waves size={13} className={mode.is_imaginary ? "text-bad" : "text-fg-3"} />
      <span className="font-mono text-xs text-fg">
        {label} · band {mode.band_index} · f = <span className={mode.is_imaginary ? "text-bad" : ""}>{formatFrequency(mode.frequency_thz, unit)}</span>
      </span>
      {toolMode !== "PHONON_SHAKE" && <span className="text-2xs text-fg-4">(paused: Phonon mode animates)</span>}
      <button
        type="button"
        className="ml-1 rounded-ctl px-1 text-2xs text-fg-3 hover:bg-hover hover:text-fg"
        onClick={() => useCrystalStore.getState().selectEigenmode(null)}
        title="Stop the eigenmode animation"
      >
        <X size={11} />
      </button>
    </Panel>
  );
}
