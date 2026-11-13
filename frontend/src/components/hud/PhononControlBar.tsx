/**
 * Phonon control bar (§3, §7 Phase 5): mode list at the selected q-point
 * (imaginary modes marked), animation controls (play/pause, amplitude gain,
 * temperature, slow-motion speed) and the "Freeze & Relax Imaginary Mode
 * (Follow Instability)" action with its amplitude.
 * Phase B (§5.2): a "Γ irreps" tab lists the Γ-point irreducible
 * representations with IR / Raman / silent / acoustic badges; a row selects
 * that Γ mode.
 */
import { BarChart3, GitBranch, Loader2, Pause, Play } from "lucide-react";
import { useState } from "react";
import { showPane } from "../../services/dockActions";
import { canFollow, followSelectedInstability, selectModeAt } from "../../services/phononActions";
import { commands } from "../../services/wsClient";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import { formatFrequency } from "../../utils/units";
import type { GammaModeIrrep } from "../../types/protocol";
import { classifyFrequency, formatIrrep, gammaQIndex, irrepBadges, qLabelAt, type IrrepBadge } from "../../utils/bandChart";
import type { FrequencyUnit } from "../../utils/units";
import { Button, Segmented, Slider, cx } from "./ui";

const BADGE_CLS: Record<IrrepBadge, string> = {
  acoustic: "border-line text-fg-3",
  IR: "border-bad/40 text-bad",
  Raman: "border-ok/40 text-ok",
  silent: "border-line text-fg-4",
  unknown: "border-line text-fg-5",
};

export function GammaIrrepList({
  modes,
  unit,
  selectedBand,
  onSelect,
}: {
  modes: readonly GammaModeIrrep[];
  unit: FrequencyUnit;
  selectedBand: number | null;
  onSelect(band: number): void;
}) {
  return (
    <div role="listbox" aria-label="Γ irreps" data-gamma-irreps>
      {modes.map((m, i) => {
        const sel = selectedBand !== null && m.band_indices.includes(selectedBand);
        return (
          <button
            key={i}
            type="button"
            role="option"
            aria-selected={sel}
            onClick={() => m.band_indices.length && onSelect(m.band_indices[0]!)}
            title={`bands ${m.band_indices.join(", ")} (degeneracy ${m.band_indices.length})`}
            className={cx("flex w-full items-center gap-1.5 px-2 py-[3px] font-mono text-xs", sel ? "bg-accent-soft text-accent-fg" : "text-fg-2 hover:bg-hover")}
          >
            <span className="w-11 shrink-0 text-left">{formatIrrep(m.label)}</span>
            <span className={cx("w-[72px] shrink-0 text-right tabular-nums", classifyFrequency(m.frequency_thz) === "imaginary" && "text-bad")}>
              {formatFrequency(m.frequency_thz, unit, 2)}
            </span>
            <span className="flex flex-1 justify-end gap-0.5">
              {irrepBadges(m).map((b) => (
                <span key={b} className={cx("rounded-ctl border px-1 text-2xs leading-[14px]", BADGE_CLS[b])}>
                  {b === "unknown" ? "?" : b}
                </span>
              ))}
            </span>
          </button>
        );
      })}
    </div>
  );
}

export function PhononControlBar() {
  const phonon = useCrystalStore((s) => s.phonon);
  const selection = useCrystalStore((s) => s.modeSelection);
  const loaded = useCrystalStore((s) => s.selectedEigenmode);
  const params = useCrystalStore((s) => s.phononParams);
  const playing = useCrystalStore((s) => s.phononPlaying);
  const toolMode = useCrystalStore((s) => s.toolMode);
  const followable = useCrystalStore(canFollow);
  const unit = useSettings((s) => s.units.frequency);
  const hasDos = useCrystalStore((s) => s.dos !== null);
  const dosPending = useCrystalStore((s) => s.dosPending);
  const [amplitude, setAmplitude] = useState("0.1");
  const [tab, setTab] = useState<"q" | "gamma">("q");
  if (!phonon) return null;
  const gammaQ = gammaQIndex(phonon.kpath_labels);
  const hasIrreps = !!phonon.gamma_modes?.length && gammaQ !== null;
  const showIrreps = hasIrreps && tab === "gamma";
  const st = useCrystalStore.getState();

  const q = selection?.qIndex ?? phonon.kpath_labels[0]?.index ?? 0;
  const freqs = phonon.frequencies_thz[q] ?? [];
  const qLabel = qLabelAt(phonon.kpath_labels, q) ?? `q #${q}`;
  const amp = Number.parseFloat(amplitude);
  const ampOk = Number.isFinite(amp) && amp > 0 && amp <= 1;
  const pending = selection && (!loaded || loaded.q_index !== selection.qIndex || loaded.band_index !== selection.band);

  return (
    <div className="flex h-full w-[270px] shrink-0 flex-col gap-1.5 overflow-y-auto border-r border-line-soft pr-3">
      <div className="flex items-center justify-between">
        <span className="font-mono text-xs text-fg-2">
          {qLabel} <span className="text-fg-4">· {freqs.length} modes</span>
        </span>
        <button
          type="button"
          title={playing ? "Pause the animation" : "Play the animation"}
          className="flex items-center gap-1 rounded-ctl px-1.5 py-0.5 text-xs text-fg-2 hover:bg-hover hover:text-fg"
          onClick={() => {
            st.setPhononPlaying(!playing);
            if (toolMode !== "PHONON_SHAKE") st.setToolMode("PHONON_SHAKE");
          }}
        >
          {playing ? <Pause size={11} /> : <Play size={11} />} {playing ? "Pause" : "Play"}
        </button>
      </div>
      {hasIrreps && (
        <Segmented<"q" | "gamma">
          value={tab}
          options={[
            { value: "q", label: `modes at ${qLabel}` },
            { value: "gamma", label: "Γ irreps (IR / Raman)" },
          ]}
          onChange={setTab}
        />
      )}
      <div className="min-h-[92px] flex-1 shrink-0 overflow-y-auto rounded-ctl border border-line-soft bg-field" role="listbox" aria-label="modes at q">
        {showIrreps && (
          <GammaIrrepList
            modes={phonon.gamma_modes!}
            unit={unit}
            selectedBand={selection?.qIndex === gammaQ ? selection.band : null}
            onSelect={(band) => selectModeAt(gammaQ!, band)}
          />
        )}
        {!showIrreps && freqs.map((f, b) => {
          const cls = classifyFrequency(f);
          const sel = selection?.qIndex === q && selection.band === b;
          return (
            <button
              key={b}
              type="button"
              role="option"
              aria-selected={sel}
              onClick={() => selectModeAt(q, b)}
              className={cx(
                "flex w-full items-center justify-between px-2 py-[3px] font-mono text-xs",
                sel ? "bg-accent-soft text-accent-fg" : "text-fg-2 hover:bg-hover",
              )}
            >
              <span>ν{b}</span>
              <span className={cx(cls === "imaginary" && "text-bad", cls === "zero" && "text-fg-4")}>
                {cls === "imaginary" && "⚠ "}
                {formatFrequency(f, unit, 3)}{cls === "imaginary" ? " (i)" : ""}
              </span>
            </button>
          );
        })}
      </div>
      <div className="flex items-center gap-2">
        <label className="flex items-center gap-1 text-xs text-fg-3" title="Maximum displacement of the frozen-in mode (Å)">
          amp
          <input
            className="w-14 rounded-ctl border border-line bg-field px-1 py-0.5 font-mono text-xs text-fg outline-none focus:border-accent"
            value={amplitude}
            inputMode="decimal"
            onChange={(e) => setAmplitude(e.target.value)}
          />
          Å
        </label>
        <Button
          variant="primary"
          className="flex-1 px-2 py-1 text-xs leading-tight"
          disabled={!followable || !ampOk}
          title={
            !loaded?.is_imaginary
              ? "Select an imaginary (red) mode first"
              : pending
                ? "Waiting for the eigenvector…"
                : !ampOk
                  ? "Amplitude must be in (0, 1] Å"
                  : "Displace along this imaginary mode in its commensurate supercell and FIRE-relax"
          }
          onClick={() => followSelectedInstability(amp)}
        >
          <GitBranch size={12} /> Follow instability
        </Button>
      </div>
      <button
        type="button"
        className="flex items-center justify-center gap-1.5 rounded-ctl border border-line bg-raised px-2 py-1 text-xs text-fg-2 hover:border-line-strong hover:text-fg"
        title="Phonon density of states, F(T), S(T), C_v(T) and the zero-point energy"
        onClick={() => {
          if (!hasDos && !dosPending) commands.requestDos();
          showPane("dos");
        }}
      >
        {dosPending ? <Loader2 size={11} className="animate-spin" /> : <BarChart3 size={11} />} DOS &amp; thermodynamics
      </button>
      <Slider label="Amplitude gain" value={params.amplitude} min={0.1} max={6} step={0.1} format={(v) => `${v.toFixed(1)}×`} onChange={(amplitude) => st.setPhononParams({ amplitude })} />
      <Slider label="Temperature" value={params.temperatureK} min={10} max={1500} step={10} format={(v) => `${v} K`} onChange={(temperatureK) => st.setPhononParams({ temperatureK })} />
      <Slider
        label="Slow motion"
        value={params.slowMotionPsPerSecond}
        min={0.05}
        max={1.5}
        step={0.05}
        format={(v) => `${v.toFixed(2)} ps/s`}
        onChange={(slowMotionPsPerSecond) => st.setPhononParams({ slowMotionPsPerSecond })}
      />
    </div>
  );
}
