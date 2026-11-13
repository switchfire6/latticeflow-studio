/** Error / warning / info toasts (ERROR events, unrelaxed-phonon warning, …). Auto-dismiss after a few seconds. */
import { AlertOctagon, AlertTriangle, Info, X } from "lucide-react";
import { useEffect } from "react";
import { commands } from "../../services/wsClient";
import { useUi } from "../../store/uiStore";
import { useCrystalStore, type Toast, type ToastAction } from "../../store/useCrystalStore";
import { cx } from "./ui";

const LIFETIME_MS: Record<Toast["level"], number> = { error: 8000, warning: 9000, info: 5000 };
const ACTION_LIFETIME_MS = 15000;

function runAction(a: ToastAction): void {
  switch (a.kind) {
    case "continue-relaxation":
      commands.relax();
      return;
    case "find-orderings":
      useUi.getState().openDialog("ordering");
      return;
    case "make-supercell":
      useUi.getState().openDialog("supercell");
      return;
  }
}

function ToastItem({ t }: { t: Toast }) {
  const dismiss = useCrystalStore((s) => s.dismissToast);
  useEffect(() => {
    const h = setTimeout(() => dismiss(t.id), t.action ? ACTION_LIFETIME_MS : LIFETIME_MS[t.level]);
    return () => clearTimeout(h);
  }, [t.id, t.level, t.action, dismiss]);
  const Icon = t.level === "error" ? AlertOctagon : t.level === "warning" ? AlertTriangle : Info;
  return (
    <div
      role="status"
      className={cx(
        "pointer-events-auto flex w-[360px] items-start gap-2.5 rounded-panel border bg-surface p-3 shadow-lg shadow-black/40",
        t.level === "error" && "border-bad/50",
        t.level === "warning" && "border-warn/45",
        t.level === "info" && "border-accent/60",
      )}
    >
      <Icon
        size={16}
        className={cx("mt-0.5 shrink-0", t.level === "error" ? "text-bad" : t.level === "warning" ? "text-warn" : "text-fg-3")}
      />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-semibold text-fg">{t.title}</div>
        <div className="mt-0.5 text-sm leading-snug break-words text-fg-2">{t.message}</div>
        {t.action && (
          <button
            type="button"
            className="mt-1.5 rounded-ctl border border-line-strong bg-raised px-2 py-0.5 text-xs font-medium text-fg hover:bg-hover"
            onClick={() => {
              runAction(t.action!);
              dismiss(t.id);
            }}
          >
            {t.action.label}
          </button>
        )}
      </div>
      <button type="button" className="text-fg-4 hover:text-fg" onClick={() => dismiss(t.id)} aria-label="dismiss">
        <X size={13} />
      </button>
    </div>
  );
}

export function Toasts() {
  const toasts = useCrystalStore((s) => s.toasts);
  return (
    <div className="pointer-events-none absolute right-3 bottom-3 z-40 flex flex-col items-end gap-2">
      {toasts.map((t) => (
        <ToastItem key={t.id} t={t} />
      ))}
    </div>
  );
}
