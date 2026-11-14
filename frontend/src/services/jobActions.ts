/**
 * Background job queue (protocol §7): batch submission, polling, cancel,
 * reorder and clear. `GET /api/jobs` is polled about every 1.5 s, but only
 * while the drawer is open or jobs are active; a job that finishes refreshes
 * the library list (new child structures and results).
 */
import { useEffect } from "react";
import { elasticPayload, relaxPayload, useSettings } from "../store/settings";
import { useCrystalStore } from "../store/useCrystalStore";
import { activeJobCount, useLibrary } from "../store/libraryStore";
import { FINISHED_STATUSES, type Job, type JobStep, type StepKind } from "../types/library";
import { api } from "./apiClient";
import { errorText, refreshProjects, saveCurrent, scheduleRefresh } from "./libraryActions";

export const POLL_MS = 1500;

const isFinished = (j: Job) => FINISHED_STATUSES.includes(j.status);

let lastStatus = new Map<number, Job["status"]>();

export async function pollJobs(): Promise<void> {
  try {
    const r = await api.jobs();
    let finishedNow = false;
    for (const j of r.jobs) {
      const prev = lastStatus.get(j.id);
      if (prev !== undefined && !FINISHED_STATUSES.includes(prev) && isFinished(j)) finishedNow = true;
    }
    lastStatus = new Map(r.jobs.map((j) => [j.id, j.status]));
    useLibrary.getState().setJobs(r.jobs, r.paused);
    if (finishedNow) scheduleRefresh(0);
  } catch (e) {
    useLibrary.getState().setJobsError(`Job queue unavailable (${errorText(e)})`);
  }
}

/** Mount once (App): one poll at start, then every POLL_MS while the drawer is open or jobs are active. */
export function useJobPolling(): void {
  const open = useLibrary((s) => s.drawerOpen);
  const active = useLibrary((s) => activeJobCount(s) > 0);
  useEffect(() => {
    void pollJobs();
  }, []);
  useEffect(() => {
    if (!open && !active) return;
    void pollJobs();
    const h = setInterval(() => void pollJobs(), POLL_MS);
    return () => clearInterval(h);
  }, [open, active]);
}

// ------------------------------------------------------------------ steps
export type StepChoice = StepKind | "follow-if-unstable";
/**
 * Canonical order of the step picker. Follow needs phonons with no relax in
 * between; SC proxies reuse the phonons without resetting them, so
 * phonons → superconductivity → follow is valid.
 */
export const STEP_ORDER: readonly StepChoice[] = ["relax", "phonons", "superconductivity", "follow-if-unstable", "elastic", "eos", "hull"];
/** Steps that need a phonons step earlier in the chain (dropped when phonons is unticked). */
export const NEEDS_PHONONS: readonly StepChoice[] = ["superconductivity", "follow-if-unstable"];
export const STEP_LABEL: Record<StepChoice, string> = {
  relax: "Relax",
  phonons: "Phonons",
  superconductivity: "SC proxies",
  "follow-if-unstable": "Follow if unstable",
  follow: "Follow",
  elastic: "Elastic",
  eos: "EOS",
  hull: "Hull",
};
export const PRESETS: readonly { label: string; steps: StepChoice[] }[] = [
  { label: "Relax → Phonons", steps: ["relax", "phonons"] },
  { label: "Relax → Phonons → Elastic → Hull", steps: ["relax", "phonons", "elastic", "hull"] },
  { label: "… + follow if unstable", steps: ["relax", "phonons", "follow-if-unstable", "elastic", "hull"] },
];

/** JobStep[] for the ticked steps, with the user's interactive settings (omitted keys take the server defaults). */
export function buildSteps(choices: readonly StepChoice[]): JobStep[] {
  const s = useSettings.getState();
  const out: JobStep[] = [];
  for (const c of STEP_ORDER) {
    if (!choices.includes(c)) continue;
    switch (c) {
      case "relax": {
        const p = relaxPayload(s.relax);
        const settings: Record<string, unknown> = { ...p };
        if (p.stress_tol_gpa == null) delete settings.stress_tol_gpa;
        out.push({ kind: "relax", settings });
        break;
      }
      case "phonons": {
        const ph = s.phonon;
        const settings: Record<string, unknown> = { displacement_distance: ph.displacement, cell_mode: ph.cellMode ?? "primitive" };
        if (ph.supercell) settings.supercell_dim = ph.supercell;
        else if (ph.minLength !== undefined) settings.min_supercell_length = ph.minLength;
        out.push({ kind: "phonons", settings });
        break;
      }
      case "superconductivity":
        // mesh null = the server's default DOS mesh; use_mp falls back to the heuristic without an MP key.
        if (choices.includes("phonons")) out.push({ kind: "superconductivity", settings: { mesh: null, use_mp: true } });
        break;
      case "follow-if-unstable":
        if (choices.includes("phonons")) out.push({ kind: "follow", settings: {}, only_if_unstable: true });
        break;
      case "elastic":
        out.push({ kind: "elastic", settings: { ...elasticPayload(s.elastic) } });
        break;
      case "eos":
        out.push({ kind: "eos", settings: {} });
        break;
      case "hull":
        out.push({ kind: "hull", settings: { relax: true } });
        break;
    }
  }
  return out;
}

/**
 * Submit one chain per structure. `includeCurrent` saves the session
 * structure first (when it is not in the library yet) and adds it.
 */
export async function submitBatch(structureIds: number[], choices: StepChoice[], includeCurrent: boolean): Promise<boolean> {
  const lib = useLibrary.getState();
  const st = useCrystalStore.getState();
  let ids = [...structureIds];
  if (includeCurrent) {
    await saveCurrent();
    const link = useLibrary.getState().link;
    if (link && link.stateVersion === st.frame.stateVersion && !ids.includes(link.structureId)) ids = [link.structureId, ...ids];
  }
  const pid = useLibrary.getState().activeProjectId;
  const steps = buildSteps(choices);
  if (pid === null || !ids.length || !steps.length) return false;
  try {
    const m = st.model;
    await api.createJobs({ project_id: pid, structure_ids: ids, steps, model_id: m?.model_id ?? null, dispersion: m?.dispersion ?? false });
    lib.setJobsError(null);
    await pollJobs();
    void refreshProjects();
    return true;
  } catch (e) {
    st.pushToast("warning", "Batch not queued", errorText(e));
    return false;
  }
}

// ------------------------------------------------------------------ chains
export interface Chain {
  id: string;
  jobs: Job[]; // by step_index
  /** Lowest position (display order). */
  position: number;
  status: "active" | "finished";
}

export function groupChains(jobs: readonly Job[]): Chain[] {
  const map = new Map<string, Job[]>();
  for (const j of jobs) (map.get(j.chain_id) ?? map.set(j.chain_id, []).get(j.chain_id)!).push(j);
  const chains: Chain[] = [...map.entries()].map(([id, js]) => {
    js.sort((a, b) => a.step_index - b.step_index);
    return { id, jobs: js, position: Math.min(...js.map((j) => j.position)), status: js.every(isFinished) ? "finished" : "active" };
  });
  return chains.sort((a, b) => (a.status === b.status ? a.position - b.position : a.status === "active" ? -1 : 1));
}

async function run(label: string, fn: () => Promise<unknown>): Promise<void> {
  try {
    await fn();
  } catch (e) {
    useCrystalStore.getState().pushToast("warning", label, errorText(e));
  }
  await pollJobs();
}

/** Cancel a chain: its first unfinished step (the queue cancels the remaining queued steps). */
export function cancelChain(chain: Chain): Promise<void> {
  const j = chain.jobs.find((x) => !isFinished(x));
  return run("Cancel failed", async () => {
    if (j) await api.cancelJob(j.id);
  });
}

/**
 * Move a chain's queued jobs before (dir −1) or after (dir +1) the neighbouring
 * chain's queued jobs. `position` in /move is the index among queued jobs.
 */
export function moveChain(chain: Chain, dir: -1 | 1): Promise<void> {
  return run("Reorder failed", async () => {
    const queued = useLibrary.getState().jobs.filter((j) => j.status === "queued").sort((a, b) => a.position - b.position);
    const order = groupChains(queued).map((c) => c.id);
    const i = order.indexOf(chain.id);
    const k = i + dir;
    if (i < 0 || k < 0 || k >= order.length) return;
    [order[i], order[k]] = [order[k]!, order[i]!];
    // Rebuild the queued order chain by chain and apply it from the front.
    const target = order.flatMap((cid) => queued.filter((j) => j.chain_id === cid).sort((a, b) => a.step_index - b.step_index));
    for (let idx = 0; idx < target.length; idx++) {
      if (queued[idx]?.id !== target[idx]!.id) {
        await api.moveJob(target[idx]!.id, idx);
        // keep the local mirror in step with the server's reordering
        const from = queued.findIndex((j) => j.id === target[idx]!.id);
        const [moved] = queued.splice(from, 1);
        queued.splice(idx, 0, moved!);
      }
    }
  });
}

export function clearFinished(): Promise<void> {
  return run("Clear failed", () => api.clearFinishedJobs());
}
