/**
 * FIRE relaxation convergence: energy/atom (left axis, cyan) and max |F|
 * (right axis, log scale, amber) vs step for the current relaxation or follow
 * stream, with the fmax target line. Reads the transient relaxation history
 * and redraws on `session.relaxVersion` (no React render per frame).
 */
import { useRef } from "react";
import { useCrystalStore } from "../../store/useCrystalStore";
import { niceTicks } from "../../utils/bandChart";
import { CHART_COLORS, CHART_FONT, scale, useCanvasChart } from "./useCanvasChart";

const M = { left: 52, right: 46, top: 12, bottom: 20 };

/** Log-axis decades covering [lo, hi]. */
export function logDecades(lo: number, hi: number): number[] {
  const out: number[] = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) out.push(10 ** e);
  return out;
}

export function RelaxationCurve() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useCanvasChart(
    canvasRef,
    (ctx, size) => {
      const session = useCrystalStore.getState().session;
      const hist = session.relaxHistory;
      const fmax = session.relaxFmax;
      const xMax = Math.max(10, hist.length ? hist[hist.length - 1]!.step : 0);
      const x = scale(0, xMax, M.left, size.width - M.right);
      const yBot = size.height - M.bottom;
      let eLo = Infinity;
      let eHi = -Infinity;
      let fLo = fmax / 3;
      let fHi = fmax * 10;
      for (const p of hist) {
        eLo = Math.min(eLo, p.energyPerAtom);
        eHi = Math.max(eHi, p.energyPerAtom);
        if (p.maxForce > 0) {
          fLo = Math.min(fLo, p.maxForce);
          fHi = Math.max(fHi, p.maxForce);
        }
      }
      if (!Number.isFinite(eLo)) {
        eLo = -1;
        eHi = 0;
      }
      const ePad = Math.max(1e-4, (eHi - eLo) * 0.08);
      const yE = scale(eLo - ePad, eHi + ePad, yBot, M.top);
      const decades = logDecades(fLo, fHi);
      const lLo = Math.log10(decades[0]!);
      const lHi = Math.log10(decades[decades.length - 1]!);
      const yF = (v: number) => scale(lLo, lHi, yBot, M.top)(Math.log10(Math.max(v, 1e-12)));

      ctx.font = CHART_FONT;
      // force decades (right) + grid
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      for (const d of decades) {
        ctx.strokeStyle = CHART_COLORS.grid;
        ctx.beginPath();
        ctx.moveTo(M.left, Math.round(yF(d)) + 0.5);
        ctx.lineTo(size.width - M.right, Math.round(yF(d)) + 0.5);
        ctx.stroke();
        ctx.fillStyle = "rgba(245, 165, 36, 0.85)";
        ctx.fillText(d >= 1 ? String(d) : d.toExponential(0), size.width - M.right + 4, yF(d));
      }
      // energy ticks (left)
      ctx.textAlign = "right";
      for (const t of niceTicks(eLo - ePad, eHi + ePad, 4)) {
        ctx.fillStyle = "rgba(103, 232, 249, 0.85)";
        ctx.fillText(t.toFixed(3), M.left - 4, yE(t));
      }
      // step ticks
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = CHART_COLORS.text;
      for (const t of niceTicks(0, xMax, 6)) ctx.fillText(String(t), x(t), yBot + 5);
      // axis titles
      ctx.save();
      ctx.translate(10, (M.top + yBot) / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "rgba(103, 232, 249, 0.85)";
      ctx.fillText("E (eV/atom)", 0, 0);
      ctx.restore();
      ctx.save();
      ctx.translate(size.width - 8, (M.top + yBot) / 2);
      ctx.rotate(Math.PI / 2);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "rgba(245, 165, 36, 0.85)";
      ctx.fillText("max |F| (eV/Å)", 0, 0);
      ctx.restore();

      // fmax target
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = "rgba(52, 211, 153, 0.8)";
      ctx.beginPath();
      ctx.moveTo(M.left, yF(fmax));
      ctx.lineTo(size.width - M.right, yF(fmax));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(52, 211, 153, 0.9)";
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText(`fmax ${fmax}`, M.left + 4, yF(fmax) - 2);

      if (hist.length === 0) {
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = CHART_COLORS.text;
        ctx.fillText("No relaxation yet: run FIRE Relax or Follow Instability.", (M.left + size.width - M.right) / 2, (M.top + yBot) / 2);
        return;
      }
      const curve = (val: (i: number) => number, color: string) => {
        ctx.beginPath();
        hist.forEach((p, i) => (i === 0 ? ctx.moveTo(x(p.step), val(i)) : ctx.lineTo(x(p.step), val(i))));
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.7;
        ctx.stroke();
      };
      curve((i) => yF(hist[i]!.maxForce), CHART_COLORS.amber);
      curve((i) => yE(hist[i]!.energyPerAtom), CHART_COLORS.cyan);
      const last = hist[hist.length - 1]!;
      ctx.fillStyle = CHART_COLORS.amber;
      ctx.beginPath();
      ctx.arc(x(last.step), yF(last.maxForce), 3, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = CHART_COLORS.cyan;
      ctx.beginPath();
      ctx.arc(x(last.step), yE(last.energyPerAtom), 3, 0, Math.PI * 2);
      ctx.fill();
    },
    () => useCrystalStore.getState().session.relaxVersion,
  );

  return (
    <div className="relative h-full w-full">
      <canvas ref={canvasRef} className="absolute inset-0" />
    </div>
  );
}
