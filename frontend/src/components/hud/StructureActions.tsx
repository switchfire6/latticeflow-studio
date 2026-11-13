/**
 * Relaxation and phonon actions with their settings (§3.4):
 *  - FIRE Relax (Stop while running) + a settings panel (fmax, max steps,
 *    relax cell, keep symmetry, target pressure, stress tolerance, optimizer)
 *    and "Continue relaxation" after an unconverged run;
 *  - the phonon shake-test + settings (supercell, auto-suggested so every
 *    supercell vector is ≥ 10 Å within ~500 atoms, displacement, auto DOS),
 *    warning with the same verdict as the RELAXED badge (§3.2).
 * Settings are inline panels (the sidebar scrolls, so floating popovers would clip).
 */
import { Play, RotateCcw, Settings2, Square, Waves, Wind } from "lucide-react";
import { useState } from "react";
import { commands } from "../../services/wsClient";
import { OPTIMIZERS, badgeCriteria, useSettings, type Optimizer } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import { latticeLengths, phononWarning, relaxationVerdict, suggestSupercell, SUPERCELL_MAX_ATOMS, SUPERCELL_MIN_LENGTH } from "../../utils/structureInfo";
import { Button, NumberField, Segmented, cx } from "./ui";

const PHONON_MAX_ATOMS = 64;

function GearButton({ open, onClick, title }: { open: boolean; onClick(): void; title: string }) {
  return (
    <button
      type="button"
      title={title}
      aria-expanded={open}
      onClick={onClick}
      className={cx(
        "grid w-8 shrink-0 place-items-center rounded-lg border border-white/10 bg-white/[0.03] text-slate-400 hover:border-cyan-400/40 hover:text-cyan-200",
        open && "border-cyan-400/50 text-cyan-200",
      )}
    >
      <Settings2 size={13} />
    </button>
  );
}

function CheckRow({ label, checked, onChange, title }: { label: string; checked: boolean; onChange(v: boolean): void; title?: string }) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-2 text-[11px] text-slate-400" title={title}>
      <span>{label}</span>
      <input type="checkbox" className="accent-cyan-400" checked={checked} onChange={(e) => onChange(e.target.checked)} />
    </label>
  );
}

export function RelaxSettingsPanel() {
  const r = useSettings((s) => s.relax);
  const set = useSettings((s) => s.setRelax);
  return (
    <div className="space-y-1.5 rounded-lg border border-white/[0.06] bg-black/20 p-2" data-relax-settings>
      <NumberField label="fmax" unit="eV/Å" value={r.fmax} min={0.0001} max={1} onCommit={(fmax) => set({ fmax })} title="Force convergence threshold" />
      <NumberField label="max steps" value={r.maxSteps} integer min={1} max={5000} onCommit={(maxSteps) => set({ maxSteps })} />
      <CheckRow label="relax cell" checked={r.relaxCell} onChange={(relaxCell) => set({ relaxCell })} title="Relax lattice vectors too (FrechetCellFilter)" />
      <CheckRow label="keep symmetry" checked={r.fixSymmetry} onChange={(fixSymmetry) => set({ fixSymmetry })} title="ASE FixSymmetry: the space group cannot change" />
      <NumberField
        label="target pressure"
        unit="GPa"
        value={r.pressureGpa}
        min={-50}
        max={500}
        disabled={!r.relaxCell}
        onCommit={(pressureGpa) => set({ pressureGpa })}
        title="External hydrostatic pressure (+ = compressive)"
      />
      <NumberField
        label="stress tolerance"
        unit="GPa"
        value={r.stressTolGpa}
        min={0.001}
        max={10}
        disabled={!r.relaxCell}
        onCommit={(stressTolGpa) => set({ stressTolGpa })}
        title="Converged also needs max |σ_ij + P δ_ij| below this"
      />
      <div className="flex items-center justify-between text-[11px] text-slate-400">
        <span>optimizer</span>
        <Segmented<Optimizer> value={r.optimizer} options={OPTIMIZERS.map((o) => ({ value: o, label: o }))} onChange={(optimizer) => set({ optimizer })} />
      </div>
      <button type="button" className="flex items-center gap-1 text-[10px] text-slate-500 hover:text-slate-300" onClick={() => useSettings.getState().resetRelax()}>
        <RotateCcw size={10} /> defaults
      </button>
    </div>
  );
}

export function PhononSettingsPanel() {
  const p = useSettings((s) => s.phonon);
  const set = useSettings((s) => s.setPhonon);
  const matrix = useCrystalStore((s) => s.crystal?.lattice.matrix ?? null);
  const nAtoms = useCrystalStore((s) => s.crystal?.atoms.length ?? 0);
  const lengths = matrix ? latticeLengths(matrix) : null;
  const auto = lengths ? suggestSupercell(lengths, nAtoms) : ([2, 2, 2] as [number, number, number]);
  const dims = p.supercell ?? auto;
  const superAtoms = nAtoms * dims[0] * dims[1] * dims[2];
  return (
    <div className="space-y-1.5 rounded-lg border border-white/[0.06] bg-black/20 p-2" data-phonon-settings>
      <div className="flex items-center justify-between text-[11px] text-slate-400">
        <span>supercell</span>
        <Segmented
          value={p.supercell ? "manual" : "auto"}
          options={[
            { value: "auto", label: "auto", title: `Smallest n_i with n_i·|a_i| ≥ ${SUPERCELL_MIN_LENGTH} Å, ≤ ${SUPERCELL_MAX_ATOMS} atoms` },
            { value: "manual", label: "manual" },
          ]}
          onChange={(v) => set({ supercell: v === "auto" ? null : [...auto] })}
        />
      </div>
      <div className="flex items-center justify-end gap-1">
        {[0, 1, 2].map((i) => (
          <input
            key={i}
            aria-label={`supercell n${i + 1}`}
            disabled={!p.supercell}
            inputMode="numeric"
            className="w-10 rounded border border-white/10 bg-slate-900/80 px-1 py-0.5 text-center font-mono text-[11px] text-slate-100 outline-none focus:border-cyan-400/50 disabled:opacity-60"
            value={dims[i]}
            onChange={(e) => {
              const v = Number.parseInt(e.target.value, 10);
              if (!Number.isFinite(v) || !p.supercell) return;
              const next = [...p.supercell] as [number, number, number];
              next[i] = Math.min(8, Math.max(1, v));
              set({ supercell: next });
            }}
          />
        ))}
        <span className="ml-1 font-mono text-[10px] text-slate-500">
          {superAtoms} at
          {lengths && ` · ≥${Math.min(...lengths.map((L, i) => L * dims[i]!)).toFixed(1)} Å`}
        </span>
      </div>
      <NumberField label="displacement Δu" unit="Å" value={p.displacement} min={0.001} max={0.1} onCommit={(displacement) => set({ displacement })} />
      <CheckRow label="DOS + thermodynamics after each run" checked={p.autoDos} onChange={(autoDos) => set({ autoDos })} />
      <button type="button" className="flex items-center gap-1 text-[10px] text-slate-500 hover:text-slate-300" onClick={() => useSettings.getState().resetPhonon()}>
        <RotateCcw size={10} /> defaults
      </button>
    </div>
  );
}

/** FIRE relax + phonon shake-test buttons with settings (sidebar and the selected-atom toolbar). */
export function StructureActions({ compact }: { compact?: boolean }) {
  const connected = useCrystalStore((s) => s.connection === "open");
  const relaxing = useCrystalStore((s) => s.streams.some((x) => x.kind === "relaxation"));
  const phononRunning = useCrystalStore((s) => s.streams.some((x) => x.kind === "phonon"));
  const unconverged = useCrystalStore((s) => s.relaxUnconverged);
  const crystal = useCrystalStore((s) => s.crystal);
  const relax = useSettings((s) => s.relax);
  const [relaxOpen, setRelaxOpen] = useState(false);
  const [phononOpen, setPhononOpen] = useState(false);
  const nAtoms = crystal?.atoms.length ?? 0;
  const warning = crystal ? phononWarning(relaxationVerdict(crystal, badgeCriteria(relax))) : null;
  const tooBig = nAtoms > PHONON_MAX_ATOMS;
  return (
    <div className="grid gap-2">
      <div className="flex gap-1.5">
        <Button variant={relaxing ? "danger" : "ghost"} className="flex-1" disabled={!connected} onClick={() => (relaxing ? commands.stop() : commands.relax())}>
          {relaxing ? <Square size={12} /> : <Wind size={13} />}
          {relaxing ? "Stop Relaxation" : `${relax.optimizer} Relax${relax.relaxCell ? " (cell)" : ""}`}
        </Button>
        <GearButton open={relaxOpen} onClick={() => setRelaxOpen((o) => !o)} title="Relaxation settings" />
      </div>
      {relaxOpen && <RelaxSettingsPanel />}
      {unconverged && !relaxing && (
        <Button variant="ghost" className="border-amber-400/40 text-amber-200" disabled={!connected} onClick={() => commands.relax()} title={`Stopped at ${unconverged.steps} steps, max |F| = ${unconverged.maxForce.toFixed(3)} eV/Å`}>
          <Play size={12} /> Continue relaxation
        </Button>
      )}
      <div className="flex gap-1.5">
        <Button
          variant={phononRunning ? "danger" : warning ? "warning" : "ghost"}
          className="flex-1"
          disabled={!connected || tooBig}
          title={tooBig ? `The shake-test supports at most ${PHONON_MAX_ATOMS} atoms per cell` : (warning ?? "Finite-displacement phonons (settings: gear)")}
          onClick={() => (phononRunning ? commands.stop() : commands.runPhonons())}
        >
          {phononRunning ? <Square size={12} /> : <Waves size={13} />}
          {phononRunning ? "Stop Shake-Test" : "Run Phonon Shake-Test"}
        </Button>
        <GearButton open={phononOpen} onClick={() => setPhononOpen((o) => !o)} title="Phonon settings" />
      </div>
      {phononOpen && <PhononSettingsPanel />}
      {warning && !compact && (
        <p className="text-[10.5px] leading-snug text-amber-300/80" data-phonon-warning>
          {warning[0]!.toUpperCase() + warning.slice(1)}.
        </p>
      )}
    </div>
  );
}
