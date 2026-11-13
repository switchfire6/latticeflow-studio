/**
 * LatticeFlow Studio client state (zustand).
 *
 * Two layers:
 *  * React state (this store's fields): only what the HUD shows. High-rate
 *    updates (flow frames, relaxation frames, drag echoes, stream progress,
 *    relaxation history) reach it through a 10 Hz leading+trailing throttle;
 *    lifecycle changes (stream start/end, phonon results, errors) publish at once.
 *  * Transient buffers (`frame`, a mutable FrameBuffers instance whose identity
 *    never changes): every structure is copied in synchronously; the viewport
 *    reads it in `useFrame` via `useCrystalStore.getState().frame`.
 *
 * The server-event reducer (phonon invalidation, stream FIFO) lives in
 * services/wsClient.ts (`dispatchServerMessage`), which calls the actions below.
 */
import { create } from "zustand";
import type {
  BulkModulusResult,
  ClientCommand,
  DiffractionResponse,
  ElasticTensorResult,
  CrystalStatePayload,
  Eigenmode,
  ErrorPayload,
  HealthResponse,
  HistoryState,
  HullEvaluateResponse,
  ModelChanged,
  ModelListResponse,
  PhononAnalysisComplete,
  PhononConvergenceResult,
  PhononDosData,
  PresetInfo,
  Progress,
} from "../types/protocol";
import type { ExperimentalPattern } from "../utils/diffraction";
import { loadJSON, saveJSON } from "../utils/persist";
import { atomsPerFormulaUnit, type LatticeReference } from "../utils/structureInfo";
import { DEFAULT_PHONON_PARAMS, type PhononAnimParams } from "../utils/phonon";
import { throttle, type Clock, realClock } from "../utils/throttle";
import { FrameBuffers, type CrystalSource } from "./frameBuffers";

export type { CrystalSource } from "./frameBuffers";

export type ConnectionState = "connecting" | "open" | "closed";
export type ToolMode = "VIEW" | "DRAG_ATOM" | "TRANSMUTE" | "PHONON_SHAKE";

export type StreamCommand = Extract<
  ClientCommand,
  | "START_FLOW_GENERATION"
  | "START_RELAXATION"
  | "RUN_PHONON_SHAKE_TEST"
  | "FOLLOW_INSTABILITY"
  | "PERTURB_OR_TRANSMUTE_ATOM"
  // Phase B
  | "CHECK_PHONON_CONVERGENCE"
  | "COMPUTE_ELASTIC_TENSOR"
  | "SET_MODEL"
>;
export type StreamKind = "flow" | "relaxation" | "phonon" | "elastic" | "model";

export function streamKind(command: StreamCommand): StreamKind {
  if (command === "START_FLOW_GENERATION") return "flow";
  if (command === "RUN_PHONON_SHAKE_TEST" || command === "CHECK_PHONON_CONVERGENCE") return "phonon";
  if (command === "COMPUTE_ELASTIC_TENSOR") return "elastic";
  if (command === "SET_MODEL") return "model";
  return "relaxation"; // START_RELAXATION, FOLLOW_INSTABILITY, PERTURB(auto_relax)
}

export interface StreamProgress {
  step: number | null;
  totalSteps: number | null;
  t: number | null;
  maxForce: number | null;
  energyPerAtom: number | null;
  /** Phase B: latest PROGRESS event of this stream (done / total + message). */
  done?: number | null;
  total?: number | null;
  message?: string | null;
}

export interface ActiveStream {
  id: number;
  command: StreamCommand;
  kind: StreamKind;
  startedAt: number;
  /** STOP_STREAM was sent for it. */
  stopping: boolean;
  progress: StreamProgress;
}

export interface RelaxPoint {
  step: number;
  energyPerAtom: number;
  maxForce: number;
}

export type ToastLevel = "error" | "warning" | "info";
/** Serializable toast action; the Toasts component maps the kind to a handler. */
export type ToastAction = { kind: "continue-relaxation"; label: string };
export interface Toast {
  id: number;
  level: ToastLevel;
  title: string;
  message: string;
  at: number;
  action?: ToastAction;
}

export type Representation = "ball-and-stick" | "space-filling";
export interface DisplayOptions {
  representation: Representation;
  ghostHalo: boolean;
  forceVectors: boolean;
  bonds: boolean;
  trails: boolean;
  unitCell: boolean;
  postprocessing: boolean;
}

export const DEFAULT_DISPLAY: DisplayOptions = {
  representation: "ball-and-stick",
  ghostHalo: false,
  forceVectors: true,
  bonds: true,
  trails: true,
  unitCell: true,
  postprocessing: true,
};

/** Compact description of a structure, for before/after comparisons. */
export interface StructureSummary {
  formula: string;
  spacegroupSymbol: string;
  spacegroupNumber: number;
  energyPerAtom: number;
  maxForce: number;
  nAtoms: number;
  /** N / Z (for per-formula-unit energies). */
  atomsPerFormulaUnit: number;
  volumePerAtom: number;
}

export function summarize(c: CrystalStatePayload): StructureSummary {
  return {
    formula: c.formula,
    spacegroupSymbol: c.spacegroup_symbol,
    spacegroupNumber: c.spacegroup_number,
    energyPerAtom: c.energy_per_atom,
    maxForce: c.max_force,
    nAtoms: c.atoms.length,
    atomsPerFormulaUnit: atomsPerFormulaUnit(c),
    volumePerAtom: c.lattice.volume / Math.max(1, c.atoms.length),
  };
}

/** What FOLLOW_INSTABILITY started from and (once finished) where it ended. */
export interface FollowResult {
  from: StructureSummary;
  to: StructureSummary | null;
  qLabel: string | null;
  bandIndex: number;
  frequencyThz: number;
  amplitude: number;
  status: "running" | "converged" | "max_steps" | "stopped" | "failed";
  steps: number | null;
}

export type HullState =
  | { status: "idle" }
  | { status: "busy"; startedAt: number; stateVersion: number }
  | { status: "ok"; result: HullEvaluateResponse; stateVersion: number }
  | { status: "unavailable"; message: string; stateVersion: number }
  | { status: "error"; code: string; message: string; stateVersion: number };

export type DockPane = "dispersion" | "dos" | "rdf" | "relaxation" | "eos" | "elastic" | "xrd";
export const DOCK_PANES: readonly DockPane[] = ["dispersion", "dos", "rdf", "relaxation", "eos", "elastic", "xrd"];
/** Panes that appear only once requested (button / dock toggle) or when they hold a result. */
export const ON_DEMAND_PANES: readonly DockPane[] = ["elastic", "xrd"];
export interface DockState {
  open: boolean;
  height: number;
  hidden: DockPane[];
  weights: Record<DockPane, number>;
}
export const DEFAULT_DOCK: DockState = {
  open: true,
  height: 300,
  // The DOS pane is offered (toggle + "DOS & thermo" button) but not forced into the layout.
  hidden: ["dos"],
  weights: { dispersion: 1.7, dos: 1.5, rdf: 1, relaxation: 1.2, eos: 1.4, elastic: 1.5, xrd: 1.7 },
};

/** Merge a stored dock layout over the defaults (older layouts lack the Phase A panes). */
export function normalizeDock(d: Partial<DockState> | null | undefined): DockState {
  const weights = { ...DEFAULT_DOCK.weights };
  const w = d?.weights;
  // A layout saved before the Phase A panes existed: keep the DOS pane hidden like a fresh default.
  const legacy = !!w && typeof w === "object" && !("dos" in w);
  if (w && typeof w === "object") {
    for (const p of DOCK_PANES) {
      const v = (w as Record<string, unknown>)[p];
      if (typeof v === "number" && Number.isFinite(v) && v > 0) weights[p] = v;
    }
  }
  return {
    open: typeof d?.open === "boolean" ? d.open : DEFAULT_DOCK.open,
    height: typeof d?.height === "number" && Number.isFinite(d.height) ? d.height : DEFAULT_DOCK.height,
    hidden: Array.isArray(d?.hidden)
      ? [
          ...d.hidden.filter((p): p is DockPane => DOCK_PANES.includes(p as DockPane)),
          ...(legacy && !d.hidden.includes("dos") ? (["dos"] as DockPane[]) : []),
        ]
      : [...DEFAULT_DOCK.hidden],
    weights,
  };
}

/** BULK_MODULUS_RESULT tied to the structure state it was computed for. */
export interface EosState {
  result: BulkModulusResult;
  stateVersion: number;
  nAtoms: number;
}

/** Phase B: what the scorecard keeps of the latest phonon analysis (survives its invalidation, then shown stale). */
export interface PhononSummary {
  stable: boolean;
  minFrequencyThz: number;
  imaginaryCount: number;
  supercellDim: [number, number, number] | null;
  minImageDistance: number | null;
  cellMode: "as_displayed" | "primitive" | null;
  stateVersion: number;
  convergence: PhononConvergenceResult | null;
}

export function summarizePhonon(p: PhononAnalysisComplete, stateVersion: number, convergence: PhononConvergenceResult | null): PhononSummary {
  return {
    stable: p.is_dynamically_stable,
    minFrequencyThz: p.min_frequency_thz,
    imaginaryCount: p.imaginary_mode_count,
    supercellDim: p.supercell_dim ?? null,
    minImageDistance: p.min_image_distance ?? null,
    cellMode: p.cell_mode ?? null,
    stateVersion,
    convergence,
  };
}

/** ELASTIC_TENSOR_RESULT tied to the state version it was computed for. */
export interface ElasticState {
  result: ElasticTensorResult;
  stateVersion: number;
}

/** POST /api/diffraction result for a state version. */
export interface DiffractionState {
  result: DiffractionResponse;
  stateVersion: number;
}

/** A relaxation that stopped at max_steps without converging ("Continue relaxation"). */
export interface UnconvergedRelaxation {
  steps: number;
  maxForce: number;
}
export const DOCK_MIN_HEIGHT = 150;
/** Visible panes beyond this squeeze the charts at 1280 px; revealing another hides g(r). */
export const MAX_COMFORTABLE_PANES = 3;
export const DOCK_MAX_HEIGHT = 560;

export const HUD_THROTTLE_MS = 100;
export const RELAX_HISTORY_LIMIT = 1000;
const TOAST_LIMIT = 4;

/** Mutable, non-reactive bookkeeping shared by the dispatcher and the actions. */
export interface SessionBookkeeping {
  /** The SINGLE_POINT_UPDATE right after PHONON_ANALYSIS_COMPLETE carries actor flags and must not invalidate it. */
  awaitingActorFlags: boolean;
  nextStreamId: number;
  /** Streams with id < this were sent before the latest STOP_STREAM. */
  stopBarrier: number;
  /** Stream id the current relaxation history belongs to. */
  relaxHistoryStreamId: number | null;
  /** Live (unpublished) stream list; `streams` in state is its published snapshot. */
  streams: ActiveStream[];
  relaxHistory: RelaxPoint[];
  /** Bumped on every relaxation point (charts redraw on change). */
  relaxVersion: number;
  /** fmax of the latest relaxation-type command (target line of the relaxation curve). */
  relaxFmax: number;
  /** Supercell of the latest RUN_PHONON_SHAKE_TEST (progress text). */
  phononSupercell: [number, number, number];
  /** min_supercell_length of the latest shake-test (null = explicit supercell). */
  phononMinLength: number | null;
  /** PHONON_CONVERGENCE_RESULT seen; the next PHONON_ANALYSIS_COMPLETE belongs to it. */
  convergenceArrived: boolean;
  nextToastId: number;
}

export interface CrystalStoreState {
  // connection
  connection: ConnectionState;
  reconnectAttempt: number;
  health: HealthResponse | null;
  healthError: string | null;

  // structure (throttled HUD copy of frame.crystal)
  crystal: CrystalStatePayload | null;
  crystalSource: CrystalSource | null;
  structureVersion: number;
  topologyVersion: number;
  /** frame.stateVersion at the last publish (hull results are tied to it). */
  stateVersion: number;

  // streams: FIFO in server order, streams[0] is the one the server is running
  streams: ActiveStream[];

  // tools & selection
  toolMode: ToolMode;
  selectedAtom: number | null;
  hoveredAtom: number | null;

  // phonons
  phonon: PhononAnalysisComplete | null;
  selectedEigenmode: Eigenmode | null;
  phononParams: PhononAnimParams;
  phononPlaying: boolean;
  /** The (q, band) picked in the chart / mode list; may be ahead of selectedEigenmode while REQUEST_EIGENMODE is in flight. */
  modeSelection: { qIndex: number; band: number } | null;
  followResult: FollowResult | null;
  hull: HullState;
  dock: DockState;

  // Phase A
  history: HistoryState | null;
  eos: EosState | null;
  eosPending: boolean;
  dos: PhononDosData | null;
  dosPending: boolean;
  /** Lattice at the start of the latest relaxation / follow (Δa, ΔV in the Structure card). */
  latticeRef: LatticeReference | null;
  relaxUnconverged: UnconvergedRelaxation | null;

  // Phase B
  /** The session's model (MODEL_CHANGED); null = process default (see health). */
  model: ModelChanged | null;
  models: ModelListResponse | null;
  modelsError: string | null;
  /** Latest PROGRESS per command (cleared when the command ends). */
  progress: Partial<Record<ClientCommand, Progress>>;
  phononSummary: PhononSummary | null;
  /** Convergence check of the current phonon analysis (cleared with it). */
  convergence: PhononConvergenceResult | null;
  elastic: ElasticState | null;
  diffraction: DiffractionState | null;
  diffractionPending: boolean;
  diffractionError: string | null;
  xrdExperimental: ExperimentalPattern | null;
  phononExportPending: boolean;
  /** On-demand panes the user opened this session. */
  openedPanes: DockPane[];

  // misc
  relaxHistory: RelaxPoint[];
  toasts: Toast[];
  lastError: ErrorPayload | null;
  display: DisplayOptions;
  presets: PresetInfo[];

  // non-reactive (identity never changes)
  frame: FrameBuffers;
  session: SessionBookkeeping;

  // ---- actions ----
  setConnection(state: ConnectionState, reconnectAttempt?: number): void;
  setHealth(health: HealthResponse | null, error?: string | null): void;
  setPresets(presets: PresetInfo[]): void;
  /** Server session was (re)created: drop streams, phonons, selection, trails. */
  resetSession(): void;

  ingestCrystal(crystal: CrystalStatePayload, source: CrystalSource): void;

  beginStream(command: StreamCommand): ActiveStream;
  /** Mark every in-flight stream as stopping (STOP_STREAM sent). */
  markStopping(): void;
  /**
   * Remove one stream entry. Without `command` the head is removed; with it, the
   * first (or, with `fromBack`, the last) entry with that command.
   * Returns the removed entry.
   */
  endStream(opts?: { command?: ClientCommand; fromBack?: boolean }): ActiveStream | null;
  /** Remove every stream sent before the latest STOP_STREAM (answer `{stopped: null}`). */
  dropStreamsBeforeStopBarrier(): void;
  /** The head stream, adopting one of `command` if nothing is tracked (e.g. frames after a client reload). */
  ensureHeadStream(kind: StreamKind, command: StreamCommand): ActiveStream;
  updateStreamProgress(progress: Partial<StreamProgress>): void;
  pushRelaxPoint(streamId: number, point: RelaxPoint): void;

  setPhonon(analysis: PhononAnalysisComplete): void;
  clearPhonon(): void;
  selectEigenmode(mode: Eigenmode | null): void;
  setPhononParams(p: Partial<PhononAnimParams>): void;
  setPhononPlaying(playing: boolean): void;
  setModeSelection(sel: { qIndex: number; band: number } | null): void;
  setFollowResult(r: FollowResult | null): void;
  setHull(h: HullState): void;
  setDock(p: Partial<DockState>): void;
  /**
   * Show a dock pane (open the dock, un-hide the pane). With `visible` (the
   * panes currently shown) holding MAX_COMFORTABLE_PANES or more others, g(r)
   * is hidden to make room.
   */
  revealPane(p: DockPane, visible?: readonly DockPane[]): void;

  setHistory(h: HistoryState | null): void;
  setEos(e: EosState | null): void;
  setEosPending(p: boolean): void;
  setDos(d: PhononDosData | null): void;
  setDosPending(p: boolean): void;
  setLatticeRef(r: LatticeReference | null): void;
  setRelaxUnconverged(r: UnconvergedRelaxation | null): void;
  /** Patch fields of the current crystal without counting it as a structure change (e.g. bulk_modulus_gpa). */
  patchCrystal(p: Partial<CrystalStatePayload>): void;

  // Phase B
  setModel(m: ModelChanged | null): void;
  setModels(m: ModelListResponse | null, error?: string | null): void;
  /** Record a PROGRESS event (also on the matching stream entry). */
  setProgress(p: Progress): void;
  clearProgress(command: ClientCommand): void;
  setPhononSummary(p: PhononSummary | null): void;
  setConvergence(c: PhononConvergenceResult | null): void;
  setElastic(e: ElasticState | null): void;
  setDiffraction(d: DiffractionState | null): void;
  setDiffractionPending(p: boolean, error?: string | null): void;
  setXrdExperimental(p: ExperimentalPattern | null): void;
  setPhononExportPending(p: boolean): void;
  /** Make an on-demand pane available and show it. */
  openPane(p: DockPane, visible?: readonly DockPane[]): void;
  /**
   * Count a non-structural change of the evaluated state (model switch) as a
   * new state version, so every result stamped with the old one reads stale.
   */
  bumpStateVersion(): void;

  setToolMode(mode: ToolMode): void;
  selectAtom(index: number | null): void;
  setHoveredAtom(index: number | null): void;
  setDisplay(p: Partial<DisplayOptions>): void;

  pushToast(level: ToastLevel, title: string, message: string, action?: ToastAction): number;
  dismissToast(id: number): void;
  setLastError(err: ErrorPayload | null): void;

  /** Publish pending throttled HUD state now (tests; also used before lifecycle changes). */
  flushHud(): void;
}

export interface CrystalStoreOptions {
  clock?: Clock;
  hudThrottleMs?: number;
  /** localStorage key for the dock layout (null = not persisted, e.g. in tests). */
  dockStorageKey?: string | null;
}

const emptyProgress = (): StreamProgress => ({ step: null, totalSteps: null, t: null, maxForce: null, energyPerAtom: null });

export function createCrystalStore(opts: CrystalStoreOptions = {}) {
  const clock = opts.clock ?? realClock;
  return create<CrystalStoreState>()((set, get) => {
    const frame = new FrameBuffers();
    const session: SessionBookkeeping = {
      awaitingActorFlags: false,
      nextStreamId: 1,
      stopBarrier: 0,
      relaxHistoryStreamId: null,
      streams: [],
      relaxHistory: [],
      relaxVersion: 0,
      relaxFmax: 0.02,
      phononSupercell: [2, 2, 2],
      phononMinLength: null,
      convergenceArrived: false,
      nextToastId: 1,
    };
    const dockKey = opts.dockStorageKey ?? null;
    const initialDock: DockState = normalizeDock(dockKey ? loadJSON<Partial<DockState>>(dockKey, DEFAULT_DOCK) : DEFAULT_DOCK);

    const snapshotStreams = () => session.streams.map((s) => ({ ...s, progress: { ...s.progress } }));

    /** Copy every throttled field into React state. */
    const publish = () => {
      set({
        crystal: frame.crystal,
        crystalSource: frame.crystal ? frame.source : null,
        structureVersion: frame.version,
        topologyVersion: frame.topologyVersion,
        stateVersion: frame.stateVersion,
        streams: snapshotStreams(),
        relaxHistory: session.relaxHistory.slice(),
      });
    };
    const hud = throttle(publish, opts.hudThrottleMs ?? HUD_THROTTLE_MS, clock);
    const publishNow = () => {
      hud.cancel();
      publish();
    };

    const clampSelection = () => {
      const { selectedAtom, hoveredAtom } = get();
      const patch: Partial<CrystalStoreState> = {};
      if (selectedAtom !== null && selectedAtom >= frame.n) patch.selectedAtom = null;
      if (hoveredAtom !== null && hoveredAtom >= frame.n) patch.hoveredAtom = null;
      if (Object.keys(patch).length) set(patch);
    };

    return {
      connection: "connecting",
      reconnectAttempt: 0,
      health: null,
      healthError: null,
      crystal: null,
      crystalSource: null,
      structureVersion: 0,
      topologyVersion: 0,
      stateVersion: 0,
      streams: [],
      toolMode: "DRAG_ATOM",
      selectedAtom: null,
      hoveredAtom: null,
      phonon: null,
      selectedEigenmode: null,
      phononParams: { ...DEFAULT_PHONON_PARAMS },
      phononPlaying: true,
      modeSelection: null,
      followResult: null,
      hull: { status: "idle" },
      dock: initialDock,
      history: null,
      eos: null,
      eosPending: false,
      dos: null,
      dosPending: false,
      latticeRef: null,
      relaxUnconverged: null,
      model: null,
      models: null,
      modelsError: null,
      progress: {},
      phononSummary: null,
      convergence: null,
      elastic: null,
      diffraction: null,
      diffractionPending: false,
      diffractionError: null,
      xrdExperimental: null,
      phononExportPending: false,
      openedPanes: [],
      relaxHistory: [],
      toasts: [],
      lastError: null,
      display: { ...DEFAULT_DISPLAY },
      presets: [],
      frame,
      session,

      setConnection: (connection, reconnectAttempt) =>
        set((s) => ({ connection, reconnectAttempt: reconnectAttempt ?? s.reconnectAttempt })),
      setHealth: (health, error = null) => set({ health, healthError: error }),
      setPresets: (presets) => set({ presets }),

      resetSession: () => {
        session.streams = [];
        session.awaitingActorFlags = false;
        session.convergenceArrived = false;
        session.stopBarrier = 0;
        frame.drag = null;
        frame.phonon.active = false;
        frame.trails.reset(0);
        set((s) => ({
          phonon: null,
          selectedEigenmode: null,
          selectedAtom: null,
          hoveredAtom: null,
          toolMode: s.toolMode === "PHONON_SHAKE" ? "DRAG_ATOM" : s.toolMode,
          history: null,
          eosPending: false,
          dos: null,
          dosPending: false,
          relaxUnconverged: null,
          // A fresh server session runs the process-default model.
          model: null,
          progress: {},
          convergence: null,
          phononExportPending: false,
        }));
        publishNow();
      },

      ingestCrystal: (crystal, source) => {
        const topologyChanged = frame.ingest(crystal, source);
        if (topologyChanged) {
          clampSelection();
          publishNow();
        } else {
          hud();
        }
      },

      beginStream: (command) => {
        const entry: ActiveStream = {
          id: session.nextStreamId++,
          command,
          kind: streamKind(command),
          startedAt: clock.now(),
          stopping: false,
          progress: emptyProgress(),
        };
        session.streams.push(entry);
        publishNow();
        return entry;
      },

      markStopping: () => {
        session.stopBarrier = session.nextStreamId;
        for (const s of session.streams) s.stopping = true;
        publishNow();
      },

      endStream: (o = {}) => {
        const list = session.streams;
        let idx = -1;
        if (o.command === undefined) idx = list.length ? 0 : -1;
        else if (o.fromBack) {
          for (let i = list.length - 1; i >= 0; i--) if (list[i]!.command === o.command) { idx = i; break; }
        } else idx = list.findIndex((s) => s.command === o.command);
        if (idx < 0) return null;
        const [removed] = list.splice(idx, 1);
        publishNow();
        return removed ?? null;
      },

      dropStreamsBeforeStopBarrier: () => {
        const before = session.streams.length;
        session.streams = session.streams.filter((s) => s.id >= session.stopBarrier);
        if (session.streams.length !== before) publishNow();
      },

      ensureHeadStream: (kind, command) => {
        const head = session.streams[0];
        if (head && head.kind === kind) return head;
        if (head) {
          // The server only runs one stream; a frame of another kind means the
          // tracked head already ended without us seeing it (should not happen).
          session.streams.shift();
        }
        const entry: ActiveStream = {
          id: session.nextStreamId++,
          command,
          kind,
          startedAt: clock.now(),
          stopping: false,
          progress: emptyProgress(),
        };
        session.streams.unshift(entry);
        publishNow();
        return entry;
      },

      updateStreamProgress: (p) => {
        const head = session.streams[0];
        if (!head) return;
        Object.assign(head.progress, p);
        hud();
      },

      pushRelaxPoint: (streamId, point) => {
        if (session.relaxHistoryStreamId !== streamId) {
          session.relaxHistoryStreamId = streamId;
          session.relaxHistory = [];
        }
        session.relaxHistory.push(point);
        if (session.relaxHistory.length > RELAX_HISTORY_LIMIT) session.relaxHistory.shift();
        session.relaxVersion++;
        hud();
      },

      setPhonon: (analysis) => set({ phonon: analysis }),

      clearPhonon: () => {
        const s = get();
        frame.phonon.active = false;
        if (s.phonon === null && s.selectedEigenmode === null && s.modeSelection === null && s.dos === null && !s.dosPending && s.convergence === null) return;
        set({
          phonon: null,
          selectedEigenmode: null,
          modeSelection: null,
          toolMode: s.toolMode === "PHONON_SHAKE" ? "DRAG_ATOM" : s.toolMode,
          dos: null,
          dosPending: false,
          convergence: null,
        });
      },

      selectEigenmode: (mode) => {
        if (mode === null) frame.phonon.active = false;
        set({ selectedEigenmode: mode, modeSelection: mode ? { qIndex: mode.q_index, band: mode.band_index } : null });
      },
      setPhononParams: (p) => set((s) => ({ phononParams: { ...s.phononParams, ...p } })),
      setPhononPlaying: (phononPlaying) => set({ phononPlaying }),
      setModeSelection: (modeSelection) => set({ modeSelection }),
      setFollowResult: (followResult) => set({ followResult }),
      setHull: (hull) => set({ hull }),
      setDock: (p) => {
        const dock = { ...get().dock, ...p };
        dock.height = Math.min(DOCK_MAX_HEIGHT, Math.max(DOCK_MIN_HEIGHT, Math.round(dock.height)));
        set({ dock });
        if (dockKey) saveJSON(dockKey, dock);
      },
      revealPane: (p, visible) => {
        const d = get().dock;
        if (d.open && !d.hidden.includes(p)) return;
        const hidden = d.hidden.filter((x) => x !== p);
        const shown = (visible ?? []).filter((x) => x !== p && !hidden.includes(x));
        if (shown.length >= MAX_COMFORTABLE_PANES && p !== "rdf" && shown.includes("rdf")) hidden.push("rdf");
        get().setDock({ open: true, hidden });
      },

      setHistory: (history) => set({ history }),
      setEos: (eos) => set({ eos }),
      setEosPending: (eosPending) => set({ eosPending }),
      setDos: (dos) => set({ dos }),
      setDosPending: (dosPending) => set({ dosPending }),
      setLatticeRef: (latticeRef) => set({ latticeRef }),
      setRelaxUnconverged: (relaxUnconverged) => {
        if (get().relaxUnconverged !== relaxUnconverged) set({ relaxUnconverged });
      },
      patchCrystal: (p) => {
        if (!frame.crystal) return;
        frame.crystal = { ...frame.crystal, ...p };
        frame.version++;
        publishNow();
      },

      setModel: (model) => set({ model }),
      setModels: (models, error = null) => set({ models, modelsError: error }),
      setProgress: (p) => {
        const entry = session.streams.find((x) => x.command === p.command);
        if (entry) Object.assign(entry.progress, { done: p.done, total: p.total, message: p.message });
        set((s) => ({ progress: { ...s.progress, [p.command]: p }, streams: snapshotStreams() }));
      },
      clearProgress: (command) => {
        if (!(command in get().progress)) return;
        set((s) => {
          const next = { ...s.progress };
          delete next[command];
          return { progress: next };
        });
      },
      setPhononSummary: (phononSummary) => set({ phononSummary }),
      setConvergence: (convergence) => set({ convergence }),
      setElastic: (elastic) => set({ elastic }),
      setDiffraction: (diffraction) => set({ diffraction }),
      setDiffractionPending: (diffractionPending, error) =>
        set((s) => ({ diffractionPending, diffractionError: error === undefined ? s.diffractionError : error })),
      setXrdExperimental: (xrdExperimental) => set({ xrdExperimental }),
      setPhononExportPending: (phononExportPending) => set({ phononExportPending }),
      openPane: (p, visible) => {
        if (ON_DEMAND_PANES.includes(p) && !get().openedPanes.includes(p)) set((s) => ({ openedPanes: [...s.openedPanes, p] }));
        get().revealPane(p, visible);
      },
      bumpStateVersion: () => {
        frame.stateVersion++;
        publishNow();
      },

      setToolMode: (toolMode) => set({ toolMode }),
      selectAtom: (index) => set({ selectedAtom: index !== null && index >= 0 && index < frame.n ? index : null }),
      setHoveredAtom: (index) => {
        if (get().hoveredAtom !== index) set({ hoveredAtom: index });
      },
      setDisplay: (p) => set((s) => ({ display: { ...s.display, ...p } })),

      pushToast: (level, title, message, action) => {
        const id = session.nextToastId++;
        const toast: Toast = { id, level, title, message, at: Date.now(), ...(action ? { action } : {}) };
        set((s) => ({ toasts: [...s.toasts, toast].slice(-TOAST_LIMIT) }));
        return id;
      },
      dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
      setLastError: (lastError) => set({ lastError }),

      flushHud: () => publishNow(),
    };
  });
}

export type CrystalStore = ReturnType<typeof createCrystalStore>;

/** The app-wide store. Tests create isolated ones with `createCrystalStore()`. */
export const useCrystalStore: CrystalStore = createCrystalStore({ dockStorageKey: "latticeflow.dock.v1" });

// Selectors
export const selectActiveStream = (s: CrystalStoreState): ActiveStream | null => s.streams[0] ?? null;
export const selectIsStreaming = (kind?: StreamKind) => (s: CrystalStoreState) =>
  kind ? s.streams.some((x) => x.kind === kind) : s.streams.length > 0;
