/**
 * Equation-of-state dock pane (§3.3): E(V) and P(V) from COMPUTE_BULK_MODULUS
 * with the given volume V0 and the fit's zero-pressure volume V_eq marked, the
 * readouts K(V0), B0, B0′, V_eq (flagged when extrapolated) and the method
 * text. Clamped-ion: internal coordinates are not relaxed at each volume.
 * `EosReadouts` is pure (server-rendered in tests).
 */
import { Gauge, Loader2 } from "lucide-react";
import { commands } from "../../services/wsClient";
import { useSettings } from "../../store/settings";
import { useCrystalStore, type EosState } from "../../store/useCrystalStore";
import { eosPoints } from "../../utils/chartData";
import { formatPressure, type PressureUnit } from "../../utils/units";
import { LineChart } from "../charts/LineChart";
import { CHART_COLORS } from "../charts/useCanvasChart";
import { Button, FallbackTag, cx } from "./ui";

export const CLAMPED_ION_NOTE = "Clamped-ion: atoms are scaled with the cell, not relaxed at each volume (an upper bound on the relaxed-ion K).";

function R({ label, value, title, warn }: { label: string; value: string; title?: string; warn?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2" title={title}>
      <span className="text-slate-500">{label}</span>
      <span className={cx("tabular-nums", warn ? "text-amber-300" : "text-slate-100")}>{value}</span>
    </div>
  );
}

const num = (v: number | null | undefined, d: number, unit = "") => (v == null || !Number.isFinite(v) ? "—" : `${v.toFixed(d)}${unit}`);

export function EosReadouts({
  eos,
  stale,
  pressureUnit,
  onRefit,
  refitDisabled,
}: {
  eos: EosState;
  stale: boolean;
  pressureUnit: PressureUnit;
  onRefit?: () => void;
  refitDisabled?: boolean;
}) {
  const r = eos.result;
  const n = Math.max(1, eos.nAtoms);
  return (
    <div className="flex h-full w-[190px] shrink-0 flex-col gap-1 overflow-y-auto border-r border-white/[0.06] pr-3 font-mono text-[10.5px]" data-eos-readouts>
      <R label="K(V0)" value={num(r.k_gpa, 1, " GPa")} title="K = V d²E/dV² at the given volume" />
      <R label="K from P(V)" value={num(r.k_stress_gpa, 1, " GPa")} title="−V dP/dV from the stresses (cross-check)" />
      <R label="B0 (BM, at V_eq)" value={num(r.k_eq_gpa, 1, " GPa")} />
      <R label="B0′" value={num(r.k_eq_prime, 2)} />
      <R label="V0" value={`${r.v0.toFixed(3)} Å³`} title={`${(r.v0 / n).toFixed(3)} Å³/atom`} />
      <R
        label={r.extrapolated ? "V_eq (extrapolated)" : "V_eq"}
        value={r.v_eq != null ? `${r.v_eq.toFixed(3)} Å³` : "—"}
        warn={r.extrapolated}
        title={r.extrapolated ? "V_eq lies outside the sampled volume window; widen the window for a reliable value" : undefined}
      />
      {r.v_eq != null && <R label="ΔV (V_eq vs V0)" value={`${(((r.v_eq - r.v0) / r.v0) * 100).toFixed(2)} %`} />}
      <R label="P(V0)" value={formatPressure(r.pressure_gpa, pressureUnit)} />
      {stale && <p className="text-amber-300/80">Structure changed since this fit; recompute.</p>}
      <p className="mt-1 leading-snug text-slate-500">{r.method}</p>
      <p className="leading-snug text-slate-500">{CLAMPED_ION_NOTE}</p>
      <FallbackTag className="self-start" />
      {onRefit && (
        <Button className="mt-1 self-start px-2 py-0.5 text-[10px]" disabled={refitDisabled} onClick={onRefit} title="Refit over ±6 % (9 points) for a better B0′ and V_eq">
          <Gauge size={10} /> Refit over ±6 %
        </Button>
      )}
    </div>
  );
}

export function EosPane() {
  const eos = useCrystalStore((s) => s.eos);
  const pending = useCrystalStore((s) => s.eosPending);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const connected = useCrystalStore((s) => s.connection === "open");
  const pressureUnit = useSettings((s) => s.units.pressure);
  if (!eos) {
    return (
      <div className="grid h-full w-full place-items-center text-[11px] text-slate-500">
        <div className="text-center">
          <p className="mb-2">Compute the bulk modulus to see E(V) and P(V).</p>
          <Button disabled={!connected || pending} onClick={() => commands.computeBulkModulus()}>
            {pending ? <Loader2 size={12} className="animate-spin" /> : <Gauge size={12} />} Compute K
          </Button>
        </div>
      </div>
    );
  }
  const r = eos.result;
  const pts = eosPoints(r, eos.nAtoms);
  const vs = pts.map((p) => p.volume);
  const markers = [
    { x: r.v0, label: "V0", color: "rgba(226, 232, 240, 0.7)" },
    ...(r.v_eq != null ? [{ x: r.v_eq, label: r.extrapolated ? "V_eq (extrap.)" : "V_eq", color: CHART_COLORS.amber, dashed: true, labelAt: "bottom" as const }] : []),
  ];
  const vLo = Math.min(...vs, r.v_eq ?? Infinity);
  const vHi = Math.max(...vs, r.v_eq ?? -Infinity);
  const pad = (vHi - vLo) * 0.04 || 1;
  const xDomain: [number, number] = [vLo - pad, vHi + pad];
  const stale = eos.stateVersion !== stateVersion;
  const k = pressureUnit === "kbar" ? 10 : 1;
  return (
    <div className="flex h-full w-full gap-3">
      <EosReadouts
        eos={eos}
        stale={stale}
        pressureUnit={pressureUnit}
        refitDisabled={!connected || pending}
        onRefit={() => commands.computeBulkModulus({ strain_window: 0.06, n_points: 9 })}
      />
      <div className="relative flex min-w-0 flex-1 flex-col gap-1">
        <div className="min-h-0 flex-1">
          <LineChart
            series={[{ label: "E(V)", xs: vs, ys: pts.map((p) => p.dEmeVPerAtom), color: CHART_COLORS.cyan, points: true }]}
            xLabel="V (Å³)"
            yLabel="ΔE (meV/at)"
            markers={markers}
            xDomain={xDomain}
            includeZeroY
            formatX={(v) => `${v.toFixed(2)} Å³`}
            formatY={(v) => `${v.toFixed(2)} meV/atom`}
          />
        </div>
        <div className="min-h-0 flex-1">
          <LineChart
            series={[{ label: "P(V)", xs: vs, ys: pts.map((p) => p.pressureGpa * k), color: CHART_COLORS.sky, points: true }]}
            xLabel="V (Å³)"
            yLabel={`P (${pressureUnit})`}
            markers={markers}
            xDomain={xDomain}
            includeZeroY
            zeroLine
            formatX={(v) => `${v.toFixed(2)} Å³`}
            formatY={(v) => `${v.toFixed(2)} ${pressureUnit}`}
          />
        </div>
      </div>
    </div>
  );
}
