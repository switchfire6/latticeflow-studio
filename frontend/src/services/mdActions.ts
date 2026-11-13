/**
 * MD settings → START_MD payload, and the Arrhenius scan (§5.8): MD at a list
 * of temperatures, each from the same history entry (UNDO back to it between
 * runs; the server answers in order, so START_MD runs on the restored
 * structure), D per element collected from every MD_COMPLETE. A stop or error
 * aborts the scan; at the end the history is walked back to the start entry.
 */
import type { StartMDPayload } from "../types/protocol";
import { useCrystalStore } from "../store/useCrystalStore";
import { useUi, type ArrheniusScan } from "../store/uiStore";
import { historySteps } from "../utils/analysisC";
import { commands, studio } from "./wsClient";

export interface MdSettings {
  temperatureK: number;
  ensemble: NonNullable<StartMDPayload["ensemble"]>;
  timestepFs: number;
  nSteps: number;
  frictionPerFs: number;
  pressureGpa: number;
  frameInterval: number;
  seed: string;
}

export const DEFAULT_MD: MdSettings = {
  temperatureK: 300,
  ensemble: "NVT",
  timestepFs: 1,
  nSteps: 2000,
  frictionPerFs: 0.01,
  pressureGpa: 0,
  frameInterval: 10,
  seed: "",
};

export function mdPayload(m: MdSettings, temperatureK = m.temperatureK): StartMDPayload {
  const seed = m.seed.trim() === "" ? null : Number.parseInt(m.seed, 10);
  const p: StartMDPayload = {
    temperature_k: temperatureK,
    ensemble: m.ensemble,
    timestep_fs: m.timestepFs,
    n_steps: m.nSteps,
    frame_interval: m.frameInterval,
    seed: seed !== null && Number.isFinite(seed) ? seed : null,
  };
  if (m.ensemble !== "NVE") p.friction_per_fs = m.frictionPerFs;
  if (m.ensemble === "NPT") p.pressure_gpa = m.pressureGpa;
  return p;
}

let unsubscribe: (() => void) | null = null;
let scanSettings: MdSettings = DEFAULT_MD;

function walkTo(target: number): void {
  const h = useCrystalStore.getState().history;
  if (!h) return;
  const steps = historySteps(h.position, target);
  if (steps) for (let i = 0; i < steps.count; i++) studio.send(steps.command, {});
}

function finish(status: ArrheniusScan["status"], message: string | null): void {
  unsubscribe?.();
  unsubscribe = null;
  const a = useUi.getState().arrhenius;
  if (a) useUi.getState().setArrhenius({ ...a, status, message });
}

/** Run MD at every temperature from the current history entry. */
export function startArrheniusScan(temperatures: number[], settings: MdSettings): boolean {
  const h = useCrystalStore.getState().history;
  if (!h || temperatures.length < 2) return false;
  if (unsubscribe) return false;
  scanSettings = settings;
  const start = h.position;
  useUi.getState().setArrhenius({ temperatures, index: 0, startPosition: start, results: [], status: "running", message: null });
  if (!commands.startMd(mdPayload(settings, temperatures[0]!))) {
    finish("aborted", "not connected");
    return false;
  }
  // MD_COMPLETE is followed by the HISTORY_STATE of the new MD entry: walk back from there.
  let awaitingHistory = false;
  unsubscribe = studio.addListener((msg) => {
    const a = useUi.getState().arrhenius;
    if (!a || a.status !== "running") return;
    if (msg.type === "MD_COMPLETE") {
      const results = [...a.results, { temperatureK: a.temperatures[a.index]!, d: msg.payload.diffusion_coefficients_cm2_s }];
      useUi.getState().setArrhenius({ ...a, results, index: a.index + 1 });
      awaitingHistory = true;
    } else if (msg.type === "HISTORY_STATE" && awaitingHistory) {
      awaitingHistory = false;
      walkTo(a.startPosition);
      if (a.index >= a.temperatures.length) {
        finish("done", null);
        return;
      }
      if (!studio.send("START_MD", mdPayload(scanSettings, a.temperatures[a.index]!))) finish("aborted", "connection lost");
    } else if (msg.type === "STREAM_STOPPED" && msg.payload.stopped === "START_MD") {
      finish("aborted", "stopped");
    } else if (msg.type === "ERROR" && (msg.payload.command === "START_MD" || msg.payload.command === "UNDO")) {
      finish("aborted", msg.payload.message || msg.payload.code);
    }
  });
  return true;
}

export function abortArrheniusScan(): void {
  if (useCrystalStore.getState().streams.some((s) => s.command === "START_MD")) commands.stop();
  finish("aborted", "stopped");
}
