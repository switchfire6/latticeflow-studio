/**
 * PNG export of chart panes (§4.2). Each canvas chart registers a renderer
 * (components/charts/useCanvasChart.ts) that redraws it offscreen at any DPR;
 * a pane's canvases are composited at their on-screen offsets into one image.
 *
 * "Publication" mode: the charts are redrawn in the light chart theme (dark
 * axes and text, the series palette stepped for white; see
 * components/charts/useCanvasChart.ts) and composited over white.
 * `invertLightness` is the per-pixel fallback for bitmaps that only exist in
 * the dark theme (pure pixel maths, unit-tested).
 */

/** In-place HSL lightness inversion L → 1 − L of RGBA pixels (alpha untouched). */
export function invertLightness(data: Uint8ClampedArray): void {
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] === 0) continue;
    const r = data[i]! / 255;
    const g = data[i + 1]! / 255;
    const b = data[i + 2]! / 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const l = (max + min) / 2;
    const d = max - min;
    let h = 0;
    let s = 0;
    if (d > 1e-9) {
      s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
      if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h /= 6;
    }
    // Darken a little more than a plain inversion so mid-tones read on white.
    const l2 = Math.max(0, Math.min(1, (1 - l) * 0.92));
    const [r2, g2, b2] = hslToRgb(h, s, l2);
    data[i] = r2;
    data[i + 1] = g2;
    data[i + 2] = b2;
  }
}

function hue(p: number, q: number, t: number): number {
  if (t < 0) t += 1;
  if (t > 1) t -= 1;
  if (t < 1 / 6) return p + (q - p) * 6 * t;
  if (t < 1 / 2) return q;
  if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
  return p;
}

export function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  if (s === 0) {
    const v = Math.round(l * 255);
    return [v, v, v];
  }
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  return [Math.round(hue(p, q, h + 1 / 3) * 255), Math.round(hue(p, q, h) * 255), Math.round(hue(p, q, h - 1 / 3) * 255)];
}

export interface RenderedChart {
  canvas: HTMLCanvasElement;
  /** CSS-px offset relative to the composite origin. */
  x: number;
  y: number;
}

export const DARK_BACKGROUND = "#16191e";

/**
 * Composite rendered charts (each drawn at `dpr`) into one canvas. With
 * `publication` the charts are expected in the light theme already; pass
 * `invert` only for bitmaps drawn in the dark theme.
 */
export function compositeCharts(charts: readonly RenderedChart[], width: number, height: number, dpr: number, publication: boolean, invert = false): HTMLCanvasElement {
  const out = document.createElement("canvas");
  out.width = Math.max(1, Math.round(width * dpr));
  out.height = Math.max(1, Math.round(height * dpr));
  const ctx = out.getContext("2d")!;
  for (const c of charts) ctx.drawImage(c.canvas, Math.round(c.x * dpr), Math.round(c.y * dpr));
  if (publication && invert) {
    const img = ctx.getImageData(0, 0, out.width, out.height);
    invertLightness(img.data);
    ctx.putImageData(img, 0, 0);
  }
  ctx.globalCompositeOperation = "destination-over";
  ctx.fillStyle = publication ? "#ffffff" : DARK_BACKGROUND;
  ctx.fillRect(0, 0, out.width, out.height);
  return out;
}
