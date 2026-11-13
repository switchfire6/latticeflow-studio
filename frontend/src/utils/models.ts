/**
 * Model picker helpers (IMPROVEMENT_SUGGESTIONS.md §6.1), pure and unit-tested:
 * which model the session runs (MODEL_CHANGED, else /api/health), the header
 * badge text, the fallback test for the §6.4 watermark and list annotations.
 */
import type { HealthResponse, ModelChanged, ModelInfo, SetModelPayload } from "../types/protocol";

export const FALLBACK_BACKEND = "lj-fallback";

export const MODEL_HINT = "Explore with a small model, then confirm the key numbers (relaxed cell, soft modes, E_hull, C_ij) with a larger one.";

export interface ActiveModel {
  /** model_id when known (after a switch), else null. */
  modelId: string | null;
  backendName: string;
  dispersion: boolean;
  device: string | null;
  dtype: string | null;
  /** "session" after MODEL_CHANGED, "process" from /api/health. */
  source: "session" | "process";
}

/** The session's model, falling back to the process default from /api/health. */
export function activeModel(model: ModelChanged | null, health: HealthResponse | null): ActiveModel | null {
  if (model) {
    return { modelId: model.model_id, backendName: model.backend_name, dispersion: model.dispersion, device: model.device, dtype: model.dtype, source: "session" };
  }
  if (health) {
    return { modelId: null, backendName: health.umlip_backend, dispersion: false, device: health.device, dtype: health.dtype, source: "process" };
  }
  return null;
}

export function isFallback(m: Pick<ActiveModel, "backendName" | "modelId"> | null): boolean {
  return !!m && (m.backendName === FALLBACK_BACKEND || m.modelId === FALLBACK_BACKEND);
}

/** "mace-mp-0-medium + D3 · cuda". */
export function modelBadgeText(m: ActiveModel): string {
  return `${m.backendName}${m.dispersion ? " + D3" : ""}${m.device ? ` · ${m.device}` : ""}`;
}

/** "cached" / "download ≈ 70 MB" / "download". */
export function modelAvailabilityText(m: Pick<ModelInfo, "checkpoint_cached" | "approx_size_mb" | "family">): string {
  if (m.family === "fallback" || m.checkpoint_cached) return m.family === "fallback" ? "built-in" : "cached";
  return m.approx_size_mb != null ? `download ≈ ${Math.round(m.approx_size_mb)} MB` : "download";
}

/** The list entry the session runs (by id after a switch; else the default entry). */
export function currentModelId(model: ModelChanged | null, defaultId: string | null | undefined): string | null {
  return model?.model_id ?? defaultId ?? null;
}

/** SET_MODEL payload; D3 only where the model supports it. */
export function setModelPayload(info: Pick<ModelInfo, "id" | "supports_dispersion">, dispersion: boolean): SetModelPayload {
  return { model_id: info.id, dispersion: info.supports_dispersion ? dispersion : false };
}

/** True when the choice differs from what the session runs. */
export function isModelChange(choice: { id: string; dispersion: boolean }, model: ModelChanged | null, defaultId: string | null | undefined): boolean {
  const cur = currentModelId(model, defaultId);
  return choice.id !== cur || choice.dispersion !== (model?.dispersion ?? false);
}
