/**
 * Strip above the dispersion chart (§5.2–5.3): cell mode (textbook primitive
 * / as displayed), supercell and minimum image distance, k-path source, the
 * branch-colour toggle and "Check convergence" (CHECK_PHONON_CONVERGENCE) with
 * its progress and result (max |Δf|, converged ✓/✗ against the tolerance,
 * per-label deltas on expand).
 */
import { ChevronDown, ChevronRight, Loader2, Microscope, Palette } from "lucide-react";
import { useState } from "react";
import { commands } from "../../services/wsClient";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { PhononAnalysisComplete, PhononConvergenceResult } from "../../types/protocol";
import { formatKLabel } from "../../utils/bandChart";
import { formatFrequency, type FrequencyUnit } from "../../utils/units";
import { Button, cx } from "./ui";

export function phononCellText(p: Pick<PhononAnalysisComplete, "cell_mode" | "n_primitive_atoms" | "supercell_dim" | "min_image_distance">): string {
  const parts: string[] = [];
  if (p.cell_mode === "primitive") parts.push(`primitive${p.n_primitive_atoms ? ` (${p.n_primitive_atoms} at)` : ""}`);
  else if (p.cell_mode === "as_displayed") parts.push("as displayed");
  if (p.supercell_dim) parts.push(`${p.supercell_dim.join("×")} supercell`);
  if (p.min_image_distance != null) parts.push(`d_min ${p.min_image_distance.toFixed(1)} Å`);
  return parts.join(" · ");
}

export function ConvergenceReadout({ c, unit }: { c: PhononConvergenceResult; unit: FrequencyUnit }) {
  const [open, setOpen] = useState(false);
  const labels = Object.entries(c.per_label_max_delta_thz);
  return (
    <span className="flex min-w-0 items-center" data-convergence>
      <button
        type="button"
        className="flex min-w-0 items-center gap-1 hover:text-slate-200"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title={`Convergence ${c.supercell_dim_a.join("×")} → ${c.supercell_dim_b.join("×")}: max |Δf| ${formatFrequency(c.max_abs_delta_thz, unit, 3)} (tolerance ${formatFrequency(c.tolerance_thz, unit, 3)}); click for per-label deltas`}
      >
        {open ? <ChevronDown size={10} className="shrink-0" /> : <ChevronRight size={10} className="shrink-0" />}
        <span className={cx("truncate", c.converged ? "text-emerald-300" : "text-rose-300")}>
          {c.converged ? "✓" : "✗"} max |Δf| {formatFrequency(c.max_abs_delta_thz, unit, 3)}
          <span className="text-slate-500"> · {c.supercell_dim_a.join("×")}→{c.supercell_dim_b.join("×")}</span>
        </span>
      </button>
      {open && (
        <div className="absolute top-full right-0 z-30 mt-1 w-[260px] font-mono text-[10px] rounded-lg border border-white/10 bg-slate-950/95 p-2 shadow-lg">
          <div className="mb-1 grid grid-cols-2 gap-x-2 text-slate-400">
            <span>d_min</span>
            <span className="text-right text-slate-200">
              {c.min_image_distance_a.toFixed(1)} → {c.min_image_distance_b.toFixed(1)} Å
            </span>
            <span>min f</span>
            <span className="text-right text-slate-200">
              {formatFrequency(c.min_frequency_thz_a, unit)} → {formatFrequency(c.min_frequency_thz_b, unit)}
            </span>
            <span>tolerance</span>
            <span className="text-right text-slate-200">{formatFrequency(c.tolerance_thz, unit, 3)}</span>
            <span>rms Δf</span>
            <span className="text-right text-slate-200">{formatFrequency(c.rms_delta_thz, unit, 3)}</span>
          </div>
          <table className="w-full">
            <tbody>
              {labels.map(([l, d]) => (
                <tr key={l}>
                  <td className="text-slate-400">{formatKLabel(l)}</td>
                  <td className={cx("text-right tabular-nums", d > c.tolerance_thz ? "text-rose-300" : "text-slate-200")}>{formatFrequency(d, unit, 3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </span>
  );
}

export function PhononInfoStrip() {
  const phonon = useCrystalStore((s) => s.phonon);
  const convergence = useCrystalStore((s) => s.convergence);
  const checking = useCrystalStore((s) => s.streams.some((x) => x.command === "CHECK_PHONON_CONVERGENCE"));
  const busy = useCrystalStore((s) => s.streams.length > 0);
  const progress = useCrystalStore((s) => s.progress.CHECK_PHONON_CONVERGENCE ?? null);
  const connected = useCrystalStore((s) => s.connection === "open");
  const unit = useSettings((s) => s.units.frequency);
  const colorBranches = useSettings((s) => !!s.phonon.colorBranches);
  if (!phonon) return null;
  const cell = phononCellText(phonon);
  return (
    <div className="relative flex h-6 min-w-0 shrink-0 items-center gap-2 overflow-x-clip font-mono text-[10px] whitespace-nowrap text-slate-400" data-phonon-info>
      <span className="min-w-0 truncate text-slate-300" title={[cell, phonon.band_path_source ? `k-path: ${phonon.band_path_source}` : ""].filter(Boolean).join(" · ")}>
        {cell || "phonons"}
      </span>
      {phonon.band_order && (
        <button
          type="button"
          onClick={() => useSettings.getState().setPhonon({ colorBranches: !colorBranches })}
          title="Branches are connected through crossings (band connection); colour each one"
          className={cx("flex shrink-0 items-center gap-0.5 rounded px-1 hover:text-slate-200", colorBranches ? "text-cyan-200" : "text-slate-500")}
          aria-pressed={colorBranches}
        >
          <Palette size={10} />
          <span className="max-[1499px]:hidden">branches</span>
        </button>
      )}
      <span className="ml-auto flex min-w-0 shrink items-center gap-1.5">
        {checking ? (
          <span className="flex items-center gap-1 text-cyan-200">
            <Loader2 size={10} className="animate-spin" />
            {progress?.total ? `${progress.done}/${progress.total}` : "larger supercell…"}
          </span>
        ) : convergence ? (
          <ConvergenceReadout c={convergence} unit={unit} />
        ) : (
          <span className="min-w-0 truncate text-slate-500" title="Supercell convergence not checked">
            not checked
          </span>
        )}
        <Button
          className="shrink-0 px-1.5 py-0 text-[10px]"
          disabled={!connected || busy}
          onClick={() => commands.checkConvergence()}
          title="Check convergence: re-run in the next larger supercell (+1 per axis) and compare frequencies along the path; the larger analysis replaces this one"
        >
          <Microscope size={10} /> Check
        </Button>
      </span>
    </div>
  );
}
