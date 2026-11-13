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
        "pointer-events-auto flex w-[360px] items-start gap-2.5 rounded-xl border bg-slate-950/90 p-3 shadow-[0_10px_40px_rgba(0,0,0,0.5)] backdrop-blur-md",
        t.level === "error" && "border-rose-500/50",
        t.level === "warning" && "border-amber-400/45",
        t.level === "info" && "border-cyan-400/35",
      )}
    >
      <Icon
        size={16}
        className={cx("mt-0.5 shrink-0", t.level === "error" ? "text-rose-400" : t.level === "warning" ? "text-amber-300" : "text-cyan-300")}
      />
      <div className="min-w-0 flex-1">
        <div className="text-[12px] font-semibold text-slate-100">{t.title}</div>
        <div className="mt-0.5 text-[11.5px] leading-snug break-words text-slate-300">{t.message}</div>
        {t.action && (
          <button
            type="button"
            className="mt-1.5 rounded-md border border-cyan-400/40 bg-cyan-400/10 px-2 py-0.5 text-[11px] font-medium text-cyan-100 hover:bg-cyan-400/20"
            onClick={() => {
              runAction(t.action!);
              dismiss(t.id);
            }}
          >
            {t.action.label}
          </button>
        )}
      </div>
      <button type="button" className="text-slate-500 hover:text-slate-200" onClick={() => dismiss(t.id)} aria-label="dismiss">
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
