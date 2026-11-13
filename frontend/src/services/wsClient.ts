/**
 * Reconnecting `/ws/studio` client and the server-event dispatcher
 * (PROJECT_SPEC.md §5.2, including "Session & streaming rules").
 *
 * Stream tracking: the server runs at most one stream, answers commands in
 * order and ends every stream with exactly one terminal message, so the client
 * keeps a FIFO of the streaming commands it sent (`streams`, head = running)
 * and pops it on each terminal message:
 *   flow       CRYSTAL_STATE_FINAL | STREAM_STOPPED | ERROR
 *   relaxation RELAXATION_FRAME.final | STREAM_STOPPED   (+ ERROR RELAXATION_DIVERGED after final)
 *   phonon     PHONON_ANALYSIS_COMPLETE | STREAM_STOPPED | ERROR
 * A refused streaming command (validation / precondition ERROR) never started,
 * so its ERROR removes the newest entry of that command, not the head.
 *
 * Phonon invalidation: any state change invalidates the analysis server-side,
 * so the client clears it on flow frames, CRYSTAL_STATE_FINAL, RELAXATION_FRAME
 * and SINGLE_POINT_UPDATE - except the SINGLE_POINT_UPDATE that immediately
 * follows PHONON_ANALYSIS_COMPLETE (it carries the is_unstable_actor flags).
 *
 * Phase A (IMPROVEMENT_SUGGESTIONS.md): HISTORY_STATE (undo/redo), instant
 * BULK_MODULUS_RESULT (K is patched into the crystal client-side, the EOS is
 * stale once the structure changes) and PHONON_DOS_DATA (dropped with the
 * phonon analysis). The StudioClient also restores the last committed
 * structure after a reconnect (SessionMemory) and can request the DOS after
 * every shake-test.
 */
import { wsUrl } from "../config";
import { badgeCriteria, phononPayload, relaxPayload, useSettings } from "../store/settings";
import { summarize, useCrystalStore, type CrystalStore, type StreamCommand, type ToastLevel } from "../store/useCrystalStore";
import type {
  ClientCommand,
  ClientCommandMap,
  CrystalStatePayload,
  ErrorCode,
  ErrorPayload,
  ServerEvent,
  ServerMessage,
  StructureTextFormat,
} from "../types/protocol";
import { registerDefaultSend } from "./phononActions";
import { SessionMemory } from "./sessionMemory";
import { pickDefaultEigenmode } from "../utils/phonon";
import { latticeLengths, phononWarning, relaxationVerdict, sameStructure } from "../utils/structureInfo";

const SERVER_EVENTS: ReadonlySet<string> = new Set<ServerEvent>([
  "FLOW_TRAJECTORY_FRAME",
  "CRYSTAL_STATE_FINAL",
  "SINGLE_POINT_UPDATE",
  "RELAXATION_FRAME",
  "PHONON_ANALYSIS_COMPLETE",
  "EIGENMODE_DATA",
  "STREAM_STOPPED",
  "ERROR",
  "BULK_MODULUS_RESULT",
  "PHONON_DOS_DATA",
  "HISTORY_STATE",
]);

const STREAM_COMMANDS: ReadonlySet<ClientCommand> = new Set<ClientCommand>([
  "START_FLOW_GENERATION",
  "START_RELAXATION",
  "RUN_PHONON_SHAKE_TEST",
  "FOLLOW_INSTABILITY",
  "PERTURB_OR_TRANSMUTE_ATOM",
]);

/** Errors that arrive after a stream's terminal message and must not pop another stream. */
const POST_TERMINAL_ERRORS: ReadonlySet<ErrorCode> = new Set<ErrorCode>(["RELAXATION_DIVERGED"]);

/** Commands that change the server structure (a user sending one cancels a pending session restore). */
export const STATE_CHANGING_COMMANDS: ReadonlySet<ClientCommand> = new Set<ClientCommand>([
  "START_FLOW_GENERATION",
  "PERTURB_OR_TRANSMUTE_ATOM",
  "START_RELAXATION",
  "LOAD_STRUCTURE",
  "DELETE_ATOM",
  "FOLLOW_INSTABILITY",
  "REFINE_STRUCTURE",
  "UNDO",
  "REDO",
]);

export const SESSION_RESTORED_TITLE = "Session restored";
export const SESSION_RESTORED_MESSAGE = "Your last structure was reloaded after the reconnect (phonons need re-running).";

/** Parse and validate a raw frame; returns null for anything that is not a known ServerMessage envelope. */
export function parseServerMessage(raw: string): ServerMessage | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null) return null;
  const { type, payload } = data as { type?: unknown; payload?: unknown };
  if (typeof type !== "string" || !SERVER_EVENTS.has(type) || typeof payload !== "object" || payload === null) return null;
  return data as ServerMessage;
}

function errorTitle(code: string): string {
  return code
    .toLowerCase()
    .split("_")
    .map((w) => w[0]!.toUpperCase() + w.slice(1))
    .join(" ");
}

/** User-facing title and message for an ERROR event (with actionable hints for the phonon workflow). */
export function describeError(p: ErrorPayload): { title: string; message: string; level?: ToastLevel } {
  const detail = p.message ? ` (${p.message})` : "";
  if ((p.command === "UNDO" || p.command === "REDO") && p.code === "INVALID_OPERATION") {
    return { title: p.command === "UNDO" ? "Nothing to undo" : "Nothing to redo", message: p.message || "The history is at its end.", level: "info" };
  }
  if (p.command === "REQUEST_PHONON_DOS" && p.code === "NO_PHONON_ANALYSIS") {
    return { title: "Phonons out of date", message: `Run the shake-test on the current structure before computing the DOS${detail}.` };
  }
  if (p.command === "COMPUTE_BULK_MODULUS") {
    return { title: "Bulk modulus failed", message: p.message };
  }
  if (p.command === "REFINE_STRUCTURE") {
    return { title: "Symmetry refinement failed", message: p.message };
  }
  if (p.command === "LOAD_STRUCTURE" && p.code === "INVALID_ELEMENT") {
    return { title: "Element not supported", message: `The model covers Z ≤ 89 (H–Ac)${detail}.` };
  }
  if (p.command === "LOAD_STRUCTURE" && (p.code === "INVALID_STRUCTURE" || p.code === "INVALID_PAYLOAD" || p.code === "DISORDERED_STRUCTURE")) {
    return { title: p.code === "DISORDERED_STRUCTURE" ? "Disordered structure" : "Could not read the structure", message: p.message };
  }
  if (p.command === "FOLLOW_INSTABILITY") {
    switch (p.code) {
      case "INVALID_OPERATION":
        return {
          title: "Pick a high-symmetry point",
          message: `This q-point is not commensurate with a small supercell, so it cannot be frozen in. Pick a labelled high-symmetry point (Γ, X, M, R, …) on the dispersion chart${detail}.`,
        };
      case "STRUCTURE_TOO_LARGE":
        return {
          title: "Supercell too large",
          message: `The supercell commensurate with this q-point exceeds the 128-atom studio limit. Pick Γ or a point with a smaller supercell${detail}.`,
        };
      case "MODE_NOT_IMAGINARY":
        return { title: "Mode is not imaginary", message: `Only imaginary modes (f < 0) can be followed${detail}.` };
      case "NO_PHONON_ANALYSIS":
        return { title: "Phonons out of date", message: `The structure changed since the shake-test; run it again first${detail}.` };
    }
  }
  if (p.command === "RUN_PHONON_SHAKE_TEST" && p.code === "STRUCTURE_TOO_LARGE") {
    return { title: "Structure too large for phonons", message: `The shake-test supports at most 64 atoms per cell${detail}.` };
  }
  return { title: errorTitle(p.code), message: p.message };
}

/** Apply one server event to the store. Pure with respect to the socket (unit-tested directly). */
export function dispatchServerMessage(store: CrystalStore, msg: ServerMessage): void {
  const s = store.getState();
  const session = s.session;

  switch (msg.type) {
    case "FLOW_TRAJECTORY_FRAME": {
      const p = msg.payload;
      session.awaitingActorFlags = false;
      s.clearPhonon();
      if (s.latticeRef) s.setLatticeRef(null);
      s.setRelaxUnconverged(null);
      const head = s.ensureHeadStream("flow", "START_FLOW_GENERATION");
      const trails = s.frame.trails;
      if (trails.streamId !== head.id || p.step === 1) trails.reset(p.crystal.atoms.length, head.id);
      s.ingestCrystal(p.crystal, "flow");
      trails.push(s.frame.positions, s.frame.n, s.frame.lattice, s.frame.latticeInv);
      s.updateStreamProgress({
        step: p.step,
        totalSteps: p.total_steps,
        t: p.t,
        maxForce: p.crystal.max_force,
        energyPerAtom: p.crystal.energy_per_atom,
      });
      return;
    }

    case "CRYSTAL_STATE_FINAL": {
      session.awaitingActorFlags = false;
      s.clearPhonon();
      if (s.latticeRef) s.setLatticeRef(null);
      s.setRelaxUnconverged(null);
      s.ingestCrystal(msg.payload.crystal, "flow_final");
      s.frame.trails.endedAt = performance.now();
      s.endStream({ command: "START_FLOW_GENERATION" }) ?? s.endStream();
      return;
    }

    case "SINGLE_POINT_UPDATE": {
      if (session.awaitingActorFlags) {
        // Sent right after PHONON_ANALYSIS_COMPLETE: same structure + actor flags.
        session.awaitingActorFlags = false;
        s.ingestCrystal(msg.payload.crystal, "phonon_flags");
        return;
      }
      s.clearPhonon();
      s.frame.trails.reset(0);
      if (s.latticeRef) s.setLatticeRef(null);
      s.setRelaxUnconverged(null);
      s.ingestCrystal(msg.payload.crystal, "single_point");
      return;
    }

    case "RELAXATION_FRAME": {
      const p = msg.payload;
      session.awaitingActorFlags = false;
      s.clearPhonon();
      if (s.frame.trails.count) s.frame.trails.reset(0);
      const head = s.ensureHeadStream("relaxation", "START_RELAXATION");
      if (s.latticeRef?.streamId !== head.id) {
        // First frame of this stream: remember where it started (Δa, ΔV).
        // Follow starts from the parent cell shown before the displaced supercell.
        const follow = head.command === "FOLLOW_INSTABILITY";
        const origin = follow ? (s.frame.crystal ?? p.crystal) : p.step === 0 ? p.crystal : (s.frame.crystal ?? p.crystal);
        s.setLatticeRef({ lattice: origin.lattice, nAtoms: origin.atoms.length, kind: follow ? "follow" : "relaxation", streamId: head.id });
        s.setRelaxUnconverged(null);
      }
      s.ingestCrystal(p.crystal, "relaxation");
      s.pushRelaxPoint(head.id, { step: p.step, energyPerAtom: p.crystal.energy_per_atom, maxForce: p.crystal.max_force });
      s.updateStreamProgress({ step: p.step, maxForce: p.crystal.max_force, energyPerAtom: p.crystal.energy_per_atom });
      if (p.final) {
        s.flushHud();
        s.endStream();
        const follow = s.followResult;
        if (head.command === "FOLLOW_INSTABILITY" && follow && follow.status === "running") {
          s.setFollowResult({ ...follow, to: summarize(p.crystal), status: p.converged ? "converged" : "max_steps", steps: p.step });
        }
        if (!p.converged && !head.stopping) {
          s.setRelaxUnconverged({ steps: p.step, maxForce: p.crystal.max_force });
          s.pushToast(
            "info",
            "Relaxation ended",
            `Stopped after ${p.step} steps without converging (max |F| = ${p.crystal.max_force.toFixed(3)} eV/Å).`,
            { kind: "continue-relaxation", label: "Continue relaxation" },
          );
        }
      }
      return;
    }

    case "PHONON_ANALYSIS_COMPLETE": {
      const p = msg.payload;
      session.awaitingActorFlags = true;
      s.setPhonon(p);
      const mode = pickDefaultEigenmode(p.eigenmodes);
      s.selectEigenmode(mode);
      if (mode) s.setToolMode("PHONON_SHAKE");
      s.endStream({ command: "RUN_PHONON_SHAKE_TEST" }) ?? s.endStream();
      return;
    }

    case "HISTORY_STATE": {
      s.setHistory(msg.payload);
      return;
    }

    case "BULK_MODULUS_RESULT": {
      const p = msg.payload;
      s.setEosPending(false);
      const c = s.frame.crystal;
      s.patchCrystal({ bulk_modulus_gpa: p.k_gpa });
      s.setEos({ result: p, stateVersion: s.frame.stateVersion, nAtoms: c?.atoms.length ?? 0 });
      return;
    }

    case "PHONON_DOS_DATA": {
      s.setDosPending(false);
      // A DOS for an analysis that was invalidated meanwhile is dropped.
      if (s.phonon) s.setDos(msg.payload);
      return;
    }

    case "EIGENMODE_DATA": {
      if (s.phonon) {
        s.selectEigenmode(msg.payload.eigenmode);
        s.setToolMode("PHONON_SHAKE");
      }
      return;
    }

    case "STREAM_STOPPED": {
      const stopped = msg.payload.stopped;
      if (stopped === null) {
        s.dropStreamsBeforeStopBarrier();
        return;
      }
      if (stopped === "FOLLOW_INSTABILITY" && s.followResult?.status === "running") {
        const c = s.frame.crystal;
        s.setFollowResult({ ...s.followResult, to: c ? summarize(c) : null, status: "stopped" });
      }
      const head = s.session.streams[0];
      if (head && head.command === stopped) s.endStream();
      else s.endStream({ command: stopped }) ?? s.endStream();
      if (s.frame.trails.count && stopped === "START_FLOW_GENERATION") s.frame.trails.endedAt = performance.now();
      return;
    }

    case "ERROR": {
      const p = msg.payload;
      s.setLastError(p);
      const { title, message, level } = describeError(p);
      s.pushToast(level ?? "error", title, message);
      if (p.command === "COMPUTE_BULK_MODULUS") s.setEosPending(false);
      if (p.command === "REQUEST_PHONON_DOS") s.setDosPending(false);
      if (p.command === "FOLLOW_INSTABILITY" && s.followResult && (s.followResult.status === "running" || p.code === "RELAXATION_DIVERGED")) {
        s.setFollowResult({ ...s.followResult, status: "failed" });
      }
      if (p.command && STREAM_COMMANDS.has(p.command) && !POST_TERMINAL_ERRORS.has(p.code)) {
        s.endStream({ command: p.command, fromBack: true });
      }
      return;
    }
  }
}

// --------------------------------------------------------------------------- //
// Socket
// --------------------------------------------------------------------------- //
export interface WebSocketLike {
  readonly readyState: number;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface StudioClientOptions {
  url?: string | (() => string);
  store?: CrystalStore;
  createSocket?: (url: string) => WebSocketLike;
  baseDelayMs?: number;
  maxDelayMs?: number;
  random?: () => number;
  /** Last committed structure, restored after a (re)connect; null = no restore. */
  memory?: SessionMemory | null;
  /** Request PHONON_DOS after every PHONON_ANALYSIS_COMPLETE when this returns true. */
  autoDos?: () => boolean;
}

/**
 * Session restore state:
 *  armed  - connected with a remembered structure; waiting for the server's auto-load
 *  sent   - LOAD_STRUCTURE {crystal} sent; waiting for its SINGLE_POINT_UPDATE / ERROR
 *  idle   - nothing to do (restored, nothing remembered, or the user took over)
 */
export type RestoreState = "idle" | "armed" | "sent";
/** A connection that dies while a restore is in flight counts as a failed restore; after this many the memory is dropped. */
export const MAX_RESTORE_FAILURES = 2;

const WS_OPEN = 1;

/** Exponential backoff with full jitter: base · 2^attempt, capped, scaled into [0.5, 1]. */
export function backoffDelay(attempt: number, baseMs = 500, maxMs = 10_000, random: () => number = Math.random): number {
  const exp = Math.min(maxMs, baseMs * 2 ** Math.max(0, attempt));
  return Math.round(exp * (0.5 + 0.5 * random()));
}

export class StudioClient {
  private socket: WebSocketLike | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private wanted = false;
  private readonly opts: Required<Omit<StudioClientOptions, "url" | "memory">> & { url: string | (() => string); memory: SessionMemory | null };
  restore: RestoreState = "idle";
  private restoreFailures = 0;

  constructor(opts: StudioClientOptions = {}) {
    this.opts = {
      url: opts.url ?? wsUrl,
      store: opts.store ?? useCrystalStore,
      createSocket: opts.createSocket ?? ((u) => new WebSocket(u) as unknown as WebSocketLike),
      baseDelayMs: opts.baseDelayMs ?? 500,
      maxDelayMs: opts.maxDelayMs ?? 10_000,
      random: opts.random ?? Math.random,
      memory: opts.memory ?? null,
      autoDos: opts.autoDos ?? (() => false),
    };
  }

  get memory(): SessionMemory | null {
    return this.opts.memory;
  }

  get store(): CrystalStore {
    return this.opts.store;
  }

  get isOpen(): boolean {
    return this.socket?.readyState === WS_OPEN;
  }

  connect(): void {
    this.wanted = true;
    if (this.socket || this.timer) return;
    this.open();
  }

  disconnect(): void {
    this.wanted = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const sock = this.socket;
    this.socket = null;
    if (sock) {
      sock.onopen = sock.onclose = sock.onerror = null;
      sock.onmessage = null;
      try {
        sock.close(1000, "client disconnect");
      } catch {
        /* already closed */
      }
    }
    this.store.getState().setConnection("closed");
  }

  private open(): void {
    const url = typeof this.opts.url === "function" ? this.opts.url() : this.opts.url;
    const state = this.store.getState();
    state.setConnection("connecting", this.attempt);
    let sock: WebSocketLike;
    try {
      sock = this.opts.createSocket(url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = sock;
    sock.onopen = () => {
      this.attempt = 0;
      const st = this.store.getState();
      // A fresh server session auto-loads the default preset; forget the old one
      // (the remembered structure is re-sent once that auto-load arrives).
      st.resetSession();
      this.restore = this.opts.memory?.crystal ? "armed" : "idle";
      st.setConnection("open", 0);
    };
    sock.onmessage = (ev) => {
      if (typeof ev.data !== "string") return;
      const msg = parseServerMessage(ev.data);
      if (!msg) {
        console.warn("[ws] ignoring unrecognised message", String(ev.data).slice(0, 200));
        return;
      }
      this.handle(msg);
    };
    sock.onerror = () => {
      /* onclose follows */
    };
    sock.onclose = () => {
      if (this.socket !== sock) return;
      this.socket = null;
      if (this.restore === "sent" && ++this.restoreFailures >= MAX_RESTORE_FAILURES) {
        // The restored structure keeps killing the session: stop trying.
        this.opts.memory?.forget();
        this.restoreFailures = 0;
      }
      this.restore = "idle";
      const st = this.store.getState();
      st.resetSession();
      st.setConnection("closed", this.attempt);
      if (this.wanted) this.scheduleReconnect();
    };
  }

  /** Dispatch one server message, then run the session-restore / auto-DOS / memory hooks. */
  handle(msg: ServerMessage): void {
    const memory = this.opts.memory;
    let skipRemember = false;
    if (msg.type === "SINGLE_POINT_UPDATE" && this.restore === "armed") {
      // The server's auto-loaded default: re-send the remembered structure if it differs.
      this.restore = "idle";
      const remembered = memory?.crystal ?? null;
      if (remembered && !sameStructure(remembered, msg.payload.crystal) && this.rawSend("LOAD_STRUCTURE", { crystal: remembered })) {
        this.restore = "sent";
        skipRemember = true;
      }
    } else if (this.restore === "sent") {
      if (msg.type === "SINGLE_POINT_UPDATE") {
        this.restore = "idle";
        this.restoreFailures = 0;
        this.store.getState().pushToast("info", SESSION_RESTORED_TITLE, SESSION_RESTORED_MESSAGE);
      } else if (msg.type === "ERROR" && msg.payload.command === "LOAD_STRUCTURE") {
        // Do not fight the server: adopt what it has.
        this.restore = "idle";
        this.restoreFailures = 0;
        memory?.forget();
        memory?.remember(this.store.getState().frame.crystal);
        this.store.getState().pushToast("warning", "Session not restored", `The previous structure could not be reloaded: ${msg.payload.message}`);
      }
    }

    dispatchServerMessage(this.store, msg);

    if (msg.type === "PHONON_ANALYSIS_COMPLETE" && this.opts.autoDos()) {
      if (this.rawSend("REQUEST_PHONON_DOS", {})) this.store.getState().setDosPending(true);
    }

    if (memory && !skipRemember && this.restore !== "armed" && isCommitPoint(msg)) {
      memory.remember(this.store.getState().frame.crystal);
    }
  }

  /** Send without stream bookkeeping or restore cancellation (internal commands). */
  private rawSend<K extends ClientCommand>(type: K, payload: ClientCommandMap[K]): boolean {
    const sock = this.socket;
    if (!sock || sock.readyState !== WS_OPEN) return false;
    sock.send(JSON.stringify({ type, payload }));
    return true;
  }

  private scheduleReconnect(): void {
    if (!this.wanted || this.timer) return;
    const delay = backoffDelay(this.attempt, this.opts.baseDelayMs, this.opts.maxDelayMs, this.opts.random);
    this.attempt++;
    this.store.getState().setConnection("closed", this.attempt);
    this.timer = setTimeout(() => {
      this.timer = null;
      if (this.wanted) this.open();
    }, delay);
  }

  /**
   * Send a typed command. Streaming commands are registered in the store's
   * stream FIFO (so buttons flip to "Stop" at once). Returns false when the
   * socket is not open.
   */
  send<K extends ClientCommand>(type: K, payload: ClientCommandMap[K]): boolean {
    const sock = this.socket;
    if (!sock || sock.readyState !== WS_OPEN) return false;
    sock.send(JSON.stringify({ type, payload }));
    // The user acted before the server's auto-load arrived: do not overwrite their choice.
    if (this.restore === "armed" && STATE_CHANGING_COMMANDS.has(type)) this.restore = "idle";
    const st = this.store.getState();
    if (type === "STOP_STREAM") st.markStopping();
    else if (isStreamingSend(type, payload)) {
      st.beginStream(type as StreamCommand);
      if (type === "START_RELAXATION" || type === "FOLLOW_INSTABILITY") {
        st.session.relaxFmax = (payload as { fmax?: number }).fmax ?? 0.02;
      } else if (type === "PERTURB_OR_TRANSMUTE_ATOM") st.session.relaxFmax = 0.02;
      else if (type === "RUN_PHONON_SHAKE_TEST") {
        st.session.phononSupercell = (payload as ClientCommandMap["RUN_PHONON_SHAKE_TEST"]).supercell_dim ?? [2, 2, 2];
      }
    }
    return true;
  }
}

/** Messages after which the current structure is a committed state worth remembering. */
function isCommitPoint(msg: ServerMessage): boolean {
  switch (msg.type) {
    case "SINGLE_POINT_UPDATE":
    case "CRYSTAL_STATE_FINAL":
    case "STREAM_STOPPED":
    case "BULK_MODULUS_RESULT":
      return true;
    case "RELAXATION_FRAME":
      return msg.payload.final;
    default:
      return false;
  }
}

function isStreamingSend<K extends ClientCommand>(type: K, payload: ClientCommandMap[K]): boolean {
  if (type === "PERTURB_OR_TRANSMUTE_ATOM") return (payload as ClientCommandMap["PERTURB_OR_TRANSMUTE_ATOM"]).auto_relax === true;
  return STREAM_COMMANDS.has(type);
}

// --------------------------------------------------------------------------- //
// App singleton + convenience commands used by the HUD
// --------------------------------------------------------------------------- //
export const sessionMemory = new SessionMemory({ storageKey: "latticeflow.session.v1" });
export const studio = new StudioClient({ memory: sessionMemory, autoDos: () => useSettings.getState().phonon.autoDos });
registerDefaultSend((type, payload) => sendOrToast(type, payload));

function sendOrToast<K extends ClientCommand>(type: K, payload: ClientCommandMap[K]): boolean {
  const ok = studio.send(type, payload);
  if (!ok) studio.store.getState().pushToast("warning", "Not connected", `Cannot send ${type}: the studio socket is not open.`);
  return ok;
}

/** Phonon-button warning for the current structure under the current settings (null = relaxed). */
export function currentPhononWarning(crystal: CrystalStatePayload | null = studio.store.getState().frame.crystal): string | null {
  if (!crystal) return null;
  return phononWarning(relaxationVerdict(crystal, badgeCriteria(useSettings.getState().relax)));
}

export const commands = {
  loadPreset: (presetId: string) => sendOrToast("LOAD_STRUCTURE", { preset_id: presetId }),
  loadCif: (cif: string) => sendOrToast("LOAD_STRUCTURE", { cif }),
  loadText: (text: string, textFormat: StructureTextFormat = "auto") => sendOrToast("LOAD_STRUCTURE", { text, text_format: textFormat }),
  startFlow: (p: ClientCommandMap["START_FLOW_GENERATION"]) => sendOrToast("START_FLOW_GENERATION", p),
  /** START_RELAXATION with the user's settings (overridable). */
  relax: (p: ClientCommandMap["START_RELAXATION"] = {}) =>
    sendOrToast("START_RELAXATION", { ...relaxPayload(useSettings.getState().relax), ...p }),
  undo: () => sendOrToast("UNDO", {}),
  redo: () => sendOrToast("REDO", {}),
  computeBulkModulus: (p: ClientCommandMap["COMPUTE_BULK_MODULUS"] = {}) => {
    const ok = sendOrToast("COMPUTE_BULK_MODULUS", p);
    if (ok) studio.store.getState().setEosPending(true);
    return ok;
  },
  requestDos: (p: ClientCommandMap["REQUEST_PHONON_DOS"] = {}) => {
    const ok = sendOrToast("REQUEST_PHONON_DOS", p);
    if (ok) studio.store.getState().setDosPending(true);
    return ok;
  },
  refine: (p: ClientCommandMap["REFINE_STRUCTURE"]) => sendOrToast("REFINE_STRUCTURE", p),
  stop: () => sendOrToast("STOP_STREAM", {}),
  transmute: (atomIndex: number, element: string, allEquivalent: boolean) =>
    sendOrToast("PERTURB_OR_TRANSMUTE_ATOM", {
      atom_index: atomIndex,
      new_element: element,
      apply_to_equivalent_sites: allEquivalent,
    }),
  moveAtom: (atomIndex: number, cart: [number, number, number]) =>
    studio.send("PERTURB_OR_TRANSMUTE_ATOM", { atom_index: atomIndex, new_cart_coords: cart }),
  deleteAtom: (atomIndex: number) => {
    const ok = sendOrToast("DELETE_ATOM", { atom_index: atomIndex });
    if (ok) studio.store.getState().selectAtom(null);
    return ok;
  },
  /** RUN_PHONON_SHAKE_TEST with the user's settings (auto supercell); warns when the structure is not relaxed. */
  runPhonons: (p: ClientCommandMap["RUN_PHONON_SHAKE_TEST"] = {}) => {
    const st = studio.store.getState();
    const crystal = st.frame.crystal;
    const warning = currentPhononWarning(crystal);
    if (warning) st.pushToast("warning", "Structure not relaxed", `${warning[0]!.toUpperCase()}${warning.slice(1)}.`);
    const base = crystal
      ? phononPayload(useSettings.getState().phonon, latticeLengths(crystal.lattice.matrix), crystal.atoms.length)
      : {};
    return sendOrToast("RUN_PHONON_SHAKE_TEST", { ...base, ...p });
  },
  requestEigenmode: (qIndex: number, bandIndex: number) =>
    sendOrToast("REQUEST_EIGENMODE", { q_index: qIndex, band_index: bandIndex }),
  followInstability: (p: ClientCommandMap["FOLLOW_INSTABILITY"]) => sendOrToast("FOLLOW_INSTABILITY", p),
};
