/**
 * Molecular-dynamics dock pane (§5.8): settings (T, ensemble, Δt, steps,
 * Langevin friction, NPT pressure, frame interval, seed), START_MD / Stop,
 * live T(t), E_pot(t), P(t) or V(t) and MSD per element from MD_FRAME, and
 * after MD_COMPLETE the diffusion coefficient per element (cm²/s) and the
 * run means. The "Arrhenius" tab runs MD at several temperatures from the
 * same starting entry (services/mdActions.ts) and fits ln D vs 1/T per
 * element → activation energy and prefactor.
 * Phase C2: Nernst–Einstein ionic conductivity σ = n q² D / (k_B T) per
 * species (charge from the bond-valence analysis, else a common oxidation
 * state; n from the MD cell), after a run and as σ(T) in the Arrhenius tab.
 */
import { Flame, Play, Square } from "lucide-react";
import { useState } from "react";
import { DEFAULT_MD, abortArrheniusScan, mdPayload, startArrheniusScan, type MdSettings } from "../../services/mdActions";
import { commands } from "../../services/wsClient";
import { useCrystalStore, type MdSeries } from "../../store/useCrystalStore";
import { scanPoints, useUi } from "../../store/uiStore";
import { arrheniusFit, parseTemperatures, sciText } from "../../utils/analysisC";
import { chargeText, elementCountMap, ionicConductivities, oxidationStatesFromSites, type ConductivityRow } from "../../utils/analysisC2";
import { LineChart, type LineSeries } from "../charts/LineChart";
import { SERIES_COLORS } from "../../utils/chartData";
import { Button, FallbackTag, NumberField, Segmented, cx, inputCls } from "./ui";

type Tab = "live" | "arrhenius";

function Settings({ m, set, disabled }: { m: MdSettings; set(p: Partial<MdSettings>): void; disabled: boolean }) {
  return (
    <div className="space-y-1" data-md-settings>
      <NumberField label="T" unit="K" value={m.temperatureK} min={1} max={5000} onCommit={(temperatureK) => set({ temperatureK })} disabled={disabled} />
      <div className="flex items-center justify-between text-xs text-fg-3" title="NVE: microcanonical · NVT: Langevin thermostat · NPT: Langevin + isotropic barostat">
        <span>ensemble</span>
        <Segmented<MdSettings["ensemble"]>
          value={m.ensemble}
          options={(["NVE", "NVT", "NPT"] as const).map((e) => ({ value: e, label: e, disabled }))}
          onChange={(ensemble) => set({ ensemble })}
        />
      </div>
      <NumberField label="Δt" unit="fs" value={m.timestepFs} min={0.1} max={5} onCommit={(timestepFs) => set({ timestepFs })} disabled={disabled} />
      <NumberField label="steps" value={m.nSteps} integer min={1} max={200000} onCommit={(nSteps) => set({ nSteps })} disabled={disabled} title={`${((m.nSteps * m.timestepFs) / 1000).toFixed(2)} ps`} />
      <NumberField label="friction" unit="1/fs" value={m.frictionPerFs} min={0.0001} max={1} onCommit={(frictionPerFs) => set({ frictionPerFs })} disabled={disabled || m.ensemble === "NVE"} title="Langevin friction" />
      <NumberField label="pressure" unit="GPa" value={m.pressureGpa} min={-50} max={500} onCommit={(pressureGpa) => set({ pressureGpa })} disabled={disabled || m.ensemble !== "NPT"} />
      <NumberField label="frame every" unit="steps" value={m.frameInterval} integer min={1} max={1000} onCommit={(frameInterval) => set({ frameInterval })} disabled={disabled} />
      <label className="flex items-center justify-between gap-2 text-xs text-fg-3">
        <span>seed</span>
        <input
          className="w-[68px] rounded-ctl border border-line bg-field px-1.5 py-0.5 text-right font-mono text-xs text-fg outline-none"
          placeholder="random"
          inputMode="numeric"
          value={m.seed}
          disabled={disabled}
          onChange={(e) => set({ seed: e.target.value.replace(/[^\d]/g, "") })}
        />
      </label>
    </div>
  );
}

function Chart({ title, series, yLabel }: { title: string; series: LineSeries[]; yLabel: string }) {
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col rounded-ctl border border-line-soft">
      <div className="px-1.5 text-2xs text-fg-4">{title}</div>
      <div className="relative min-h-0 flex-1">
        <LineChart series={series} xLabel="t (ps)" yLabel={yLabel} empty="waiting for frames" />
      </div>
    </div>
  );
}

function LiveCharts({ md }: { md: MdSeries }) {
  const [pv, setPv] = useState<"P" | "V">("P");
  const t = md.times;
  const hasP = md.pressure.some((v) => Number.isFinite(v));
  const showP = pv === "P" && hasP;
  const els = Object.keys(md.msd).sort();
  return (
    <div className="grid h-full min-h-0 grid-cols-2 grid-rows-2 gap-1">
      <Chart title="temperature" yLabel="T (K)" series={[{ label: "T", xs: t, ys: md.temperature, color: SERIES_COLORS[0], width: 1.4 }]} />
      <Chart title="potential energy" yLabel="E_pot (eV/atom)" series={[{ label: "E_pot", xs: t, ys: md.epotPerAtom, color: SERIES_COLORS[0], width: 1.4 }]} />
      <div className="relative min-h-0 min-w-0">
        <Chart
          title={showP ? "pressure" : "volume"}
          yLabel={showP ? "P (GPa)" : "V (Å³)"}
          series={[showP ? { label: "P", xs: t, ys: md.pressure, color: SERIES_COLORS[0], width: 1.4 } : { label: "V", xs: t, ys: md.volume, color: SERIES_COLORS[0], width: 1.4 }]}
        />
        <span className="absolute top-0 right-1">
          <Segmented<"P" | "V">
            value={showP ? "P" : "V"}
            options={[
              { value: "P", label: "P", disabled: !hasP },
              { value: "V", label: "V" },
            ]}
            onChange={setPv}
          />
        </span>
      </div>
      <Chart title="mean-squared displacement" yLabel="MSD (Å²)" series={els.map((el, i) => ({ label: el, xs: t, ys: md.msd[el]!, color: SERIES_COLORS[i % SERIES_COLORS.length]!, width: 1.4 }))} />
    </div>
  );
}

export const NE_CAVEAT = "Nernst–Einstein (no correlation, Haven ratio = 1)";

/** Charges from the current local analysis (bond-valence oxidation states), if any. */
function useAnalysisCharges(): Record<string, number> {
  const sites = useCrystalStore((s) => s.localAnalysis?.data.sites ?? null);
  return oxidationStatesFromSites(sites);
}

const plain = (v: number) => (v >= 0.01 && v < 1e4 ? (v >= 100 ? v.toFixed(0) : v.toPrecision(3)) : sciText(v, 2));
function sigmaText(sigma: number | null): string {
  if (sigma == null) return "—";
  return `${plain(sigma)} S/cm · ${plain(sigma * 1000)} mS/cm`;
}
const SUP: Record<string, string> = { "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹" };
/** Li⁺, O²⁻; a non-integer bond-valence charge as "Li(+0.98)". */
function ionLabel(el: string, z: number | null): string {
  if (z == null) return el;
  const r = Math.round(z);
  if (Math.abs(z - r) > 0.05) return `${el}(${chargeText(z)})`;
  const m = Math.abs(r);
  return `${el}${m === 1 ? "" : [...String(m)].map((d) => SUP[d]).join("")}${r < 0 ? "⁻" : "⁺"}`;
}

function ConductivityLine({ rows }: { rows: ConductivityRow[] }) {
  const shown = rows.filter((r) => r.d != null);
  if (!shown.length) return null;
  return (
    <div className="flex shrink-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 font-mono text-xs" data-md-sigma>
      {shown.map((r) => (
        <span key={r.element} title={`q = ${chargeText(r.charge)}e (${r.chargeSource === "analysis" ? "bond-valence analysis" : r.chargeSource === "guess" ? "common oxidation state" : "unknown"}), n = ${sciText(r.density, 2)} cm⁻³`}>
          <span className="text-fg-3">σ({ionLabel(r.element, r.charge)}) </span>
          <span className={r.sigma == null ? "text-fg-5" : "text-ok"}>{sigmaText(r.sigma)}</span>
        </span>
      ))}
      <span className="text-2xs text-fg-4">{NE_CAVEAT}</span>
    </div>
  );
}

function CompleteSummary({ md }: { md: MdSeries }) {
  const charges = useAnalysisCharges();
  const c = md.complete;
  if (!c) return null;
  const sigmaRows = ionicConductivities(elementCountMap(c.final_crystal.atoms), c.mean_volume, c.diffusion_coefficients_cm2_s, c.mean_temperature_k, charges);
  const ds = Object.entries(c.diffusion_coefficients_cm2_s).sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="flex shrink-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 font-mono text-xs" data-md-result>
      <span className="text-fg-4">
        {c.n_steps} steps · {c.time_ps.toFixed(2)} ps · ⟨T⟩ {c.mean_temperature_k.toFixed(0)} K{c.mean_pressure_gpa != null ? ` · ⟨P⟩ ${c.mean_pressure_gpa.toFixed(2)} GPa` : ""} · ⟨V⟩ {c.mean_volume.toFixed(1)} Å³
      </span>
      {ds.map(([el, d]) => (
        <span key={el} title="D = slope(MSD)/6 over the second half of the run">
          <span className="text-fg-3">D({el}) </span>
          <span className={d == null ? "text-fg-5" : "text-fg"}>{d == null ? "no linear regime" : `${sciText(d)} cm²/s`}</span>
        </span>
      ))}
      <div className="basis-full" />
      <ConductivityLine rows={sigmaRows} />
    </div>
  );
}

function ArrheniusTab({ settings, busy }: { settings: MdSettings; busy: boolean }) {
  const scan = useUi((s) => s.arrhenius);
  const canRun = useCrystalStore((s) => s.connection === "open" && s.crystal !== null && s.history !== null);
  const crystal = useCrystalStore((s) => s.crystal);
  const charges = useAnalysisCharges();
  const [text, setText] = useState("600 800 1000 1200");
  const temps = parseTemperatures(text);
  const running = scan?.status === "running";
  const els = [...new Set((scan?.results ?? []).flatMap((r) => Object.keys(r.d)))].sort();
  const fits = els.map((el) => ({ el, fit: arrheniusFit(scanPoints(scan, el)) }));
  const series: LineSeries[] = [];
  els.forEach((el, i) => {
    const color = SERIES_COLORS[i % SERIES_COLORS.length]!;
    const pts = scanPoints(scan, el).filter((p) => p.d != null && p.d > 0);
    if (!pts.length) return;
    series.push({ label: `${el}`, xs: pts.map((p) => 1000 / p.temperatureK), ys: pts.map((p) => Math.log10(p.d!)), color, points: true, width: 0.01 });
    const fit = fits[i]!.fit;
    if (fit) {
      const x = pts.map((p) => 1000 / p.temperatureK);
      const x0 = Math.min(...x);
      const x1 = Math.max(...x);
      const y = (xk: number) => Math.log10(fit.d0) - (fit.ea / 8.617333262e-5) * (xk / 1000) / Math.LN10;
      series.push({ label: `${el} fit`, xs: [x0, x1], ys: [y(x0), y(x1)], color, width: 1.2 });
    }
  });
  return (
    <div className="flex h-full min-h-0 gap-2">
      <div className="flex w-[240px] shrink-0 flex-col gap-1.5 overflow-y-auto text-xs">
        <label className="block">
          <div className="mb-0.5 text-fg-4">temperatures (K)</div>
          <input className={inputCls} value={text} onChange={(e) => setText(e.target.value)} disabled={running} />
        </label>
        <p className="leading-snug text-fg-4">
          {temps.length} runs × {settings.nSteps} steps ({settings.ensemble}) from the current history entry; the structure is restored (undo) between runs.
        </p>
        {running ? (
          <Button variant="danger" onClick={abortArrheniusScan}>
            <Square size={11} /> Stop scan
          </Button>
        ) : (
          <Button variant="primary" disabled={!canRun || busy || temps.length < 2} onClick={() => startArrheniusScan(temps, settings)}>
            <Flame size={12} /> Run Arrhenius scan
          </Button>
        )}
        {scan && (
          <div className="font-mono text-fg-3">
            {scan.status === "running" ? `run ${Math.min(scan.index + 1, scan.temperatures.length)}/${scan.temperatures.length} at ${scan.temperatures[scan.index] ?? "—"} K` : scan.status === "done" ? "scan complete" : `scan aborted${scan.message ? ` (${scan.message})` : ""}`}
          </div>
        )}
        {fits.length > 0 && (
          <table className="w-full font-mono tabular-nums" data-arrhenius>
            <thead className="text-fg-4">
              <tr>
                <th className="text-left font-normal">el</th>
                <th className="text-right font-normal">E_a (eV)</th>
                <th className="text-right font-normal">D₀ (cm²/s)</th>
                <th className="text-right font-normal">r²</th>
              </tr>
            </thead>
            <tbody>
              {fits.map(({ el, fit }) => (
                <tr key={el}>
                  <td className="text-fg">{el}</td>
                  <td className="text-right text-fg">{fit ? fit.ea.toFixed(3) : "—"}</td>
                  <td className="text-right text-fg">{fit ? sciText(fit.d0, 1) : "—"}</td>
                  <td className="text-right text-fg-3">{fit ? fit.r2.toFixed(3) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {scan && scan.results.length > 0 && crystal && (
          <table className="w-full font-mono tabular-nums" data-arrhenius-sigma title={`${NE_CAVEAT}; n from the start cell (${crystal.lattice.volume.toFixed(1)} Å³)`}>
            <thead className="text-fg-4">
              <tr>
                <th className="text-left font-normal">T (K)</th>
                {els.map((el) => (
                  <th key={el} className="text-right font-normal">
                    σ({el}) mS/cm
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {scan.results.map((r) => {
                const rows = ionicConductivities(elementCountMap(crystal.atoms), crystal.lattice.volume, r.d, r.temperatureK, charges);
                return (
                  <tr key={r.temperatureK}>
                    <td className="text-fg-2">{r.temperatureK}</td>
                    {els.map((el) => {
                      const sg = rows.find((x) => x.element === el)?.sigma ?? null;
                      return (
                        <td key={el} className="text-right text-ok">
                          {sg == null ? "—" : plain(sg * 1000)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr>
                <td colSpan={els.length + 1} className="pt-0.5 text-2xs text-fg-4">
                  {NE_CAVEAT}
                </td>
              </tr>
            </tfoot>
          </table>
        )}
        {scan && scan.results.length > 0 && (
          <table className="w-full font-mono tabular-nums">
            <thead className="text-fg-4">
              <tr>
                <th className="text-left font-normal">T (K)</th>
                {els.map((el) => (
                  <th key={el} className="text-right font-normal">
                    D({el})
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {scan.results.map((r) => (
                <tr key={r.temperatureK}>
                  <td className="text-fg-2">{r.temperatureK}</td>
                  {els.map((el) => (
                    <td key={el} className="text-right text-fg">
                      {sciText(r.d[el] ?? null, 1)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="relative min-w-0 flex-1">
        <LineChart series={series} xLabel="1000/T (1/K)" yLabel="log₁₀ D (cm²/s)" empty="Run a scan: log₁₀ D vs 1000/T with a linear fit per element" />
      </div>
    </div>
  );
}

export function MdPane() {
  useCrystalStore((s) => s.mdVersion); // re-render on MD data
  const md = useCrystalStore.getState().session.md;
  const running = useCrystalStore((s) => s.streams.find((x) => x.command === "START_MD") ?? null);
  const busy = useCrystalStore((s) => s.streams.length > 0);
  const connected = useCrystalStore((s) => s.connection === "open");
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const scanRunning = useUi((s) => s.arrhenius?.status === "running");
  const [m, setM] = useState<MdSettings>(DEFAULT_MD);
  const [tab, setTab] = useState<Tab>("live");
  const set = (p: Partial<MdSettings>) => setM((x) => ({ ...x, ...p }));
  return (
    <div className="flex h-full w-full min-w-0 gap-2" data-md-pane>
      <div className="flex w-[170px] shrink-0 flex-col gap-1.5 overflow-y-auto pr-1">
        <Button
          variant={running ? "danger" : "primary"}
          className="py-1"
          disabled={!connected || !hasCrystal || (!running && busy) || !!running?.stopping || scanRunning}
          onClick={() => (running ? commands.stop() : commands.startMd(mdPayload(m)))}
        >
          {running ? <Square size={11} /> : <Play size={11} />} {running ? "Stop MD" : "Run MD"}
        </Button>
        <FallbackTag compact />
        <Settings m={m} set={set} disabled={!!running || scanRunning} />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex shrink-0 items-center gap-2">
          <Segmented<Tab>
            value={tab}
            options={[
              { value: "live", label: "live" },
              { value: "arrhenius", label: "Arrhenius" },
            ]}
            onChange={setTab}
          />
          {tab === "live" && (
            <span className={cx("text-2xs", md.status === "failed" ? "text-bad" : md.status === "stopped" ? "text-warn" : "text-fg-4")}>
              {md.status === "idle" ? "frames render like a relaxation; the last frame becomes the structure (one undo step)" : md.status}
              {md.times.length ? ` · ${md.times.length} frames · t = ${md.times[md.times.length - 1]!.toFixed(2)} ps` : ""}
            </span>
          )}
        </div>
        {tab === "live" ? (
          <>
            <CompleteSummary md={md} />
            <div className="min-h-0 flex-1">
              <LiveCharts md={md} />
            </div>
          </>
        ) : (
          <div className="min-h-0 flex-1">
            <ArrheniusTab settings={m} busy={busy} />
          </div>
        )}
      </div>
    </div>
  );
}
