/**
 * Phonon DOS & thermodynamics dock pane (§5.1): total and element-projected
 * DOS (frequency in the user's unit), F(T), S(T) and C_v(T) with the
 * zero-point energy, and a warning when imaginary modes are excluded from the
 * thermal sums. Requested automatically after each shake-test (setting) or
 * with the button here.
 */
import { AlertTriangle, BarChart3, Loader2 } from "lucide-react";
import { commands } from "../../services/wsClient";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { PhononDosData } from "../../types/protocol";
import { dosInUnit, interpAt } from "../../utils/chartData";
import { frequencyUnitLabel } from "../../utils/units";
import { LineChart } from "../charts/LineChart";
import { Button, FallbackTag } from "./ui";

const TOTAL_COLOR = "rgba(226, 232, 240, 0.85)";
const S_COLOR = "#3987e5";
const CV_COLOR = "#d95926";
const F_COLOR = "#199e70";

/** Text for the imaginary-mode warning (null when all mesh modes are real). */
export function imaginaryDosWarning(d: Pick<PhononDosData, "imaginary_fraction">): string | null {
  if (!(d.imaginary_fraction > 0)) return null;
  return `${(d.imaginary_fraction * 100).toFixed(1)} % of the mesh modes are imaginary and are excluded from F, S and C_v; the thermal properties describe an unstable structure.`;
}

export function DosPane() {
  const dos = useCrystalStore((s) => s.dos);
  const pending = useCrystalStore((s) => s.dosPending);
  const hasPhonon = useCrystalStore((s) => s.phonon !== null);
  const atoms = useCrystalStore((s) => s.crystal?.atoms);
  const unit = useSettings((s) => s.units.frequency);
  if (!dos) {
    return (
      <div className="grid h-full w-full place-items-center text-[11px] text-slate-500">
        <div className="text-center">
          <p className="mb-2">{hasPhonon ? "Phonon DOS and thermodynamics from the cached force constants." : "Run the phonon shake-test first."}</p>
          <Button disabled={!hasPhonon || pending} onClick={() => commands.requestDos()}>
            {pending ? <Loader2 size={12} className="animate-spin" /> : <BarChart3 size={12} />} {pending ? "Computing DOS…" : "Compute DOS"}
          </Button>
        </div>
      </div>
    );
  }
  const order = atoms ? [...new Set(atoms.map((a) => a.element))] : undefined;
  const s = dosInUnit(dos, unit, order);
  const ul = frequencyUnitLabel(unit);
  const th = dos.thermal;
  const warn = imaginaryDosWarning(dos);
  const at300 = (ys: number[]) => interpAt(th.temperatures_k, ys, 300);
  const f300 = at300(th.free_energy_ev_per_atom);
  const s300 = at300(th.entropy_kb_per_atom);
  const cv300 = at300(th.heat_capacity_kb_per_atom);
  const fmt = (v: number | null, d: number) => (v === null ? "—" : v.toFixed(d));

  return (
    <div className="flex h-full w-full flex-col gap-1">
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 font-mono text-[10px] text-slate-400">
        <span>
          ZPE <span className="text-slate-100">{(th.zero_point_energy_ev_per_atom * 1000).toFixed(2)} meV/atom</span>
        </span>
        <span>
          300 K: F <span className="text-slate-100">{fmt(f300 !== null ? f300 * 1000 : null, 1)} meV/at</span> · S{" "}
          <span className="text-slate-100">{fmt(s300, 3)}</span> · C_v <span className="text-slate-100">{fmt(cv300, 3)}</span> k_B/at
        </span>
        <span className="text-slate-500">mesh {dos.mesh.join("×")}</span>
        <FallbackTag />
        <Button className="ml-auto px-1.5 py-0 text-[10px]" disabled={pending} onClick={() => commands.requestDos()} title="Recompute the DOS">
          {pending ? <Loader2 size={10} className="animate-spin" /> : <BarChart3 size={10} />} recompute
        </Button>
      </div>
      {warn && (
        <div className="flex shrink-0 items-start gap-1.5 rounded border border-rose-500/30 bg-rose-500/10 px-2 py-0.5 text-[10px] leading-snug text-rose-200" data-imaginary-warning>
          <AlertTriangle size={11} className="mt-px shrink-0" /> {warn}
        </div>
      )}
      <div className="flex min-h-0 flex-1 gap-2">
        <div className="min-w-0 flex-[1.4]">
          <LineChart
            series={[
              { label: "total", xs: s.frequencies, ys: s.total, color: TOTAL_COLOR, fill: true, width: 1.6 },
              ...s.projected.map((p) => ({ label: p.element, xs: s.frequencies, ys: p.values, color: p.color, width: 1.6 })),
            ]}
            xLabel={`f (${ul})`}
            yLabel={`DOS (states/${ul}/cell)`}
            shadeBelowX={0}
            includeZeroY
            formatX={(v) => `${v.toFixed(unit === "cm-1" ? 0 : 2)} ${ul}`}
            formatY={(v) => v.toFixed(3)}
          />
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="min-h-0 flex-1">
            <LineChart
              series={[
                { label: "S", xs: th.temperatures_k, ys: th.entropy_kb_per_atom, color: S_COLOR },
                { label: "C_v", xs: th.temperatures_k, ys: th.heat_capacity_kb_per_atom, color: CV_COLOR },
              ]}
              xLabel="T (K)"
              yLabel="k_B/atom"
              includeZeroY
              formatX={(v) => `${v.toFixed(0)} K`}
              formatY={(v) => `${v.toFixed(3)} k_B`}
            />
          </div>
          <div className="min-h-0 flex-1">
            <LineChart
              series={[{ label: "F_vib", xs: th.temperatures_k, ys: th.free_energy_ev_per_atom.map((v) => v * 1000), color: F_COLOR }]}
              xLabel="T (K)"
              yLabel="F (meV/at)"
              zeroLine
              formatX={(v) => `${v.toFixed(0)} K`}
              formatY={(v) => `${v.toFixed(1)} meV/atom`}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
