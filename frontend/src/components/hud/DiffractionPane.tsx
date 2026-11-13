/**
 * Simulated diffraction dock pane (§5.6): radiation (Cu/Mo/Co/Cr/Fe/Ag Kα or a
 * custom λ), X-ray / neutron, 2θ range, stick pattern + Gaussian / pseudo-Voigt
 * profile (adjustable FWHM), and an experimental overlay (.xy / .xye / .csv)
 * with a 2θ shift (MACE lattice error) and scale. The pattern is computed by
 * POST /api/diffraction and is stale once the structure changes.
 */
import { FileUp, Loader2, RefreshCw, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { computeDiffraction } from "../../services/diffractionActions";
import { diffractionWavelength, useSettings, type DiffractionSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import { broadenPattern, parsePattern, RADIATIONS, transformPattern, type ProfileShape } from "../../utils/diffraction";
import { DiffractionChart } from "../charts/DiffractionChart";
import { Button, Segmented, Slider, cx } from "./ui";

const sel = "w-full rounded border border-white/10 bg-slate-900/80 px-1 py-0.5 font-mono text-[10.5px] text-slate-100 outline-none focus:border-cyan-400/50";
const num = "w-12 rounded border border-white/10 bg-slate-900/80 px-1 py-0.5 text-right font-mono text-[10.5px] text-slate-100 outline-none focus:border-cyan-400/50";

/** Whether a settings change needs a new server computation (profile settings are client-side). */
export function needsRecompute(a: DiffractionSettings, b: DiffractionSettings): boolean {
  return a.kind !== b.kind || a.radiation !== b.radiation || a.twoThetaMin !== b.twoThetaMin || a.twoThetaMax !== b.twoThetaMax || (a.radiation === "custom" && a.wavelength !== b.wavelength);
}

function DraftNumber({ value, onCommit, label, w = "w-12" }: { value: number; onCommit(v: number): void; label: string; w?: string }) {
  const [d, setD] = useState<string | null>(null);
  return (
    <input
      aria-label={label}
      className={cx(num, w)}
      inputMode="decimal"
      value={d ?? String(value)}
      onChange={(e) => setD(e.target.value)}
      onBlur={() => {
        const v = Number.parseFloat(d ?? "");
        setD(null);
        if (Number.isFinite(v) && v !== value) onCommit(v);
      }}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}

export function DiffractionPane() {
  const ds = useSettings((s) => s.diffraction);
  const set = useSettings((s) => s.setDiffraction);
  const diffraction = useCrystalStore((s) => s.diffraction);
  const pending = useCrystalStore((s) => s.diffractionPending);
  const error = useCrystalStore((s) => s.diffractionError);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const experimental = useCrystalStore((s) => s.xrdExperimental);
  const [shift, setShift] = useState(0);
  const [scaleF, setScaleF] = useState(1);
  const fileRef = useRef<HTMLInputElement>(null);
  const lastReq = useRef<DiffractionSettings | null>(null);

  // First open: compute at once.
  useEffect(() => {
    if (hasCrystal && !diffraction && !pending && !error) {
      lastReq.current = ds;
      void computeDiffraction();
    }
  }, [hasCrystal]);

  const stale = !!diffraction && diffraction.stateVersion !== stateVersion;
  const outdated = stale || (!!lastReq.current && needsRecompute(lastReq.current, ds));
  const recompute = () => {
    lastReq.current = ds;
    void computeDiffraction();
  };
  const range: [number, number] = [ds.twoThetaMin, ds.twoThetaMax];
  const peaks = diffraction?.result.peaks ?? [];
  const profile = useMemo(
    () => (ds.shape === "sticks" || !peaks.length ? null : broadenPattern(peaks, range, ds.fwhm, ds.shape, ds.eta)),
    [peaks, ds.shape, ds.fwhm, ds.eta, ds.twoThetaMin, ds.twoThetaMax],
  );
  const exp = useMemo(() => (experimental ? transformPattern(experimental, shift, scaleF) : null), [experimental, shift, scaleF]);

  const loadFile = async (file: File) => {
    const st = useCrystalStore.getState();
    const r = parsePattern(await file.text(), file.name);
    if (!r.ok) {
      st.pushToast("warning", "Could not read the pattern", `${file.name}: ${r.error}`);
      return;
    }
    st.setXrdExperimental(r.pattern);
    setShift(0);
    setScaleF(1);
    if (r.skipped) st.pushToast("info", "Pattern loaded", `${r.pattern.twoTheta.length} points (${r.skipped} non-numeric lines skipped).`);
  };

  return (
    <div className="flex h-full w-full min-w-0 gap-3">
      <div className="flex w-[200px] shrink-0 flex-col gap-1.5 overflow-y-auto border-r border-white/[0.06] pr-3 text-[10.5px] text-slate-400" data-xrd-controls>
        <div className="flex items-center justify-between gap-1">
          <Segmented
            value={ds.kind}
            options={[
              { value: "xray", label: "X-ray" },
              { value: "neutron", label: "neutron" },
            ]}
            onChange={(kind) => set({ kind })}
          />
          <Button className="px-1.5 py-0.5 text-[10px]" variant={outdated ? "primary" : "ghost"} disabled={!hasCrystal || pending} onClick={recompute} title="Compute the pattern for the current structure">
            {pending ? <Loader2 size={10} className="animate-spin" /> : <RefreshCw size={10} />} {diffraction ? "update" : "compute"}
          </Button>
        </div>
        <label className="flex items-center gap-1.5">
          <span className="w-10 shrink-0">source</span>
          <select className={sel} value={ds.radiation} onChange={(e) => set({ radiation: e.target.value })}>
            {RADIATIONS.map((r) => (
              <option key={r.key} value={r.key}>
                {r.label} ({r.wavelength.toFixed(4)} Å)
              </option>
            ))}
            <option value="custom">custom λ…</option>
          </select>
        </label>
        {ds.radiation === "custom" && (
          <label className="flex items-center justify-between">
            <span>λ (Å)</span>
            <DraftNumber label="wavelength" value={ds.wavelength} onCommit={(wavelength) => set({ wavelength })} w="w-16" />
          </label>
        )}
        <label className="flex items-center justify-between">
          <span>2θ range (°)</span>
          <span className="flex items-center gap-1">
            <DraftNumber label="2θ min" value={ds.twoThetaMin} onCommit={(twoThetaMin) => set({ twoThetaMin })} />
            –
            <DraftNumber label="2θ max" value={ds.twoThetaMax} onCommit={(twoThetaMax) => set({ twoThetaMax })} />
          </span>
        </label>
        <label className="flex items-center gap-1.5">
          <span className="w-10 shrink-0">profile</span>
          <select className={sel} value={ds.shape} onChange={(e) => set({ shape: e.target.value as ProfileShape })}>
            <option value="pseudo-voigt">pseudo-Voigt</option>
            <option value="gaussian">Gaussian</option>
            <option value="sticks">sticks only</option>
          </select>
        </label>
        {ds.shape !== "sticks" && (
          <Slider label="FWHM" value={ds.fwhm} min={0.02} max={1} step={0.01} format={(v) => `${v.toFixed(2)}°`} onChange={(fwhm) => set({ fwhm })} />
        )}
        <div className="mt-0.5 border-t border-white/[0.06] pt-1.5">
          <input
            ref={fileRef}
            type="file"
            accept=".xy,.xye,.csv,.txt,.dat"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void loadFile(f);
              e.target.value = "";
            }}
          />
          {experimental ? (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-1">
                <span className="truncate text-amber-300" title={experimental.name}>
                  {experimental.name}
                </span>
                <button type="button" aria-label="remove the experimental pattern" className="text-slate-500 hover:text-slate-200" onClick={() => useCrystalStore.getState().setXrdExperimental(null)}>
                  <X size={11} />
                </button>
              </div>
              <Slider label="2θ shift" value={shift} min={-2} max={2} step={0.01} format={(v) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}°`} onChange={setShift} />
              <Slider label="scale" value={scaleF} min={0.1} max={3} step={0.05} format={(v) => `${v.toFixed(2)}×`} onChange={setScaleF} />
            </div>
          ) : (
            <Button className="w-full px-1.5 py-0.5 text-[10px]" onClick={() => fileRef.current?.click()} title="Two or three columns (2θ, I[, σ]); '#' comments">
              <FileUp size={10} /> Overlay experiment (.xy/.xye/.csv)
            </Button>
          )}
        </div>
        {diffraction && (
          <p className="font-mono text-[10px] leading-snug text-slate-500">
            {peaks.length} peaks · λ {diffraction.result.wavelength_angstrom.toFixed(4)} Å ({diffraction.result.kind}
            {diffraction.result.radiation ? `, ${diffraction.result.radiation}` : ""})
          </p>
        )}
        {stale && <p className="text-amber-300/80">Structure changed: update the pattern.</p>}
        {error && <p className="text-rose-300">{error}</p>}
      </div>
      <div className="relative min-w-0 flex-1">
        {diffraction ? (
          <DiffractionChart peaks={peaks} profile={profile} experimental={exp} range={range} showSticks stale={stale} />
        ) : (
          <div className="grid h-full place-items-center text-[11px] text-slate-500">
            {pending ? (
              <span className="flex items-center gap-1.5 text-cyan-200">
                <Loader2 size={12} className="animate-spin" /> computing the {ds.kind === "neutron" ? "neutron" : "X-ray"} pattern (λ {diffractionWavelength(ds).toFixed(4)} Å)…
              </span>
            ) : (
              "Compute the powder pattern of the current structure."
            )}
          </div>
        )}
      </div>
    </div>
  );
}
