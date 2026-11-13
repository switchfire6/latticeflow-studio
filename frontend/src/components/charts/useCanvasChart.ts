/**
 * Canvas chart plumbing: DPR-aware sizing via ResizeObserver and a
 * requestAnimationFrame loop that redraws only when `key()` changes (or the
 * size does). Charts fed by the transient frame buffers use a key such as
 * `frame.version`, so high-rate data redraws the canvas without any React render.
 */
import { useEffect, useRef, type RefObject } from "react";

export interface ChartSize {
  width: number; // CSS px
  height: number; // CSS px
}

export type DrawFn = (ctx: CanvasRenderingContext2D, size: ChartSize) => void;

export function useCanvasChart(
  canvasRef: RefObject<HTMLCanvasElement | null>,
  draw: DrawFn,
  key: () => unknown,
): void {
  const drawRef = useRef(draw);
  const keyRef = useRef(key);
  drawRef.current = draw;
  keyRef.current = key;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let size: ChartSize = { width: 0, height: 0 };
    let dirty = true;
    let lastKey: unknown = Symbol("init");
    let lastDraw: DrawFn | null = null;

    const resize = () => {
      const rect = canvas.parentElement?.getBoundingClientRect() ?? canvas.getBoundingClientRect();
      const dpr = Math.min(globalThis.devicePixelRatio || 1, 2);
      const w = Math.max(1, Math.floor(rect.width));
      const h = Math.max(1, Math.floor(rect.height));
      if (w === size.width && h === size.height && canvas.width === Math.round(w * dpr)) return;
      size = { width: w, height: h };
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      dirty = true;
    };
    resize();
    const ro = new ResizeObserver(resize);
    if (canvas.parentElement) ro.observe(canvas.parentElement);

    let raf = 0;
    const loop = () => {
      const k = keyRef.current();
      if (dirty || k !== lastKey || drawRef.current !== lastDraw) {
        dirty = false;
        lastKey = k;
        lastDraw = drawRef.current;
        ctx.save();
        ctx.clearRect(0, 0, size.width, size.height);
        drawRef.current(ctx, size);
        ctx.restore();
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [canvasRef]);
}

export const CHART_FONT = '10px "JetBrains Mono", "Cascadia Mono", ui-monospace, Consolas, monospace';
export const CHART_COLORS = {
  axis: "rgba(148, 163, 184, 0.35)",
  grid: "rgba(148, 163, 184, 0.08)",
  text: "rgba(148, 163, 184, 0.85)",
  cyan: "#22d3ee",
  sky: "rgba(125, 211, 252, 0.8)",
  amber: "#f5a524",
  crimson: "#ff2a54",
  crimsonFill: "rgba(255, 42, 84, 0.09)",
};

/** Linear scale helper. */
export function scale(d0: number, d1: number, r0: number, r1: number) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  const f = (v: number) => r0 + (v - d0) * k;
  f.invert = (p: number) => (k === 0 ? d0 : d0 + (p - r0) / k);
  return f as ((v: number) => number) & { invert(p: number): number };
}
