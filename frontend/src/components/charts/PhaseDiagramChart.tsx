/**
 * Formation-energy phase diagram from the hull response (protocol §8).
 *   binary   E_form vs x(B) with the hull tie lines (facets), stable phases
 *            filled and labelled, metastable ones hollow;
 *   ternary  a composition triangle (barycentric) with the hull facets and
 *            the stable phases.
 * The evaluated structure is a diamond in series colour 2 with its E_hull.
 * Hover shows formula, composition and energies.
 */
import { useRef, useState } from "react";
import type { PhaseDiagramData, PhaseDiagramPoint } from "../../types/protocol";
import { SERIES_COLORS } from "../../utils/chartData";
import { drawAxes, drawTooltip, padded } from "./chartAxes";
import { CHART_COLORS, CHART_FONT, ink, renderState, useCanvasChart, type ChartSize } from "./useCanvasChart";

const STABLE = SERIES_COLORS[0];
const CANDIDATE = SERIES_COLORS[1];
const HIT_PX = 9;

interface Placed {
  p: PhaseDiagramPoint;
  px: number;
  py: number;
  candidate: boolean;
}


/** Legend drawn into exported PNGs (on screen it is HTML, see PhaseDiagramLegend). */
function drawLegend(ctx: CanvasRenderingContext2D, size: ChartSize) {
  let x = size.width - 12;
  const y = 10;
  const items: [string, (cx: number) => void][] = [
    ["evaluated", (cx) => diamond(ctx, cx, y)],
    ["above hull", (cx) => dot(ctx, cx, y, false)],
    ["on hull", (cx) => dot(ctx, cx, y, true)],
  ];
  ctx.font = CHART_FONT;
  for (const [t, mark] of items) {
    const w = ctx.measureText(t).width;
    x -= w;
    text(ctx, t, x, y, "left", "middle", CHART_COLORS.text);
    x -= 10;
    mark(x);
    x -= 16;
  }
}

export function PhaseDiagramLegend() {
  return (
    <span className="flex items-center gap-2.5 text-2xs text-fg-3">
      <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full" style={{ background: STABLE }} />on hull</span>
      <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full border border-fg-3" />above hull</span>
      <span className="flex items-center gap-1"><span className="h-2 w-2 rotate-45" style={{ background: CANDIDATE }} />evaluated</span>
    </span>
  );
}

function dot(ctx: CanvasRenderingContext2D, x: number, y: number, stable: boolean) {
  ctx.beginPath();
  ctx.arc(x, y, stable ? 4.5 : 3.5, 0, Math.PI * 2);
  if (stable) {
    ctx.fillStyle = ink(STABLE);
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = CHART_COLORS.background;
    ctx.stroke();
  } else {
    ctx.fillStyle = CHART_COLORS.background;
    ctx.fill();
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = CHART_COLORS.text;
    ctx.stroke();
  }
}

function diamond(ctx: CanvasRenderingContext2D, x: number, y: number) {
  const r = 6;
  ctx.beginPath();
  ctx.moveTo(x, y - r);
  ctx.lineTo(x + r, y);
  ctx.lineTo(x, y + r);
  ctx.lineTo(x - r, y);
  ctx.closePath();
  ctx.fillStyle = ink(CANDIDATE);
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = CHART_COLORS.background;
  ctx.stroke();
}

function text(ctx: CanvasRenderingContext2D, s: string, x: number, y: number, align: CanvasTextAlign, base: CanvasTextBaseline, color = CHART_COLORS.ink) {
  ctx.font = CHART_FONT;
  ctx.fillStyle = color;
  ctx.textAlign = align;
  ctx.textBaseline = base;
  ctx.fillText(s, x, y);
}

/** Direct labels that never overlap: the first come first served (candidate, then the most stable phases). */
class Labeler {
  private rects: [number, number, number, number][] = [];
  constructor(private ctx: CanvasRenderingContext2D) {}
  place(s: string, x: number, y: number, align: CanvasTextAlign, base: CanvasTextBaseline, color?: string, force = false): boolean {
    this.ctx.font = CHART_FONT;
    const w = this.ctx.measureText(s).width;
    const h = 11;
    const x0 = align === "center" ? x - w / 2 : align === "right" || align === "end" ? x - w : x;
    const y0 = base === "middle" ? y - h / 2 : base === "bottom" ? y - h : y;
    const r: [number, number, number, number] = [x0 - 2, y0 - 1, x0 + w + 2, y0 + h + 1];
    if (!force && this.rects.some((o) => r[0] < o[2] && r[2] > o[0] && r[1] < o[3] && r[3] > o[1])) return false;
    this.rects.push(r);
    text(this.ctx, s, x, y, align, base, color);
    return true;
  }
}

const candidateText = (p: PhaseDiagramPoint) => `${p.formula} (${p.e_above_hull >= 0 ? "+" : "−"}${Math.abs(p.e_above_hull * 1000).toFixed(0)} meV)`;
const byStability = (a: Placed, b: Placed) => a.p.formation_energy_per_atom - b.p.formation_energy_per_atom;

function drawBinary(ctx: CanvasRenderingContext2D, size: ChartSize, pd: PhaseDiagramData): Placed[] {
  const [, b] = pd.elements as [string, string];
  const all = [...pd.entries, pd.candidate];
  const a = drawAxes(ctx, size, [-0.02, 1.02], padded(all.map((p) => p.formation_energy_per_atom), 0.12, 0), `x(${b})`, "E_form (eV/atom)", { zeroLine: true });
  const xy = (p: PhaseDiagramPoint) => [a.x(p.composition[b] ?? 0), a.y(p.formation_energy_per_atom)] as const;
  ctx.strokeStyle = CHART_COLORS.ink;
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  for (const f of pd.facets) {
    const [i, j] = f;
    const p = pd.entries[i!];
    const q = pd.entries[j!];
    if (!p || !q) continue;
    const [x1, y1] = xy(p);
    const [x2, y2] = xy(q);
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
  }
  ctx.stroke();
  const placed: Placed[] = [];
  for (const p of pd.entries) {
    const [px, py] = xy(p);
    dot(ctx, px, py, p.on_hull);
    placed.push({ p, px, py, candidate: false });
  }
  const [cx, cy] = xy(pd.candidate);
  diamond(ctx, cx, cy);
  // Direct labels: the evaluated structure, then the stable phases that fit (selective, no overlaps).
  const lab = new Labeler(ctx);
  lab.place(candidateText(pd.candidate), cx, cy - 9, cx > a.xR - 80 ? "right" : "left", "bottom", undefined, true);
  const seen = new Set<string>();
  for (const e of placed.filter((x) => x.p.on_hull).sort(byStability)) {
    if (seen.has(e.p.formula)) continue;
    if (lab.place(e.p.formula, e.px, e.py + 7, "center", "top", CHART_COLORS.text)) seen.add(e.p.formula);
  }
  placed.push({ p: pd.candidate, px: cx, py: cy, candidate: true });
  return placed;
}

function drawTernary(ctx: CanvasRenderingContext2D, size: ChartSize, pd: PhaseDiagramData): Placed[] {
  const els = pd.elements as [string, string, string];
  const pad = 26;
  const side = Math.max(40, Math.min(size.width - 2 * pad, ((size.height - 2 * pad) * 2) / Math.sqrt(3)));
  const h = (side * Math.sqrt(3)) / 2;
  const ox = (size.width - side) / 2;
  const oy = (size.height + h) / 2;
  const V = [
    [ox, oy],
    [ox + side, oy],
    [ox + side / 2, oy - h],
  ] as const;
  const xy = (p: PhaseDiagramPoint): [number, number] => {
    let x = 0;
    let y = 0;
    els.forEach((e, i) => {
      const c = p.composition[e] ?? 0;
      x += c * V[i]![0];
      y += c * V[i]![1];
    });
    return [x, y];
  };
  // outline
  ctx.strokeStyle = CHART_COLORS.axis;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(V[0][0], V[0][1]);
  ctx.lineTo(V[1][0], V[1][1]);
  ctx.lineTo(V[2][0], V[2][1]);
  ctx.closePath();
  ctx.stroke();
  // hull facets (triangles of stable phases)
  ctx.strokeStyle = CHART_COLORS.inkSoft;
  ctx.lineWidth = 1.1;
  ctx.beginPath();
  for (const f of pd.facets) {
    const pts = f.map((i) => pd.entries[i]).filter((p): p is PhaseDiagramPoint => !!p).map(xy);
    if (pts.length < 2) continue;
    ctx.moveTo(pts[0]![0], pts[0]![1]);
    for (const q of pts.slice(1)) ctx.lineTo(q[0], q[1]);
    ctx.closePath();
  }
  ctx.stroke();
  const lab = new Labeler(ctx);
  lab.place(els[0], V[0][0] - 4, V[0][1] + 4, "right", "top", undefined, true);
  lab.place(els[1], V[1][0] + 4, V[1][1] + 4, "left", "top", undefined, true);
  lab.place(els[2], V[2][0], V[2][1] - 5, "center", "bottom", undefined, true);
  const placed: Placed[] = [];
  for (const p of pd.entries) {
    const [px, py] = xy(p);
    if (!p.on_hull) dot(ctx, px, py, false);
    placed.push({ p, px, py, candidate: false });
  }
  for (const e of placed) if (e.p.on_hull) dot(ctx, e.px, e.py, true);
  const [cx, cy] = xy(pd.candidate);
  diamond(ctx, cx, cy);
  const flip = cx > size.width - 120;
  lab.place(candidateText(pd.candidate), flip ? cx - 9 : cx + 9, cy - 6, flip ? "right" : "left", "bottom", undefined, true);
  const seen = new Set<string>();
  for (const e of placed.filter((x) => x.p.on_hull).sort(byStability)) {
    const isCorner = els.some((el) => (e.p.composition[el] ?? 0) > 0.999);
    if (isCorner || seen.has(e.p.formula)) continue;
    if (lab.place(e.p.formula, e.px, e.py + 6, "center", "top", CHART_COLORS.text)) seen.add(e.p.formula);
  }
  placed.push({ p: pd.candidate, px: cx, py: cy, candidate: true });
  return placed;
}

function tipLines(e: Placed, elements: string[]): string[] {
  const comp = elements.map((el) => `${el} ${((e.p.composition[el] ?? 0) * 100).toFixed(0)}%`).join(" · ");
  return [
    `${e.p.formula}${e.candidate ? " (evaluated)" : e.p.material_id ? ` · ${e.p.material_id}` : ""}`,
    comp,
    `E_form ${e.p.formation_energy_per_atom.toFixed(3)} eV/atom`,
    `E_hull ${e.p.e_above_hull.toFixed(3)} eV/atom${e.p.on_hull ? " · on hull" : ""}`,
  ];
}

export function PhaseDiagramChart({ pd }: { pd: PhaseDiagramData }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const placedRef = useRef<Placed[]>([]);
  const [hover, setHover] = useState<number | null>(null);
  const n = pd.elements.length;

  useCanvasChart(
    canvasRef,
    (ctx, size) => {
      if (n < 2 || n > 3) return;
      const placed = n === 2 ? drawBinary(ctx, size, pd) : drawTernary(ctx, size, pd);
      placedRef.current = placed;
      if (renderState.exporting) {
        drawLegend(ctx, size);
        return;
      }
      const h = hover !== null ? placed[hover] : undefined;
      if (h) drawTooltip(ctx, size, h.px, h.py, tipLines(h, pd.elements));
    },
    () => `${hover}`,
  );

  if (n < 2) return <div className="grid h-full place-items-center text-xs text-fg-4">A single-element system has no phase diagram.</div>;
  if (n > 3) return <div className="grid h-full place-items-center text-xs text-fg-4">{n}-component systems are not drawn (binary and ternary only).</div>;

  return (
    <div className="relative h-full w-full">
      <canvas
        ref={canvasRef}
        className="absolute inset-0 cursor-crosshair"
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const mx = e.clientX - r.left;
          const my = e.clientY - r.top;
          let best: number | null = null;
          let bd = HIT_PX * HIT_PX;
          placedRef.current.forEach((p, i) => {
            const d = (p.px - mx) ** 2 + (p.py - my) ** 2;
            if (d <= bd) (bd = d), (best = i);
          });
          if (best !== hover) setHover(best);
        }}
        onMouseLeave={() => setHover(null)}
      />
    </div>
  );
}
