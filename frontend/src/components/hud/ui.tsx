/** Small HUD primitives shared by the panels; colours, sizes and radii come from the tokens in index.css. */
import { ChevronDown, ChevronRight } from "lucide-react";
import { useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import { activeModel, isFallback } from "../../utils/models";

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

/** Opaque surface. `floating` (viewport overlays, popovers) adds a small neutral shadow. */
export function Panel({ className, floating, children }: { className?: string; floating?: boolean; children: ReactNode }) {
  return (
    <div className={cx("rounded-panel border border-line bg-surface", floating && "shadow-md shadow-black/40", className)}>
      {children}
    </div>
  );
}

export function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-2 flex items-center justify-between">
      <h3 className="text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase">{children}</h3>
      {right}
    </div>
  );
}

type Variant = "primary" | "danger" | "ghost" | "subtle" | "warning";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-fg font-semibold text-canvas hover:bg-white",
  danger: "bg-bad font-semibold text-white hover:bg-bad/85",
  warning: "border border-warn/50 bg-warn/10 text-warn hover:bg-warn/20",
  ghost: "border border-line bg-raised text-fg hover:border-line-strong hover:bg-hover",
  subtle: "text-fg-2 hover:bg-hover",
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
        "inline-flex items-center justify-center gap-1.5 rounded-ctl px-3 py-1.5 text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-accent transition-colors disabled:cursor-not-allowed disabled:opacity-40",
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
      aria-pressed={checked}
      onClick={() => onChange(!checked)}
      className={cx(
        "rounded-ctl px-2 py-1 text-xs font-medium transition-colors outline-none focus-visible:ring-2 focus-visible:ring-accent",
        checked ? "bg-accent-soft text-accent-fg" : "text-fg-3 hover:bg-hover hover:text-fg",
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
      <div className="mb-1 flex items-center justify-between text-xs text-fg-3">
        <span>{label}</span>
        <span className="flex items-center gap-2">
          <span className="font-mono text-fg tabular-nums">{format(value)}</span>
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

export function Badge({ tone, children, title }: { tone: "green" | "amber" | "red" | "slate" | "cyan"; children: ReactNode; title?: string }) {
  const tones = {
    green: "border-ok/40 bg-ok/10 text-ok",
    amber: "border-warn/40 bg-warn/10 text-warn",
    red: "border-bad/50 bg-bad/15 text-bad",
    slate: "border-line bg-raised text-fg-2",
    cyan: "border-line-strong bg-raised text-fg",
  } as const;
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center gap-1 rounded-ctl border px-1.5 py-px text-2xs font-semibold whitespace-nowrap normal-case",
        tones[tone],
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
          className="flex items-center gap-1.5 text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase hover:text-fg"
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
  "w-full rounded-ctl border border-line bg-field px-2 py-1.5 text-sm text-fg outline-none focus:border-accent";

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
    <label className={cx("flex items-center justify-between gap-2 text-xs text-fg-3", disabled && "opacity-45")} title={title}>
      <span className="min-w-0 truncate">{label}</span>
      <span className="flex items-center gap-1">
        <input
          className="w-[68px] rounded-ctl border border-line bg-field px-1.5 py-0.5 text-right font-mono text-xs text-fg outline-none focus:border-accent"
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
        {unit && <span className="w-9 font-mono text-2xs text-fg-4">{unit}</span>}
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
    <span className="inline-flex rounded-ctl border border-line bg-raised p-0.5" title={title}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          title={o.title}
          disabled={o.disabled}
          onClick={() => onChange(o.value)}
          className={cx(
            "rounded-ctl px-1.5 py-0.5 text-2xs transition-colors disabled:opacity-30",
            o.value === value ? "bg-accent-soft text-accent-fg" : "text-fg-3 hover:text-fg",
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
  // Phase B: the session's model when switched, else the process default.
  const fallback = useCrystalStore((s) => isFallback(activeModel(s.model, s.health)));
  return <FallbackTagView active={fallback} compact={compact} className={className} />;
}

export function FallbackTagView({ active, compact, className }: { active: boolean; compact?: boolean; className?: string }) {
  if (!active) return null;
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-ctl border border-warn/50 bg-warn/15 px-1 py-px text-2xs font-semibold whitespace-nowrap text-warn",
        className,
      )}
      title="MACE could not be loaded; the backend uses a Lennard-Jones fallback. Numbers are not physical."
      data-fallback-tag
    >
      {compact ? "LJ fallback" : FALLBACK_LABEL}
    </span>
  );
}
