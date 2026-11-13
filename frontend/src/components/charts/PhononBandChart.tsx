/**
 * Phonon dispersion ω(q) (§6.3): all 3N branches vs kpath_distances on a
 * canvas. The static layer (grid, shaded f < 0 region, zero line, branches;
 * segments with f < −F_TOL in crimson, two batched strokes in total) is
 * rendered once per (analysis, size) into an offscreen canvas; selection and
 * hover are a cheap overlay. Smooth for 96 bands × ~150 q (Li₃PS₄).
 *
 * Clicking picks the nearest (q_index, band) and animates it at once: the
 * cached eigenvector of a labelled point, otherwise REQUEST_EIGENMODE.
 *
 * Phase B (§5.2): with `band_order` the branches are drawn connected through
 * crossings (optionally one colour per branch), and the selected / hovered
 * mode highlights its whole connected branch. Export renders (PNG) bypass the
 * on-screen base-layer cache.
 */
import { useMemo, useRef, useState, type MouseEvent } from "react";
import { selectModeAt } from "../../services/phononActions";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { PhononAnalysisComplete } from "../../types/protocol";
import { formatFrequency, frequencyFactor, frequencyUnitLabel, type FrequencyUnit } from "../../utils/units";
import { SERIES_COLORS } from "../../utils/chartData";
import {
  branchOfBand,
  classifyBands,
  connectedFrequencies,
  frequencyRange,
  isImaginarySegment,
  nearestMode,
  niceTicks,
  pathTicks,
  qLabelAt,
  segmentBreaks,
} from "../../utils/bandChart";
import { CHART_COLORS, CHART_FONT, ink, renderState, scale, useCanvasChart, type ChartSize } from "./useCanvasChart";

/** Branch-ordered frequencies per analysis (connectedFrequencies is O(nq·nb)). */
const branchCache = new WeakMap<PhononAnalysisComplete, number[][]>();
export function branchRows(p: PhononAnalysisComplete): number[][] {
  let rows = branchCache.get(p);
  if (!rows) {
    rows = connectedFrequencies(p.frequencies_thz, p.band_order);
    branchCache.set(p, rows);
  }
  return rows;
}

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

function drawBase(ctx: CanvasRenderingContext2D, p: PhononAnalysisComplete, size: ChartSize, g: Geometry, unit: FrequencyUnit, colorBranches: boolean) {
  const k = frequencyFactor(unit);
  const { x, y } = g;
  const d = p.kpath_distances;
  const f = branchRows(p);
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
    ctx.fillStyle = t < 0 ? CHART_COLORS.crimsonText : CHART_COLORS.text;
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
    ctx.fillStyle = CHART_COLORS.ink;
    ctx.fillText(tick.label, Math.min(Math.max(px, x0 + 6), x1 - 6), yBot + 5);
  }
  // zero line
  ctx.strokeStyle = CHART_COLORS.inkSoft;
  ctx.beginPath();
  ctx.moveTo(x0, Math.round(y(0)) + 0.5);
  ctx.lineTo(x1, Math.round(y(0)) + 0.5);
  ctx.stroke();

  // branches: batched paths for real segments (one, or one per colour), one for imaginary
  const breaks = segmentBreaks(d);
  const nb = f[0]?.length ?? 0;
  const nColors = colorBranches ? SERIES_COLORS.length : 1;
  const real = Array.from({ length: nColors }, () => new Path2D());
  const imag = new Path2D();
  for (let b = 0; b < nb; b++) {
    const realPath = real[b % nColors]!;
    for (let q = 0; q + 1 < d.length; q++) {
      if (breaks[q]) continue;
      const fa = f[q]![b]!;
      const fb = f[q + 1]![b]!;
      const path = isImaginarySegment(fa, fb) ? imag : realPath;
      path.moveTo(x(d[q]!), y(fa));
      path.lineTo(x(d[q + 1]!), y(fb));
    }
  }
  ctx.lineWidth = colorBranches ? 1.4 : 1.2;
  ctx.lineJoin = "round";
  real.forEach((path, i) => {
    ctx.strokeStyle = colorBranches ? ink(SERIES_COLORS[i]!) : CHART_COLORS.sky;
    ctx.stroke(path);
  });
  ctx.lineWidth = 1.8;
  ctx.strokeStyle = CHART_COLORS.crimson;
  ctx.stroke(imag);
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

/** Draw connected branch `branch` (= ascending band index without a band_order). */
function drawBand(ctx: CanvasRenderingContext2D, p: PhononAnalysisComplete, g: Geometry, branch: number, color: string, width: number) {
  const d = p.kpath_distances;
  const rows = branchRows(p);
  const breaks = segmentBreaks(d);
  ctx.beginPath();
  for (let q = 0; q < d.length; q++) {
    const px = g.x(d[q]!);
    const py = g.y(rows[q]![branch]!);
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
  const colorBranches = useSettings((s) => !!s.phonon.colorBranches);
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
      if (renderState.exporting) {
        // Offscreen export: draw the base directly at the export resolution, no selection overlay.
        drawBase(ctx, phonon, size, g, unit, colorBranches);
        return;
      }
      const dpr = renderState.dpr;
      const key = `${size.width}x${size.height}@${dpr}:${unit}:${colorBranches}`;
      let base = baseRef.current;
      if (!base || base.key !== key || (base.canvas as unknown as { _p?: unknown })._p !== phonon) {
        const c = document.createElement("canvas");
        c.width = Math.round(size.width * dpr);
        c.height = Math.round(size.height * dpr);
        const bctx = c.getContext("2d")!;
        bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        drawBase(bctx, phonon, size, g, unit, colorBranches);
        (c as unknown as { _p?: unknown })._p = phonon;
        base = baseRef.current = { canvas: c, key };
      }
      ctx.drawImage(base.canvas, 0, 0, size.width, size.height);

      const d = phonon.kpath_distances;
      const order = phonon.band_order;
      const hoverBranch = hover ? branchOfBand(order, hover.qIndex, hover.band) : null;
      const selBranch = selection ? branchOfBand(order, selection.qIndex, selection.band) : null;
      if (hoverBranch !== null && hoverBranch !== selBranch) drawBand(ctx, phonon, g, hoverBranch, CHART_COLORS.inkSoft, 1.5);
      if (selection && selBranch !== null) {
        drawBand(ctx, phonon, g, selBranch, CHART_COLORS.cyan, 2.2);
        const fq = phonon.frequencies_thz[selection.qIndex]?.[selection.band];
        if (fq !== undefined) {
          const px = g.x(d[selection.qIndex]!);
          const py = g.y(fq);
          const pending = !loaded || loaded.q_index !== selection.qIndex || loaded.band_index !== selection.band;
          ctx.strokeStyle = CHART_COLORS.cyan;
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(px + 0.5, M.top);
          ctx.lineTo(px + 0.5, size.height - M.bottom);
          ctx.stroke();
          ctx.setLineDash(pending ? [2, 2] : []);
          ctx.fillStyle = pending ? CHART_COLORS.tooltipBg : CHART_COLORS.ink;
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
          ctx.fillStyle = CHART_COLORS.ink;
          ctx.beginPath();
          ctx.arc(px, py, 3, 0, Math.PI * 2);
          ctx.fill();
          const label = qLabelAt(phonon.kpath_labels, hover.qIndex) ?? `q${hover.qIndex}`;
          const text = `${label} · ν${hover.band} · ${formatFrequency(fq, unit)}`;
          ctx.font = CHART_FONT;
          const w = ctx.measureText(text).width + 10;
          const tx = Math.min(Math.max(px + 8, M.left), size.width - M.right - w);
          const ty = Math.max(M.top, py - 22);
          ctx.fillStyle = CHART_COLORS.tooltipBg;
          ctx.fillRect(tx, ty, w, 16);
          ctx.fillStyle = fq < 0 ? CHART_COLORS.crimsonText : CHART_COLORS.ink;
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

  if (!phonon) return <div className="grid h-full place-items-center text-xs text-fg-4">Run phonons to compute ω(q).</div>;

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
        <div className="pointer-events-none absolute top-2 right-3 left-12 truncate text-right font-mono text-2xs whitespace-nowrap text-fg-3">
          {summary.imaginaryBands.length ? (
            <span className="text-bad">{phonon.imaginary_mode_count} imaginary · min {formatFrequency(phonon.min_frequency_thz, unit)}</span>
          ) : (
            <span className="text-ok">all real</span>
          )}
          {` · ${summary.nBands} branches × ${summary.nq} q`}
        </div>
      )}
    </div>
  );
}
