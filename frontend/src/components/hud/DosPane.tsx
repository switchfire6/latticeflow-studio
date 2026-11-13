/**
 * Phonon DOS & thermodynamics dock pane (§5.1): total and element-projected
 * DOS (frequency in the user's unit), F(T), S(T) and C_v(T) with the
 * zero-point energy, and a warning when imaginary modes are excluded from the
 * thermal sums. Requested automatically after each shake-test (setting) or
 * with the button here.
 */
import { CHART_COLORS } from "../charts/useCanvasChart";
import { AlertTriangle, BarChart3, Loader2 } from "lucide-react";
import { commands } from "../../services/wsClient";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { PhononDosData } from "../../types/protocol";
import { SERIES_COLORS, dosInUnit, interpAt } from "../../utils/chartData";
import { frequencyUnitLabel } from "../../utils/units";
import { LineChart } from "../charts/LineChart";
import { Button, FallbackTag } from "./ui";

const TOTAL_COLOR = CHART_COLORS.ink;
const [S_COLOR, CV_COLOR, F_COLOR] = SERIES_COLORS;

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
      <div className="grid h-full w-full place-items-center text-xs text-fg-4">
        <div className="text-center">
          <p className="mb-2">{hasPhonon ? "Phonon DOS and thermodynamics from the cached force constants." : "Run phonons first."}</p>
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
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 font-mono text-2xs text-fg-3">
        <span>
          ZPE <span className="text-fg">{(th.zero_point_energy_ev_per_atom * 1000).toFixed(2)} meV/atom</span>
        </span>
        <span>
          300 K: F <span className="text-fg">{fmt(f300 !== null ? f300 * 1000 : null, 1)} meV/at</span> · S{" "}
          <span className="text-fg">{fmt(s300, 3)}</span> · C_v <span className="text-fg">{fmt(cv300, 3)}</span> k_B/at
        </span>
        <span className="text-fg-4">mesh {dos.mesh.join("×")}</span>
        <FallbackTag />
        <Button className="ml-auto px-1.5 py-0 text-2xs" disabled={pending} onClick={() => commands.requestDos()} title="Recompute the DOS">
          {pending ? <Loader2 size={10} className="animate-spin" /> : <BarChart3 size={10} />} recompute
        </Button>
      </div>
      {warn && (
        <div className="flex shrink-0 items-start gap-1.5 rounded-ctl border border-bad/30 bg-bad/10 px-2 py-0.5 text-2xs leading-snug text-bad" data-imaginary-warning>
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
