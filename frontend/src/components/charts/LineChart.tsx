/**
 * Small canvas line chart on the shared plumbing (useCanvasChart): one y-axis,
 * recessive grid, 2 px lines, optional area fill, vertical markers (V0, V_eq),
 * an optional shaded x-region (imaginary frequencies), an HTML legend for ≥ 2
 * series and a crosshair tooltip with every series' value at the cursor.
 */
import { useRef, useState } from "react";
import { interpAt } from "../../utils/chartData";
import { niceTicks } from "../../utils/bandChart";
import { CHART_COLORS, CHART_FONT, renderState, scale, useCanvasChart } from "./useCanvasChart";

export interface LineSeries {
  label: string;
  xs: readonly number[];
  ys: readonly number[];
  color: string;
  width?: number;
  fill?: boolean;
  /** Draw sample points (measured data, e.g. EOS points). */
  points?: boolean;
}

export interface VMarker {
  x: number;
  label: string;
  color: string;
  dashed?: boolean;
  /** Where the label sits (default top). */
  labelAt?: "top" | "bottom";
}

export interface LineChartProps {
  series: LineSeries[];
  xLabel: string;
  yLabel: string;
  markers?: VMarker[];
  /** Shade x < this value (e.g. imaginary frequencies). */
  shadeBelowX?: number;
  includeZeroY?: boolean;
  /** Draw a y = 0 reference line. */
  zeroLine?: boolean;
  formatX?: (v: number) => string;
  formatY?: (v: number) => string;
  xDomain?: [number, number];
  yDomain?: [number, number];
  empty?: string;
}

const M = { left: 44, right: 10, top: 10, bottom: 30 };

function tickText(t: number): string {
  const a = Math.abs(t);
  if (a === 0) return "0";
  if (a >= 1000) return t.toFixed(0);
  if (a >= 10) return String(+t.toFixed(1));
  return String(+t.toPrecision(3));
}

function domainOf(series: LineSeries[], axis: "xs" | "ys", include?: number): [number, number] {
  let lo = Infinity;
  let hi = -Infinity;
  for (const s of series) for (const v of s[axis]) if (Number.isFinite(v)) (lo = Math.min(lo, v)), (hi = Math.max(hi, v));
  if (include !== undefined) (lo = Math.min(lo, include)), (hi = Math.max(hi, include));
  if (!Number.isFinite(lo)) return [0, 1];
  if (hi - lo < 1e-12) return [lo - 1, hi + 1];
  const pad = axis === "ys" ? 0.06 * (hi - lo) : 0;
  return [lo - pad, hi + pad];
}

export function LineChart(p: LineChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [hoverX, setHoverX] = useState<number | null>(null);
  const geom = useRef<{ x: ReturnType<typeof scale>; y: ReturnType<typeof scale>; w: number; h: number } | null>(null);
  const hasData = p.series.some((s) => s.xs.length > 1);
  const fx = p.formatX ?? tickText;
  const fy = p.formatY ?? tickText;

  useCanvasChart(
    canvasRef,
    (ctx, size) => {
      if (!hasData) return;
      const [x0d, x1d] = p.xDomain ?? domainOf(p.series, "xs");
      const [y0d, y1d] = p.yDomain ?? domainOf(p.series, "ys", p.includeZeroY ? 0 : undefined);
      const x = scale(x0d, x1d, M.left, size.width - M.right);
      const y = scale(y0d, y1d, size.height - M.bottom, M.top);
      geom.current = { x, y, w: size.width, h: size.height };
      const xL = M.left;
      const xR = size.width - M.right;
      const yT = M.top;
      const yB = size.height - M.bottom;

      if (p.shadeBelowX !== undefined && p.shadeBelowX > x0d) {
        ctx.fillStyle = CHART_COLORS.crimsonFill;
        ctx.fillRect(xL, yT, Math.min(x(p.shadeBelowX), xR) - xL, yB - yT);
      }
      ctx.font = CHART_FONT;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (const t of niceTicks(y0d, y1d, 4)) {
        const py = Math.round(y(t)) + 0.5;
        ctx.strokeStyle = CHART_COLORS.grid;
        ctx.beginPath();
        ctx.moveTo(xL, py);
        ctx.lineTo(xR, py);
        ctx.stroke();
        ctx.fillStyle = CHART_COLORS.text;
        ctx.fillText(tickText(t), xL - 5, y(t));
      }
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const xt = niceTicks(x0d, x1d, Math.max(3, Math.floor((xR - xL) / 70)));
      for (const t of xt) {
        ctx.fillStyle = CHART_COLORS.text;
        ctx.fillText(tickText(t), x(t), yB + 4);
      }
      ctx.strokeStyle = CHART_COLORS.axis;
      ctx.beginPath();
      ctx.moveTo(xL, yB + 0.5);
      ctx.lineTo(xR, yB + 0.5);
      ctx.stroke();
      if (p.zeroLine && y0d < 0 && y1d > 0) {
        ctx.strokeStyle = "rgba(226, 232, 240, 0.35)";
        ctx.beginPath();
        ctx.moveTo(xL, Math.round(y(0)) + 0.5);
        ctx.lineTo(xR, Math.round(y(0)) + 0.5);
        ctx.stroke();
      }
      // axis labels
      ctx.fillStyle = CHART_COLORS.text;
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      ctx.fillText(p.xLabel, xR, size.height - 1);
      ctx.save();
      ctx.translate(10, (yT + yB) / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(p.yLabel, 0, 0);
      ctx.restore();

      ctx.save();
      ctx.beginPath();
      ctx.rect(xL, yT - 2, xR - xL, yB - yT + 4);
      ctx.clip();
      for (const s of p.series) {
        const n = Math.min(s.xs.length, s.ys.length);
        if (n < 1) continue;
        const path = new Path2D();
        for (let i = 0; i < n; i++) (i === 0 ? path.moveTo : path.lineTo).call(path, x(s.xs[i]!), y(s.ys[i]!));
        if (s.fill) {
          const area = new Path2D(path);
          const base = y(Math.max(y0d, Math.min(y1d, 0)));
          area.lineTo(x(s.xs[n - 1]!), base);
          area.lineTo(x(s.xs[0]!), base);
          area.closePath();
          ctx.globalAlpha = 0.16;
          ctx.fillStyle = s.color;
          ctx.fill(area);
          ctx.globalAlpha = 1;
        }
        ctx.lineWidth = s.width ?? 2;
        ctx.lineJoin = "round";
        ctx.strokeStyle = s.color;
        ctx.stroke(path);
        if (s.points) {
          for (let i = 0; i < n; i++) {
            ctx.beginPath();
            ctx.arc(x(s.xs[i]!), y(s.ys[i]!), 3.5, 0, Math.PI * 2);
            ctx.fillStyle = "#0b0f19";
            ctx.fill();
            ctx.lineWidth = 1.6;
            ctx.stroke();
          }
        }
      }
      ctx.restore();

      ctx.textBaseline = "top";
      for (const m of p.markers ?? []) {
        if (!Number.isFinite(m.x) || m.x < x0d || m.x > x1d) continue;
        const px = Math.round(x(m.x)) + 0.5;
        ctx.strokeStyle = m.color;
        ctx.setLineDash(m.dashed ? [4, 3] : []);
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(px, yT);
        ctx.lineTo(px, yB);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "rgba(226, 232, 240, 0.9)";
        ctx.textAlign = px > (xL + xR) / 2 ? "right" : "left";
        ctx.textBaseline = m.labelAt === "bottom" ? "bottom" : "top";
        ctx.fillText(m.label, px + (px > (xL + xR) / 2 ? -4 : 4), m.labelAt === "bottom" ? yB - 2 : yT + 1);
      }

      if (renderState.exporting) {
        // PNG export: the HTML legend is not part of the canvas, draw it in.
        if (p.series.length >= 2) {
          ctx.font = CHART_FONT;
          ctx.textAlign = "left";
          ctx.textBaseline = "middle";
          let lx = xR;
          for (const s of [...p.series].reverse()) {
            const w = ctx.measureText(s.label).width;
            lx -= w + 22;
            ctx.fillStyle = s.color;
            ctx.fillRect(lx, yT + 6, 12, 2);
            ctx.fillStyle = "rgba(226, 232, 240, 0.9)";
            ctx.fillText(s.label, lx + 16, yT + 7);
          }
        }
        return;
      }

      if (hoverX !== null) {
        const px = Math.round(x(hoverX)) + 0.5;
        ctx.strokeStyle = "rgba(226, 232, 240, 0.4)";
        ctx.beginPath();
        ctx.moveTo(px, yT);
        ctx.lineTo(px, yB);
        ctx.stroke();
        for (const s of p.series) {
          const v = interpAt(s.xs, s.ys, hoverX);
          if (v === null) continue;
          ctx.beginPath();
          ctx.arc(px, y(v), 3.5, 0, Math.PI * 2);
          ctx.fillStyle = s.color;
          ctx.fill();
          ctx.lineWidth = 2;
          ctx.strokeStyle = "#0b0f19";
          ctx.stroke();
        }
      }
    },
    () => hoverX,
  );

  if (!hasData) return <div className="grid h-full place-items-center text-[11px] text-slate-500">{p.empty ?? "No data"}</div>;

  const tooltip =
    hoverX !== null
      ? p.series.map((s) => ({ label: s.label, color: s.color, v: interpAt(s.xs, s.ys, hoverX) })).filter((r) => r.v !== null)
      : [];
  const g = geom.current;
  const tipLeft = g && hoverX !== null ? g.x(hoverX) : 0;

  return (
    <div className="relative h-full w-full">
      <canvas
        ref={canvasRef}
        className="absolute inset-0 cursor-crosshair"
        onMouseMove={(e) => {
          const gg = geom.current;
          if (!gg) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const px = e.clientX - rect.left;
          if (px < M.left || px > gg.w - M.right) return setHoverX(null);
          setHoverX(gg.x.invert(px));
        }}
        onMouseLeave={() => setHoverX(null)}
      />
      {p.series.length >= 2 && (
        <div className="pointer-events-none absolute top-1 right-2 flex flex-wrap justify-end gap-x-2.5 font-mono text-[10px] text-slate-300">
          {p.series.map((s) => (
            <span key={s.label} className="flex items-center gap-1">
              <span className="h-[2px] w-3 rounded" style={{ background: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
      )}
      {hoverX !== null && tooltip.length > 0 && g && (
        <div
          className="pointer-events-none absolute z-10 rounded-md border border-white/10 bg-slate-950/90 px-2 py-1 font-mono text-[10px] text-slate-200 shadow-lg"
          style={{ top: M.top + 14, left: tipLeft > g.w / 2 ? undefined : tipLeft + 10, right: tipLeft > g.w / 2 ? g.w - tipLeft + 10 : undefined }}
        >
          <div className="text-slate-400">{p.xLabel.replace(/\s*\(.*\)$/, "")} = {fx(hoverX)}</div>
          {tooltip.map((r) => (
            <div key={r.label} className="flex items-center gap-1.5">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: r.color }} />
              <span className="text-slate-400">{r.label}</span>
              <span className="ml-auto pl-2 tabular-nums">{fy(r.v!)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
