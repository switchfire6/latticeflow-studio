/**
 * Phonon control bar (§3, §7 Phase 5): mode list at the selected q-point
 * (imaginary modes marked), animation controls (play/pause, amplitude gain,
 * temperature, slow-motion speed) and the "Freeze & Relax Imaginary Mode
 * (Follow Instability)" action with its amplitude.
 */
import { BarChart3, Loader2, Pause, Play, Snowflake } from "lucide-react";
import { useState } from "react";
import { showPane } from "../../services/dockActions";
import { canFollow, followSelectedInstability, selectModeAt } from "../../services/phononActions";
import { commands } from "../../services/wsClient";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import { formatFrequency } from "../../utils/units";
import { classifyFrequency, qLabelAt } from "../../utils/bandChart";
import { Button, Slider, cx } from "./ui";

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
  if (!phonon) return null;
  const st = useCrystalStore.getState();

  const q = selection?.qIndex ?? phonon.kpath_labels[0]?.index ?? 0;
  const freqs = phonon.frequencies_thz[q] ?? [];
  const qLabel = qLabelAt(phonon.kpath_labels, q) ?? `q #${q}`;
  const amp = Number.parseFloat(amplitude);
  const ampOk = Number.isFinite(amp) && amp > 0 && amp <= 1;
  const pending = selection && (!loaded || loaded.q_index !== selection.qIndex || loaded.band_index !== selection.band);

  return (
    <div className="flex h-full w-[270px] shrink-0 flex-col gap-1.5 overflow-y-auto border-r border-white/[0.06] pr-3">
      <div className="flex items-center justify-between">
        <span className="font-mono text-[11px] text-slate-300">
          {qLabel} <span className="text-slate-500">· {freqs.length} modes</span>
        </span>
        <button
          type="button"
          title={playing ? "Pause the animation" : "Play the animation"}
          className="flex items-center gap-1 rounded-md border border-white/10 px-1.5 py-0.5 text-[10px] text-slate-300 hover:border-cyan-400/50"
          onClick={() => {
            st.setPhononPlaying(!playing);
            if (toolMode !== "PHONON_SHAKE") st.setToolMode("PHONON_SHAKE");
          }}
        >
          {playing ? <Pause size={11} /> : <Play size={11} />} {playing ? "Pause" : "Play"}
        </button>
      </div>
      <div className="min-h-[92px] flex-1 shrink-0 overflow-y-auto rounded-md border border-white/[0.06] bg-black/20" role="listbox" aria-label="modes at q">
        {freqs.map((f, b) => {
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
                "flex w-full items-center justify-between px-2 py-[3px] font-mono text-[10.5px]",
                sel ? "bg-cyan-400/15 text-cyan-100" : "text-slate-300 hover:bg-white/5",
              )}
            >
              <span>ν{b}</span>
              <span className={cx(cls === "imaginary" && "text-rose-300", cls === "zero" && "text-slate-500")}>
                {cls === "imaginary" && "⚠ "}
                {formatFrequency(f, unit, 3)}{cls === "imaginary" ? " (i)" : ""}
              </span>
            </button>
          );
        })}
      </div>
      <div className="flex items-center gap-2">
        <label className="flex items-center gap-1 text-[10.5px] text-slate-400" title="Maximum displacement of the frozen-in mode (Å)">
          amp
          <input
            className="w-14 rounded border border-white/10 bg-slate-900/80 px-1 py-0.5 font-mono text-[11px] text-slate-100 outline-none focus:border-cyan-400/50"
            value={amplitude}
            inputMode="decimal"
            onChange={(e) => setAmplitude(e.target.value)}
          />
          Å
        </label>
        <Button
          variant="danger"
          className="flex-1 px-2 py-1 text-[10.5px] leading-tight"
          disabled={!followable || !ampOk}
          title={
            !loaded?.is_imaginary
              ? "Select an imaginary (crimson) mode first"
              : pending
                ? "Waiting for the eigenvector…"
                : !ampOk
                  ? "Amplitude must be in (0, 1] Å"
                  : "Displace along this imaginary mode in its commensurate supercell and FIRE-relax"
          }
          onClick={() => followSelectedInstability(amp)}
        >
          <Snowflake size={12} /> Freeze &amp; Relax Imaginary Mode (Follow Instability)
        </Button>
      </div>
      <button
        type="button"
        className="flex items-center justify-center gap-1.5 rounded-md border border-white/10 bg-white/[0.03] px-2 py-1 text-[10.5px] text-slate-300 hover:border-cyan-400/40 hover:text-cyan-100"
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
