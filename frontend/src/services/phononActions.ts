/**
 * Mode selection and Follow Instability actions (shared by the dispersion chart
 * and the phonon control bar; the store and transport are injectable for tests).
 */
import { summarize, useCrystalStore, type CrystalStore } from "../store/useCrystalStore";
import type { ClientCommand, ClientCommandMap, Eigenmode, PhononAnalysisComplete } from "../types/protocol";
import { formatKLabel, qLabelAt } from "../utils/bandChart";

export type Send = <K extends ClientCommand>(type: K, payload: ClientCommandMap[K]) => boolean;

/** The cached eigenmode for (q, band) if the analysis carries it (labelled points only). */
export function cachedEigenmode(phonon: PhononAnalysisComplete, qIndex: number, band: number): Eigenmode | null {
  return phonon.eigenmodes.find((m) => m.q_index === qIndex && m.band_index === band) ?? null;
}

/**
 * Select (q, band) for animation: use the cached eigenvector of a labelled
 * point at once, otherwise mark the selection and fetch it with
 * REQUEST_EIGENMODE (EIGENMODE_DATA then selects it). Returns "cached",
 * "requested" or "unavailable".
 */
export function selectModeAt(qIndex: number, band: number, store: CrystalStore = useCrystalStore, send?: Send): "cached" | "requested" | "unavailable" {
  const st = store.getState();
  const phonon = st.phonon;
  if (!phonon || qIndex < 0 || qIndex >= phonon.frequencies_thz.length || band < 0 || band >= (phonon.frequencies_thz[qIndex]?.length ?? 0)) {
    return "unavailable";
  }
  st.setToolMode("PHONON_SHAKE");
  const cached = cachedEigenmode(phonon, qIndex, band);
  if (cached) {
    st.selectEigenmode(cached);
    return "cached";
  }
  st.setModeSelection({ qIndex, band });
  const ok = (send ?? defaultSend())("REQUEST_EIGENMODE", { q_index: qIndex, band_index: band });
  return ok ? "requested" : "unavailable";
}

/** True when the selected, loaded eigenmode is imaginary and can be followed. */
export function canFollow(st: ReturnType<CrystalStore["getState"]>): boolean {
  const m = st.selectedEigenmode;
  const sel = st.modeSelection;
  return !!(st.phonon && m && m.is_imaginary && sel && sel.qIndex === m.q_index && sel.band === m.band_index && st.streams.length === 0);
}

/**
 * Send FOLLOW_INSTABILITY for the selected imaginary mode, remember where we
 * started (for the "old → new space group" summary) and switch the dock to the
 * relaxation curve.
 */
export function followSelectedInstability(amplitude: number, store: CrystalStore = useCrystalStore, send?: Send): boolean {
  const st = store.getState();
  const m = st.selectedEigenmode;
  const crystal = st.frame.crystal;
  if (!st.phonon || !m || !m.is_imaginary || !crystal) return false;
  const ok = (send ?? defaultSend())("FOLLOW_INSTABILITY", {
    q_index: m.q_index,
    band_index: m.band_index,
    amplitude,
    fmax: 0.02,
    max_steps: 200,
  });
  if (!ok) return false;
  st.setFollowResult({
    from: summarize(crystal),
    to: null,
    qLabel: m.q_label ? formatKLabel(m.q_label) : qLabelAt(st.phonon.kpath_labels, m.q_index),
    bandIndex: m.band_index,
    frequencyThz: m.frequency_thz,
    amplitude,
    status: "running",
    steps: null,
  });
  st.setDock({ open: true, hidden: st.dock.hidden.filter((p) => p !== "relaxation") });
  return true;
}

let lazySend: Send | null = null;
function defaultSend(): Send {
  if (!lazySend) throw new Error("phononActions: no transport registered");
  return lazySend;
}
/** Registered by wsClient (avoids an import cycle). */
export function registerDefaultSend(send: Send): void {
  lazySend = send;
}
