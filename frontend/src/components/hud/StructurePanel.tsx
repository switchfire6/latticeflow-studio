/**
 * Structure card (§3.1–3.3): cell parameters, V, V/atom, density, Z, symmetry
 * (space group, crystal system, symprec), pressure + Voigt stress with the
 * relaxation verdict, bulk modulus on demand, the lattice change since the
 * start of the latest relaxation / follow, and the site table (element,
 * Wyckoff, fractional coordinates, |F|). `StructureInfoView` is pure
 * (server-rendered in tests).
 */
import { Gauge, Loader2, Radar, Sigma } from "lucide-react";
import { useState, type ReactNode } from "react";
import { showPane } from "../../services/dockActions";
import { commands } from "../../services/wsClient";
import { badgeCriteria, useSettings } from "../../store/settings";
import { useCrystalStore, type EosState } from "../../store/useCrystalStore";
import type { CrystalStatePayload } from "../../types/protocol";
import { cpkHex } from "../../utils/cpkColors";
import {
  crystalSystemOf,
  densityOf,
  formulaUnitsOf,
  latticeDelta,
  relaxationVerdict,
  type LatticeReference,
  type RelaxationCriteria,
} from "../../utils/structureInfo";
import { formatPressure, type PressureUnit } from "../../utils/units";
import { MpMatchSection } from "./DiscoveryDialogs";
import { RelaxationBadge } from "./TelemetryHeader";
import { Button, CollapsibleCard, FallbackTag, cx } from "./ui";

function Cell({ label, value, title }: { label: ReactNode; value: ReactNode; title?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-1 rounded-ctl bg-raised px-1.5 py-0.5" title={title}>
      <span className="text-2xs text-fg-4">{label}</span>
      <span className="font-mono text-xs text-fg tabular-nums">{value}</span>
    </div>
  );
}

function pctText(v: number | null): string {
  if (v === null) return "—";
  const s = Math.abs(v) < 0.005 ? "0.00" : Math.abs(v).toFixed(2);
  return `${v < -0.005 ? "−" : v > 0.005 ? "+" : "±"}${s} %`;
}

const VOIGT_INDEX = [
  [0, 5, 4],
  [5, 1, 3],
  [4, 3, 2],
];

export interface StructureInfoProps {
  crystal: CrystalStatePayload;
  criteria: RelaxationCriteria;
  pressureUnit: PressureUnit;
  latticeRef: LatticeReference | null;
  eos: EosState | null;
  eosStale: boolean;
  eosPending: boolean;
  canCompute: boolean;
  onComputeK(): void;
  selectedAtom?: number | null;
  onSelectAtom?(i: number): void;
}

export function StructureInfoView(p: StructureInfoProps) {
  const c = p.crystal;
  const L = c.lattice;
  const n = c.atoms.length;
  const verdict = relaxationVerdict(c, p.criteria);
  const density = densityOf(c);
  const system = crystalSystemOf(c);
  const z = formulaUnitsOf(c);
  const delta = p.latticeRef ? latticeDelta(p.latticeRef, { lattice: L, nAtoms: n }) : null;
  const [sitesOpen, setSitesOpen] = useState(n <= 24);
  const k = c.bulk_modulus_gpa;
  const pu = p.pressureUnit;

  return (
    <div className="space-y-2.5 text-xs" data-structure-info>
      <div>
        <div className="mb-1 text-xs font-medium text-fg-3">Cell</div>
        <div className="grid grid-cols-3 gap-1">
          <Cell label="a" value={`${L.a.toFixed(4)} Å`} />
          <Cell label="b" value={`${L.b.toFixed(4)} Å`} />
          <Cell label="c" value={`${L.c.toFixed(4)} Å`} />
          <Cell label="α" value={`${L.alpha.toFixed(2)}°`} />
          <Cell label="β" value={`${L.beta.toFixed(2)}°`} />
          <Cell label="γ" value={`${L.gamma.toFixed(2)}°`} />
        </div>
        <div className="mt-1 grid grid-cols-2 gap-1">
          <Cell label="V" value={`${L.volume.toFixed(3)} Å³`} />
          <Cell label="V/atom" value={`${(L.volume / Math.max(1, n)).toFixed(3)} Å³`} />
          <Cell label="ρ" value={density !== null ? `${density.toFixed(3)} g/cm³` : "—"} title="Density from atomic masses / V" />
          <Cell label="Z · N" value={`${z} · ${n}`} title="Formula units per cell · atoms per cell" />
        </div>
      </div>

      <div>
        <div className="mb-1 text-xs font-medium text-fg-3">Symmetry</div>
        <div className="grid grid-cols-2 gap-1">
          <div className="col-span-2">
            <Cell label="space group" value={`${c.spacegroup_symbol} (#${c.spacegroup_number})`} />
          </div>
          <Cell label="system" value={system ?? "—"} />
          <Cell label="symprec" value={c.symprec != null ? `${c.symprec} Å` : "—"} title="spglib tolerance used for the space group and Wyckoff sites" />
        </div>
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between text-xs font-medium text-fg-3">
          <span>Stress</span>
          <RelaxationBadge verdict={verdict} />
        </div>
        <div className="space-y-1">
          <div className="flex items-baseline justify-between rounded-ctl bg-raised px-1.5 py-0.5">
            <span className="text-2xs text-fg-4">pressure P = −tr σ/3 (+ = compressed)</span>
            <span className={cx("font-mono text-sm tabular-nums", verdict.state === "stressed" ? "text-warn" : "text-fg")} data-pressure>
              {verdict.pressureGpa !== null ? formatPressure(verdict.pressureGpa, pu) : "—"}
            </span>
          </div>
          <table className="w-full font-mono text-2xs text-fg-2 tabular-nums" title="Stress tensor σ_ij (ASE sign: + = tensile)">
            <tbody>
              {VOIGT_INDEX.map((row, i) => (
                <tr key={i}>
                  {row.map((vi, j) => (
                    <td key={j} className={cx("px-1 text-right", i === j ? "text-fg" : "text-fg-3")}>
                      {formatPressure(c.stress_gpa[vi] ?? NaN, pu, 2, { sign: false }).replace(/ (GPa|kbar)$/, "")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-1 font-mono text-2xs text-fg-4">
          σ in {pu}; max |σ_ij + P_ext δ_ij| = {verdict.maxStressDeviation?.toFixed(3) ?? "—"} GPa (tol {p.criteria.stressTolGpa}, P_ext {p.criteria.targetPressureGpa} GPa)
        </div>
      </div>

      <div className="flex items-center gap-2">
        <div className="flex-1">
          <Cell
            label="bulk modulus K"
            value={
              <span className="flex items-center gap-1">
                {k != null ? `${k.toFixed(1)} GPa` : "—"}
                {p.eos && p.eosStale && <span className="text-2xs text-warn/80">(stale)</span>}
              </span>
            }
            title="Clamped-ion K from an E(V) fit at the current volume"
          />
        </div>
        <Button className="px-2 py-1 text-xs" disabled={!p.canCompute || p.eosPending} onClick={p.onComputeK} title="Compute K and the equation of state (opens the EOS pane)">
          {p.eosPending ? <Loader2 size={11} className="animate-spin" /> : <Gauge size={11} />} Compute K
        </Button>
        {k != null && <FallbackTag compact />}
      </div>

      {delta && (
        <div data-lattice-delta>
          <div className="mb-1 text-xs font-medium text-fg-3">
            Change since the {p.latticeRef?.kind === "follow" ? "parent (follow)" : "relaxation start"}
          </div>
          <div className="grid grid-cols-2 gap-1">
            <Cell label="Δa" value={pctText(delta.a)} />
            <Cell label="Δb" value={pctText(delta.b)} />
            <Cell label="Δc" value={pctText(delta.c)} />
            <Cell label={delta.volume !== null ? "ΔV" : "ΔV/at"} value={pctText(delta.volume ?? delta.volumePerAtom)} />
          </div>
          {delta.volume === null && <div className="mt-0.5 text-2xs text-fg-4">Supercell: only the volume per atom is comparable.</div>}
        </div>
      )}

      <div>
        <button
          type="button"
          className="mb-1 flex w-full items-center justify-between text-xs font-medium text-fg-3 hover:text-fg-2"
          onClick={() => setSitesOpen((o) => !o)}
          aria-expanded={sitesOpen}
        >
          <span>Sites ({n})</span>
          <span className="normal-case tracking-normal">{sitesOpen ? "hide" : "show"}</span>
        </button>
        {sitesOpen && (
          <div className="max-h-56 overflow-y-auto rounded-ctl border border-line-soft bg-field">
            <table className="w-full font-mono text-2xs tabular-nums" data-site-table>
              <thead className="sticky top-0 bg-surface text-fg-4">
                <tr>
                  <th className="px-1 py-0.5 text-right font-normal">#</th>
                  <th className="px-1 text-left font-normal">el</th>
                  <th className="px-1 text-left font-normal" title="Wyckoff multiplicity + letter">Wy</th>
                  <th className="px-1 text-right font-normal">x</th>
                  <th className="px-1 text-right font-normal">y</th>
                  <th className="px-1 text-right font-normal">z</th>
                  <th className="px-1 text-right font-normal" title="|F| (eV/Å)">|F|</th>
                </tr>
              </thead>
              <tbody>
                {c.atoms.map((a) => (
                  <tr
                    key={a.index}
                    onClick={() => p.onSelectAtom?.(a.index)}
                    className={cx("cursor-pointer text-fg-2 hover:bg-hover", p.selectedAtom === a.index && "bg-accent-soft text-accent-fg")}
                  >
                    <td className="px-1 text-right text-fg-4">{a.index}</td>
                    <td className="px-1">
                      <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full" style={{ background: cpkHex(a.atomic_number) }} />
                      {a.element}
                    </td>
                    <td className="px-1 text-fg-3">{a.wyckoff ?? "—"}</td>
                    {a.frac_coords.map((x, i) => (
                      <td key={i} className="px-1 text-right">
                        {x.toFixed(4)}
                      </td>
                    ))}
                    <td className={cx("px-1 text-right", a.force_norm >= p.criteria.fmax ? "text-warn" : "text-fg-4")}>{a.force_norm.toFixed(3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

/** Phase B: elastic tensor (§5.5) and simulated diffraction (§5.6) entry points. */
function PropertyActions({ connected }: { connected: boolean }) {
  const elasticRunning = useCrystalStore((s) => s.streams.some((x) => x.command === "COMPUTE_ELASTIC_TENSOR"));
  const busy = useCrystalStore((s) => s.streams.length > 0);
  return (
    <div className="mt-2 grid grid-cols-2 gap-1.5 border-t border-line-soft pt-2">
      <Button
        className="px-2 py-1 text-xs"
        disabled={!connected || (busy && !elasticRunning)}
        title="Elastic tensor C_ij, Born stability, VRH moduli (opens the elastic pane; settings there)"
        onClick={() => {
          // First time: compute at once; with a result, open the pane (recompute there).
          if (!elasticRunning && !useCrystalStore.getState().elastic) commands.computeElastic();
          showPane("elastic");
        }}
      >
        {elasticRunning ? <Loader2 size={11} className="animate-spin" /> : <Sigma size={11} />} Elastic tensor
      </Button>
      <Button className="px-2 py-1 text-xs" onClick={() => showPane("xrd")} title="Simulated X-ray / neutron powder pattern with an experimental overlay">
        <Radar size={11} /> Diffraction (XRD)
      </Button>
    </div>
  );
}

export function StructurePanel() {
  const crystal = useCrystalStore((s) => s.crystal);
  const latticeRef = useCrystalStore((s) => s.latticeRef);
  const eos = useCrystalStore((s) => s.eos);
  const eosPending = useCrystalStore((s) => s.eosPending);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const selectedAtom = useCrystalStore((s) => s.selectedAtom);
  const connected = useCrystalStore((s) => s.connection === "open");
  const relax = useSettings((s) => s.relax);
  const pressureUnit = useSettings((s) => s.units.pressure);
  return (
    <CollapsibleCard id="structure-info" title="Structure">
      {crystal ? (
        <StructureInfoView
          crystal={crystal}
          criteria={badgeCriteria(relax)}
          pressureUnit={pressureUnit}
          latticeRef={latticeRef}
          eos={eos}
          eosStale={!!eos && eos.stateVersion !== stateVersion}
          eosPending={eosPending}
          canCompute={connected}
          onComputeK={() => {
            if (commands.computeBulkModulus()) showPane("eos");
          }}
          selectedAtom={selectedAtom}
          onSelectAtom={(i) => useCrystalStore.getState().selectAtom(i)}
        />
      ) : null}
      {crystal ? (
        <>
          <PropertyActions connected={connected} />
          <MpMatchSection />
        </>
      ) : (
        <p className="text-xs text-fg-4">No structure loaded.</p>
      )}
    </CollapsibleCard>
  );
}
