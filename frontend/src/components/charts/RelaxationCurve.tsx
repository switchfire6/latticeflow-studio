/**
 * FIRE relaxation convergence: energy/atom (left axis, cyan) and max |F|
 * (right axis, log scale, amber) vs step for the current relaxation or follow
 * stream, with the fmax target line. Reads the transient relaxation history
 * and redraws on `session.relaxVersion` (no React render per frame).
 * The force axis is clamped at 1e-12 eV/Å and its decade labels are thinned
 * to what fits the chart height (Phase B fix for tiny residual forces).
 */
import { useRef } from "react";
import { useCrystalStore } from "../../store/useCrystalStore";
import { SERIES_COLORS } from "../../utils/chartData";
import { decadeLabel, formatTick, logAxisTicks, niceTicks } from "../../utils/bandChart";
import { CHART_COLORS, CHART_FONT, ink, scale, useCanvasChart } from "./useCanvasChart";

const M = { left: 56, right: 46, top: 12, bottom: 20 };
const ENERGY_COLOR: string = SERIES_COLORS[0];
const FORCE_COLOR: string = SERIES_COLORS[1];

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
      const fFloor = Math.max(1e-5, fmax * 0.05);
      let eLo = Infinity;
      let eHi = -Infinity;
      let fLo = fmax / 3;
      let fHi = fmax * 10;
      for (const p of hist) {
        eLo = Math.min(eLo, p.energyPerAtom);
        eHi = Math.max(eHi, p.energyPerAtom);
        if (p.maxForce > 0) {
          fLo = Math.min(fLo, Math.max(p.maxForce, fFloor));
          fHi = Math.max(fHi, p.maxForce);
        }
      }
      if (!Number.isFinite(eLo)) {
        eLo = -1;
        eHi = 0;
      }
      const ePad = Math.max(1e-4, (eHi - eLo) * 0.08);
      const yE = scale(eLo - ePad, eHi + ePad, yBot, M.top);
      const axis = logAxisTicks(fLo, fHi, Math.max(2, Math.floor((yBot - M.top) / 22)), fFloor);
      const decades = axis.ticks.map((e) => 10 ** e);
      const lLo = axis.lo;
      const lHi = axis.hi;
      const yF = (v: number) => scale(lLo, lHi, yBot, M.top)(Math.log10(Math.max(v, 10 ** lLo)));

      ctx.font = CHART_FONT;
      // force decades (right) + grid
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      for (const [i, d] of decades.entries()) {
        ctx.strokeStyle = CHART_COLORS.grid;
        ctx.beginPath();
        ctx.moveTo(M.left, Math.round(yF(d)) + 0.5);
        ctx.lineTo(size.width - M.right, Math.round(yF(d)) + 0.5);
        ctx.stroke();
        ctx.fillStyle = ink(FORCE_COLOR);
        ctx.fillText(decadeLabel(axis.ticks[i]!), size.width - M.right + 4, yF(d));
      }
      // energy ticks (left) with step-aware precision
      ctx.textAlign = "right";
      const eTicks = niceTicks(eLo - ePad, eHi + ePad, 4);
      const eStep = eTicks.length >= 2 ? Math.abs(eTicks[1]! - eTicks[0]!) : Math.max(1e-4, eHi - eLo);
      for (const t of eTicks) {
        ctx.fillStyle = ink(ENERGY_COLOR);
        ctx.fillText(formatTick(t, eStep), M.left - 4, yE(t));
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
      ctx.fillStyle = ink(ENERGY_COLOR);
      ctx.fillText("E (eV/atom)", 0, 0);
      ctx.restore();
      ctx.save();
      ctx.translate(size.width - 8, (M.top + yBot) / 2);
      ctx.rotate(Math.PI / 2);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = ink(FORCE_COLOR);
      ctx.fillText("max |F| (eV/Å)", 0, 0);
      ctx.restore();

      // fmax target
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = CHART_COLORS.inkSoft;
      ctx.beginPath();
      ctx.moveTo(M.left, yF(fmax));
      ctx.lineTo(size.width - M.right, yF(fmax));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = CHART_COLORS.text;
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText(`fmax ${fmax}`, M.left + 4, yF(fmax) - 2);

      if (hist.length === 0) {
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = CHART_COLORS.text;
        ctx.fillText("No relaxation yet: run a relaxation or follow an instability.", (M.left + size.width - M.right) / 2, (M.top + yBot) / 2);
        return;
      }
      const curve = (val: (i: number) => number, color: string) => {
        ctx.beginPath();
        hist.forEach((p, i) => (i === 0 ? ctx.moveTo(x(p.step), val(i)) : ctx.lineTo(x(p.step), val(i))));
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.7;
        ctx.stroke();
      };
      curve((i) => yF(hist[i]!.maxForce), ink(FORCE_COLOR));
      curve((i) => yE(hist[i]!.energyPerAtom), ink(ENERGY_COLOR));
      const last = hist[hist.length - 1]!;
      ctx.fillStyle = ink(FORCE_COLOR);
      ctx.beginPath();
      ctx.arc(x(last.step), yF(last.maxForce), 3, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = ink(ENERGY_COLOR);
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
