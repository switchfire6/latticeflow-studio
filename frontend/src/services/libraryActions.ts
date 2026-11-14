/**
 * Project library actions (protocol §6): projects, structures, bundles,
 * opening a stored structure (LOAD_STRUCTURE {crystal}, then its stored
 * results are fed back into the panes without recomputing) and auto-save of
 * interactive results.
 *
 * Auto-save: when a relaxation / follow, phonons, elastic tensor, EOS, hull or
 * diffraction run finishes, its result is POSTed for the structure it belongs
 * to, saving that structure first (deduplicated by the server). `data` is the
 * exact event / response payload (§6.3): the final RELAXATION_FRAME,
 * PHONON_ANALYSIS_COMPLETE, ELASTIC_TENSOR_RESULT, BULK_MODULUS_RESULT and the
 * hull / diffraction responses; `settings` is the command / request that
 * produced it; `model_id` the session's model (omitted = process default).
 * A relaxation is saved as a child (`origin "relax"` or `"follow <q> ν<band>"`)
 * of the structure it started from, once its HISTORY_STATE confirms it did not
 * diverge.
 */
import { summarizePhonon, useCrystalStore } from "../store/useCrystalStore";
import { useLibrary, type Provenance } from "../store/libraryStore";
import type {
  BulkModulusResult,
  ClientCommandMap,
  CrystalStatePayload,
  DiffractionResponse,
  ElasticTensorResult,
  HullEvaluateResponse,
  PhononAnalysisComplete,
  RelaxationFrame,
} from "../types/protocol";
import type { Project, Result, ResultCreate, ResultKind } from "../types/library";
import { formatKLabel, qLabelAt } from "../utils/bandChart";
import { fileStem } from "../utils/csv";
import { downloadText } from "../utils/download";
import { buildMethodsText, type MethodsInput, type MethodsModel } from "../utils/methodsText";
import { pickDefaultEigenmode } from "../utils/phonon";
import { ApiError, api } from "./apiClient";
import { onResult, type ResultEvent } from "./resultEvents";
import { commands, studio, type MessageContext } from "./wsClient";

export const KIND_LABEL: Record<ResultKind, string> = {
  relax: "relaxation",
  phonons: "phonons",
  elastic: "elastic tensor",
  eos: "EOS",
  hull: "E_hull",
  md: "MD",
  diffraction: "diffraction",
  superconductivity: "SC proxies",
};

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return `${e.code}: ${e.message}`;
  return e instanceof Error ? e.message : String(e);
}

const toast = (level: "error" | "warning" | "info", title: string, message: string) => useCrystalStore.getState().pushToast(level, title, message);

// ------------------------------------------------------------------ projects & lists
export async function refreshProjects(): Promise<Project[]> {
  try {
    const projects = await api.projects();
    const lib = useLibrary.getState();
    lib.setProjects(projects);
    lib.setError(null);
    if (lib.activeProjectId === null || !projects.some((p) => p.id === lib.activeProjectId)) {
      lib.setActiveProject((projects.find((p) => p.is_default) ?? projects[0])?.id ?? null);
    }
    return projects;
  } catch (e) {
    useLibrary.getState().setError(`Library unavailable (${errorText(e)})`);
    return [];
  }
}

let listSeq = 0;
export async function refreshStructures(): Promise<void> {
  const pid = useLibrary.getState().activeProjectId;
  if (pid === null) return;
  const seq = ++listSeq;
  useLibrary.getState().setStructuresLoading(true);
  try {
    const rows = await api.structures(pid);
    if (seq === listSeq && useLibrary.getState().activeProjectId === pid) useLibrary.getState().setStructures(rows);
  } catch (e) {
    if (seq === listSeq) {
      useLibrary.getState().setStructuresLoading(false);
      useLibrary.getState().setError(`Could not list structures (${errorText(e)})`);
    }
  }
}

let refreshTimer: ReturnType<typeof setTimeout> | null = null;
/** Refresh the structure list and project counts soon (coalesces bursts of saves / finished jobs). */
export function scheduleRefresh(ms = 250): void {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    void refreshStructures();
    void refreshProjects();
  }, ms);
}

async function ensureProjectId(): Promise<number | null> {
  if (useLibrary.getState().activeProjectId === null) await refreshProjects();
  return useLibrary.getState().activeProjectId;
}

export async function selectProject(pid: number): Promise<void> {
  useLibrary.getState().setActiveProject(pid);
  await refreshStructures();
}

export async function createProject(name: string): Promise<void> {
  try {
    const p = await api.createProject(name.trim());
    await refreshProjects();
    await selectProject(p.id);
  } catch (e) {
    toast("warning", "Project not created", errorText(e));
  }
}

export async function renameProject(pid: number, name: string): Promise<void> {
  try {
    await api.renameProject(pid, name.trim());
    await refreshProjects();
  } catch (e) {
    toast("warning", "Project not renamed", errorText(e));
  }
}

export async function deleteProject(pid: number): Promise<void> {
  try {
    await api.deleteProject(pid);
    for (const k of [...savedByVersion.keys()]) if (k.startsWith(`${pid}:`)) savedByVersion.delete(k);
    await refreshProjects();
    await refreshStructures();
  } catch (e) {
    toast("warning", "Project not deleted", errorText(e));
  }
}

export async function exportBundle(pid: number): Promise<void> {
  try {
    const bundle = await api.exportProject(pid);
    downloadText(JSON.stringify(bundle), `${fileStem(bundle.project?.name, "project")}.latticeflow.json`, "application/json");
  } catch (e) {
    toast("warning", "Export failed", errorText(e));
  }
}

export async function importBundle(file: File): Promise<void> {
  let bundle: unknown;
  try {
    bundle = JSON.parse(await file.text());
  } catch {
    toast("warning", "Not a project bundle", `${file.name} is not JSON.`);
    return;
  }
  try {
    const p = await api.importProject(bundle);
    await refreshProjects();
    await selectProject(p.id);
    toast("info", "Project imported", `“${p.name}” with ${p.n_structures} structure${p.n_structures === 1 ? "" : "s"}.`);
  } catch (e) {
    toast("warning", "Import failed", errorText(e));
  }
}

// ------------------------------------------------------------------ saving structures
/** `${projectId}:${stateVersion}` -> library structure id of that session state. */
const savedByVersion = new Map<string, number>();

function rememberLink(pid: number, stateVersion: number, sid: number): void {
  savedByVersion.set(`${pid}:${stateVersion}`, sid);
  if (useCrystalStore.getState().frame.stateVersion === stateVersion) {
    useLibrary.getState().setLink({ projectId: pid, structureId: sid, stateVersion });
  }
}

async function ensureStructure(
  pid: number,
  crystal: CrystalStatePayload,
  stateVersion: number,
  opts: { origin?: string; parentId?: number | null } = {},
): Promise<number> {
  const cached = savedByVersion.get(`${pid}:${stateVersion}`);
  if (cached !== undefined) return cached;
  const s = await api.addStructure(pid, { crystal, origin: opts.origin ?? "session", parent_id: opts.parentId ?? null });
  rememberLink(pid, stateVersion, s.id);
  return s.id;
}

/** "Save current structure" (the server answers `existing: true` for a structure the project already holds). */
export async function saveCurrent(name?: string): Promise<void> {
  const st = useCrystalStore.getState();
  const crystal = st.frame.crystal;
  if (!crystal) return;
  const pid = await ensureProjectId();
  if (pid === null) return;
  const stateVersion = st.frame.stateVersion;
  try {
    const s = await api.addStructure(pid, { crystal, name: name?.trim() || undefined, origin: "session" });
    rememberLink(pid, stateVersion, s.id);
    useLibrary.getState().setLastSave(s.existing ? `already saved as “${s.name}”` : `saved “${s.name}”`);
    scheduleRefresh(0);
  } catch (e) {
    useLibrary.getState().setLastSave(`save failed: ${errorText(e)}`, true);
  }
}

export async function renameStructure(sid: number, name: string): Promise<void> {
  try {
    await api.updateStructure(sid, { name: name.trim() });
    await refreshStructures();
  } catch (e) {
    toast("warning", "Structure not renamed", errorText(e));
  }
}

/** Delete a stored structure and its results (the server keeps its children, with `parent_id: null`). */
export async function deleteStructure(sid: number): Promise<void> {
  try {
    await api.deleteStructure(sid);
    for (const [k, v] of savedByVersion) if (v === sid) savedByVersion.delete(k);
    const lib = useLibrary.getState();
    if (lib.link?.structureId === sid) lib.setLink(null);
    if (lib.phononOverlay?.structureId === sid) lib.setPhononOverlay(null);
    await refreshStructures();
    await refreshProjects();
  } catch (e) {
    toast("warning", "Structure not deleted", errorText(e));
  }
}

// ------------------------------------------------------------------ auto-save
interface SaveRequest {
  kind: ResultKind;
  data: object;
  settings: Record<string, unknown>;
  crystal: CrystalStatePayload;
  stateVersion: number;
  modelId: string | null;
  dispersion: boolean;
  origin?: string;
  parent?: { crystal: CrystalStatePayload; stateVersion: number } | null;
}

let saveChain: Promise<void> = Promise.resolve();

function sessionModel(): { modelId: string | null; dispersion: boolean } {
  const m = useCrystalStore.getState().model;
  return { modelId: m?.model_id ?? null, dispersion: m?.dispersion ?? false };
}

async function postResult(req: SaveRequest, retry = true): Promise<void> {
  const pid = await ensureProjectId();
  if (pid === null) return;
  const parentId = req.parent ? await ensureStructure(pid, req.parent.crystal, req.parent.stateVersion) : null;
  const sid = await ensureStructure(pid, req.crystal, req.stateVersion, { origin: req.origin, parentId });
  const body: ResultCreate = { kind: req.kind, data: req.data, settings: req.settings, dispersion: req.dispersion };
  if (req.modelId) body.model_id = req.modelId;
  try {
    await api.addResult(sid, body);
  } catch (e) {
    if (retry && e instanceof ApiError && e.status === 404) {
      // The structure was deleted from the library meanwhile: save it again.
      for (const [k, v] of savedByVersion) if (v === sid) savedByVersion.delete(k);
      return postResult(req, false);
    }
    throw e;
  }
  useLibrary.getState().setLastSave(`auto-saved ${KIND_LABEL[req.kind]} · ${req.crystal.formula}`);
  scheduleRefresh();
}

/** Record the provenance of an interactive result and, with auto-save on, store it. */
function resultFinished(req: SaveRequest): void {
  const lib = useLibrary.getState();
  lib.setProvenance(req.kind, { settings: req.settings, modelId: req.modelId, dispersion: req.dispersion, data: req.data, stateVersion: req.stateVersion });
  if (!lib.autoSave) return;
  saveChain = saveChain
    .then(() => postResult(req))
    .catch((e) => {
      useLibrary.getState().setLastSave(`auto-save of ${KIND_LABEL[req.kind]} failed: ${errorText(e)}`, true);
    });
}

// ------------------------------------------------------------------ opening stored structures
const FEED_KINDS: readonly ResultKind[] = ["relax", "phonons", "elastic", "eos", "hull", "diffraction"];

interface PendingOpen {
  pid: number;
  sid: number;
  name: string;
  results: Result[];
}
let pendingOpen: PendingOpen | null = null;
let openingFromLibrary = false;

/** Load a library structure into the session; its newest stored results fill the panes once it is active. */
export async function openStructure(sid: number): Promise<void> {
  const row = useLibrary.getState().structures.find((r) => r.id === sid);
  try {
    const s = await api.structure(sid);
    const ids = row?.latest_result_ids ?? {};
    const fetched = await Promise.all(FEED_KINDS.filter((k) => ids[k] !== undefined).map((k) => api.result(ids[k]!).catch(() => null)));
    pendingOpen = { pid: s.project_id, sid: s.id, name: s.name, results: fetched.filter((r): r is Result => r !== null) };
    openingFromLibrary = true;
    const ok = commands.loadCrystal(s.crystal);
    openingFromLibrary = false;
    if (!ok) pendingOpen = null;
  } catch (e) {
    openingFromLibrary = false;
    toast("warning", "Could not open the structure", errorText(e));
  }
}

function feedResult(r: Result, stateVersion: number): void {
  const st = useCrystalStore.getState();
  const lib = useLibrary.getState();
  const data = r.data as object;
  switch (r.kind) {
    case "phonons": {
      const p = data as PhononAnalysisComplete;
      st.setConvergence(null);
      st.setPhonon(p);
      st.setPhononSummary(summarizePhonon(p, stateVersion, null));
      const mode = pickDefaultEigenmode(p.eigenmodes);
      st.selectEigenmode(mode);
      if (mode) st.setToolMode("PHONON_SHAKE");
      break;
    }
    case "elastic":
      st.setElastic({ result: data as ElasticTensorResult, stateVersion });
      break;
    case "eos": {
      const e = data as BulkModulusResult;
      st.patchCrystal({ bulk_modulus_gpa: e.k_gpa });
      st.setEos({ result: e, stateVersion, nAtoms: st.frame.crystal?.atoms.length ?? 0 });
      break;
    }
    case "hull":
      st.setHull({ status: "ok", result: data as HullEvaluateResponse, stateVersion });
      break;
    case "diffraction":
      st.setDiffraction({ result: data as DiffractionResponse, stateVersion });
      break;
    default:
      break; // relax: provenance only (the structure itself is the result)
  }
  lib.markFromLibrary(r.kind, data);
  lib.setProvenance(r.kind, { settings: r.settings, modelId: r.model_id, dispersion: r.dispersion, data, stateVersion });
}

function applyPendingOpen(): void {
  const p = pendingOpen;
  pendingOpen = null;
  if (!p) return;
  const sv = useCrystalStore.getState().frame.stateVersion;
  savedByVersion.set(`${p.pid}:${sv}`, p.sid);
  useLibrary.getState().setLink({ projectId: p.pid, structureId: p.sid, stateVersion: sv });
  for (const r of p.results) feedResult(r, sv);
  const shown = p.results.filter((r) => r.kind !== "relax").map((r) => KIND_LABEL[r.kind]);
  if (shown.length) toast("info", `Opened ${p.name}`, `Stored ${shown.join(", ")} shown from the library (not recomputed).`);
}

/** Whether a pane's result is the one fed back from the library. */
export function isFromLibrary(kind: ResultKind, data: unknown): boolean {
  return !!data && useLibrary.getState().fromLibrary[kind] === data;
}

// ------------------------------------------------------------------ dispersion overlay
export async function loadPhononOverlay(sid: number, label: string): Promise<void> {
  const lib = useLibrary.getState();
  const rid = lib.structures.find((r) => r.id === sid)?.latest_result_ids.phonons;
  if (rid === undefined) {
    lib.setPhononOverlay({ structureId: sid, label, data: null, status: "error", error: "no stored phonons" });
    return;
  }
  lib.setPhononOverlay({ structureId: sid, label, data: null, status: "loading" });
  try {
    const r = await api.result<PhononAnalysisComplete>(rid);
    if (useLibrary.getState().phononOverlay?.structureId === sid) useLibrary.getState().setPhononOverlay({ structureId: sid, label, data: r.data, status: "ok" });
  } catch (e) {
    if (useLibrary.getState().phononOverlay?.structureId === sid) {
      useLibrary.getState().setPhononOverlay({ structureId: sid, label, data: null, status: "error", error: errorText(e) });
    }
  }
}

// ------------------------------------------------------------------ methods text
function resolveModel(modelId: string | null, dispersion: boolean): MethodsModel {
  const st = useCrystalStore.getState();
  const list = st.models?.models ?? [];
  const id = modelId ?? st.models?.default_model_id ?? null;
  const info = id ? list.find((m) => m.id === id) : undefined;
  const label = info?.label ?? id ?? st.health?.umlip_backend ?? "the default model";
  const sessionMatches = st.model ? st.model.model_id === id : !modelId;
  const dtype = (sessionMatches ? st.model?.dtype : null) ?? st.health?.dtype ?? null;
  return { label, dtype, dispersion };
}

/** Methods input from the results currently shown for the current structure. */
export function collectMethodsInput(): MethodsInput {
  const st = useCrystalStore.getState();
  const prov = useLibrary.getState().provenance;
  const sv = st.frame.stateVersion;
  const session = sessionModel();
  const pick = (kind: ResultKind, data: object | null | undefined): Provenance | null => (data && prov[kind]?.data === data ? prov[kind]! : null);
  const model = (p: Provenance | null) => (p ? resolveModel(p.modelId, p.dispersion) : resolveModel(session.modelId, session.dispersion));
  const input: MethodsInput = {};

  const rp = prov.relax;
  if (rp && rp.stateVersion === sv) {
    const frame = rp.data as RelaxationFrame;
    input.relax = { settings: rp.settings, steps: frame.step ?? null, converged: frame.converged ?? null, model: model(rp) };
  }
  if (st.phonon) {
    const p = pick("phonons", st.phonon);
    input.phonons = {
      settings: p?.settings ?? {},
      supercell: st.phonon.supercell_dim ?? null,
      cellMode: st.phonon.cell_mode ?? null,
      bandPathSource: st.phonon.band_path_source ?? null,
      model: model(p),
    };
  }
  if (st.elastic && st.elastic.stateVersion === sv) {
    const p = pick("elastic", st.elastic.result);
    input.elastic = { settings: p?.settings ?? { max_strain: st.elastic.result.max_strain, relax_ions: st.elastic.result.relaxed_ions }, model: model(p) };
  }
  if (st.eos && st.eos.stateVersion === sv) {
    const p = pick("eos", st.eos.result);
    input.eos = { settings: p?.settings ?? {}, model: model(p) };
  }
  if (st.hull.status === "ok" && st.hull.stateVersion === sv) {
    const p = pick("hull", st.hull.result);
    const h = st.hull.result;
    input.hull = { nReferences: h.references.length, chemsys: h.chemsys, candidateRelaxed: !!h.candidate_relaxed, model: model(p) };
  }
  if (st.diffraction && st.diffraction.stateVersion === sv) {
    const d = st.diffraction.result;
    const p = pick("diffraction", d);
    const lo = p?.settings.two_theta_min;
    const hi = p?.settings.two_theta_max;
    input.diffraction = {
      kind: d.kind,
      radiation: d.radiation,
      wavelength: d.wavelength_angstrom,
      range: typeof lo === "number" && typeof hi === "number" ? [lo, hi] : null,
    };
  }
  if (st.phonon && st.dos?.eph_proxy) input.superconductivity = { mesh: st.dos.mesh };
  return input;
}

export async function copyMethodsText(): Promise<void> {
  const text = buildMethodsText(collectMethodsInput());
  if (!text) {
    toast("info", "Nothing to describe", "Run a calculation on the current structure first.");
    return;
  }
  lastMethodsText = text;
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch {
    // Clipboard API blocked (insecure context, no focus): legacy copy from a hidden textarea.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    ta.remove();
  }
  const preview = text.length > 160 ? `${text.slice(0, 157)}…` : text;
  if (ok) toast("info", "Methods text copied", preview);
  else toast("warning", "Copy blocked by the browser", preview);
}

/** The last methods text built (debugging / scripted checks). */
export let lastMethodsText: string | null = null;

// ------------------------------------------------------------------ wiring
interface RelaxStart {
  start: CrystalStatePayload | null;
  startVersion: number;
  follow: { qLabel: string; band: number; frequency: number | null } | null;
}

let installed = false;

/** Hook the library into the socket (send / message listeners) and the REST result events. Idempotent. */
export function installLibrary(): void {
  if (installed) return;
  installed = true;
  const payloads = new Map<number, Record<string, unknown>>();
  const relaxStarts = new Map<number, RelaxStart>();
  const eosQueue: Record<string, unknown>[] = [];
  let pendingRelax: { frame: RelaxationFrame; settings: Record<string, unknown>; start: RelaxStart | null; stateVersion: number } | null = null;

  studio.addSendListener((type, payload, streamId) => {
    const st = useCrystalStore.getState();
    if (type === "LOAD_STRUCTURE" && !openingFromLibrary) pendingOpen = null;
    if (type === "COMPUTE_BULK_MODULUS") eosQueue.push({ ...(payload as object) });
    if (streamId === null) return;
    payloads.set(streamId, { ...(payload as object) });
    if (type === "START_RELAXATION" || type === "FOLLOW_INSTABILITY") {
      let follow: RelaxStart["follow"] = null;
      if (type === "FOLLOW_INSTABILITY") {
        const p = payload as ClientCommandMap["FOLLOW_INSTABILITY"];
        const label = st.phonon ? qLabelAt(st.phonon.kpath_labels, p.q_index) : null;
        follow = { qLabel: formatKLabel(label) || `q${p.q_index}`, band: p.band_index, frequency: st.phonon?.frequencies_thz[p.q_index]?.[p.band_index] ?? null };
      }
      relaxStarts.set(streamId, { start: st.frame.crystal, startVersion: st.frame.stateVersion, follow });
    }
  });

  const take = (ctx: MessageContext, commands: readonly string[]) => {
    const s = ctx.streams.find((x) => commands.includes(x.command));
    if (!s) return { settings: {}, stream: null };
    const settings = payloads.get(s.id) ?? {};
    payloads.delete(s.id);
    return { settings, stream: s };
  };

  /** Drop the bookkeeping of streams that ended without a result (stopped, refused). */
  const forgetEnded = () => {
    const live = new Set(useCrystalStore.getState().session.streams.map((x) => x.id));
    for (const id of [...payloads.keys()]) if (!live.has(id)) payloads.delete(id);
    for (const id of [...relaxStarts.keys()]) if (!live.has(id)) relaxStarts.delete(id);
  };

  studio.addListener((msg, ctx) => {
    const st = useCrystalStore.getState();
    const crystal = st.frame.crystal;
    switch (msg.type) {
      case "RELAXATION_FRAME": {
        if (!msg.payload.final) return;
        const head = ctx.streams[0];
        if (!head || (head.command !== "START_RELAXATION" && head.command !== "FOLLOW_INSTABILITY")) return;
        const settings = payloads.get(head.id) ?? {};
        payloads.delete(head.id);
        const start = relaxStarts.get(head.id) ?? null;
        relaxStarts.delete(head.id);
        if (head.stopping) return;
        pendingRelax = { frame: msg.payload, settings, start, stateVersion: st.frame.stateVersion };
        return;
      }
      case "HISTORY_STATE": {
        const p = pendingRelax;
        pendingRelax = null;
        if (!p) return;
        const f = p.start?.follow ?? null;
        resultFinished({
          kind: "relax",
          data: p.frame,
          settings: f ? { ...p.settings, follow: true, q_label: f.qLabel, frequency_thz: f.frequency } : p.settings,
          crystal: p.frame.crystal,
          stateVersion: p.stateVersion,
          origin: f ? `follow ${f.qLabel} ν${f.band}` : "relax",
          parent: p.start?.start ? { crystal: p.start.start, stateVersion: p.start.startVersion } : null,
          ...sessionModel(),
        });
        return;
      }
      case "PHONON_ANALYSIS_COMPLETE": {
        const { settings } = take(ctx, ["RUN_PHONON_SHAKE_TEST", "CHECK_PHONON_CONVERGENCE"]);
        if (crystal) resultFinished({ kind: "phonons", data: msg.payload, settings, crystal, stateVersion: st.frame.stateVersion, ...sessionModel() });
        return;
      }
      case "ELASTIC_TENSOR_RESULT": {
        const { settings } = take(ctx, ["COMPUTE_ELASTIC_TENSOR"]);
        if (crystal) resultFinished({ kind: "elastic", data: msg.payload, settings, crystal, stateVersion: st.frame.stateVersion, ...sessionModel() });
        return;
      }
      case "BULK_MODULUS_RESULT": {
        const settings = eosQueue.shift() ?? {};
        if (crystal) resultFinished({ kind: "eos", data: msg.payload, settings, crystal, stateVersion: st.frame.stateVersion, ...sessionModel() });
        return;
      }
      case "SINGLE_POINT_UPDATE":
        if (pendingOpen) applyPendingOpen();
        return;
      case "STREAM_STOPPED":
        forgetEnded();
        return;
      case "ERROR": {
        forgetEnded();
        const p = msg.payload;
        if (p.code === "RELAXATION_DIVERGED") pendingRelax = null;
        if (p.command === "COMPUTE_BULK_MODULUS") eosQueue.shift();
        if (p.command === "LOAD_STRUCTURE" && pendingOpen) pendingOpen = null;
        return;
      }
    }
  });

  onResult((e: ResultEvent) => resultFinished({ ...e }));
}
