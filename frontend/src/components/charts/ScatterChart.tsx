/**
 * Canvas scatter plot on the shared plumbing (results table): one point per
 * structure, hover shows its label, click picks it. The current structure is
 * ringed in the selection accent.
 */
import { useRef, useState } from "react";
import { SERIES_COLORS } from "../../utils/chartData";
import { drawAxes, drawTooltip, padded, type Axes } from "./chartAxes";
import { CHART_COLORS, ink, renderState, useCanvasChart } from "./useCanvasChart";

export interface ScatterPoint {
  id: number;
  x: number;
  y: number;
  /** Tooltip lines (first line strong). */
  lines: string[];
}

const HIT_PX = 10;

export function ScatterChart({
  points,
  xLabel,
  yLabel,
  currentId,
  onPick,
}: {
  points: ScatterPoint[];
  xLabel: string;
  yLabel: string;
  currentId: number | null;
  onPick(id: number): void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const axes = useRef<Axes | null>(null);
  const [hover, setHover] = useState<number | null>(null);

  useCanvasChart(
    canvasRef,
    (ctx, size) => {
      if (!points.length) return;
      const a = drawAxes(ctx, size, padded(points.map((p) => p.x)), padded(points.map((p) => p.y)), xLabel, yLabel, { zeroLine: true });
      axes.current = a;
      const color = ink(SERIES_COLORS[0]);
      for (const p of points) {
        ctx.beginPath();
        ctx.arc(a.x(p.x), a.y(p.y), 4.5, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = CHART_COLORS.background;
        ctx.stroke();
      }
      const cur = points.find((p) => p.id === currentId);
      if (cur) {
        ctx.beginPath();
        ctx.arc(a.x(cur.x), a.y(cur.y), 8, 0, Math.PI * 2);
        ctx.lineWidth = 1.6;
        ctx.strokeStyle = CHART_COLORS.cyan;
        ctx.stroke();
      }
      if (renderState.exporting) return;
      const h = points.find((p) => p.id === hover);
      if (h) {
        ctx.beginPath();
        ctx.arc(a.x(h.x), a.y(h.y), 6, 0, Math.PI * 2);
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = CHART_COLORS.ink;
        ctx.stroke();
        drawTooltip(ctx, size, a.x(h.x), a.y(h.y), h.lines);
      }
    },
    () => `${hover}:${currentId}`,
  );

  const nearest = (e: React.MouseEvent<HTMLCanvasElement>): number | null => {
    const a = axes.current;
    if (!a) return null;
    const r = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - r.left;
    const my = e.clientY - r.top;
    let best: number | null = null;
    let bd = HIT_PX * HIT_PX;
    for (const p of points) {
      const d = (a.x(p.x) - mx) ** 2 + (a.y(p.y) - my) ** 2;
      if (d <= bd) (bd = d), (best = p.id);
    }
    return best;
  };

  if (!points.length) return <div className="grid h-full place-items-center text-xs text-fg-4">No structure has both values.</div>;
  return (
    <div className="relative h-full w-full">
      <canvas
        ref={canvasRef}
        className={hover !== null ? "absolute inset-0 cursor-pointer" : "absolute inset-0 cursor-crosshair"}
        onMouseMove={(e) => {
          const id = nearest(e);
          if (id !== hover) setHover(id);
        }}
        onMouseLeave={() => setHover(null)}
        onClick={(e) => {
          const id = nearest(e);
          if (id !== null) onPick(id);
        }}
      />
    </div>
  );
}
