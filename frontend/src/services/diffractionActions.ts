/**
 * Simulated diffraction (POST /api/diffraction, §5.6): tied to the state
 * version it was computed for (stale after a structure change).
 */
import { diffractionRequest, useSettings, type DiffractionSettings } from "../store/settings";
import { useCrystalStore, type CrystalStore } from "../store/useCrystalStore";
import { ApiError, api, type ApiClient } from "./apiClient";
import { emitResult } from "./resultEvents";

export function diffractionErrorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.code}: ${err.message}`;
  return `Could not reach the backend: ${err instanceof Error ? err.message : String(err)}`;
}

export async function computeDiffraction(
  store: CrystalStore = useCrystalStore,
  client: ApiClient = api,
  settings: DiffractionSettings = useSettings.getState().diffraction,
): Promise<boolean> {
  const st = store.getState();
  const crystal = st.frame.crystal;
  if (!crystal || st.diffractionPending) return false;
  const stateVersion = st.frame.stateVersion;
  st.setDiffractionPending(true, null);
  try {
    const request = diffractionRequest(settings);
    const result = await client.diffraction({ crystal, ...request });
    store.getState().setDiffraction({ result, stateVersion });
    const m = st.model;
    emitResult({ kind: "diffraction", data: result, settings: { ...request }, crystal, stateVersion, modelId: m?.model_id ?? null, dispersion: m?.dispersion ?? false });
    store.getState().setDiffractionPending(false, null);
    return true;
  } catch (err) {
    store.getState().setDiffractionPending(false, diffractionErrorText(err));
    return false;
  }
}
