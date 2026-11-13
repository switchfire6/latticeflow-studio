/**
 * Powder pattern chart (§5.6): sticks (I, strongest = 100), the broadened
 * profile and an experimental overlay (amber, shifted / scaled), with hkl,
 * d-spacing and 2θ of the nearest peak on hover.
 */
import { useMemo, useRef, useState } from "react";
import type { DiffractionPeak } from "../../types/protocol";
import { niceTicks } from "../../utils/bandChart";
import { formatHkl, nearestPeak, type Profile } from "../../utils/diffraction";
import { SERIES_COLORS } from "../../utils/chartData";
import { CHART_COLORS, CHART_FONT, ink, scale, useCanvasChart } from "./useCanvasChart";

const M = { left: 38, right: 10, top: 10, bottom: 28 };
export const EXPERIMENT_COLOR: string = SERIES_COLORS[1];

export interface DiffractionChartProps {
  peaks: readonly DiffractionPeak[];
  profile: Profile | null;
  experimental: Profile | null;
  range: [number, number];
  showSticks: boolean;
  stale?: boolean;
}

export function DiffractionChart(p: DiffractionChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const geom = useRef<{ x: ReturnType<typeof scale>; w: number } | null>(null);
  const [hover, setHover] = useState<DiffractionPeak | null>(null);
  const yMax = useMemo(() => {
    let m = 100;
    for (const v of p.profile?.y ?? []) if (v > m) m = v;
    const [lo, hi] = p.range;
    const e = p.experimental;
    if (e) for (let i = 0; i < e.x.length; i++) if (e.x[i]! >= lo && e.x[i]! <= hi && e.y[i]! > m) m = e.y[i]!;
    return m * 1.08;
  }, [p.profile, p.experimental, p.range]);

  useCanvasChart(
    canvasRef,
    (ctx, size) => {
      const [lo, hi] = p.range;
      const x = scale(lo, hi, M.left, size.width - M.right);
      const y = scale(0, yMax, size.height - M.bottom, M.top);
      geom.current = { x, w: size.width };
      const xL = M.left;
      const xR = size.width - M.right;
      const yB = size.height - M.bottom;
      ctx.font = CHART_FONT;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (const t of niceTicks(0, yMax, 4)) {
        ctx.strokeStyle = CHART_COLORS.grid;
        ctx.beginPath();
        ctx.moveTo(xL, Math.round(y(t)) + 0.5);
        ctx.lineTo(xR, Math.round(y(t)) + 0.5);
        ctx.stroke();
        ctx.fillStyle = CHART_COLORS.text;
        ctx.fillText(String(+t.toFixed(0)), xL - 5, y(t));
      }
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (const t of niceTicks(lo, hi, Math.max(4, Math.floor((xR - xL) / 60)))) ctx.fillText(String(+t.toFixed(1)), x(t), yB + 4);
      ctx.strokeStyle = CHART_COLORS.axis;
      ctx.beginPath();
      ctx.moveTo(xL, yB + 0.5);
      ctx.lineTo(xR, yB + 0.5);
      ctx.stroke();
      ctx.fillStyle = CHART_COLORS.text;
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      ctx.fillText("2θ (°)", xR, size.height - 1);
      ctx.save();
      ctx.translate(10, (M.top + yB) / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("I (rel.)", 0, 0);
      ctx.restore();

      ctx.save();
      ctx.beginPath();
      ctx.rect(xL, M.top - 2, xR - xL, yB - M.top + 4);
      ctx.clip();
      const line = (prof: Profile, color: string, width: number) => {
        ctx.beginPath();
        prof.x.forEach((v, i) => (i === 0 ? ctx.moveTo(x(v), y(prof.y[i]!)) : ctx.lineTo(x(v), y(prof.y[i]!))));
        ctx.strokeStyle = color;
        ctx.lineWidth = width;
        ctx.lineJoin = "round";
        ctx.stroke();
      };
      if (p.experimental) line(p.experimental, ink(EXPERIMENT_COLOR), 1.3);
      if (p.showSticks || !p.profile) {
        ctx.strokeStyle = CHART_COLORS.sky;
        ctx.globalAlpha = p.profile ? 0.55 : 1;
        ctx.lineWidth = p.profile ? 1 : 1.6;
        ctx.beginPath();
        for (const pk of p.peaks) {
          const px = Math.round(x(pk.two_theta)) + 0.5;
          ctx.moveTo(px, yB);
          ctx.lineTo(px, y(pk.intensity));
        }
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
      if (p.profile) line(p.profile, p.stale ? CHART_COLORS.text : CHART_COLORS.sky, 1.6);
      if (hover) {
        const px = Math.round(x(hover.two_theta)) + 0.5;
        ctx.strokeStyle = CHART_COLORS.ink;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(px, yB);
        ctx.lineTo(px, y(hover.intensity));
        ctx.stroke();
      }
      ctx.restore();
    },
    () => hover,
  );

  const g = geom.current;
  const tipLeft = g && hover ? g.x(hover.two_theta) : 0;
  return (
    <div className="relative h-full w-full">
      <canvas
        ref={canvasRef}
        className="absolute inset-0 cursor-crosshair"
        onMouseMove={(e) => {
          const gg = geom.current;
          if (!gg) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const xv = gg.x.invert(e.clientX - rect.left);
          const tol = Math.abs(gg.x.invert(M.left + 8) - gg.x.invert(M.left));
          const pk = nearestPeak(p.peaks, xv, tol);
          if (pk !== hover) setHover(pk);
        }}
        onMouseLeave={() => setHover(null)}
      />
      {p.experimental && (
        <div className="pointer-events-none absolute top-1 right-2 flex gap-2.5 text-2xs text-fg-2">
          <span className="flex items-center gap-1">
            <span className="h-[2px] w-3 rounded-ctl" style={{ background: CHART_COLORS.sky }} /> simulated
          </span>
          <span className="flex items-center gap-1">
            <span className="h-[2px] w-3 rounded-ctl" style={{ background: EXPERIMENT_COLOR }} /> experiment
          </span>
        </div>
      )}
      {hover && g && (
        <div
          className="pointer-events-none absolute z-10 rounded-ctl border border-line bg-surface px-2 py-1 font-mono text-2xs text-fg shadow-lg"
          style={{ top: M.top + 14, left: tipLeft > g.w / 2 ? undefined : tipLeft + 10, right: tipLeft > g.w / 2 ? g.w - tipLeft + 10 : undefined }}
          data-xrd-tooltip
        >
          <div>
            2θ {hover.two_theta.toFixed(3)}° · d {hover.d_spacing.toFixed(4)} Å
          </div>
          <div className="text-fg-3">I {hover.intensity.toFixed(1)}</div>
          <div className="text-fg">{formatHkl(hover.hkls)}</div>
        </div>
      )}
    </div>
  );
}
