/**
 * Symmetry & files card (§4.1–4.3): "Refine symmetry" (REFINE_STRUCTURE:
 * refined / conventional / primitive at a symprec), the export menu
 * (symmetrised CIF by default, P1 CIF, POSCAR, extxyz; cell as displayed /
 * conventional / primitive) and import (file picker for CIF / POSCAR /
 * CONTCAR / .vasp / extxyz, and a paste dialog). Drag-and-drop onto the
 * viewport is handled by `ViewportDropZone`.
 */
import { ClipboardPaste, Download, FileUp, Loader2, Sparkles, X } from "lucide-react";
import { useRef, useState, type DragEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api, type ExportCell, type ExportOptions } from "../../services/apiClient";
import { importStructureFile, importStructureText } from "../../services/importActions";
import { commands } from "../../services/wsClient";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { RefineStructurePayload, StructureTextFormat } from "../../types/protocol";
import { FORMAT_LABELS, STRUCTURE_FILE_ACCEPT, hasFiles } from "../../utils/structureFile";
import { Button, CollapsibleCard, Panel, cx, inputCls } from "./ui";

export type ExportChoice = "cif-sym" | "cif-p1" | "poscar" | "extxyz";

export const EXPORT_CHOICES: { value: ExportChoice; label: string }[] = [
  { value: "cif-sym", label: "CIF · symmetrised" },
  { value: "cif-p1", label: "CIF · P1" },
  { value: "poscar", label: "POSCAR (VASP)" },
  { value: "extxyz", label: "extxyz (E, F, σ)" },
];

/** Export menu choice → request options (extxyz is always as displayed). */
export function exportOptions(choice: ExportChoice, cell: ExportCell, symprec = 0.01): ExportOptions {
  switch (choice) {
    case "cif-sym":
      return { format: "cif", symmetrize: true, symprec, cell };
    case "cif-p1":
      return { format: "cif", symmetrize: false, cell };
    case "poscar":
      return { format: "poscar", symprec, cell };
    case "extxyz":
      return { format: "extxyz", cell: "as_is" };
  }
}

const notify = (level: "warning" | "info", title: string, message: string) => useCrystalStore.getState().pushToast(level, title, message);

export function loadFile(file: File): Promise<StructureTextFormat | null> {
  return importStructureFile(file, (text, f) => commands.loadText(text, f), notify);
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <div className="mb-0.5 text-[10px] text-slate-500">{label}</div>
      {children}
    </label>
  );
}

export function PasteStructureDialog({ onClose }: { onClose(): void }) {
  const [text, setText] = useState("");
  const [format, setFormat] = useState<StructureTextFormat>("auto");
  const connected = useCrystalStore((s) => s.connection === "open");
  const submit = () => {
    if (importStructureText(text, format, (t, f) => commands.loadText(t, f))) onClose();
  };
  return createPortal(
    <div className="fixed inset-0 z-[100] grid place-items-center bg-black/50 backdrop-blur-[2px]" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <Panel className="w-[560px] max-w-[calc(100vw-32px)] p-4">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[10px] font-semibold tracking-[0.18em] text-slate-400 uppercase">Paste structure</span>
          <button type="button" onClick={onClose} className="text-slate-500 hover:text-slate-200" aria-label="close">
            <X size={14} />
          </button>
        </div>
        <textarea
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
          }}
          spellCheck={false}
          placeholder={"data_SrTiO3\n_cell_length_a 3.905\n…\n\nor a POSCAR / extxyz block"}
          className="h-64 w-full resize-none rounded-lg border border-white/10 bg-slate-900/80 p-2 font-mono text-[11px] text-slate-100 outline-none focus:border-cyan-400/50"
        />
        <div className="mt-2 flex items-center gap-2">
          <select className={cx(inputCls, "w-48")} value={format} onChange={(e) => setFormat(e.target.value as StructureTextFormat)}>
            {(Object.keys(FORMAT_LABELS) as StructureTextFormat[]).map((f) => (
              <option key={f} value={f}>
                {FORMAT_LABELS[f]}
              </option>
            ))}
          </select>
          <span className="flex-1 text-[10px] text-slate-500">Ctrl+Enter to load</span>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!connected || !text.trim()} onClick={submit}>
            Load structure
          </Button>
        </div>
      </Panel>
    </div>,
    document.body,
  );
}

/** Import buttons: file picker + paste dialog. */
export function ImportButtons() {
  const connected = useCrystalStore((s) => s.connection === "open");
  const fileRef = useRef<HTMLInputElement>(null);
  const [pasteOpen, setPasteOpen] = useState(false);
  return (
    <div className="grid grid-cols-2 gap-2">
      <input
        ref={fileRef}
        type="file"
        accept={STRUCTURE_FILE_ACCEPT}
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void loadFile(f);
          e.target.value = "";
        }}
      />
      <Button disabled={!connected} onClick={() => fileRef.current?.click()} title="CIF, POSCAR / CONTCAR / .vasp, .xyz / .extxyz (or drop a file on the viewport)">
        <FileUp size={13} /> Open file
      </Button>
      <Button disabled={!connected} onClick={() => setPasteOpen(true)} title="Paste CIF / POSCAR / extxyz text">
        <ClipboardPaste size={13} /> Paste
      </Button>
      {pasteOpen && <PasteStructureDialog onClose={() => setPasteOpen(false)} />}
    </div>
  );
}

export function SymmetryExportCard() {
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const connected = useCrystalStore((s) => s.connection === "open");
  const busyStream = useCrystalStore((s) => s.streams.length > 0);
  const [refineCell, setRefineCell] = useState<NonNullable<RefineStructurePayload["cell"]>>("refined");
  const [symprec, setSymprec] = useState("0.01");
  const [choice, setChoice] = useState<ExportChoice>("cif-sym");
  const [cell, setCell] = useState<ExportCell>("as_is");
  const [busy, setBusy] = useState(false);
  const sp = Number.parseFloat(symprec);
  const spOk = Number.isFinite(sp) && sp > 0 && sp <= 1;

  const runExport = async () => {
    const crystal = useCrystalStore.getState().frame.crystal;
    if (!crystal) return;
    setBusy(true);
    try {
      const name = await api.downloadStructure(crystal, exportOptions(choice, cell, spOk ? sp : 0.01));
      notify("info", "Exported", `${name} (${crystal.formula}, ${crystal.spacegroup_symbol})`);
    } catch (e) {
      useCrystalStore.getState().pushToast("error", "Export failed", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <CollapsibleCard id="symmetry-files" title="Symmetry & files">
      <div className="space-y-2.5">
        <div className="grid grid-cols-[1fr_70px] gap-2">
          <Field label="Refine symmetry to">
            <select className={inputCls} value={refineCell} onChange={(e) => setRefineCell(e.target.value as typeof refineCell)}>
              <option value="refined">refined (same cell)</option>
              <option value="conventional">conventional standard</option>
              <option value="primitive">primitive</option>
            </select>
          </Field>
          <Field label="symprec (Å)">
            <input className={cx(inputCls, !spOk && "border-rose-500/60")} value={symprec} inputMode="decimal" onChange={(e) => setSymprec(e.target.value)} />
          </Field>
        </div>
        <Button
          className="w-full"
          disabled={!connected || !hasCrystal || !spOk}
          title={busyStream ? "Refining stops the running stream" : "Snap the structure to its detected space group (spglib)"}
          onClick={() => commands.refine({ symprec: sp, cell: refineCell })}
        >
          <Sparkles size={12} /> Refine symmetry
        </Button>

        <div className="grid grid-cols-2 gap-2 border-t border-white/[0.06] pt-2.5">
          <Field label="Export format">
            <select className={inputCls} value={choice} onChange={(e) => setChoice(e.target.value as ExportChoice)}>
              {EXPORT_CHOICES.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Cell">
            <select className={inputCls} value={choice === "extxyz" ? "as_is" : cell} disabled={choice === "extxyz"} onChange={(e) => setCell(e.target.value as ExportCell)}>
              <option value="as_is">as displayed</option>
              <option value="conventional">conventional</option>
              <option value="primitive">primitive</option>
            </select>
          </Field>
        </div>
        <Button className="w-full" disabled={!hasCrystal || busy} onClick={() => void runExport()}>
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />} Export {EXPORT_CHOICES.find((c) => c.value === choice)?.label}
        </Button>
      </div>
    </CollapsibleCard>
  );
}

/** Drag-and-drop a structure file onto the viewport. */
export function ViewportDropZone({ children }: { children: ReactNode }) {
  const [over, setOver] = useState(false);
  const depth = useRef(0);
  const onEnter = (e: DragEvent) => {
    if (!hasFiles(e.dataTransfer?.types)) return;
    e.preventDefault();
    depth.current++;
    setOver(true);
  };
  const onLeave = (e: DragEvent) => {
    if (!hasFiles(e.dataTransfer?.types)) return;
    depth.current = Math.max(0, depth.current - 1);
    if (depth.current === 0) setOver(false);
  };
  const onDrop = (e: DragEvent) => {
    if (!hasFiles(e.dataTransfer?.types)) return;
    e.preventDefault();
    depth.current = 0;
    setOver(false);
    const file = e.dataTransfer.files?.[0];
    if (!file) return;
    if (useCrystalStore.getState().connection !== "open") {
      notify("warning", "Not connected", "Connect to the backend before loading a structure.");
      return;
    }
    void loadFile(file);
  };
  return (
    <main
      className="relative min-w-0 flex-1 overflow-hidden"
      onDragEnter={onEnter}
      onDragLeave={onLeave}
      onDragOver={(e) => {
        if (hasFiles(e.dataTransfer?.types)) e.preventDefault();
      }}
      onDrop={onDrop}
    >
      {children}
      {over && (
        <div className="pointer-events-none absolute inset-3 z-50 grid place-items-center rounded-2xl border-2 border-dashed border-cyan-400/60 bg-cyan-400/[0.06]">
          <div className="rounded-xl bg-slate-950/85 px-4 py-3 text-center">
            <FileUp size={20} className="mx-auto mb-1 text-cyan-300" />
            <div className="text-[13px] font-semibold text-slate-100">Drop to load the structure</div>
            <div className="font-mono text-[10.5px] text-slate-400">CIF · POSCAR / CONTCAR · extxyz</div>
          </div>
        </div>
      )}
    </main>
  );
}
