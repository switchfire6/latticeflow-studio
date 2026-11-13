/**
 * Model picker (§6.1): the header backend badge shows the session's model
 * (+ D3) from MODEL_CHANGED, falling back to /api/health before any switch;
 * clicking it opens a popover listing GET /api/models (label, description,
 * cached / download size, default), a D3 toggle where supported and
 * SET_MODEL with download progress and MODEL_UNAVAILABLE errors. A switch
 * re-evaluates the structure; phonons are dropped and K / EOS, C_ij and
 * E_hull read stale (the state version is bumped).
 */
import { Activity, AlertTriangle, Check, Cpu, Loader2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../../services/apiClient";
import { commands } from "../../services/wsClient";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { ModelInfo, ModelListResponse } from "../../types/protocol";
import { activeModel, currentModelId, isFallback, isModelChange, MODEL_HINT, modelAvailabilityText, modelBadgeText, setModelPayload } from "../../utils/models";
import { ProgressBar } from "./ElasticPane";
import { Badge, Button, cx } from "./ui";

export async function refreshModels(): Promise<ModelListResponse | null> {
  const st = useCrystalStore.getState();
  try {
    const m = await api.models();
    useCrystalStore.getState().setModels(m, null);
    return m;
  } catch (e) {
    st.setModels(st.models, e instanceof Error ? e.message : String(e));
    return null;
  }
}

function ModelRow({ m, selected, current, onPick }: { m: ModelInfo; selected: boolean; current: boolean; onPick(): void }) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onPick}
      className={cx(
        "flex w-full flex-col gap-0.5 rounded-ctl border px-2 py-1.5 text-left transition-colors",
        selected ? "border-accent/60 bg-accent-soft" : "border-line-soft hover:border-line-strong hover:bg-hover",
      )}
      data-model={m.id}
    >
      <span className="flex items-center gap-1.5">
        <span className={cx("h-2.5 w-2.5 shrink-0 rounded-full border", selected ? "border-accent/60 bg-accent" : "border-line-strong")} />
        <span className="text-sm font-medium text-fg">{m.label}</span>
        {current && (
          <span className="rounded-ctl border border-ok/40 px-1 text-2xs text-ok">
            <Check size={8} className="inline" /> in use
          </span>
        )}
        {m.is_default && <span className="rounded-ctl border border-line px-1 text-2xs text-fg-3">default</span>}
        <span className={cx("ml-auto text-2xs", m.checkpoint_cached || m.family === "fallback" ? "text-fg-4" : "text-warn/90")}>{modelAvailabilityText(m)}</span>
      </span>
      <span className="pl-4 text-xs leading-snug text-fg-3">{m.description}</span>
    </button>
  );
}

export function ModelPopover({ onClose }: { onClose(): void }) {
  const models = useCrystalStore((s) => s.models);
  const modelsError = useCrystalStore((s) => s.modelsError);
  const model = useCrystalStore((s) => s.model);
  const switching = useCrystalStore((s) => s.streams.some((x) => x.command === "SET_MODEL"));
  const progress = useCrystalStore((s) => s.progress.SET_MODEL ?? null);
  const lastError = useCrystalStore((s) => (s.lastError?.command === "SET_MODEL" ? s.lastError : null));
  const connected = useCrystalStore((s) => s.connection === "open");
  const cur = currentModelId(model, models?.default_model_id);
  const [choice, setChoice] = useState<string | null>(cur);
  const [d3, setD3] = useState(model?.dispersion ?? false);
  useEffect(() => {
    void refreshModels();
  }, [model]);
  useEffect(() => {
    if (choice === null && cur) setChoice(cur);
  }, [cur, choice]);
  const info = models?.models.find((m) => m.id === choice) ?? null;
  const change = info ? isModelChange({ id: info.id, dispersion: info.supports_dispersion && d3 }, model, models?.default_model_id) : false;

  return (
    <div className="w-[400px] rounded-panel border border-line bg-surface p-3 text-sm text-fg-2 shadow-lg shadow-black/40" data-model-picker>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase">uMLIP model · this session</span>
        <button type="button" onClick={onClose} className="text-fg-4 hover:text-fg" aria-label="close">
          <X size={13} />
        </button>
      </div>
      <p className="mb-2 rounded-ctl border border-line bg-raised px-2 py-1 text-xs leading-snug text-fg-2">{MODEL_HINT}</p>
      {!models ? (
        <p className={cx("py-2", modelsError ? "text-bad" : "text-fg-4")}>{modelsError ? `Could not list models: ${modelsError}` : "loading models…"}</p>
      ) : (
        <div className="max-h-[300px] space-y-1 overflow-y-auto pr-0.5" role="radiogroup">
          {models.models.map((m) => (
            <ModelRow key={m.id} m={m} selected={m.id === choice} current={m.id === cur} onPick={() => setChoice(m.id)} />
          ))}
        </div>
      )}
      <label className={cx("mt-2 flex items-center justify-between gap-2 text-xs", !info?.supports_dispersion && "opacity-40")} title="Grimme D3(BJ) dispersion correction: essential for layered, vdW and molecular crystals">
        <span>D3 dispersion correction</span>
        <input type="checkbox" className="accent-accent" disabled={!info?.supports_dispersion} checked={!!info?.supports_dispersion && d3} onChange={(e) => setD3(e.target.checked)} />
      </label>
      {switching && (
        <div className="mt-2">
          <ProgressBar done={progress?.done} total={progress?.total} message={progress?.message ?? "loading the model…"} />
        </div>
      )}
      {lastError && !switching && (
        <p className="mt-2 flex gap-1 text-xs leading-snug text-bad" data-model-error>
          <AlertTriangle size={11} className="mt-px shrink-0" /> {lastError.message || lastError.code} — the session keeps its current model.
        </p>
      )}
      <p className="mt-2 text-2xs leading-snug text-fg-4">Switching re-evaluates the structure (one undo step). Phonons are dropped; K, C_ij, E_hull and other model results are marked stale.</p>
      <Button
        variant="primary"
        className="mt-2 w-full"
        disabled={!connected || !info || !change || switching}
        onClick={() => {
          if (!info) return;
          useCrystalStore.getState().setLastError(null);
          commands.setModel(setModelPayload(info, d3));
        }}
      >
        {switching ? <Loader2 size={12} className="animate-spin" /> : <Cpu size={12} />}
        {switching ? "Switching…" : info && change ? `Switch to ${info.label}${info.supports_dispersion && d3 ? " + D3" : ""}` : "Current model"}
      </Button>
    </div>
  );
}

/** Header badge: the session's model; opens the picker. */
export function ModelBadge() {
  const model = useCrystalStore((s) => s.model);
  const health = useCrystalStore((s) => s.health);
  const healthError = useCrystalStore((s) => s.healthError);
  const switching = useCrystalStore((s) => s.streams.some((x) => x.command === "SET_MODEL"));
  const [open, setOpen] = useState(false);
  const active = activeModel(model, health);
  return (
    <span className="relative">
      {active ? (
        <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} title="Choose the uMLIP model for this session" data-model-badge>
          {isFallback(active) ? (
            <Badge tone="amber" title={health?.load_error ?? "MACE-MP-0 could not be loaded"}>
              <AlertTriangle size={11} /> {active.backendName} {active.source === "process" ? "(MACE not loaded)" : ""}
            </Badge>
          ) : (
            <Badge tone="cyan" title={`device ${active.device}, dtype ${active.dtype}${active.source === "process" ? " (process default)" : ""} · click to switch`}>
              {switching ? <Loader2 size={11} className="animate-spin" /> : <Activity size={11} />} {modelBadgeText(active)}
            </Badge>
          )}
        </button>
      ) : (
        <span className="text-xs text-fg-4" title={healthError ?? undefined}>
          backend {healthError ? "unreachable" : "…"}
        </span>
      )}
      {open && (
        <div className="absolute top-8 right-0 z-50">
          <ModelPopover onClose={() => setOpen(false)} />
        </div>
      )}
    </span>
  );
}
