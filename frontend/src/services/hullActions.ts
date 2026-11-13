/**
 * E_hull evaluation (POST /api/hull/evaluate) with a busy state and graceful
 * handling of HULL_UNAVAILABLE (503: no MP_API_KEY on the backend; 502: MP
 * unreachable / missing references). The result is tied to the structure
 * state version it was computed for, so a later structure change (or a model
 * switch, which bumps the version) marks it stale.
 */
import { useCrystalStore, type CrystalStore, type HullState } from "../store/useCrystalStore";
import { ApiError, api, type ApiClient } from "./apiClient";
import { emitResult } from "./resultEvents";

export const HULL_NO_KEY_HINT = "Set MP_API_KEY on the backend to enable convex-hull lookups.";

/** Map a failure to the hull view state. */
export function hullFailureState(err: unknown, stateVersion: number): HullState {
  if (err instanceof ApiError) {
    if (err.code === "HULL_UNAVAILABLE" && err.status === 503) {
      return { status: "unavailable", message: `${HULL_NO_KEY_HINT} (${err.message})`, stateVersion };
    }
    if (err.code === "HULL_UNAVAILABLE") {
      return { status: "error", code: err.code, message: `Materials Project lookup failed: ${err.message}`, stateVersion };
    }
    return { status: "error", code: err.code, message: err.message, stateVersion };
  }
  const msg = err instanceof Error ? err.message : String(err);
  return { status: "error", code: "NETWORK", message: `Could not reach the backend: ${msg}`, stateVersion };
}

export async function evaluateHull(store: CrystalStore = useCrystalStore, client: ApiClient = api): Promise<HullState> {
  const st = store.getState();
  const crystal = st.frame.crystal;
  if (!crystal) return st.hull;
  if (st.hull.status === "busy") return st.hull;
  const stateVersion = st.frame.stateVersion;
  store.getState().setHull({ status: "busy", startedAt: Date.now(), stateVersion });
  let next: HullState;
  try {
    // Phase B: score with the session's model (the hull cache is keyed by backend).
    const m = st.model;
    const result = await client.hullEvaluate(crystal, undefined, true, m ? { model_id: m.model_id, dispersion: m.dispersion } : null);
    next = { status: "ok", result, stateVersion };
    emitResult({ kind: "hull", data: result, settings: { relax: true }, crystal, stateVersion, modelId: m?.model_id ?? null, dispersion: m?.dispersion ?? false });
  } catch (err) {
    next = hullFailureState(err, stateVersion);
  }
  store.getState().setHull(next);
  return next;
}

/** E_hull to show in the telemetry bar: the payload value, else a fresh hull result. */
export function displayedEHull(
  payloadEHull: number | null | undefined,
  hull: HullState,
  stateVersion: number,
): { value: number; stale: boolean } | null {
  if (payloadEHull != null) return { value: payloadEHull, stale: false };
  if (hull.status === "ok") return { value: hull.result.e_hull_ev, stale: hull.stateVersion !== stateVersion };
  return null;
}
