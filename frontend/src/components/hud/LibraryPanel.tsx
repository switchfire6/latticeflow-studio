/**
 * Library card of the left sidebar (protocol §6): project switcher with
 * create / rename / delete, "Save current structure", the active project's
 * structures (lineage-indented by parent_id; click = open), auto-save, and
 * bundle export / import. The results table and scatter live in the
 * "results" dock pane.
 */
import { Check, Download, FolderPlus, Loader2, Pencil, Save, Table2, Trash2, Upload, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { showPane } from "../../services/dockActions";
import {
  createProject,
  deleteProject,
  exportBundle,
  importBundle,
  openStructure,
  refreshStructures,
  renameProject,
  saveCurrent,
  selectProject,
} from "../../services/libraryActions";
import { useLibrary } from "../../store/libraryStore";
import { useCrystalStore } from "../../store/useCrystalStore";
import { lineageOrder } from "../../utils/library";
import { Button, CollapsibleCard, FormulaText, cx, inputCls } from "./ui";

const iconBtn = "grid h-7 w-7 shrink-0 place-items-center rounded-ctl text-fg-3 hover:bg-hover hover:text-fg disabled:opacity-30";
const textBtn = "flex h-6 shrink-0 items-center gap-1 rounded-ctl px-1.5 text-2xs text-fg-3 hover:bg-hover hover:text-fg disabled:opacity-30";

function ProjectBar() {
  const projects = useLibrary((s) => s.projects);
  const activeId = useLibrary((s) => s.activeProjectId);
  const active = projects.find((p) => p.id === activeId) ?? null;
  const [mode, setMode] = useState<"none" | "create" | "rename">("none");
  const [draft, setDraft] = useState("");
  const commit = () => {
    const name = draft.trim();
    if (!name) return;
    if (mode === "create") void createProject(name);
    if (mode === "rename" && active) void renameProject(active.id, name);
    setMode("none");
  };
  if (mode !== "none") {
    return (
      <div className="flex gap-1">
        <input
          autoFocus
          className={inputCls}
          placeholder={mode === "create" ? "New project name" : "Project name"}
          value={draft}
          maxLength={200}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") setMode("none");
          }}
        />
        <button type="button" className={iconBtn} title="OK" onClick={commit} disabled={!draft.trim()}>
          <Check size={13} />
        </button>
        <button type="button" className={iconBtn} title="Cancel" onClick={() => setMode("none")}>
          <X size={13} />
        </button>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-0.5">
      <select
        className={cx(inputCls, "mr-1")}
        value={activeId ?? ""}
        onChange={(e) => void selectProject(Number(e.target.value))}
        aria-label="Project"
        disabled={!projects.length}
      >
        {!projects.length && <option value="">no library</option>}
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name} ({p.n_structures})
          </option>
        ))}
      </select>
      <button type="button" className={iconBtn} title="New project" onClick={() => (setDraft(""), setMode("create"))} disabled={!projects.length}>
        <FolderPlus size={13} />
      </button>
      <button type="button" className={iconBtn} title="Rename project" onClick={() => (setDraft(active?.name ?? ""), setMode("rename"))} disabled={!active}>
        <Pencil size={12} />
      </button>
      <button
        type="button"
        className={iconBtn}
        title={active?.is_default ? "The default project cannot be deleted" : "Delete project (its structures and results)"}
        disabled={!active || active.is_default}
        onClick={() => {
          if (active && window.confirm(`Delete project “${active.name}” with its ${active.n_structures} structures and their results?`)) void deleteProject(active.id);
        }}
      >
        <Trash2 size={12} />
      </button>
    </div>
  );
}

function StructureList() {
  const rows = useLibrary((s) => s.structures);
  const loading = useLibrary((s) => s.structuresLoading);
  const link = useLibrary((s) => s.link);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const connected = useCrystalStore((s) => s.connection === "open");
  const items = useMemo(() => lineageOrder(rows), [rows]);
  const currentId = link && link.stateVersion === stateVersion ? link.structureId : null;
  if (!rows.length) {
    return <p className="py-1 text-xs text-fg-4">{loading ? "Loading…" : "No structures yet. Save the current one, or run a calculation with auto-save on."}</p>;
  }
  return (
    <ul className="max-h-[232px] overflow-y-auto rounded-ctl border border-line-soft bg-field py-0.5" data-library-list>
      {items.map(({ row, depth }) => {
        const n = Object.keys(row.latest_result_ids).length;
        const cur = row.id === currentId;
        return (
          <li key={row.id}>
            <button
              type="button"
              disabled={!connected}
              onClick={() => void openStructure(row.id)}
              title={`Open #${row.id} (${row.n_atoms} atoms)${n ? ` with its stored ${Object.keys(row.latest_result_ids).join(", ")}` : ""}`}
              className={cx("flex w-full items-baseline gap-1.5 py-1 pr-2 text-left", cur ? "bg-accent-soft" : "hover:bg-hover")}
              style={{ paddingLeft: 8 + Math.min(depth, 5) * 12 }}
              data-structure-id={row.id}
            >
              {depth > 0 && <span className="text-2xs text-fg-5">└</span>}
              <span className={cx("min-w-0 flex-1 truncate text-xs", cur ? "text-accent-fg" : "text-fg")}>
                {row.name === row.formula ? <FormulaText formula={row.formula} /> : row.name}
                {row.name !== row.formula && (
                  <span className="ml-1 text-fg-4">
                    <FormulaText formula={row.formula} />
                  </span>
                )}
              </span>
              <span className="shrink-0 text-2xs text-fg-3">{row.spacegroup_symbol}</span>
              <span className="w-[62px] shrink-0 truncate text-right text-2xs text-fg-4" title={row.origin}>
                {row.origin}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export function LibraryPanel() {
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const formula = useCrystalStore((s) => s.crystal?.formula ?? "");
  const activeId = useLibrary((s) => s.activeProjectId);
  const autoSave = useLibrary((s) => s.autoSave);
  const lastSave = useLibrary((s) => s.lastSave);
  const error = useLibrary((s) => s.error);
  const n = useLibrary((s) => s.structures.length);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (activeId !== null) void refreshStructures();
  }, [activeId]);

  const save = async () => {
    setSaving(true);
    await saveCurrent(name);
    setSaving(false);
    setName("");
  };

  return (
    <CollapsibleCard
      id="library"
      title="Library"
      right={
        <button type="button" className="flex items-center gap-1 text-2xs text-fg-3 hover:text-fg" title="Results table and scatter of this project (dock)" onClick={() => showPane("results")}>
          <Table2 size={12} /> results
        </button>
      }
    >
      <div className="space-y-2" data-library>
        <ProjectBar />
        <div className="flex gap-1.5">
          <input
            className={inputCls}
            placeholder={formula ? `Name (default ${formula})` : "Name"}
            value={name}
            maxLength={200}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && hasCrystal && void save()}
          />
          <Button className="shrink-0 px-2" disabled={!hasCrystal || activeId === null || saving} onClick={() => void save()} title="Save the current structure in this project">
            {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />} Save current
          </Button>
        </div>
        {(lastSave || error) && (
          <p className={cx("truncate text-2xs", error || lastSave?.error ? "text-warn" : "text-fg-4")} title={error ?? lastSave?.text}>
            {error ?? lastSave?.text}
          </p>
        )}
        <div className="flex items-center justify-between text-2xs font-semibold tracking-[0.06em] text-fg-3 uppercase">
          <span>Structures {n > 0 && <span className="font-mono font-normal normal-case text-fg-4">{n}</span>}</span>
        </div>
        <StructureList />
        <div className="flex items-center gap-1">
          <label className="flex flex-1 cursor-pointer items-center gap-1.5 text-xs text-fg-3" title="Store every finished relaxation, phonon, elastic, EOS, hull and diffraction result of the current structure">
            <input type="checkbox" className="accent-[var(--color-accent)]" checked={autoSave} onChange={(e) => useLibrary.getState().setAutoSave(e.target.checked)} />
            Auto-save results
          </label>
          <input
            ref={fileRef}
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void importBundle(f);
              e.target.value = "";
            }}
          />
          <button type="button" className={textBtn} title="Export this project as a bundle file" disabled={activeId === null} onClick={() => activeId !== null && void exportBundle(activeId)}>
            <Download size={11} /> export
          </button>
          <button type="button" className={textBtn} title="Import a project bundle (creates a new project)" onClick={() => fileRef.current?.click()}>
            <Upload size={11} /> import
          </button>
        </div>
      </div>
    </CollapsibleCard>
  );
}
