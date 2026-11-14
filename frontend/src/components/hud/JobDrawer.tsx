/**
 * Job queue drawer (protocol §7), opened from the header button that shows
 * the number of active jobs. "New batch": structures of the active project
 * (default: the current structure, saved first when needed) and a step chain
 * (presets or individual steps, run with the user's interactive settings).
 * The list shows one card per chain with per-step status and progress,
 * cancel, move up / down while queued, and "clear finished"; `paused` shows
 * that an interactive calculation has priority.
 */
import { ArrowDown, ArrowUp, ListChecks, Loader2, Pause, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  PRESETS,
  NEEDS_PHONONS,
  STEP_LABEL,
  STEP_ORDER,
  cancelChain,
  clearFinished,
  groupChains,
  moveChain,
  submitBatch,
  type Chain,
  type StepChoice,
} from "../../services/jobActions";
import { activeJobCount, useLibrary } from "../../store/libraryStore";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { Job, StructureListRow } from "../../types/library";
import { lineageOrder } from "../../utils/library";
import { Badge, Button, FormulaText, Toggle, cx } from "./ui";

export function JobsButton() {
  const open = useLibrary((s) => s.drawerOpen);
  const active = useLibrary(activeJobCount);
  const paused = useLibrary((s) => s.paused);
  return (
    <button
      type="button"
      onClick={() => useLibrary.getState().setDrawerOpen(!open)}
      aria-expanded={open}
      title={paused && active ? "Job queue (paused while an interactive calculation runs)" : "Job queue"}
      className={cx("flex items-center gap-1.5 rounded-ctl px-2 py-1 text-xs transition-colors", open ? "bg-accent-soft text-accent-fg" : "text-fg-3 hover:bg-hover hover:text-fg")}
      data-jobs-button
    >
      <ListChecks size={13} />
      <span className="hidden min-[1800px]:inline">Jobs</span>
      {active > 0 && (
        <span className="rounded-ctl border border-line-strong bg-raised px-1 font-mono text-2xs leading-[14px] text-fg tabular-nums" data-active-jobs>
          {active}
        </span>
      )}
    </button>
  );
}

const STATUS_CLS: Record<Job["status"], string> = {
  queued: "border-line text-fg-3",
  running: "border-accent/60 text-accent-fg",
  done: "border-ok/40 text-ok",
  failed: "border-bad/50 text-bad",
  cancelled: "border-line text-fg-4 line-through",
  skipped: "border-line text-fg-4",
};

const STEP_TITLE: Partial<Record<StepChoice, string>> = {
  "follow-if-unstable": "Follow the softest imaginary mode; skipped when the phonons are stable",
  superconductivity: "Superconductivity proxies from the phonon DOS (ω_log, Allen–Dynes T_c at an assumed λ — not a prediction) plus a metallicity check",
};

function stepName(j: Job): string {
  if (j.kind === "follow") return j.only_if_unstable ? "follow?" : "follow";
  return STEP_LABEL[j.kind];
}

function ChainCard({ chain, names, canUp, canDown }: { chain: Chain; names: Map<number, StructureListRow>; canUp: boolean; canDown: boolean }) {
  const first = chain.jobs[0]!;
  const row = first.structure_id !== null ? names.get(first.structure_id) : undefined;
  const current = chain.jobs.find((j) => j.status === "running") ?? chain.jobs.find((j) => j.status === "failed") ?? chain.jobs.find((j) => j.status === "queued");
  const active = chain.status === "active";
  const queued = chain.jobs.some((j) => j.status === "queued");
  const last = chain.jobs.at(-1)!;
  return (
    <div className="rounded-ctl border border-line-soft bg-raised/40 px-2 py-1.5" data-chain={chain.id}>
      <div className="flex items-center gap-1.5">
        <span className="min-w-0 flex-1 truncate text-xs text-fg">
          {row ? (row.name === row.formula ? <FormulaText formula={row.formula} /> : row.name) : first.structure_id !== null ? `structure #${first.structure_id}` : "deleted structure"}
        </span>
        {active && queued && (
          <>
            <button type="button" className="text-fg-4 hover:text-fg disabled:opacity-25" disabled={!canUp} title="Run earlier" onClick={() => void moveChain(chain, -1)}>
              <ArrowUp size={12} />
            </button>
            <button type="button" className="text-fg-4 hover:text-fg disabled:opacity-25" disabled={!canDown} title="Run later" onClick={() => void moveChain(chain, 1)}>
              <ArrowDown size={12} />
            </button>
          </>
        )}
        {active && (
          <button type="button" className="text-fg-4 hover:text-bad" title="Cancel this chain" onClick={() => void cancelChain(chain)}>
            <X size={12} />
          </button>
        )}
      </div>
      <div className="mt-1 flex flex-wrap gap-1">
        {chain.jobs.map((j) => (
          <span
            key={j.id}
            className={cx("relative overflow-hidden rounded-ctl border px-1.5 text-2xs leading-[16px]", STATUS_CLS[j.status])}
            title={`${j.kind}${j.only_if_unstable ? " (only if unstable)" : ""}: ${j.status}${j.message ? ` · ${j.message}` : ""}${j.error ? ` · ${j.error}` : ""}`}
            data-job-status={j.status}
          >
            {j.status === "running" && <span className="absolute inset-y-0 left-0 bg-accent-soft" style={{ width: `${Math.round(Math.max(0.04, j.progress) * 100)}%` }} />}
            <span className="relative flex items-center gap-1">
              {j.status === "running" && <Loader2 size={9} className="animate-spin" />}
              {stepName(j)}
            </span>
          </span>
        ))}
      </div>
      {current && (current.message || current.error) && (
        <p className={cx("mt-1 truncate text-2xs", current.status === "failed" ? "text-bad" : "text-fg-4")} title={current.error ?? current.message}>
          {current.error ?? current.message}
        </p>
      )}
      {!active && last.status === "done" && <p className="mt-1 truncate text-2xs text-fg-4">{last.message}</p>}
    </div>
  );
}

function NewBatch() {
  const rows = useLibrary((s) => s.structures);
  const link = useLibrary((s) => s.link);
  const projectId = useLibrary((s) => s.activeProjectId);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const formula = useCrystalStore((s) => s.crystal?.formula ?? "");
  const linked = link && link.stateVersion === stateVersion && link.projectId === projectId ? link.structureId : null;
  const [picked, setPicked] = useState<number[]>(linked !== null ? [linked] : []);
  const [current, setCurrent] = useState(linked === null && hasCrystal);
  const [steps, setSteps] = useState<StepChoice[]>(["relax", "phonons"]);
  const [busy, setBusy] = useState(false);
  const items = useMemo(() => lineageOrder(rows), [rows]);

  const toggle = (id: number) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const toggleStep = (c: StepChoice) =>
    setSteps((s) => {
      const next = s.includes(c) ? s.filter((x) => x !== c) : [...s, c];
      return next.includes("phonons") ? next : next.filter((x) => !NEEDS_PHONONS.includes(x));
    });
  const n = picked.length + (current ? 1 : 0);
  const submit = async () => {
    setBusy(true);
    const ok = await submitBatch(picked, steps, current);
    setBusy(false);
    if (ok) setCurrent(false);
  };

  return (
    <div className="space-y-2 border-b border-line p-3" data-new-batch>
      <div className="text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase">New batch</div>
      <div className="max-h-[150px] overflow-y-auto rounded-ctl border border-line-soft bg-field py-0.5 text-xs">
        {linked === null && hasCrystal && (
          <label className="flex cursor-pointer items-center gap-1.5 px-2 py-0.5 hover:bg-hover">
            <input type="checkbox" checked={current} onChange={(e) => setCurrent(e.target.checked)} className="accent-[var(--color-accent)]" />
            <span className="min-w-0 flex-1 truncate text-fg">
              Current structure <span className="text-fg-4">(<FormulaText formula={formula} />, saved first)</span>
            </span>
          </label>
        )}
        {items.map(({ row, depth }) => (
          <label key={row.id} className="flex cursor-pointer items-center gap-1.5 py-0.5 pr-2 hover:bg-hover" style={{ paddingLeft: 8 + Math.min(depth, 5) * 12 }}>
            <input type="checkbox" checked={picked.includes(row.id)} onChange={() => toggle(row.id)} className="accent-[var(--color-accent)]" />
            <span className={cx("min-w-0 flex-1 truncate", row.id === linked ? "text-accent-fg" : "text-fg")}>
              {row.name === row.formula ? <FormulaText formula={row.formula} /> : row.name}
            </span>
            <span className="shrink-0 text-2xs text-fg-4">{row.spacegroup_symbol}</span>
          </label>
        ))}
        {!items.length && linked === null && !hasCrystal && <p className="px-2 py-1 text-fg-4">The project has no structures.</p>}
      </div>
      <div className="flex flex-wrap gap-1">
        {PRESETS.map((p) => (
          <button
            key={p.label}
            type="button"
            onClick={() => setSteps(p.steps)}
            className={cx(
              "rounded-ctl border px-1.5 py-0.5 text-2xs",
              p.steps.join() === STEP_ORDER.filter((s) => steps.includes(s)).join() ? "border-accent/50 bg-accent-soft text-accent-fg" : "border-line text-fg-3 hover:text-fg",
            )}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap gap-0.5">
        {STEP_ORDER.map((c) => (
          <span key={c} className={NEEDS_PHONONS.includes(c) && !steps.includes("phonons") ? "pointer-events-none opacity-35" : undefined}>
            <Toggle label={STEP_LABEL[c]} checked={steps.includes(c)} onChange={() => toggleStep(c)} title={STEP_TITLE[c]} />
          </span>
        ))}
      </div>
      <Button variant="primary" className="w-full" disabled={busy || n === 0 || steps.length === 0} onClick={() => void submit()}>
        {busy && <Loader2 size={12} className="animate-spin" />}
        Queue {n} chain{n === 1 ? "" : "s"} · {STEP_ORDER.filter((s) => steps.includes(s)).map((s) => STEP_LABEL[s]).join(" → ") || "no steps"}
      </Button>
    </div>
  );
}

export function JobDrawer() {
  const open = useLibrary((s) => s.drawerOpen);
  const jobs = useLibrary((s) => s.jobs);
  const paused = useLibrary((s) => s.paused);
  const error = useLibrary((s) => s.jobsError);
  const structures = useLibrary((s) => s.structures);
  const [key, setKey] = useState(0);
  useEffect(() => {
    if (open) setKey((k) => k + 1); // fresh "New batch" defaults each time the drawer opens
  }, [open]);
  const chains = useMemo(() => groupChains(jobs), [jobs]);
  const names = useMemo(() => new Map(structures.map((r) => [r.id, r])), [structures]);
  if (!open) return null;
  const queuedChains = chains.filter((c) => c.status === "active" && c.jobs.some((j) => j.status === "queued"));
  const anyActive = chains.some((c) => c.status === "active");
  const finished = chains.filter((c) => c.status === "finished").length;
  return (
    <aside className="fixed top-12 right-0 bottom-0 z-40 flex w-[360px] flex-col border-l border-line bg-surface shadow-lg shadow-black/40" data-job-drawer>
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line px-3">
        <span className="text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase">Job queue</span>
        {paused && anyActive && (
          <Badge tone="amber" title="The worker waits while an interactive calculation streams">
            <Pause size={9} /> paused: interactive calculation running
          </Badge>
        )}
        <button type="button" className="ml-auto text-fg-4 hover:text-fg" aria-label="close" onClick={() => useLibrary.getState().setDrawerOpen(false)}>
          <X size={13} />
        </button>
      </div>
      <NewBatch key={key} />
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center justify-between px-3 pt-2 pb-1.5">
          <span className="text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase">Chains</span>
          <button type="button" className="text-2xs text-fg-3 hover:text-fg disabled:opacity-30" disabled={finished === 0} onClick={() => void clearFinished()}>
            Clear finished{finished ? ` (${finished})` : ""}
          </button>
        </div>
        {error && <p className="px-3 pb-1 text-2xs text-warn">{error}</p>}
        <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto px-3 pb-3">
          {chains.map((c) => {
            const qi = queuedChains.indexOf(c);
            return <ChainCard key={c.id} chain={c} names={names} canUp={qi > 0} canDown={qi >= 0 && qi < queuedChains.length - 1} />;
          })}
          {!chains.length && <p className="text-xs text-fg-4">No jobs. Queue a batch above; results land in the library.</p>}
        </div>
      </div>
    </aside>
  );
}
