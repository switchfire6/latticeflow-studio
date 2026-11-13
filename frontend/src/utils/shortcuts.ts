/** Global keyboard shortcuts (pure mapping, unit-tested). */
export type ShortcutAction = "stop" | "undo" | "redo" | null;

/** Map a key event to a studio shortcut (ignored while typing in a field). */
export function shortcutFor(e: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey" | "defaultPrevented"> & { target: EventTarget | null }): ShortcutAction {
  if (e.defaultPrevented) return null;
  const t = e.target as HTMLElement | null;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return null;
  if (e.key === "Escape") return "stop";
  const mod = (e.ctrlKey || e.metaKey) && !e.altKey;
  if (!mod) return null;
  const k = e.key.toLowerCase();
  if (k === "z") return e.shiftKey ? "redo" : "undo";
  if (k === "y" && !e.shiftKey) return "redo";
  return null;
}
