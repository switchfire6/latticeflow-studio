/** Shared x/y axes for the canvas scatter-type charts (same look as LineChart: recessive grid, mono ticks). */
import { formatTick, niceTicks } from "../../utils/bandChart";
import { CHART_COLORS, CHART_FONT, scale, type ChartSize } from "./useCanvasChart";

export const AXIS_MARGIN = { left: 50, right: 12, top: 12, bottom: 30 };

export interface Axes {
  x: ReturnType<typeof scale>;
  y: ReturnType<typeof scale>;
  xL: number;
  xR: number;
  yT: number;
  yB: number;
}

/** Pad a [lo, hi] extent (widened when degenerate). */
export function padded(values: readonly number[], pad = 0.08, include?: number): [number, number] {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) if (Number.isFinite(v)) (lo = Math.min(lo, v)), (hi = Math.max(hi, v));
  if (include !== undefined) (lo = Math.min(lo, include)), (hi = Math.max(hi, include));
  if (!Number.isFinite(lo)) return [0, 1];
  if (hi - lo < 1e-12) {
    const w = Math.abs(lo) * 0.1 || 1;
    return [lo - w, hi + w];
  }
  const p = (hi - lo) * pad;
  return [lo - p, hi + p];
}

export function drawAxes(
  ctx: CanvasRenderingContext2D,
  size: ChartSize,
  xDomain: [number, number],
  yDomain: [number, number],
  xLabel: string,
  yLabel: string,
  opts: { zeroLine?: boolean } = {},
): Axes {
  const M = AXIS_MARGIN;
  const xL = M.left;
  const xR = size.width - M.right;
  const yT = M.top;
  const yB = size.height - M.bottom;
  const x = scale(xDomain[0], xDomain[1], xL, xR);
  const y = scale(yDomain[0], yDomain[1], yB, yT);
  ctx.font = CHART_FONT;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  const yt = niceTicks(yDomain[0], yDomain[1], Math.max(2, Math.min(5, Math.floor((yB - yT) / 28))));
  const ys = yt.length >= 2 ? Math.abs(yt[1]! - yt[0]!) : undefined;
  for (const t of yt) {
    const py = Math.round(y(t)) + 0.5;
    ctx.strokeStyle = CHART_COLORS.grid;
    ctx.beginPath();
    ctx.moveTo(xL, py);
    ctx.lineTo(xR, py);
    ctx.stroke();
    ctx.fillStyle = CHART_COLORS.text;
    ctx.fillText(formatTick(t, ys), xL - 5, y(t));
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  const xt = niceTicks(xDomain[0], xDomain[1], Math.max(3, Math.floor((xR - xL) / 70)));
  const xs = xt.length >= 2 ? Math.abs(xt[1]! - xt[0]!) : undefined;
  for (const t of xt) {
    ctx.fillStyle = CHART_COLORS.text;
    ctx.fillText(formatTick(t, xs), x(t), yB + 4);
  }
  ctx.strokeStyle = CHART_COLORS.axis;
  ctx.beginPath();
  ctx.moveTo(xL, yB + 0.5);
  ctx.lineTo(xR, yB + 0.5);
  ctx.stroke();
  if (opts.zeroLine && yDomain[0] < 0 && yDomain[1] > 0) {
    ctx.strokeStyle = CHART_COLORS.inkSoft;
    ctx.beginPath();
    ctx.moveTo(xL, Math.round(y(0)) + 0.5);
    ctx.lineTo(xR, Math.round(y(0)) + 0.5);
    ctx.stroke();
  }
  ctx.fillStyle = CHART_COLORS.text;
  ctx.textAlign = "right";
  ctx.textBaseline = "bottom";
  ctx.fillText(xLabel, xR, size.height - 1);
  ctx.save();
  ctx.translate(10, (yT + yB) / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(yLabel, 0, 0);
  ctx.restore();
  return { x, y, xL, xR, yT, yB };
}

/** Small tooltip box drawn on the canvas (lines of text, the first one strong). */
export function drawTooltip(ctx: CanvasRenderingContext2D, size: ChartSize, px: number, py: number, lines: string[]): void {
  ctx.font = CHART_FONT;
  const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 12;
  const h = lines.length * 14 + 6;
  const tx = px + 12 + w > size.width - 4 ? px - 12 - w : px + 12;
  const ty = Math.min(Math.max(4, py - h / 2), size.height - h - 4);
  ctx.fillStyle = CHART_COLORS.tooltipBg;
  ctx.strokeStyle = CHART_COLORS.axis;
  ctx.lineWidth = 1;
  ctx.fillRect(tx, ty, w, h);
  ctx.strokeRect(tx + 0.5, ty + 0.5, w - 1, h - 1);
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  lines.forEach((l, i) => {
    ctx.fillStyle = i === 0 ? CHART_COLORS.ink : CHART_COLORS.text;
    ctx.fillText(l, tx + 6, ty + 4 + i * 14);
  });
}
