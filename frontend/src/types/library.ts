/**
 * Project library and job queue bodies (docs/protocol.md §6–§7), mirroring
 * backend/app/schemas/library.py. Ids are integers, timestamps ISO 8601 UTC.
 */
import type { CrystalStatePayload } from "./protocol";

export type ResultKind = "relax" | "phonons" | "elastic" | "eos" | "hull" | "md" | "diffraction" | "superconductivity";
export type StepKind = "relax" | "phonons" | "elastic" | "eos" | "hull" | "follow" | "superconductivity";
export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled" | "skipped";
export const FINISHED_STATUSES: readonly JobStatus[] = ["done", "failed", "cancelled", "skipped"];

export interface Project {
  id: number;
  name: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
  n_structures: number;
}

export interface StructureCreate {
  crystal: CrystalStatePayload;
  name?: string | null;
  parent_id?: number | null;
  origin?: string;
  notes?: string;
}

export interface StructureRow {
  id: number;
  project_id: number;
  name: string;
  formula: string;
  n_atoms: number;
  spacegroup_symbol: string;
  spacegroup_number: number;
  content_hash: string;
  parent_id: number | null;
  origin: string;
  notes: string;
  created_at: string;
  updated_at: string;
}

/** Summary values are numbers, booleans, strings or null (merged across kinds, §6.2). */
export type SummaryValue = number | boolean | string | null;

export interface StructureListRow extends StructureRow {
  latest: Record<string, SummaryValue>;
  latest_result_ids: Partial<Record<ResultKind, number>>;
}

export interface Structure extends StructureRow {
  crystal: CrystalStatePayload;
  existing: boolean;
}

export interface ResultCreate {
  kind: ResultKind;
  data: object;
  model_id?: string | null;
  dispersion?: boolean;
  settings?: Record<string, unknown>;
}

export interface ResultRow {
  id: number;
  structure_id: number;
  kind: ResultKind;
  model_id: string;
  dispersion: boolean;
  settings: Record<string, unknown>;
  summary: Record<string, SummaryValue>;
  created_at: string;
  code_version: string;
  replaced?: boolean | null;
}

export interface Result<T = unknown> extends ResultRow {
  data: T;
}

export interface JobStep {
  kind: StepKind;
  settings?: Record<string, unknown>;
  only_if_unstable?: boolean;
}

export interface JobCreate {
  project_id: number;
  structure_ids: number[];
  steps: JobStep[];
  model_id?: string | null;
  dispersion?: boolean;
}

export interface Job {
  id: number;
  project_id: number;
  structure_id: number | null;
  chain_id: string;
  step_index: number;
  n_steps: number;
  kind: StepKind;
  settings: Record<string, unknown>;
  only_if_unstable: boolean;
  model_id: string | null;
  dispersion: boolean;
  status: JobStatus;
  progress: number;
  message: string;
  error: string | null;
  result_id: number | null;
  output_structure_id: number | null;
  position: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobList {
  jobs: Job[];
  paused: boolean;
}

/** §6.4 export bundle (opaque to the client apart from the header). */
export interface ProjectBundle {
  format: "latticeflow-project";
  version: number;
  project: { name: string; created_at?: string };
  [key: string]: unknown;
}
