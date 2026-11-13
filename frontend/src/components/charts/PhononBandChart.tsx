/**
 * Phonon dispersion ω(q) (§6.3): all 3N branches vs kpath_distances on a
 * canvas. The static layer (grid, shaded f < 0 region, zero line, branches;
 * segments with f < −F_TOL in crimson, two batched strokes in total) is
 * rendered once per (analysis, size) into an offscreen canvas; selection and
 * hover are a cheap overlay. Smooth for 96 bands × ~150 q (Li₃PS₄).
 *
 * Clicking picks the nearest (q_index, band) and animates it at once: the
 * cached eigenvector of a labelled point, otherwise REQUEST_EIGENMODE.
 */
import { useMemo, useRef, useState, type MouseEvent } from "react";
import { selectModeAt } from "../../services/phononActions";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { PhononAnalysisComplete } from "../../types/protocol";
import { formatFrequency, frequencyFactor, frequencyUnitLabel, type FrequencyUnit } from "../../utils/units";
import {
  classifyBands,
  frequencyRange,
  isImaginarySegment,
  nearestMode,
  niceTicks,
  pathTicks,
  qLabelAt,
  segmentBreaks,
} from "../../utils/bandChart";
import { CHART_COLORS, CHART_FONT, scale, useCanvasChart, type ChartSize } from "./useCanvasChart";

const M = { left: 42, right: 10, top: 10, bottom: 20 };

interface Geometry {
  x: ReturnType<typeof scale>;
  y: ReturnType<typeof scale>;
}

function geometry(p: PhononAnalysisComplete, size: ChartSize): Geometry {
  const d = p.kpath_distances;
  const summary = classifyBands(p.frequencies_thz);
  const [lo, hi] = frequencyRange(summary);
  return {
    x: scale(d[0] ?? 0, d[d.length - 1] ?? 1, M.left, size.width - M.right),
    y: scale(lo, hi, size.height - M.bottom, M.top),
  };
}

function drawBase(ctx: CanvasRenderingContext2D, p: PhononAnalysisComplete, size: ChartSize, g: Geometry, unit: FrequencyUnit) {
  const k = frequencyFactor(unit);
  const { x, y } = g;
  const d = p.kpath_distances;
  const f = p.frequencies_thz;
  const x0 = M.left;
  const x1 = size.width - M.right;
  const yTop = M.top;
  const yBot = size.height - M.bottom;
  const [lo, hi] = [y.invert(yBot), y.invert(yTop)];

  // imaginary region shading
  if (lo < 0) {
    ctx.fillStyle = CHART_COLORS.crimsonFill;
    ctx.fillRect(x0, y(0), x1 - x0, yBot - y(0));
  }
  // horizontal grid + y ticks
  ctx.font = CHART_FONT;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  // ticks are chosen in the display unit and placed at t / k THz
  for (const t of niceTicks(lo * k, hi * k, 5)) {
    const yt = y(t / k);
    ctx.strokeStyle = CHART_COLORS.grid;
    ctx.beginPath();
    ctx.moveTo(x0, Math.round(yt) + 0.5);
    ctx.lineTo(x1, Math.round(yt) + 0.5);
    ctx.stroke();
    ctx.fillStyle = t < 0 ? "rgba(255, 120, 140, 0.9)" : CHART_COLORS.text;
    ctx.fillText(t.toFixed(Math.abs(t) < 10 && t % 1 !== 0 ? 1 : 0), x0 - 5, yt);
  }
  // high-symmetry verticals + labels
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (const tick of pathTicks(p.kpath_labels, d)) {
    const px = Math.round(x(tick.x)) + 0.5;
    ctx.strokeStyle = CHART_COLORS.axis;
    ctx.beginPath();
    ctx.moveTo(px, yTop);
    ctx.lineTo(px, yBot);
    ctx.stroke();
    ctx.fillStyle = "rgba(226, 232, 240, 0.9)";
    ctx.fillText(tick.label, Math.min(Math.max(px, x0 + 6), x1 - 6), yBot + 5);
  }
  // zero line
  ctx.strokeStyle = "rgba(226, 232, 240, 0.45)";
  ctx.beginPath();
  ctx.moveTo(x0, Math.round(y(0)) + 0.5);
  ctx.lineTo(x1, Math.round(y(0)) + 0.5);
  ctx.stroke();

  // branches: one batched path for real segments, one for imaginary
  const breaks = segmentBreaks(d);
  const nb = f[0]?.length ?? 0;
  const real = new Path2D();
  const imag = new Path2D();
  for (let b = 0; b < nb; b++) {
    for (let q = 0; q + 1 < d.length; q++) {
      if (breaks[q]) continue;
      const fa = f[q]![b]!;
      const fb = f[q + 1]![b]!;
      const path = isImaginarySegment(fa, fb) ? imag : real;
      path.moveTo(x(d[q]!), y(fa));
      path.lineTo(x(d[q + 1]!), y(fb));
    }
  }
  ctx.lineWidth = 1.2;
  ctx.lineJoin = "round";
  ctx.strokeStyle = CHART_COLORS.sky;
  ctx.stroke(real);
  ctx.lineWidth = 1.8;
  ctx.strokeStyle = CHART_COLORS.crimson;
  ctx.shadowColor = "rgba(255, 42, 84, 0.6)";
  ctx.shadowBlur = 6;
  ctx.stroke(imag);
  ctx.shadowBlur = 0;
  // axis label
  ctx.save();
  ctx.translate(11, (yTop + yBot) / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = CHART_COLORS.text;
  ctx.fillText(`f (${frequencyUnitLabel(unit)})`, 0, 0);
  ctx.restore();
}

function drawBand(ctx: CanvasRenderingContext2D, p: PhononAnalysisComplete, g: Geometry, band: number, color: string, width: number) {
  const d = p.kpath_distances;
  const breaks = segmentBreaks(d);
  ctx.beginPath();
  for (let q = 0; q < d.length; q++) {
    const px = g.x(d[q]!);
    const py = g.y(p.frequencies_thz[q]![band]!);
    if (q === 0 || breaks[q - 1]) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.stroke();
}

export function PhononBandChart() {
  const phonon = useCrystalStore((s) => s.phonon);
  const selection = useCrystalStore((s) => s.modeSelection);
  const loaded = useCrystalStore((s) => s.selectedEigenmode);
  const unit = useSettings((s) => s.units.frequency);
  const [hover, setHover] = useState<{ qIndex: number; band: number } | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const geomRef = useRef<Geometry | null>(null);
  const baseRef = useRef<{ canvas: HTMLCanvasElement; key: string } | null>(null);
  const summary = useMemo(() => (phonon ? classifyBands(phonon.frequencies_thz) : null), [phonon]);

  useCanvasChart(
    canvasRef,
    (ctx, size) => {
      if (!phonon || phonon.kpath_distances.length === 0) return;
      const g = geometry(phonon, size);
      geomRef.current = g;
      const dpr = Math.min(globalThis.devicePixelRatio || 1, 2);
      const key = `${size.width}x${size.height}@${dpr}:${unit}`;
      let base = baseRef.current;
      if (!base || base.key !== key || (base.canvas as unknown as { _p?: unknown })._p !== phonon) {
        const c = document.createElement("canvas");
        c.width = Math.round(size.width * dpr);
        c.height = Math.round(size.height * dpr);
        const bctx = c.getContext("2d")!;
        bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        drawBase(bctx, phonon, size, g, unit);
        (c as unknown as { _p?: unknown })._p = phonon;
        base = baseRef.current = { canvas: c, key };
      }
      ctx.drawImage(base.canvas, 0, 0, size.width, size.height);

      const d = phonon.kpath_distances;
      if (hover && (!selection || hover.band !== selection.band)) drawBand(ctx, phonon, g, hover.band, "rgba(226, 232, 240, 0.35)", 1.5);
      if (selection) {
        ctx.shadowColor = "rgba(34, 211, 238, 0.8)";
        ctx.shadowBlur = 8;
        drawBand(ctx, phonon, g, selection.band, CHART_COLORS.cyan, 2.2);
        ctx.shadowBlur = 0;
        const fq = phonon.frequencies_thz[selection.qIndex]?.[selection.band];
        if (fq !== undefined) {
          const px = g.x(d[selection.qIndex]!);
          const py = g.y(fq);
          const pending = !loaded || loaded.q_index !== selection.qIndex || loaded.band_index !== selection.band;
          ctx.strokeStyle = "rgba(34, 211, 238, 0.5)";
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(px + 0.5, M.top);
          ctx.lineTo(px + 0.5, size.height - M.bottom);
          ctx.stroke();
          ctx.setLineDash(pending ? [2, 2] : []);
          ctx.fillStyle = pending ? "rgba(7, 9, 14, 0.9)" : "#e0f7ff";
          ctx.strokeStyle = CHART_COLORS.cyan;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(px, py, 4.5, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }
      if (hover) {
        const fq = phonon.frequencies_thz[hover.qIndex]?.[hover.band];
        if (fq !== undefined) {
          const px = g.x(d[hover.qIndex]!);
          const py = g.y(fq);
          ctx.fillStyle = "rgba(226, 232, 240, 0.9)";
          ctx.beginPath();
          ctx.arc(px, py, 3, 0, Math.PI * 2);
          ctx.fill();
          const label = qLabelAt(phonon.kpath_labels, hover.qIndex) ?? `q${hover.qIndex}`;
          const text = `${label} · ν${hover.band} · ${formatFrequency(fq, unit)}`;
          ctx.font = CHART_FONT;
          const w = ctx.measureText(text).width + 10;
          const tx = Math.min(Math.max(px + 8, M.left), size.width - M.right - w);
          const ty = Math.max(M.top, py - 22);
          ctx.fillStyle = "rgba(2, 6, 23, 0.88)";
          ctx.fillRect(tx, ty, w, 16);
          ctx.fillStyle = fq < 0 ? "#fda4af" : "#e2e8f0";
          ctx.textAlign = "left";
          ctx.textBaseline = "middle";
          ctx.fillText(text, tx + 5, ty + 8);
        }
      }
    },
    () => phonon,
  );

  const pick = (e: MouseEvent<HTMLCanvasElement>) => {
    const g = geomRef.current;
    if (!phonon || !g) return null;
    const rect = e.currentTarget.getBoundingClientRect();
    return nearestMode(phonon.kpath_distances, phonon.frequencies_thz, g.x.invert(e.clientX - rect.left), g.y.invert(e.clientY - rect.top));
  };

  if (!phonon) return <div className="grid h-full place-items-center text-[11px] text-slate-500">Run the phonon shake-test to compute ω(q).</div>;

  return (
    <div className="relative h-full w-full">
      <canvas
        ref={canvasRef}
        className="absolute inset-0 cursor-crosshair"
        onMouseMove={(e) => {
          const m = pick(e);
          if (!m || m.qIndex !== hover?.qIndex || m.band !== hover?.band) setHover(m);
        }}
        onMouseLeave={() => setHover(null)}
        onClick={(e) => {
          const m = pick(e);
          if (m) selectModeAt(m.qIndex, m.band);
        }}
      />
      {summary && (
        <div className="pointer-events-none absolute top-2 right-3 font-mono text-[10px] text-slate-400">
          {summary.nBands} branches × {summary.nq} q ·{" "}
          {summary.imaginaryBands.length ? (
            <span className="text-rose-300">{phonon.imaginary_mode_count} imaginary · min {formatFrequency(phonon.min_frequency_thz, unit)}</span>
          ) : (
            <span className="text-emerald-300">all real</span>
          )}
        </div>
      )}
    </div>
  );
}
