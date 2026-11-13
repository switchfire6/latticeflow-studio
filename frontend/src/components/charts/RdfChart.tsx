/**
 * Live radial distribution function g(r) (§4.5, 151 points over 0.5–8.0 Å).
 * Reads `frame.crystal.rdf_*` from the transient buffers and redraws on
 * `frame.version`, so drag echoes, flow and relaxation frames update it with
 * no React render.
 */
import { useRef } from "react";
import { useCrystalStore } from "../../store/useCrystalStore";
import { niceTicks } from "../../utils/bandChart";
import { CHART_COLORS, CHART_FONT, scale, useCanvasChart } from "./useCanvasChart";

const M = { left: 34, right: 10, top: 10, bottom: 20 };
const R_MIN = 0.5;
const R_MAX = 8.0;

export function RdfChart() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const peakRef = useRef(4);

  useCanvasChart(
    canvasRef,
    (ctx, size) => {
      const c = useCrystalStore.getState().frame.crystal;
      const x = scale(R_MIN, R_MAX, M.left, size.width - M.right);
      const r = c?.rdf_r ?? [];
      const g = c?.rdf_g ?? [];
      let gmax = 1.5;
      for (const v of g) if (v > gmax) gmax = v;
      // smooth the y range so it does not jump every frame
      peakRef.current = Math.max(gmax * 1.08, peakRef.current * 0.97, 1.5);
      const y = scale(0, peakRef.current, size.height - M.bottom, M.top);
      const yBot = size.height - M.bottom;

      ctx.font = CHART_FONT;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (const t of niceTicks(0, peakRef.current, 4)) {
        ctx.strokeStyle = CHART_COLORS.grid;
        ctx.beginPath();
        ctx.moveTo(M.left, Math.round(y(t)) + 0.5);
        ctx.lineTo(size.width - M.right, Math.round(y(t)) + 0.5);
        ctx.stroke();
        ctx.fillStyle = CHART_COLORS.text;
        ctx.fillText(String(+t.toFixed(1)), M.left - 5, y(t));
      }
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (let t = 1; t <= 8; t++) {
        ctx.fillStyle = CHART_COLORS.text;
        ctx.fillText(String(t), x(t), yBot + 5);
      }
      ctx.textAlign = "right";
      ctx.textBaseline = "top";
      ctx.fillText("r (Å)", size.width - M.right, M.top);
      // g = 1 reference
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = CHART_COLORS.inkSoft;
      ctx.beginPath();
      ctx.moveTo(M.left, y(1));
      ctx.lineTo(size.width - M.right, y(1));
      ctx.stroke();
      ctx.setLineDash([]);
      if (r.length < 2 || r.length !== g.length) return;

      const line = new Path2D();
      r.forEach((rv, i) => (i === 0 ? line.moveTo(x(rv), y(g[i]!)) : line.lineTo(x(rv), y(g[i]!))));
      const fill = new Path2D(line);
      fill.lineTo(x(r[r.length - 1]!), yBot);
      fill.lineTo(x(r[0]!), yBot);
      fill.closePath();
      ctx.globalAlpha = 0.12;
      ctx.fillStyle = CHART_COLORS.sky;
      ctx.fill(fill);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = CHART_COLORS.sky;
      ctx.lineWidth = 1.6;
      ctx.stroke(line);
    },
    () => useCrystalStore.getState().frame.version,
  );

  return (
    <div className="relative h-full w-full">
      <canvas ref={canvasRef} className="absolute inset-0" />
    </div>
  );
}
