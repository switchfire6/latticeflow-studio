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
 *
 * Phase B: PROGRESS (per-command progress of long streams), the convergence
 * check (PHONON_CONVERGENCE_RESULT, then the larger supercell's
 * PHONON_ANALYSIS_COMPLETE and flags), ELASTIC_TENSOR_RESULT (stream kind
 * "elastic", not state-changing), PHONON_EXPORT (downloaded as a file) and
 * MODEL_CHANGED (stream kind "model"; the session's model, phonons dropped and
 * the state version bumped so every model-dependent result reads stale). The
 * StudioClient can chain a command after a relaxation ("Relax, then run").
 */
import { wsUrl } from "../config";
import { badgeCriteria, elasticPayload, phononPayload, relaxPayload, useSettings } from "../store/settings";
import { summarize, summarizePhonon, useCrystalStore, type CrystalStore, type StreamCommand, type ToastLevel } from "../store/useCrystalStore";
import type {
  ClientCommand,
  ClientCommandMap,
  CrystalStatePayload,
  ErrorCode,
  ErrorPayload,
  PhononExport,
  ServerEvent,
  ServerMessage,
  StructureTextFormat,
} from "../types/protocol";
import { registerDefaultSend } from "./phononActions";
import { SessionMemory } from "./sessionMemory";
import { historySteps } from "../utils/analysisC";
import { downloadText } from "../utils/download";

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
  "PROGRESS",
  "PHONON_CONVERGENCE_RESULT",
  "ELASTIC_TENSOR_RESULT",
  "PHONON_EXPORT",
  "MODEL_CHANGED",
  "LOCAL_ANALYSIS_DATA",
  "FORMATION_ENERGY_RESULT",
  "SUBSTITUTION_RESULT",
  "MD_FRAME",
  "MD_COMPLETE",
  "COMMITTEE_RESULT",
]);

const STREAM_COMMANDS: ReadonlySet<ClientCommand> = new Set<ClientCommand>([
  "START_FLOW_GENERATION",
  "START_RELAXATION",
  "RUN_PHONON_SHAKE_TEST",
  "FOLLOW_INSTABILITY",
  "PERTURB_OR_TRANSMUTE_ATOM",
  "CHECK_PHONON_CONVERGENCE",
  "COMPUTE_ELASTIC_TENSOR",
  "SET_MODEL",
  "COMPUTE_FORMATION_ENERGY",
  "SUBSTITUTE_FRACTION",
  "START_MD",
  "COMMITTEE_EVALUATE",
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
  "SET_MODEL",
  "MAKE_SUPERCELL",
  "ADD_ATOM",
  "SUBSTITUTE_FRACTION",
  "START_MD",
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
  if (p.command === "SET_MODEL") {
    return {
      title: p.code === "MODEL_UNAVAILABLE" ? "Model unavailable" : "Model switch failed",
      message: `${p.message || "The model could not be loaded."} The session keeps its current model.`,
    };
  }
  if (p.command === "COMPUTE_ELASTIC_TENSOR") {
    return { title: "Elastic tensor failed", message: p.message };
  }
  if (p.command === "CHECK_PHONON_CONVERGENCE") {
    if (p.code === "NO_PHONON_ANALYSIS") return { title: "Phonons out of date", message: `Run the shake-test on the current structure before checking convergence${detail}.` };
    if (p.code === "STRUCTURE_TOO_LARGE") return { title: "Supercell too large", message: `The next supercell exceeds the atom budget; convergence cannot be checked here${detail}.` };
    return { title: "Convergence check failed", message: p.message };
  }
  if (p.command === "EXPORT_PHONON_DATA") {
    return { title: "Phonon export failed", message: p.code === "NO_PHONON_ANALYSIS" ? `Run the shake-test first${detail}.` : p.message };
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
  // ---- Phase C1
  if (p.command === "REQUEST_LOCAL_ANALYSIS") return { title: "Local analysis failed", message: p.message };
  if (p.command === "MAKE_SUPERCELL") {
    return { title: p.code === "STRUCTURE_TOO_LARGE" ? "Supercell too large" : "Supercell failed", message: p.message };
  }
  if (p.command === "ADD_ATOM") {
    return { title: p.code === "INVALID_OPERATION" ? "Atom not added" : "Add atom failed", message: p.message || "The position is too close to an existing atom." };
  }
  if (p.command === "COMPUTE_FORMATION_ENERGY") {
    const noKey = /MP_API_KEY/i.test(p.message);
    return {
      title: noKey ? "Formation energy unavailable" : "Formation energy failed",
      message: noKey ? `${p.message} The chemical potentials come from the Materials Project reference phases.` : p.message,
      level: noKey ? "warning" : undefined,
    };
  }
  if (p.command === "SUBSTITUTE_FRACTION") return { title: "Substitution failed", message: p.message };
  if (p.command === "START_MD") return { title: "Molecular dynamics failed", message: p.message };
  if (p.command === "COMMITTEE_EVALUATE") {
    return { title: p.code === "MODEL_UNAVAILABLE" ? "Model unavailable" : "Model comparison failed", message: p.message };
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
      // The larger-supercell analysis of a convergence check keeps the check; a fresh shake-test drops it.
      const convergence = session.convergenceArrived ? s.convergence : null;
      session.convergenceArrived = false;
      s.setConvergence(convergence);
      s.setPhonon(p);
      s.setPhononSummary(summarizePhonon(p, s.frame.stateVersion, convergence));
      const mode = pickDefaultEigenmode(p.eigenmodes);
      s.selectEigenmode(mode);
      if (mode) s.setToolMode("PHONON_SHAKE");
      const head = session.streams[0];
      if (head?.kind === "phonon") s.endStream();
      else s.endStream({ command: "RUN_PHONON_SHAKE_TEST" }) ?? s.endStream({ command: "CHECK_PHONON_CONVERGENCE" }) ?? s.endStream();
      s.clearProgress("RUN_PHONON_SHAKE_TEST");
      s.clearProgress("CHECK_PHONON_CONVERGENCE");
      return;
    }

    case "PHONON_CONVERGENCE_RESULT": {
      session.convergenceArrived = true;
      s.setConvergence(msg.payload);
      return;
    }

    case "PROGRESS": {
      s.setProgress(msg.payload);
      return;
    }

    case "ELASTIC_TENSOR_RESULT": {
      s.setElastic({ result: msg.payload, stateVersion: s.frame.stateVersion });
      s.clearProgress("COMPUTE_ELASTIC_TENSOR");
      s.endStream({ command: "COMPUTE_ELASTIC_TENSOR" });
      return;
    }

    case "PHONON_EXPORT": {
      s.setPhononExportPending(false);
      return;
    }

    case "MODEL_CHANGED": {
      // Phonons are invalidated server-side; every model-dependent result is now stale.
      session.awaitingActorFlags = false;
      session.convergenceArrived = false;
      s.setModel(msg.payload);
      s.clearProgress("SET_MODEL");
      s.clearPhonon();
      s.bumpStateVersion();
      s.endStream({ command: "SET_MODEL" });
      return;
    }

    case "HISTORY_STATE": {
      s.setHistory(msg.payload);
      return;
    }

    // ---- Phase C1
    case "LOCAL_ANALYSIS_DATA": {
      s.setLocalAnalysis(msg.payload);
      return;
    }

    case "FORMATION_ENERGY_RESULT": {
      s.setFormation({ result: msg.payload, stateVersion: s.frame.stateVersion });
      s.clearProgress("COMPUTE_FORMATION_ENERGY");
      s.endStream({ command: "COMPUTE_FORMATION_ENERGY" });
      return;
    }

    case "SUBSTITUTION_RESULT": {
      // The SINGLE_POINT_UPDATE + HISTORY_STATE of the chosen configuration follow.
      s.setSubstitution(msg.payload);
      s.clearProgress("SUBSTITUTE_FRACTION");
      s.endStream({ command: "SUBSTITUTE_FRACTION" });
      return;
    }

    case "COMMITTEE_RESULT": {
      s.setCommittee({ result: msg.payload, stateVersion: s.frame.stateVersion });
      s.clearProgress("COMMITTEE_EVALUATE");
      s.endStream({ command: "COMMITTEE_EVALUATE" });
      return;
    }

    case "MD_FRAME": {
      const p = msg.payload;
      session.awaitingActorFlags = false;
      s.clearPhonon();
      if (s.frame.trails.count) s.frame.trails.reset(0);
      if (s.latticeRef) s.setLatticeRef(null);
      s.setRelaxUnconverged(null);
      const head = s.ensureHeadStream("md", "START_MD");
      s.ingestCrystal(p.crystal, "md");
      s.pushMdFrame(head.id, p);
      s.updateStreamProgress({ step: p.step, totalSteps: session.md.nSteps, energyPerAtom: p.crystal.energy_per_atom, maxForce: p.crystal.max_force });
      return;
    }

    case "MD_COMPLETE": {
      const p = msg.payload;
      session.awaitingActorFlags = false;
      s.clearPhonon();
      s.ingestCrystal(p.final_crystal, "md");
      s.finishMd("complete", p);
      s.clearProgress("START_MD");
      s.endStream({ command: "START_MD" }) ?? (session.streams[0]?.kind === "md" ? s.endStream() : null);
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
      s.clearProgress(stopped);
      if (stopped === "CHECK_PHONON_CONVERGENCE") session.convergenceArrived = false;
      if (stopped === "START_MD") s.finishMd("stopped");
      if (s.frame.trails.count && stopped === "START_FLOW_GENERATION") s.frame.trails.endedAt = performance.now();
      return;
    }

    case "ERROR": {
      const p = msg.payload;
      s.setLastError(p);
      const { title, message, level } = describeError(p);
      const disordered = p.command === "LOAD_STRUCTURE" && p.code === "DISORDERED_STRUCTURE" && !!session.lastLoadText;
      if (disordered) s.setDisordered({ cif: session.lastLoadText!, message: p.message });
      s.pushToast(level ?? "error", title, message, disordered ? { kind: "find-orderings", label: "Find ordered approximants" } : undefined);
      if (p.command === "REQUEST_LOCAL_ANALYSIS") s.setLocalPending(false);
      if (p.command === "START_MD") s.finishMd("failed");
      if (p.command === "COMPUTE_BULK_MODULUS") s.setEosPending(false);
      if (p.command === "REQUEST_PHONON_DOS") s.setDosPending(false);
      if (p.command === "EXPORT_PHONON_DATA") s.setPhononExportPending(false);
      if (p.command === "CHECK_PHONON_CONVERGENCE") session.convergenceArrived = false;
      if (p.command && !POST_TERMINAL_ERRORS.has(p.code)) s.clearProgress(p.command);
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
  /** PHONON_EXPORT handler (default: download the phonopy YAML). */
  onPhononExport?: (p: PhononExport) => void;
}

/** A command queued behind a relaxation ("Relax, then run"). */
interface RelaxChain {
  streamId: number;
  label: string;
  next: () => void;
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
      onPhononExport: opts.onPhononExport ?? ((p) => downloadText(p.phonopy_yaml, p.filename || "phonopy_params.yaml", "application/x-yaml")),
    };
  }

  private chain: RelaxChain | null = null;
  private listeners = new Set<(msg: ServerMessage) => void>();
  /** Phase C1: re-send the remembered model once the (restored) structure has loaded. */
  private modelRestorePending = false;

  /** Observe every server message after it was dispatched (e.g. the Arrhenius scan). Returns an unsubscribe. */
  addListener(fn: (msg: ServerMessage) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** The label of the command waiting for the current relaxation (null = none). */
  get chainedLabel(): string | null {
    return this.chain?.label ?? null;
  }

  /**
   * START_RELAXATION, then `next()` once it converges. If it stops, fails or
   * ends unconverged, `next` is dropped with an info toast. Returns false when
   * the socket is not open.
   */
  runAfterRelax(label: string, next: () => void, payload: ClientCommandMap["START_RELAXATION"]): boolean {
    if (!this.send("START_RELAXATION", payload)) return false;
    const entry = this.store.getState().session.streams.at(-1);
    if (entry) this.chain = { streamId: entry.id, label, next };
    return true;
  }

  /** Forget a queued "then run" (e.g. the user cancelled it). */
  cancelChain(): void {
    this.chain = null;
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
      this.chain = null;
      const st = this.store.getState();
      // A fresh server session auto-loads the default preset; forget the old one
      // (the remembered structure is re-sent once that auto-load arrives).
      st.resetSession();
      this.restore = this.opts.memory?.crystal ? "armed" : "idle";
      this.modelRestorePending = !!this.opts.memory?.model;
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
      this.chain = null;
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

    const chain = this.chain;
    const chainTracked = !!chain && this.store.getState().session.streams.some((x) => x.id === chain.streamId);

    dispatchServerMessage(this.store, msg);

    if (msg.type === "PHONON_EXPORT") this.opts.onPhononExport(msg.payload);

    if (chain && chainTracked && !this.store.getState().session.streams.some((x) => x.id === chain.streamId)) {
      // The relaxation the chained command waits for has ended.
      this.chain = null;
      if (msg.type === "RELAXATION_FRAME" && msg.payload.converged) chain.next();
      else {
        const why = msg.type === "STREAM_STOPPED" ? "was stopped" : msg.type === "ERROR" ? "failed" : "did not converge";
        this.store.getState().pushToast("info", `${chain.label} not started`, `The relaxation ${why}; run it again or start ${chain.label.toLowerCase()} anyway.`);
      }
    }

    if (msg.type === "PHONON_ANALYSIS_COMPLETE" && this.opts.autoDos()) {
      if (this.rawSend("REQUEST_PHONON_DOS", {})) this.store.getState().setDosPending(true);
    }

    if (memory && !skipRemember && this.restore !== "armed" && isCommitPoint(msg)) {
      memory.remember(this.store.getState().frame.crystal);
    }

    if (msg.type === "MODEL_CHANGED") {
      this.modelRestorePending = false;
      memory?.rememberModel({ model_id: msg.payload.model_id, dispersion: msg.payload.dispersion });
    }
    if (this.modelRestorePending && this.restore === "idle" && (msg.type === "SINGLE_POINT_UPDATE" || (msg.type === "ERROR" && msg.payload.command === "LOAD_STRUCTURE"))) {
      this.modelRestorePending = false;
      this.restoreModel();
    }

    for (const fn of this.listeners) fn(msg);
  }

  /** SET_MODEL for the remembered model unless it is what a fresh session runs anyway. */
  private restoreModel(): void {
    const m = this.opts.memory?.model;
    if (!m) return;
    const st = this.store.getState();
    const cur = st.model;
    if (cur && cur.model_id === m.model_id && cur.dispersion === m.dispersion) return;
    if (!cur && !m.dispersion && st.models && st.models.default_model_id === m.model_id) return;
    this.send("SET_MODEL", { model_id: m.model_id, dispersion: m.dispersion });
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
    if (type === "SET_MODEL") this.modelRestorePending = false;
    if (type === "LOAD_STRUCTURE") {
      const lp = payload as ClientCommandMap["LOAD_STRUCTURE"];
      st.session.lastLoadText = "cif" in lp ? lp.cif : "text" in lp ? lp.text : null;
    }
    if (type === "START_MD") {
      const mp = payload as ClientCommandMap["START_MD"];
      st.resetMd(mp.n_steps ?? 2000, mp.temperature_k ?? 300);
    }
    if (type === "STOP_STREAM") st.markStopping();
    else if (isStreamingSend(type, payload)) {
      st.beginStream(type as StreamCommand);
      if (type === "START_RELAXATION" || type === "FOLLOW_INSTABILITY") {
        st.session.relaxFmax = (payload as { fmax?: number }).fmax ?? 0.02;
      } else if (type === "PERTURB_OR_TRANSMUTE_ATOM") st.session.relaxFmax = 0.02;
      else if (type === "RUN_PHONON_SHAKE_TEST") {
        const p = payload as ClientCommandMap["RUN_PHONON_SHAKE_TEST"];
        st.session.phononSupercell = p.supercell_dim ?? [2, 2, 2];
        st.session.phononMinLength = p.min_supercell_length ?? null;
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
    case "MD_COMPLETE":
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

/** "Relax, then …": START_RELAXATION with the user's settings, then `next` once it converges. */
function relaxThen(label: string, next: () => void): boolean {
  const ok = studio.runAfterRelax(label, next, relaxPayload(useSettings.getState().relax));
  if (!ok) studio.store.getState().pushToast("warning", "Not connected", "Cannot start the relaxation: the studio socket is not open.");
  return ok;
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

  // ---- Phase B
  /** Re-run the phonons in the next larger supercell and compare (null dims = current + 1). */
  checkConvergence: (p: ClientCommandMap["CHECK_PHONON_CONVERGENCE"] = {}) => sendOrToast("CHECK_PHONON_CONVERGENCE", p),
  /** COMPUTE_ELASTIC_TENSOR with the user's settings (overridable). */
  computeElastic: (p: ClientCommandMap["COMPUTE_ELASTIC_TENSOR"] = {}) =>
    sendOrToast("COMPUTE_ELASTIC_TENSOR", { ...elasticPayload(useSettings.getState().elastic), ...p }),
  /** EXPORT_PHONON_DATA; the PHONON_EXPORT answer is downloaded as a phonopy YAML file. */
  exportPhonons: () => {
    const ok = sendOrToast("EXPORT_PHONON_DATA", {});
    if (ok) studio.store.getState().setPhononExportPending(true);
    return ok;
  },
  setModel: (p: ClientCommandMap["SET_MODEL"]) => sendOrToast("SET_MODEL", p),
  relaxThen,

  // ---- Phase C1
  requestLocalAnalysis: (p: ClientCommandMap["REQUEST_LOCAL_ANALYSIS"] = {}) => {
    const ok = studio.send("REQUEST_LOCAL_ANALYSIS", p);
    if (ok) studio.store.getState().setLocalPending(true);
    return ok;
  },
  makeSupercell: (matrix: number[][]) => sendOrToast("MAKE_SUPERCELL", { matrix }),
  addAtom: (element: string, frac: [number, number, number]) => sendOrToast("ADD_ATOM", { element, frac_coords: frac }),
  computeFormationEnergy: (bulkPosition: number) => sendOrToast("COMPUTE_FORMATION_ENERGY", { bulk_position: bulkPosition }),
  substitute: (p: ClientCommandMap["SUBSTITUTE_FRACTION"]) => sendOrToast("SUBSTITUTE_FRACTION", p),
  startMd: (p: ClientCommandMap["START_MD"]) => sendOrToast("START_MD", p),
  committee: (p: ClientCommandMap["COMMITTEE_EVALUATE"]) => sendOrToast("COMMITTEE_EVALUATE", p),
  /** Walk the history to `target` with repeated UNDO / REDO (there is no GOTO command). */
  gotoHistory: (target: number) => {
    const h = studio.store.getState().history;
    if (!h) return false;
    const steps = historySteps(h.position, target);
    if (!steps) return true;
    for (let i = 0; i < steps.count; i++) if (!sendOrToast(steps.command, {})) return false;
    return true;
  },
};
