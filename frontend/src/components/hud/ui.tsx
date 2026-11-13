/** Small glassmorphic HUD primitives shared by the panels. */
import { ChevronDown, ChevronRight } from "lucide-react";
import { useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";

/** "SrTiO3" -> SrTiO₃ */
export function FormulaText({ formula }: { formula: string }) {
  const parts = formula.split(/(\d+(?:\.\d+)?)/).filter(Boolean);
  return (
    <span>
      {parts.map((p, i) => (/^\d/.test(p) ? <sub key={i} className="text-[0.75em]">{p}</sub> : <span key={i}>{p}</span>))}
    </span>
  );
}

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function Panel({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cx(
        "rounded-xl border border-white/[0.08] bg-slate-950/70 shadow-[0_8px_32px_rgba(0,0,0,0.45)] backdrop-blur-md",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-2 flex items-center justify-between">
      <h3 className="text-[10px] font-semibold tracking-[0.18em] text-slate-400 uppercase">{children}</h3>
      {right}
    </div>
  );
}

type Variant = "primary" | "danger" | "ghost" | "subtle" | "warning";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-gradient-to-r from-cyan-500/90 to-sky-500/90 text-slate-950 hover:from-cyan-400 hover:to-sky-400 shadow-[0_0_18px_rgba(34,211,238,0.35)]",
  danger: "bg-rose-500/90 text-white hover:bg-rose-400 shadow-[0_0_18px_rgba(255,42,84,0.35)]",
  warning: "bg-amber-400/90 text-slate-950 hover:bg-amber-300",
  ghost: "border border-white/10 bg-white/[0.03] text-slate-200 hover:border-cyan-400/40 hover:bg-cyan-400/10",
  subtle: "text-slate-300 hover:bg-white/5",
};

export function Button({
  variant = "ghost",
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Toggle({ label, checked, onChange, title }: { label: string; checked: boolean; onChange(v: boolean): void; title?: string }) {
  return (
    <button
      type="button"
      title={title}
      onClick={() => onChange(!checked)}
      className={cx(
        "rounded-md border px-2 py-1 text-[11px] font-medium transition-colors",
        checked ? "border-cyan-400/50 bg-cyan-400/15 text-cyan-100" : "border-white/10 bg-white/[0.02] text-slate-400 hover:text-slate-200",
      )}
    >
      {label}
    </button>
  );
}

export function Slider({
  label,
  value,
  min,
  max,
  step = 1,
  onChange,
  format = (v) => String(v),
  disabled,
  right,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange(v: number): void;
  format?: (v: number) => string;
  disabled?: boolean;
  right?: ReactNode;
}) {
  return (
    <label className={cx("block", disabled && "opacity-45")}>
      <div className="mb-1 flex items-center justify-between text-[11px] text-slate-400">
        <span>{label}</span>
        <span className="flex items-center gap-2">
          <span className="font-mono text-slate-200 tabular-nums">{format(value)}</span>
          {right}
        </span>
      </div>
      <input
        type="range"
        className="lf-range w-full"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

export function Badge({ tone, children, pulse, title }: { tone: "green" | "amber" | "red" | "slate" | "cyan"; children: ReactNode; pulse?: boolean; title?: string }) {
  const tones = {
    green: "border-emerald-400/40 bg-emerald-400/10 text-emerald-300",
    amber: "border-amber-400/40 bg-amber-400/10 text-amber-300",
    red: "border-rose-500/50 bg-rose-500/15 text-rose-300 shadow-[0_0_14px_rgba(255,42,84,0.35)]",
    slate: "border-white/10 bg-white/5 text-slate-300",
    cyan: "border-cyan-400/40 bg-cyan-400/10 text-cyan-200",
  } as const;
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wide whitespace-nowrap normal-case",
        tones[tone],
        pulse && "lf-pulse-red",
      )}
    >
      {children}
    </span>
  );
}

/** Collapsible sidebar card; the open state is remembered in the settings store. */
export function CollapsibleCard({
  id,
  title,
  right,
  defaultOpen = true,
  className,
  children,
}: {
  id: string;
  title: ReactNode;
  right?: ReactNode;
  defaultOpen?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const collapsed = useSettings((s) => s.collapsed[id]);
  const open = collapsed === undefined ? defaultOpen : !collapsed;
  return (
    <Panel className={cx("p-3", className)}>
      <div className={cx("flex items-center justify-between", open && "mb-2")}>
        <button
          type="button"
          aria-expanded={open}
          onClick={() => useSettings.getState().setCollapsed(id, open)}
          className="flex items-center gap-1.5 text-[10px] font-semibold tracking-[0.18em] text-slate-400 uppercase hover:text-slate-200"
        >
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          {title}
        </button>
        {right}
      </div>
      {open && children}
    </Panel>
  );
}

export const inputCls =
  "w-full rounded-lg border border-white/10 bg-slate-900/80 px-2 py-1.5 font-mono text-[12px] text-slate-100 outline-none focus:border-cyan-400/50";

/** Labelled numeric field that commits on blur / Enter (keeps a free-text draft while typing). */
export function NumberField({
  label,
  value,
  onCommit,
  unit,
  step,
  min,
  max,
  title,
  integer,
  disabled,
}: {
  label: ReactNode;
  value: number;
  onCommit(v: number): void;
  unit?: string;
  step?: number;
  min?: number;
  max?: number;
  title?: string;
  integer?: boolean;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const commit = () => {
    if (draft === null) return;
    const v = integer ? Number.parseInt(draft, 10) : Number.parseFloat(draft);
    setDraft(null);
    if (!Number.isFinite(v)) return;
    const clamped = Math.min(max ?? Infinity, Math.max(min ?? -Infinity, v));
    if (clamped !== value) onCommit(clamped);
  };
  return (
    <label className={cx("flex items-center justify-between gap-2 text-[11px] text-slate-400", disabled && "opacity-45")} title={title}>
      <span className="min-w-0 truncate">{label}</span>
      <span className="flex items-center gap-1">
        <input
          className="w-[68px] rounded border border-white/10 bg-slate-900/80 px-1.5 py-0.5 text-right font-mono text-[11px] text-slate-100 outline-none focus:border-cyan-400/50"
          inputMode={integer ? "numeric" : "decimal"}
          value={draft ?? String(value)}
          step={step}
          disabled={disabled}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            if (e.key === "Escape") setDraft(null);
          }}
        />
        {unit && <span className="w-9 font-mono text-[10px] text-slate-500">{unit}</span>}
      </span>
    </label>
  );
}

/** Small segmented control. */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  title,
}: {
  value: T;
  options: readonly { value: T; label: ReactNode; title?: string; disabled?: boolean }[];
  onChange(v: T): void;
  title?: string;
}) {
  return (
    <span className="inline-flex rounded-md border border-white/10 bg-white/[0.02] p-0.5" title={title}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          title={o.title}
          disabled={o.disabled}
          onClick={() => onChange(o.value)}
          className={cx(
            "rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors disabled:opacity-30",
            o.value === value ? "bg-cyan-400/20 text-cyan-100" : "text-slate-400 hover:text-slate-200",
          )}
        >
          {o.label}
        </button>
      ))}
    </span>
  );
}

export const FALLBACK_LABEL = "analytic fallback — not physical";

/** Watermark for results computed with the LJ fallback calculator (§6.4). */
export function FallbackTag({ compact, className }: { compact?: boolean; className?: string }) {
  const fallback = useCrystalStore((s) => s.health?.umlip_backend === "lj-fallback");
  return <FallbackTagView active={fallback} compact={compact} className={className} />;
}

export function FallbackTagView({ active, compact, className }: { active: boolean; compact?: boolean; className?: string }) {
  if (!active) return null;
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded border border-amber-400/50 bg-amber-400/15 px-1 py-px font-mono text-[9px] font-semibold tracking-wide whitespace-nowrap text-amber-300 uppercase",
        className,
      )}
      title="MACE could not be loaded; the backend uses a Lennard-Jones fallback. Numbers are not physical."
      data-fallback-tag
    >
      {compact ? "LJ fallback" : FALLBACK_LABEL}
    </span>
  );
}
