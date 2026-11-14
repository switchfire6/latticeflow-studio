/**
 * REST client for /api (PROJECT_SPEC.md §5.3). Bodies are the protocol.ts types.
 * Errors are thrown as ApiError carrying the backend's ErrorResponse code.
 */
import { apiBaseUrl } from "../config";
import type {
  CompareRequest,
  CompareResponse,
  CrystalStatePayload,
  DiffractionRequest,
  DiffractionResponse,
  ErrorResponse,
  ExportRequest,
  HealthResponse,
  HullEvaluateRequest,
  HullEvaluateResponse,
  MetallicityRequest,
  MetallicityResponse,
  ModelListResponse,
  MPMatchRequest,
  MPMatchResponse,
  MPSearchResponse,
  MPStructureResponse,
  OrderingRequest,
  OrderingResponse,
  PresetInfo,
} from "../types/protocol";
import type {
  Job,
  JobCreate,
  JobList,
  Project,
  ProjectBundle,
  Result,
  ResultCreate,
  ResultKind,
  ResultRow,
  Structure,
  StructureCreate,
  StructureListRow,
} from "../types/library";

/** The session's model for model-dependent REST calls (hull); null = process default. */
export interface ModelSelection {
  model_id: string | null;
  dispersion: boolean;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

type FetchLike = typeof fetch;

export interface ApiClientOptions {
  baseUrl?: string;
  fetchImpl?: FetchLike;
}

async function toApiError(res: Response): Promise<ApiError> {
  let code = `HTTP_${res.status}`;
  let message = res.statusText || `request failed (${res.status})`;
  try {
    const body = (await res.json()) as Partial<ErrorResponse> & { detail?: unknown };
    if (typeof body.code === "string") code = body.code;
    if (typeof body.message === "string") message = body.message;
    else if (body.detail !== undefined) message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(res.status, code, message);
}

/** Parse `filename="..."` from a Content-Disposition header. */
export function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header);
  if (star?.[1]) return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain?.[1]?.trim() || fallback;
}

export function createApiClient(opts: ApiClientOptions = {}) {
  const base = () => opts.baseUrl ?? apiBaseUrl();
  const f: FetchLike = (...args) => (opts.fetchImpl ?? fetch)(...args);

  async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
    const res = await f(`${base()}${path}`, { headers: { Accept: "application/json" }, signal });
    if (!res.ok) throw await toApiError(res);
    return (await res.json()) as T;
  }

  async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
    const res = await f(`${base()}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok) throw await toApiError(res);
    return (await res.json()) as T;
  }

  /** JSON request with any method; 204 answers resolve to undefined. */
  async function sendJson<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await f(`${base()}${path}`, {
      method,
      headers: body === undefined ? { Accept: "application/json" } : { "Content-Type": "application/json", Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) throw await toApiError(res);
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }

  return {
    health: (signal?: AbortSignal) => getJson<HealthResponse>("/health", signal),
    presets: (signal?: AbortSignal) => getJson<PresetInfo[]>("/presets", signal),

    /** Stateless preview: parse a CIF and evaluate it (does not change the WebSocket session). */
    importCif: (cif: string, signal?: AbortSignal) => postJson<CrystalStatePayload>("/import/cif", { cif }, signal),

    /** Relaxes the candidate first by default (§5.11), like the reference phases; Phase B: with the session's model. */
    hullEvaluate: (crystal: CrystalStatePayload, signal?: AbortSignal, relax = true, model?: ModelSelection | null) =>
      postJson<HullEvaluateResponse>("/hull/evaluate", hullRequestBody(crystal, relax, model), signal),

    // ---- Phase B
    models: (signal?: AbortSignal) => getJson<ModelListResponse>("/models", signal),
    diffraction: (body: DiffractionRequest, signal?: AbortSignal) => postJson<DiffractionResponse>("/diffraction", body, signal),
    mpSearch: (q: string, signal?: AbortSignal) => getJson<MPSearchResponse>(`/mp/search?q=${encodeURIComponent(q.trim())}`, signal),
    mpStructure: (materialId: string, signal?: AbortSignal) =>
      getJson<MPStructureResponse>(`/mp/structure/${encodeURIComponent(materialId.trim())}`, signal),

    // ---- Phase C1
    /** Ordered approximants of a disordered CIF, ranked by energy (POST /api/ordering). */
    ordering: (body: OrderingRequest, signal?: AbortSignal) => postJson<OrderingResponse>("/ordering", body, signal),

    // ---- Phase C2
    /** Stateless comparison of two structures (POST /api/compare). */
    compare: (body: CompareRequest, signal?: AbortSignal) => postJson<CompareResponse>("/compare", body, signal),
    /** Materials Project entries matching a structure (POST /api/mp/match). */
    mpMatch: (body: MPMatchRequest, signal?: AbortSignal) => postJson<MPMatchResponse>("/mp/match", body, signal),
    /** Metallicity gate for superconductivity screening (POST /api/superconductivity/metallicity). */
    metallicity: (body: MetallicityRequest, signal?: AbortSignal) => postJson<MetallicityResponse>("/superconductivity/metallicity", body, signal),

    // ---- Project library (protocol §6)
    projects: () => getJson<Project[]>("/projects"),
    createProject: (name: string) => sendJson<Project>("POST", "/projects", { name }),
    renameProject: (pid: number, name: string) => sendJson<Project>("PATCH", `/projects/${pid}`, { name }),
    deleteProject: (pid: number) => sendJson<void>("DELETE", `/projects/${pid}`),
    exportProject: (pid: number) => getJson<ProjectBundle>(`/projects/${pid}/export`),
    importProject: (bundle: unknown, name?: string) =>
      sendJson<Project>("POST", `/projects/import${name ? `?name=${encodeURIComponent(name)}` : ""}`, bundle),
    structures: (pid: number) => getJson<StructureListRow[]>(`/projects/${pid}/structures`),
    /** 201 new, or 200 with `existing: true` when the project already holds the structure (dedup). */
    addStructure: (pid: number, body: StructureCreate) => sendJson<Structure>("POST", `/projects/${pid}/structures`, body),
    structure: (sid: number) => getJson<Structure>(`/structures/${sid}`),
    updateStructure: (sid: number, body: { name?: string; notes?: string }) => sendJson<Structure>("PATCH", `/structures/${sid}`, body),
    deleteStructure: (sid: number) => sendJson<void>("DELETE", `/structures/${sid}`),
    addResult: (sid: number, body: ResultCreate) => sendJson<ResultRow>("POST", `/structures/${sid}/results`, body),
    results: (sid: number, kind?: ResultKind) => getJson<ResultRow[]>(`/structures/${sid}/results${kind ? `?kind=${kind}` : ""}`),
    result: <T = unknown>(rid: number) => getJson<Result<T>>(`/results/${rid}`),

    // ---- Job queue (protocol §7)
    jobs: (projectId?: number, active?: boolean, signal?: AbortSignal) => {
      const q = new URLSearchParams();
      if (projectId !== undefined) q.set("project_id", String(projectId));
      if (active !== undefined) q.set("active", String(active));
      const qs = q.toString();
      return getJson<JobList>(`/jobs${qs ? `?${qs}` : ""}`, signal);
    },
    createJobs: (body: JobCreate) => sendJson<Job[]>("POST", "/jobs", body),
    cancelJob: (id: number) => sendJson<Job>("POST", `/jobs/${id}/cancel`),
    moveJob: (id: number, position: number) => sendJson<Job>("POST", `/jobs/${id}/move`, { position }),
    deleteJob: (id: number) => sendJson<void>("DELETE", `/jobs/${id}`),
    clearFinishedJobs: (projectId?: number) =>
      sendJson<{ deleted: number }>("POST", `/jobs/clear-finished${projectId !== undefined ? `?project_id=${projectId}` : ""}`),

    /** POST /api/export/cif -> { blob, filename } (filename from Content-Disposition). */
    async exportStructure(crystal: CrystalStatePayload, format: ExportFormat | ExportOptions = "cif", signal?: AbortSignal) {
      const body = exportRequestBody(crystal, typeof format === "string" ? { format } : format);
      const res = await f(`${base()}/export/cif`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      });
      if (!res.ok) throw await toApiError(res);
      const blob = await res.blob();
      return { blob, filename: filenameFromDisposition(res.headers.get("Content-Disposition"), exportFallbackName(crystal, body)) };
    },

    /** Export and trigger a browser download via an object URL. */
    async downloadStructure(crystal: CrystalStatePayload, format: ExportFormat | ExportOptions = "cif") {
      const { blob, filename } = await this.exportStructure(crystal, format);
      const url = URL.createObjectURL(blob);
      try {
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      return filename;
    },
  };
}

/** Hull request body; the model fields are sent only for a session model. */
export function hullRequestBody(crystal: CrystalStatePayload, relax: boolean, model?: ModelSelection | null): HullEvaluateRequest {
  const body: HullEvaluateRequest = { crystal, relax };
  if (model && model.model_id) {
    body.model_id = model.model_id;
    body.dispersion = model.dispersion;
  }
  return body;
}

export type ExportFormat = NonNullable<ExportRequest["format"]>;
export type ExportCell = NonNullable<ExportRequest["cell"]>;
export interface ExportOptions {
  format: ExportFormat;
  /** CIF only: space group + asymmetric unit (default true); false = P1. */
  symmetrize?: boolean;
  symprec?: number;
  cell?: ExportCell;
}

/** Request body for /api/export/cif (extxyz is always written as_is, CIF symmetrisation only applies to CIF). */
export function exportRequestBody(crystal: CrystalStatePayload, o: ExportOptions): ExportRequest {
  const body: ExportRequest = { crystal, format: o.format };
  if (o.format === "cif") body.symmetrize = o.symmetrize ?? true;
  if (o.symprec !== undefined) body.symprec = o.symprec;
  body.cell = o.format === "extxyz" ? "as_is" : (o.cell ?? "as_is");
  return body;
}

export function exportFallbackName(crystal: CrystalStatePayload, body: ExportRequest): string {
  const stem = crystal.formula || "structure";
  if (body.format === "poscar") return "POSCAR";
  if (body.format === "extxyz") return `${stem}.extxyz`;
  return `${stem}.cif`;
}

export type ApiClient = ReturnType<typeof createApiClient>;

export const api: ApiClient = createApiClient();
