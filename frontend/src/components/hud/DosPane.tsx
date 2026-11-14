/**
 * Phonon DOS & thermodynamics dock pane (§5.1): total and element-projected
 * DOS (frequency in the user's unit), F(T), S(T) and C_v(T) with the
 * zero-point energy, and a warning when imaginary modes are excluded from the
 * thermal sums. Requested automatically after each shake-test (setting) or
 * with the button here.
 */
import { CHART_COLORS } from "../charts/useCanvasChart";
import { AlertTriangle, BarChart3, Loader2, Zap } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../../services/apiClient";
import { errorText } from "../../services/libraryActions";
import { commands } from "../../services/wsClient";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { EphProxyDescriptors, MetallicityResponse, MetallicityVerdict, PhononDosData } from "../../types/protocol";
import { SERIES_COLORS, dosInUnit, interpAt } from "../../utils/chartData";
import { LAMBDA_RANGE, MU_STAR_GRID, MU_STAR_RANGE, allenDynesTc, tcCurve } from "../../utils/superconductivity";
import { frequencyUnitLabel } from "../../utils/units";
import { LineChart } from "../charts/LineChart";
import { Badge, Button, FallbackTag, Slider, cx } from "./ui";

const TOTAL_COLOR = CHART_COLORS.ink;
const [S_COLOR, CV_COLOR, F_COLOR] = SERIES_COLORS;

/** Text for the imaginary-mode warning (null when all mesh modes are real). */
export function imaginaryDosWarning(d: Pick<PhononDosData, "imaginary_fraction">): string | null {
  if (!(d.imaginary_fraction > 0)) return null;
  return `${(d.imaginary_fraction * 100).toFixed(1)} % of the mesh modes are imaginary and are excluded from F, S and C_v; the thermal properties describe an unstable structure.`;
}

export function DosPane() {
  const dos = useCrystalStore((s) => s.dos);
  const pending = useCrystalStore((s) => s.dosPending);
  const hasPhonon = useCrystalStore((s) => s.phonon !== null);
  const atoms = useCrystalStore((s) => s.crystal?.atoms);
  const unit = useSettings((s) => s.units.frequency);
  if (!dos) {
    return (
      <div className="grid h-full w-full place-items-center text-xs text-fg-4">
        <div className="text-center">
          <p className="mb-2">{hasPhonon ? "Phonon DOS and thermodynamics from the cached force constants." : "Run phonons first."}</p>
          <Button disabled={!hasPhonon || pending} onClick={() => commands.requestDos()}>
            {pending ? <Loader2 size={12} className="animate-spin" /> : <BarChart3 size={12} />} {pending ? "Computing DOS…" : "Compute DOS"}
          </Button>
        </div>
      </div>
    );
  }
  const order = atoms ? [...new Set(atoms.map((a) => a.element))] : undefined;
  const s = dosInUnit(dos, unit, order);
  const ul = frequencyUnitLabel(unit);
  const th = dos.thermal;
  const warn = imaginaryDosWarning(dos);
  const at300 = (ys: number[]) => interpAt(th.temperatures_k, ys, 300);
  const f300 = at300(th.free_energy_ev_per_atom);
  const s300 = at300(th.entropy_kb_per_atom);
  const cv300 = at300(th.heat_capacity_kb_per_atom);
  const fmt = (v: number | null, d: number) => (v === null ? "—" : v.toFixed(d));

  return (
    <div className="flex h-full w-full flex-col gap-1">
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 font-mono text-2xs text-fg-3">
        <span>
          ZPE <span className="text-fg">{(th.zero_point_energy_ev_per_atom * 1000).toFixed(2)} meV/atom</span>
        </span>
        <span>
          300 K: F <span className="text-fg">{fmt(f300 !== null ? f300 * 1000 : null, 1)} meV/at</span> · S{" "}
          <span className="text-fg">{fmt(s300, 3)}</span> · C_v <span className="text-fg">{fmt(cv300, 3)}</span> k_B/at
        </span>
        <span className="text-fg-4">mesh {dos.mesh.join("×")}</span>
        <FallbackTag />
        <Button className="ml-auto px-1.5 py-0 text-2xs" disabled={pending} onClick={() => commands.requestDos()} title="Recompute the DOS">
          {pending ? <Loader2 size={10} className="animate-spin" /> : <BarChart3 size={10} />} recompute
        </Button>
      </div>
      {warn && (
        <div className="flex shrink-0 items-start gap-1.5 rounded-ctl border border-bad/30 bg-bad/10 px-2 py-0.5 text-2xs leading-snug text-bad" data-imaginary-warning>
          <AlertTriangle size={11} className="mt-px shrink-0" /> {warn}
        </div>
      )}
      <div className="flex min-h-0 flex-1 gap-2">
        <div className="min-w-0 flex-[1.4]">
          <LineChart
            series={[
              { label: "total", xs: s.frequencies, ys: s.total, color: TOTAL_COLOR, fill: true, width: 1.6 },
              ...s.projected.map((p) => ({ label: p.element, xs: s.frequencies, ys: p.values, color: p.color, width: 1.6 })),
            ]}
            xLabel={`f (${ul})`}
            yLabel={`DOS (states/${ul}/cell)`}
            shadeBelowX={0}
            includeZeroY
            formatX={(v) => `${v.toFixed(unit === "cm-1" ? 0 : 2)} ${ul}`}
            formatY={(v) => v.toFixed(3)}
          />
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="min-h-0 flex-1">
            <LineChart
              series={[
                { label: "S", xs: th.temperatures_k, ys: th.entropy_kb_per_atom, color: S_COLOR },
                { label: "C_v", xs: th.temperatures_k, ys: th.heat_capacity_kb_per_atom, color: CV_COLOR },
              ]}
              xLabel="T (K)"
              yLabel="k_B/atom"
              includeZeroY
              formatX={(v) => `${v.toFixed(0)} K`}
              formatY={(v) => `${v.toFixed(3)} k_B`}
            />
          </div>
          <div className="min-h-0 flex-1">
            <LineChart
              series={[{ label: "F_vib", xs: th.temperatures_k, ys: th.free_energy_ev_per_atom.map((v) => v * 1000), color: F_COLOR }]}
              xLabel="T (K)"
              yLabel="F (meV/at)"
              zeroLine
              formatX={(v) => `${v.toFixed(0)} K`}
              formatY={(v) => `${v.toFixed(1)} meV/atom`}
            />
          </div>
        </div>
        {dos.eph_proxy && <ScProxySection eph={dos.eph_proxy} />}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- superconductivity Tier 0
const PROXY_NOTE = "constant-coupling proxy, assumed λ — not a prediction";
const OMEGA_LOG_TIP =
  "ω_log: logarithmic-average phonon frequency under α²F(ω) ∝ F(ω). It sets the Allen–Dynes T_c prefactor and is the ranking scale; λ (electron–phonon coupling) is not computed, since MACE has no electrons.";
const [MU_A_COLOR, MU_B_COLOR] = SERIES_COLORS;

const VERDICT_TONE: Record<MetallicityVerdict, "green" | "amber" | "red" | "slate"> = {
  metal: "green",
  likely_metal: "green",
  likely_insulator: "amber",
  insulator: "red",
  unknown: "slate",
};

type MetalState = { status: "idle" | "busy" | "ok" | "error"; result: MetallicityResponse | null; error: string | null; stateVersion: number };

/** Right-hand column of the DOS pane; rendered only when the backend sent `eph_proxy`. */
function ScProxySection({ eph }: { eph: EphProxyDescriptors }) {
  const [lam, setLam] = useState(1.0);
  const [mu, setMu] = useState(0.1);
  const wl = eph.omega_log_k;
  const w2 = eph.omega_2_k ?? wl;
  const tc = wl !== null && w2 !== null ? allenDynesTc(lam, mu, wl, w2) : null;
  const curves = useMemo(() => (wl !== null && w2 !== null ? MU_STAR_GRID.map((m) => ({ mu: m, ...tcCurve(m, wl, w2) })) : []), [wl, w2]);
  const k = (v: number | null) => (v === null ? "—" : `${v.toFixed(0)} K`);

  return (
    <div className="flex w-[232px] shrink-0 flex-col gap-1 overflow-y-auto border-l border-line-soft pl-2 text-2xs" data-sc-proxies>
      <div className="font-semibold tracking-[0.06em] text-fg-3 uppercase">Superconductivity proxies</div>
      <div className="text-fg-4 italic">{PROXY_NOTE}</div>
      <div className="grid grid-cols-2 gap-x-2 font-mono text-fg-3 tabular-nums">
        <span title={OMEGA_LOG_TIP} className="cursor-help underline decoration-fg-5 decoration-dotted underline-offset-2">
          ω_log <span className="text-fg">{k(wl)}</span>
        </span>
        <span title="Allen–Dynes ω̄₂ = sqrt(∫Fω dω / ∫F/ω dω) under the proxy">
          ω̄₂ <span className="text-fg">{k(eph.omega_2_k)}</span>
        </span>
        <span title="Mean phonon frequency ∫Fω dω / ∫F dω">
          ⟨ω⟩ <span className="text-fg">{k(eph.omega_mean_k)}</span>
        </span>
        <span title="Highest frequency with F > 10⁻³ · max F">
          ω_max <span className="text-fg">{k(eph.omega_max_k)}</span>
        </span>
        <span className="col-span-2" title="Share of ∫F dω carried by H, Li, Be, B, C, N">
          light elements <span className="text-fg">{(eph.light_element_fraction * 100).toFixed(1)} %</span>
        </span>
        {eph.tc_ref_k !== null && (
          <span className="col-span-2" title="Allen–Dynes T_c at an assumed λ = 1.0, μ* = 0.10: a ranking scale, not a prediction">
            T_c,ref <span className="text-fg">{eph.tc_ref_k.toFixed(1)} K</span> <span className="text-fg-4">(λ 1.0, μ* 0.10)</span>
          </span>
        )}
      </div>
      {(!eph.reliable || eph.warnings.length > 0) && (
        <div className="flex items-start gap-1 rounded-ctl border border-warn/30 bg-warn/10 px-1.5 py-0.5 leading-snug text-warn" data-sc-warning>
          <AlertTriangle size={10} className="mt-px shrink-0" />
          <span>{[!eph.reliable ? "Not reliable." : null, ...eph.warnings].filter(Boolean).join(" ")}</span>
        </div>
      )}
      <Slider label="assumed λ" value={lam} min={LAMBDA_RANGE[0]} max={LAMBDA_RANGE[1]} step={0.05} onChange={setLam} format={(v) => v.toFixed(2)} disabled={wl === null} />
      <Slider label="μ*" value={mu} min={MU_STAR_RANGE[0]} max={MU_STAR_RANGE[1]} step={0.01} onChange={setMu} format={(v) => v.toFixed(2)} disabled={wl === null} />
      <div className="font-mono text-fg-3" title="Allen–Dynes T_c at the assumed λ and μ* (proxy, not a prediction)">
        T_c (Allen–Dynes, assumed λ) <span className="text-xs text-fg tabular-nums">{tc === null ? "—" : `${tc.toFixed(1)} K`}</span>
      </div>
      <div className="h-[120px] shrink-0">
        <LineChart
          series={curves.map((c, i) => ({ label: `μ* = ${c.mu.toFixed(2)}`, xs: c.lam, ys: c.tc, color: i === 0 ? MU_A_COLOR : MU_B_COLOR }))}
          markers={wl !== null ? [{ x: lam, label: "λ", color: CHART_COLORS.ink, dashed: true }] : undefined}
          xLabel="assumed λ"
          yLabel="T_c (K)"
          includeZeroY
          xDomain={[LAMBDA_RANGE[0], LAMBDA_RANGE[1]]}
          formatX={(v) => `λ ${v.toFixed(2)}`}
          formatY={(v) => `${v.toFixed(1)} K`}
          empty="No ω_log (no real modes above 0.05 THz)"
        />
      </div>
      <MetallicityCheck />
      <div className="text-fg-4">{eph.method}</div>
    </div>
  );
}

function MetallicityCheck() {
  const crystal = useCrystalStore((s) => s.crystal);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const [st, setSt] = useState<MetalState>({ status: "idle", result: null, error: null, stateVersion: -1 });
  if (!crystal) return null;
  const stale = st.status === "ok" && st.stateVersion !== stateVersion;
  const run = async () => {
    const v = stateVersion;
    setSt({ status: "busy", result: null, error: null, stateVersion: v });
    try {
      const result = await api.metallicity({ crystal, use_mp: true });
      setSt({ status: "ok", result, error: null, stateVersion: v });
    } catch (e) {
      setSt({ status: "error", result: null, error: errorText(e), stateVersion: v });
    }
  };
  const r = st.result;
  return (
    <div className="border-t border-line-soft pt-1" data-sc-metallicity>
      <Button
        className="w-full px-2 py-0.5 text-2xs"
        disabled={st.status === "busy"}
        onClick={() => void run()}
        title="Phonon-mediated superconductors must be metals: the Materials Project gap of the matching entry, else a composition heuristic"
      >
        {st.status === "busy" ? <Loader2 size={10} className="animate-spin" /> : <Zap size={10} />} Check metallicity
      </Button>
      {st.status === "error" && st.error && <p className="mt-0.5 leading-snug text-bad">{st.error}</p>}
      {r && (
        <div className={cx("mt-0.5 space-y-0.5 leading-snug", stale && "opacity-50")}>
          <div className="flex flex-wrap items-center gap-1">
            <Badge tone={VERDICT_TONE[r.verdict] ?? "slate"}>{r.verdict.replace("_", " ")}</Badge>
            <span className="text-fg-4">
              {r.source === "materials_project" ? `MP${r.mp_id ? ` ${r.mp_id}` : ""}` : "heuristic"}
              {r.band_gap_ev !== null ? ` · gap ${r.band_gap_ev.toFixed(2)} eV` : ""}
              {stale ? " · stale (structure changed)" : ""}
            </span>
          </div>
          <p className="text-fg-3">{r.reason}</p>
        </div>
      )}
    </div>
  );
}
