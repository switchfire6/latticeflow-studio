/** Compact periodic table (Z = 1–89, MACE-MP-0 coverage) used as a popover picker. */
import { useEffect, useRef } from "react";
import { cpkHex, elementBySymbol } from "../../utils/cpkColors";
import { TABLE_CELLS } from "../../utils/periodicTable";
import { cx } from "./ui";

export function PeriodicTablePicker({
  selected = [],
  current,
  onPick,
  onClose,
  className,
}: {
  selected?: readonly string[];
  current?: string;
  onPick(symbol: string): void;
  onClose(): void;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("pointerdown", onDown, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  return (
    <div
      ref={ref}
      className={cx(
        "z-50 rounded-panel border border-line bg-surface p-2 shadow-lg shadow-black/40",
        className,
      )}
    >
      <div className="grid gap-[2px]" style={{ gridTemplateColumns: "repeat(18, 1.45rem)", gridTemplateRows: "repeat(7, 1.45rem) 0.4rem repeat(2, 1.45rem)" }}>
        {TABLE_CELLS.map((c) => {
          const isSel = selected.includes(c.symbol);
          const isCur = current === c.symbol;
          const mass = elementBySymbol(c.symbol)?.mass ?? 0;
          return (
            <button
              key={c.symbol}
              type="button"
              title={`${c.symbol} (Z = ${c.z}, ${mass.toFixed(2)} u)`}
              onClick={() => onPick(c.symbol)}
              style={{ gridRow: c.row, gridColumn: c.col, borderColor: isSel || isCur ? undefined : `${cpkHex(c.z)}55` }}
              className={cx(
                "flex items-center justify-center rounded-[4px] border text-2xs leading-none transition-colors",
                isCur
                  ? "border-white bg-white/20 text-fg"
                  : isSel
                    ? "border-accent/60 bg-accent-soft text-accent-fg"
                    : "bg-raised text-fg-2 hover:bg-hover hover:text-fg",
              )}
            >
              {c.symbol}
            </button>
          );
        })}
      </div>
    </div>
  );
}
