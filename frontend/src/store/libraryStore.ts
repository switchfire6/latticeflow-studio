/**
 * Client state of the project library and the job queue (protocol §6–§7).
 * The library itself lives on the server; this store holds what the UI shows,
 * which library structure the session currently displays (`link`), which
 * panes show results fed back from the library (`fromLibrary`, by identity of
 * the data object) and the settings / model behind each displayed result
 * (`provenance`, used by the methods text). Preferences (active project,
 * auto-save) are kept in localStorage.
 */
import { create } from "zustand";
import type { PhononAnalysisComplete } from "../types/protocol";
import type { Job, Project, ResultKind, StructureListRow } from "../types/library";
import { loadJSON, saveJSON } from "../utils/persist";

export interface Provenance {
  settings: Record<string, unknown>;
  /** null = the process default model. */
  modelId: string | null;
  dispersion: boolean;
  /** The data object the provenance belongs to (compared by identity with what the panes show). */
  data: object;
  /** Structure state version it was computed for (relaxations). */
  stateVersion: number;
}

export interface LibraryLink {
  projectId: number;
  structureId: number;
  /** frame.stateVersion of the session structure that is this library structure. */
  stateVersion: number;
}

export interface PhononOverlay {
  structureId: number;
  label: string;
  data: PhononAnalysisComplete | null;
  status: "loading" | "ok" | "error";
  error?: string;
}

export interface LibraryState {
  projects: Project[];
  activeProjectId: number | null;
  structures: StructureListRow[];
  structuresLoading: boolean;
  error: string | null;
  autoSave: boolean;
  /** Short status line ("saved phonons of SrTiO3"). */
  lastSave: { text: string; error: boolean; at: number } | null;
  link: LibraryLink | null;
  fromLibrary: Partial<Record<ResultKind, object>>;
  provenance: Partial<Record<ResultKind, Provenance>>;
  phononOverlay: PhononOverlay | null;

  jobs: Job[];
  paused: boolean;
  jobsError: string | null;
  drawerOpen: boolean;

  setProjects(p: Project[]): void;
  setActiveProject(id: number | null): void;
  setStructures(s: StructureListRow[], loading?: boolean): void;
  setStructuresLoading(v: boolean): void;
  setError(e: string | null): void;
  setAutoSave(v: boolean): void;
  setLastSave(text: string, error?: boolean): void;
  setLink(l: LibraryLink | null): void;
  markFromLibrary(kind: ResultKind, data: object | null): void;
  setProvenance(kind: ResultKind, p: Provenance | null): void;
  setPhononOverlay(o: PhononOverlay | null): void;
  setJobs(jobs: Job[], paused: boolean, error?: string | null): void;
  setJobsError(e: string | null): void;
  setDrawerOpen(v: boolean): void;
}

interface Persisted {
  activeProjectId: number | null;
  autoSave: boolean;
}

export function createLibraryStore(storageKey: string | null = null) {
  const stored = storageKey ? loadJSON<Persisted>(storageKey, { activeProjectId: null, autoSave: true }) : { activeProjectId: null, autoSave: true };
  return create<LibraryState>()((set, get) => {
    const save = () => {
      if (!storageKey) return;
      const { activeProjectId, autoSave } = get();
      saveJSON(storageKey, { activeProjectId, autoSave } satisfies Persisted);
    };
    return {
      projects: [],
      activeProjectId: typeof stored.activeProjectId === "number" ? stored.activeProjectId : null,
      structures: [],
      structuresLoading: false,
      error: null,
      autoSave: typeof stored.autoSave === "boolean" ? stored.autoSave : true,
      lastSave: null,
      link: null,
      fromLibrary: {},
      provenance: {},
      phononOverlay: null,
      jobs: [],
      paused: false,
      jobsError: null,
      drawerOpen: false,

      setProjects: (projects) => set({ projects }),
      setActiveProject: (activeProjectId) => {
        if (get().activeProjectId === activeProjectId) return;
        set({ activeProjectId, structures: [], phononOverlay: null });
        save();
      },
      setStructures: (structures, loading = false) => set({ structures, structuresLoading: loading }),
      setStructuresLoading: (structuresLoading) => set({ structuresLoading }),
      setError: (error) => set({ error }),
      setAutoSave: (autoSave) => {
        set({ autoSave });
        save();
      },
      setLastSave: (text, error = false) => set({ lastSave: { text, error, at: Date.now() } }),
      setLink: (link) => set({ link }),
      markFromLibrary: (kind, data) =>
        set((s) => {
          const next = { ...s.fromLibrary };
          if (data) next[kind] = data;
          else delete next[kind];
          return { fromLibrary: next };
        }),
      setProvenance: (kind, p) =>
        set((s) => {
          const next = { ...s.provenance };
          if (p) next[kind] = p;
          else delete next[kind];
          return { provenance: next };
        }),
      setPhononOverlay: (phononOverlay) => set({ phononOverlay }),
      setJobs: (jobs, paused, error = null) => set({ jobs, paused, jobsError: error }),
      setJobsError: (jobsError) => set({ jobsError }),
      setDrawerOpen: (drawerOpen) => set({ drawerOpen }),
    };
  });
}

export const useLibrary = createLibraryStore("latticeflow.library.v1");

export const activeJobCount = (s: Pick<LibraryState, "jobs">): number => s.jobs.filter((j) => j.status === "queued" || j.status === "running").length;
