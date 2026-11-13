/**
 * Canvas chart plumbing: DPR-aware sizing via ResizeObserver and a
 * requestAnimationFrame loop that redraws only when `key()` changes (or the
 * size does). Charts fed by the transient frame buffers use a key such as
 * `frame.version`, so high-rate data redraws the canvas without any React render.
 */
import { useEffect, useRef, type RefObject } from "react";
import { SERIES_COLORS } from "../../utils/chartData";

export interface ChartSize {
  width: number; // CSS px
  height: number; // CSS px
}

export type DrawFn = (ctx: CanvasRenderingContext2D, size: ChartSize) => void;

/**
 * Set while a chart is drawn: the device-pixel ratio of the target and whether
 * it is an offscreen export render (charts may then draw their HTML legends
 * into the canvas and bypass on-screen caches).
 */
export const renderState = { dpr: 1, exporting: false };

/** Offscreen renderers of the mounted charts (PNG export, utils/pngExport.ts). */
export type ChartRenderer = (dpr: number, theme?: ChartTheme) => HTMLCanvasElement;
const renderers = new WeakMap<HTMLCanvasElement, ChartRenderer>();
export function chartRenderer(canvas: HTMLCanvasElement): ChartRenderer | null {
  return renderers.get(canvas) ?? null;
}

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

    renderers.set(canvas, (dpr: number, theme: ChartTheme = "dark") => {
      const off = document.createElement("canvas");
      off.width = Math.max(1, Math.round(size.width * dpr));
      off.height = Math.max(1, Math.round(size.height * dpr));
      const octx = off.getContext("2d");
      if (!octx) return off;
      octx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const prev = { ...renderState };
      renderState.dpr = dpr;
      renderState.exporting = true;
      try {
        withChartTheme(theme, () => drawRef.current(octx, size));
      } finally {
        Object.assign(renderState, prev);
      }
      return off;
    });

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
        renderState.dpr = Math.min(globalThis.devicePixelRatio || 1, 2);
        renderState.exporting = false;
        drawRef.current(ctx, size);
        ctx.restore();
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      renderers.delete(canvas);
    };
  }, [canvasRef]);
}

export const CHART_FONT = '10px "JetBrains Mono Variable", "JetBrains Mono", "Cascadia Mono", ui-monospace, Consolas, monospace';
export type ChartTheme = "dark" | "publication";

/** Light-surface steps of the categorical series (same hues, validated on white). */
export const SERIES_COLORS_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6"] as const;

/** Mirrors the tokens in src/index.css (canvas cannot read CSS variables cheaply). */
const DARK = {
  background: "#16191e",
  axis: "rgba(155, 161, 170, 0.35)",
  grid: "rgba(155, 161, 170, 0.08)",
  text: "rgba(155, 161, 170, 0.9)",
  /** Strong ink: path labels, hover marks, the zero line. */
  ink: "rgba(231, 233, 236, 0.9)",
  inkSoft: "rgba(231, 233, 236, 0.4)",
  /** Selection (the UI accent). */
  cyan: "#3fb0c9",
  /** Single-series default (series slot 1). */
  sky: SERIES_COLORS[0] as string,
  /** Second series (slot 2). */
  amber: SERIES_COLORS[1] as string,
  /** Status: imaginary / unstable. */
  crimson: "#e5534b",
  crimsonText: "#f0857e",
  crimsonFill: "rgba(229, 83, 75, 0.09)",
  ok: "#3fb950",
  tooltipBg: "rgba(22, 25, 30, 0.94)",
};
const PUBLICATION: typeof DARK = {
  background: "#ffffff",
  axis: "rgba(40, 44, 52, 0.7)",
  grid: "rgba(40, 44, 52, 0.1)",
  text: "#30343b",
  ink: "#16191e",
  inkSoft: "rgba(22, 25, 30, 0.35)",
  cyan: "#1d7f96",
  sky: SERIES_COLORS_LIGHT[0],
  amber: SERIES_COLORS_LIGHT[1],
  crimson: "#c92f2f",
  crimsonText: "#b42525",
  crimsonFill: "rgba(201, 47, 47, 0.08)",
  ok: "#1f8a32",
  tooltipBg: "rgba(255, 255, 255, 0.94)",
};

/**
 * Live chart palette: `CHART_COLORS` holds the dark values and is swapped to
 * the publication (white) values while an export renders (`withChartTheme`).
 * Draw code must read it at draw time, never cache it in module constants.
 */
export const CHART_COLORS = { ...DARK };
let currentTheme: ChartTheme = "dark";

export function chartTheme(): ChartTheme {
  return currentTheme;
}

/** Map a series colour (dark step) to the current theme's step; other colours pass through. */
export function ink(color: string): string {
  if (currentTheme === "dark") return color;
  const i = (SERIES_COLORS as readonly string[]).indexOf(color);
  if (i >= 0) return SERIES_COLORS_LIGHT[i]!;
  const key = (Object.keys(DARK) as (keyof typeof DARK)[]).find((k) => DARK[k] === color);
  return key ? PUBLICATION[key] : color;
}

/** Run `fn` with the chart palette switched to `theme`. */
export function withChartTheme<T>(theme: ChartTheme, fn: () => T): T {
  const prev = currentTheme;
  currentTheme = theme;
  Object.assign(CHART_COLORS, theme === "publication" ? PUBLICATION : DARK);
  try {
    return fn();
  } finally {
    currentTheme = prev;
    Object.assign(CHART_COLORS, prev === "publication" ? PUBLICATION : DARK);
  }
}

/** Linear scale helper. */
export function scale(d0: number, d1: number, r0: number, r1: number) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  const f = (v: number) => r0 + (v - d0) * k;
  f.invert = (p: number) => (k === 0 ? d0 : d0 + (p - r0) / k);
  return f as ((v: number) => number) & { invert(p: number): number };
}
