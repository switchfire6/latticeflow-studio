/**
 * Left panel (§6.3, §3.5): structure loading (presets, files, paste,
 * Materials Project), the Structure card, the generator form for
 * START_FLOW_GENERATION, symmetry & export, and a sticky action bar at the
 * bottom (relax / shake-test with their settings). The generator speaks
 * stoichiometry: a formula ("SrTiO3", "Ca3(PO4)2") plus formula units per
 * cell fills `composition`; the element-only mode lets the server choose the
 * stoichiometry. Sampler internals (η, steps, seed) live under "Advanced".
 * Phase C2 (§7.5): honest framing ("Generate symmetric candidates": a
 * Wyckoff-site search scored with the session model, streamed as a
 * flow-matching style trajectory, not a learned generative model), a top-k
 * "candidates" control with the ranked GENERATION_CANDIDATES list, and the
 * "From known prototypes" substitution search.
 */
import { ChevronDown, ChevronRight, FlaskConical, Grid3x3, Library, Sparkles, Square, X } from "lucide-react";
import { useState, type KeyboardEvent } from "react";
import { commands } from "../../services/wsClient";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { CrystalSystem, StartFlowGenerationPayload } from "../../types/protocol";
import { cpkHex, elementBySymbol, normalizeSymbol } from "../../utils/cpkColors";
import { compositionText, parseFormula, scaleComposition } from "../../utils/formula";
import { ImportButtons, SymmetryExportCard } from "./ImportExport";
import { MaterialsProjectButton } from "./MaterialsProjectDialog";
import { PeriodicTablePicker } from "./PeriodicTablePicker";
import { StructureActions } from "./StructureActions";
import { StructurePanel } from "./StructurePanel";
import { HistoryPanel } from "./HistoryPanel";
import { LibraryPanel } from "./LibraryPanel";
import { DefectsCard } from "./PhaseCDialogs";
import { GenerationCandidatesList } from "./PhaseC2Dialogs";
import { useUi } from "../../store/uiStore";
import { Button, CollapsibleCard, FormulaText, Slider, Toggle, cx, inputCls } from "./ui";

const FAMILIES: CrystalSystem[] = ["triclinic", "monoclinic", "orthorhombic", "tetragonal", "trigonal", "hexagonal", "cubic"];
type SymmetryChoice = "any" | CrystalSystem | "number";

/** Backend limit on generated cells. */
export const MAX_GENERATED_ATOMS = 64;

export interface GeneratorForm {
  /** "formula": composition from a formula × formula units; "elements": the server picks the stoichiometry. */
  mode: "formula" | "elements";
  formula: string;
  formulaUnits: number;
  elements: string[];
  numAtoms: number;
  symmetry: SymmetryChoice;
  spacegroup: number;
  bulkEnabled: boolean;
  bulkModulus: number;
  guidance: number;
  steps: number;
  seed: string;
  /** Phase C2: number of ranked candidates to return (1–10). */
  topK: number;
}

export const DEFAULT_FORM: GeneratorForm = {
  mode: "formula",
  formula: "SrTiO3",
  formulaUnits: 1,
  elements: ["Sr", "Ti", "O"],
  numAtoms: 5,
  symmetry: "cubic",
  spacegroup: 221,
  bulkEnabled: false,
  bulkModulus: 160,
  guidance: 1.5,
  steps: 60,
  seed: "",
  topK: 1,
};

export interface FormPreview {
  ok: boolean;
  error: string | null;
  /** Cell composition text ("Sr2Ti2O6"), formula mode only. */
  composition: string | null;
  atoms: number;
}

/** What the form will ask for, or why it cannot be sent. */
export function previewForm(f: GeneratorForm): FormPreview {
  if (f.mode === "elements") {
    if (f.elements.length === 0) return { ok: false, error: "Add at least one element.", composition: null, atoms: f.numAtoms };
    return { ok: true, error: null, composition: null, atoms: f.numAtoms };
  }
  const parsed = parseFormula(f.formula);
  if (!parsed.ok) return { ok: false, error: parsed.error, composition: null, atoms: 0 };
  const comp = scaleComposition(parsed.composition, f.formulaUnits);
  const atoms = parsed.atoms * Math.max(1, Math.round(f.formulaUnits));
  if (atoms > MAX_GENERATED_ATOMS) {
    return { ok: false, error: `${atoms} atoms per cell exceeds the generator limit of ${MAX_GENERATED_ATOMS}.`, composition: compositionText(comp), atoms };
  }
  return { ok: true, error: null, composition: compositionText(comp), atoms };
}

/** Largest number of formula units that fits the generator limit. */
export function maxFormulaUnits(formula: string): number {
  const p = parseFormula(formula);
  return p.ok && p.atoms > 0 ? Math.max(1, Math.floor(MAX_GENERATED_ATOMS / p.atoms)) : 1;
}

/** Build the START_FLOW_GENERATION payload from the form (exported for tests). */
export function buildFlowPayload(f: GeneratorForm): StartFlowGenerationPayload {
  const seed = f.seed.trim() === "" ? null : Number.parseInt(f.seed, 10);
  const common = {
    target_spacegroup: f.symmetry === "number" ? Math.min(230, Math.max(1, Math.round(f.spacegroup))) : null,
    target_crystal_system: f.symmetry !== "number" && f.symmetry !== "any" ? f.symmetry : null,
    target_bulk_modulus_gpa: f.bulkEnabled ? f.bulkModulus : null,
    steps: f.steps,
    physics_guidance_scale: f.guidance,
    seed: seed !== null && Number.isFinite(seed) ? seed : null,
    ...(f.topK > 1 ? { top_k: Math.min(10, Math.max(1, Math.round(f.topK))) } : {}),
  };
  if (f.mode === "formula") {
    const parsed = parseFormula(f.formula);
    if (parsed.ok) {
      const composition = scaleComposition(parsed.composition, f.formulaUnits);
      const atoms = Object.values(composition).reduce((a, b) => a + b, 0);
      return { elements: Object.keys(composition), num_atoms: atoms, composition, ...common };
    }
  }
  return { elements: f.elements, num_atoms: f.numAtoms, ...common };
}

function ElementTags({ elements, onChange }: { elements: string[]; onChange(e: string[]): void }) {
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);
  const add = (raw: string) => {
    const syms = raw.split(/[\s,;]+/).map(normalizeSymbol).filter((s): s is string => !!s && (elementBySymbol(s)?.z ?? 99) <= 89);
    if (syms.length) onChange([...new Set([...elements, ...syms])]);
    setDraft("");
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === "," || e.key === " ") {
      e.preventDefault();
      add(draft);
    } else if (e.key === "Backspace" && draft === "" && elements.length) {
      onChange(elements.slice(0, -1));
    }
  };
  const toggle = (s: string) => onChange(elements.includes(s) ? elements.filter((x) => x !== s) : [...elements, s]);
  return (
    <div className="relative">
      <div className="flex min-h-9 flex-wrap items-center gap-1 rounded-ctl border border-line bg-raised p-1.5 focus-within:border-accent">
        {elements.map((s) => (
          <span key={s} className="inline-flex items-center gap-1 rounded-ctl bg-raised py-0.5 pr-1 pl-1.5 font-mono text-xs text-fg">
            <span className="h-2 w-2 rounded-full" style={{ background: cpkHex(elementBySymbol(s)!.z) }} />
            {s}
            <button type="button" className="text-fg-4 hover:text-bad" onClick={() => toggle(s)} aria-label={`remove ${s}`}>
              <X size={11} />
            </button>
          </span>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          onBlur={() => draft && add(draft)}
          placeholder={elements.length ? "" : "Li, Ti, O…"}
          className="min-w-[3rem] flex-1 bg-transparent px-1 font-mono text-sm text-fg outline-none placeholder:text-fg-5"
        />
        <button
          type="button"
          title="Periodic table"
          onClick={() => setOpen((o) => !o)}
          className={cx("rounded-ctl p-1 text-fg-3 hover:bg-hover hover:text-fg", open && "text-fg-3")}
        >
          <Grid3x3 size={14} />
        </button>
      </div>
      {open && (
        <PeriodicTablePicker className="absolute top-full left-0 mt-1" selected={elements} onPick={toggle} onClose={() => setOpen(false)} />
      )}
    </div>
  );
}

export function GeneratorSidebar() {
  return (
    <aside className="flex w-[320px] shrink-0 flex-col border-r border-line-soft bg-surface">
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
        <LoadCard />
        <LibraryPanel />
        <StructurePanel />
        <HistoryPanel />
        <DefectsCard />
        <GeneratorCard />
        <SymmetryExportCard />
      </div>
      {/* Primary actions stay reachable without scrolling (Phase B): sticky action bar. */}
      <div className="max-h-[60%] shrink-0 overflow-y-auto border-t border-line bg-surface p-3" data-action-bar>
        <StructureActions layout="bar" />
      </div>
    </aside>
  );
}

function LoadCard() {
  const connected = useCrystalStore((s) => s.connection === "open");
  const presets = useCrystalStore((s) => s.presets);
  const [presetId, setPresetId] = useState("");
  const selectedPreset = presets.find((p) => p.id === presetId);
  return (
    <CollapsibleCard id="load" title="Load structure" right={<FlaskConical size={12} className="text-fg-4" />}>
      <div className="flex gap-2">
        <select className={inputCls} value={presetId} onChange={(e) => setPresetId(e.target.value)}>
          <option value="">{presets.length ? "Choose a preset…" : "Loading presets…"}</option>
          {presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.formula} · {p.spacegroup_symbol} · {p.expected_stability}
            </option>
          ))}
        </select>
        <Button disabled={!presetId || !connected} onClick={() => commands.loadPreset(presetId)}>
          Load
        </Button>
      </div>
      {selectedPreset && <p className="mt-2 text-xs leading-snug text-fg-3">{selectedPreset.description}</p>}
      <div className="mt-2 space-y-2">
        <ImportButtons />
        <MaterialsProjectButton />
      </div>
    </CollapsibleCard>
  );
}

function GeneratorCard() {
  const [form, setForm] = useState<GeneratorForm>(DEFAULT_FORM);
  const [advanced, setAdvanced] = useState(false);
  const set = (p: Partial<GeneratorForm>) => setForm((f) => ({ ...f, ...p }));
  const flowStream = useCrystalStore((s) => s.streams.some((x) => x.kind === "flow"));
  const connected = useCrystalStore((s) => s.connection === "open");
  const preview = previewForm(form);
  const symText = form.symmetry === "number" ? `SG #${form.spacegroup}` : form.symmetry === "any" ? "any symmetry" : form.symmetry;
  const maxFu = maxFormulaUnits(form.formula);
  const formulaInvalid = form.formula.trim() !== "" && !parseFormula(form.formula).ok;

  const synthesize = () => {
    if (flowStream) {
      commands.stop();
      return;
    }
    if (!preview.ok) {
      useCrystalStore.getState().pushToast("warning", "Check the composition", preview.error ?? "Invalid input.");
      return;
    }
    commands.startFlow(buildFlowPayload(form));
  };

  return (
    <CollapsibleCard id="generator" title="Generate candidates" right={<Sparkles size={12} className="text-fg-4" />}>
      <div className="space-y-3">
        <p className="text-xs leading-snug text-fg-4" data-generator-note>
          Symmetry-constrained search over Wyckoff assignments, scored with the session model and shown as a flow-matching style trajectory (not a learned generative model).
        </p>
        <div className="flex rounded-ctl border border-line bg-raised p-0.5 text-xs">
          {(
            [
              ["formula", "Formula"],
              ["elements", "Elements only"],
            ] as const
          ).map(([m, label]) => (
            <button
              key={m}
              type="button"
              onClick={() => set({ mode: m })}
              title={m === "elements" ? "Let the server choose the stoichiometry" : "Fix the stoichiometry"}
              className={cx("flex-1 rounded-ctl px-2 py-1 transition-colors", form.mode === m ? "bg-accent-soft text-accent-fg" : "text-fg-3 hover:text-fg")}
            >
              {label}
            </button>
          ))}
        </div>

        {form.mode === "formula" ? (
          <div className="grid grid-cols-[1fr_84px] gap-2">
            <label className="block">
              <div className="mb-1 text-xs text-fg-3">Formula</div>
              <input
                className={cx(inputCls, formulaInvalid && "border-bad/60")}
                value={form.formula}
                spellCheck={false}
                placeholder="SrTiO3, Li2MnO3…"
                onChange={(e) => {
                  const formula = e.target.value;
                  const parsed = parseFormula(formula);
                  set(parsed.ok ? { formula, elements: parsed.elements, formulaUnits: Math.min(form.formulaUnits, maxFormulaUnits(formula)) } : { formula });
                }}
              />
            </label>
            <label className="block" title="Formula units per cell (Z)">
              <div className="mb-1 text-xs text-fg-3">f.u. / cell</div>
              <input
                type="number"
                min={1}
                max={maxFu}
                className={inputCls}
                value={form.formulaUnits}
                onChange={(e) => set({ formulaUnits: Math.max(1, Math.min(maxFu, Math.round(Number(e.target.value) || 1))) })}
              />
            </label>
          </div>
        ) : (
          <>
            <div>
              <div className="mb-1 text-xs text-fg-3">Chemical system</div>
              <ElementTags elements={form.elements} onChange={(elements) => set({ elements })} />
            </div>
            <Slider label="Atoms / cell" value={form.numAtoms} min={2} max={MAX_GENERATED_ATOMS} onChange={(numAtoms) => set({ numAtoms })} />
          </>
        )}

        <div>
          <div className="mb-1 text-xs text-fg-3">Symmetry target</div>
          <div className="flex gap-2">
            <select className={inputCls} value={form.symmetry} onChange={(e) => set({ symmetry: e.target.value as SymmetryChoice })}>
              <option value="any">Any (server picks)</option>
              {FAMILIES.map((f) => (
                <option key={f} value={f}>
                  {f[0]!.toUpperCase() + f.slice(1)}
                </option>
              ))}
              <option value="number">Space group #…</option>
            </select>
            {form.symmetry === "number" && (
              <input
                type="number"
                min={1}
                max={230}
                className={cx(inputCls, "w-20")}
                value={form.spacegroup}
                onChange={(e) => set({ spacegroup: Number(e.target.value) })}
              />
            )}
          </div>
        </div>
        <Slider
          label="Target bulk modulus K"
          value={form.bulkModulus}
          min={10}
          max={400}
          step={5}
          disabled={!form.bulkEnabled}
          onChange={(bulkModulus) => set({ bulkModulus })}
          format={(v) => (form.bulkEnabled ? `${v} GPa` : "off")}
          right={<Toggle label={form.bulkEnabled ? "on" : "off"} checked={form.bulkEnabled} onChange={(bulkEnabled) => set({ bulkEnabled })} />}
        />

        <div>
          <button
            type="button"
            aria-expanded={advanced}
            onClick={() => setAdvanced((a) => !a)}
            className="flex items-center gap-1 text-xs font-medium text-fg-3 hover:text-fg"
          >
            {advanced ? <ChevronDown size={11} /> : <ChevronRight size={11} />} Advanced (sampler)
          </button>
          {advanced && (
            <div className="mt-2 space-y-3" data-advanced>
              <Slider
                label="Physics guidance η"
                value={form.guidance}
                min={0}
                max={5}
                step={0.1}
                onChange={(guidance) => set({ guidance })}
                format={(v) => v.toFixed(1)}
              />
              <Slider label="Integration steps" value={form.steps} min={10} max={200} step={5} onChange={(steps) => set({ steps })} />
              <label className="block">
                <div className="mb-1 text-xs text-fg-3">Seed (optional)</div>
                <input
                  className={inputCls}
                  inputMode="numeric"
                  placeholder="random"
                  value={form.seed}
                  onChange={(e) => set({ seed: e.target.value.replace(/[^\d]/g, "") })}
                />
              </label>
            </div>
          )}
        </div>

        <div className={cx("rounded-ctl bg-raised px-2 py-1 font-mono text-xs", preview.ok ? "text-fg-3" : "text-bad")} data-preview>
          {preview.ok ? (
            <>
              {"→ "}
              {preview.composition ? (
                <span className="text-fg">
                  <FormulaText formula={preview.composition} />
                </span>
              ) : (
                <span className="text-fg">{form.elements.join("–")} (server stoichiometry)</span>
              )}
              {` · ${preview.atoms} atoms · ${symText}`}
            </>
          ) : (
            preview.error
          )}
        </div>

        <Slider
          label="Candidates"
          value={form.topK}
          min={1}
          max={10}
          onChange={(topK) => set({ topK })}
          format={(v) => (v === 1 ? "best only" : `top ${v}`)}
        />

        <Button variant={flowStream ? "danger" : "primary"} className="w-full py-2 text-md" disabled={!connected || (!flowStream && !preview.ok)} onClick={synthesize}>
          {flowStream ? (
            <>
              <Square size={13} /> Stop
            </>
          ) : (
            <>
              <Sparkles size={14} /> Generate symmetric candidates
            </>
          )}
        </Button>
        <GenerationCandidatesList />
        <Button className="w-full" disabled={!connected} onClick={() => useUi.getState().openDialog("prototype")} title="Ionic substitution into known Materials Project prototypes (PROTOTYPE_SEARCH)">
          <Library size={12} /> From known prototypes…
        </Button>
      </div>
    </CollapsibleCard>
  );
}
