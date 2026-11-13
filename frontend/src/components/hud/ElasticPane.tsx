/**
 * Elastic-tensor dock pane (§5.5): settings (max strain, relaxed ions),
 * COMPUTE_ELASTIC_TENSOR with a progress bar from PROGRESS, and the result:
 * the 6×6 C_ij (GPa), eigenvalues, the Born badge, Voigt / Reuss / Hill K and G,
 * E, ν, Pugh's G/K with the ductile / brittle hint, A^U, the Debye
 * temperature, a reference-pressure warning and the method text. Stale once the
 * structure (or model) changes; fallback watermark like the Phase A panes.
 */
import { AlertTriangle, Loader2, Square, Sigma } from "lucide-react";
import { commands } from "../../services/wsClient";
import { useSettings } from "../../store/settings";
import { useCrystalStore } from "../../store/useCrystalStore";
import type { ElasticTensorResult } from "../../types/protocol";
import { elasticSummary, PUGH_DUCTILE_GK } from "../../utils/elastic";
import { Badge, Button, FallbackTag, NumberField, Segmented, cx } from "./ui";

const f = (v: number | null | undefined, d = 1) => (v == null || !Number.isFinite(v) ? "—" : v.toFixed(d));

export function ProgressBar({ done, total, message }: { done?: number | null; total?: number | null; message?: string | null }) {
  const frac = done != null && total ? Math.min(1, done / total) : null;
  return (
    <div className="w-full" data-progress>
      <div className="mb-0.5 flex justify-between font-mono text-2xs text-fg-3">
        <span className="truncate">{message || "working…"}</span>
        {frac !== null && <span className="tabular-nums">{done}/{total}</span>}
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-raised">
        {frac !== null ? (
          <div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${Math.round(frac * 100)}%` }} />
        ) : (
          <div className="lf-indeterminate h-full w-1/3 rounded-full bg-accent" />
        )}
      </div>
    </div>
  );
}

function Row({ label, value, title, tone }: { label: string; value: string; title?: string; tone?: "amber" | "green" | "red" }) {
  return (
    <div className="flex items-baseline justify-between gap-2" title={title}>
      <span className="text-fg-4">{label}</span>
      <span className={cx("tabular-nums", tone === "amber" ? "text-warn" : tone === "green" ? "text-ok" : tone === "red" ? "text-bad" : "text-fg")}>{value}</span>
    </div>
  );
}

export function ElasticResultView({ r, stale }: { r: ElasticTensorResult; stale: boolean }) {
  const sum = elasticSummary(r);
  const maxAbs = Math.max(1, ...r.c_ij_gpa.flat().map((v) => Math.abs(v)));
  return (
    <div className="flex h-full w-full min-w-0 gap-3 overflow-hidden font-mono text-xs" data-elastic-result>
      <div className="flex w-[210px] shrink-0 flex-col gap-1 overflow-y-auto border-r border-line-soft pr-3">
        <div className="flex flex-wrap items-center gap-1">
          {r.born_stable ? (
            <Badge tone="green" title="All six eigenvalues of C are positive (Born criteria)">
              MECHANICALLY STABLE
            </Badge>
          ) : (
            <Badge tone="red" title="C has a non-positive eigenvalue: a homogeneous strain lowers the energy">
              MECHANICALLY UNSTABLE
            </Badge>
          )}
          <FallbackTag compact />
        </div>
        {stale && <p className="text-warn/80">Structure or model changed since this fit; recompute.</p>}
        {sum.pressureWarning && (
          <p className="flex gap-1 leading-snug text-warn" data-pressure-warning>
            <AlertTriangle size={11} className="mt-px shrink-0" /> {sum.pressureWarning}
          </p>
        )}
        <table className="w-full text-right tabular-nums">
          <thead className="text-fg-4">
            <tr>
              <th className="text-left font-normal">GPa</th>
              <th className="font-normal">Voigt</th>
              <th className="font-normal">Reuss</th>
              <th className="font-normal">Hill</th>
            </tr>
          </thead>
          <tbody className="text-fg">
            <tr>
              <td className="text-left text-fg-4">K</td>
              <td>{f(r.k_voigt_gpa)}</td>
              <td>{f(r.k_reuss_gpa)}</td>
              <td className="text-fg">{f(r.k_vrh_gpa)}</td>
            </tr>
            <tr>
              <td className="text-left text-fg-4">G</td>
              <td>{f(r.g_voigt_gpa)}</td>
              <td>{f(r.g_reuss_gpa)}</td>
              <td className="text-fg">{f(r.g_vrh_gpa)}</td>
            </tr>
          </tbody>
        </table>
        <Row label="E (Hill)" value={`${f(r.youngs_modulus_vrh_gpa)} GPa`} />
        <Row label="ν (Hill)" value={f(r.poisson_ratio_vrh, 3)} />
        <Row
          label="Pugh G/K"
          value={sum.gOverK !== null ? `${sum.gOverK.toFixed(2)} · ${sum.ductility}` : "—"}
          tone={sum.ductility === "ductile" ? "green" : sum.ductility === "brittle" ? "amber" : undefined}
          title={`Pugh's criterion: G/K < ${PUGH_DUCTILE_GK} (K/G > 1.75) suggests ductile, above it brittle (a heuristic for metals and simple ionic solids)`}
        />
        <Row label="A^U" value={f(r.universal_anisotropy, 3)} title="Universal anisotropy index (0 = isotropic)" />
        <Row label="Θ_D" value={r.debye_temperature_k != null ? `${r.debye_temperature_k.toFixed(0)} K` : "—"} title="Debye temperature from the averaged sound velocity" />
        <Row label="P_ref" value={`${r.reference_pressure_gpa >= 0 ? "+" : "−"}${Math.abs(r.reference_pressure_gpa).toFixed(2)} GPa`} tone={sum.pressureWarning ? "amber" : undefined} />
        <Row label="fit residual" value={`${f(r.fit_residual_gpa, 2)} GPa`} />
        <p className="mt-1 leading-snug text-fg-4">
          {r.relaxed_ions ? "relaxed-ion" : "clamped-ion"} · ±{(r.max_strain * 100).toFixed(1)} % · {r.method}
        </p>
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-2 overflow-auto">
        <table className="tabular-nums" data-cij>
          <thead>
            <tr className="text-fg-4">
              <th className="px-1 font-normal">C_ij</th>
              {[1, 2, 3, 4, 5, 6].map((j) => (
                <th key={j} className="px-1.5 text-right font-normal">
                  {j}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {r.c_ij_gpa.map((row, i) => (
              <tr key={i}>
                <td className="px-1 text-fg-4">{i + 1}</td>
                {row.map((v, j) => {
                  const a = Math.min(1, Math.abs(v) / maxAbs);
                  return (
                    <td
                      key={j}
                      className={cx("px-1.5 py-px text-right", Math.abs(v) < 0.5 ? "text-fg-5" : "text-fg")}
                      style={{ background: Math.abs(v) >= 0.5 ? (v > 0 ? `rgba(57,135,229,${0.06 + 0.24 * a})` : `rgba(217,89,38,${0.08 + 0.26 * a})`) : undefined }}
                    >
                      {v.toFixed(1)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        <div className="text-fg-3">
          eigenvalues (GPa):{" "}
          {r.eigenvalues_gpa.map((e, i) => (
            <span key={i} className={cx("mr-1.5", e <= 0 ? "text-bad" : "text-fg")}>
              {e.toFixed(1)}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

export function ElasticSettingsRow({ compact }: { compact?: boolean }) {
  const e = useSettings((s) => s.elastic);
  const set = useSettings((s) => s.setElastic);
  return (
    <div className={cx("flex flex-wrap items-center gap-x-3 gap-y-1", compact && "text-2xs")} data-elastic-settings>
      <div className="w-[180px]">
        <NumberField label="max strain" unit="%" value={+(e.maxStrain * 100).toFixed(2)} min={0.1} max={5} onCommit={(v) => set({ maxStrain: v / 100 })} title="Largest applied strain (± in each of the six independent directions)" />
      </div>
      <span className="flex items-center gap-1 text-xs text-fg-3" title="Relaxed-ion: positions are relaxed at each fixed strain (needed for any structure with internal degrees of freedom)">
        ions
        <Segmented
          value={e.relaxIons ? "relaxed" : "clamped"}
          options={[
            { value: "relaxed", label: "relaxed" },
            { value: "clamped", label: "clamped" },
          ]}
          onChange={(v) => set({ relaxIons: v === "relaxed" })}
        />
      </span>
    </div>
  );
}

export function ElasticPane() {
  const elastic = useCrystalStore((s) => s.elastic);
  const running = useCrystalStore((s) => s.streams.find((x) => x.command === "COMPUTE_ELASTIC_TENSOR") ?? null);
  const progress = useCrystalStore((s) => s.progress.COMPUTE_ELASTIC_TENSOR ?? null);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const connected = useCrystalStore((s) => s.connection === "open");
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const compute = () => (running ? commands.stop() : commands.computeElastic());
  const button = (
    <Button className="px-2 py-0.5 text-xs" variant={running ? "danger" : "ghost"} disabled={!connected || !hasCrystal || !!running?.stopping} onClick={compute}>
      {running ? <Square size={10} /> : <Sigma size={11} />} {running ? "Stop" : elastic ? "Recompute" : "Compute elastic tensor"}
    </Button>
  );
  return (
    <div className="flex h-full w-full min-w-0 flex-col gap-1.5">
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        <ElasticSettingsRow />
        <span className="ml-auto">{button}</span>
      </div>
      {running && (
        <div className="shrink-0">
          <ProgressBar done={progress?.done} total={progress?.total} message={progress?.message ?? "straining the cell…"} />
        </div>
      )}
      <div className="min-h-0 flex-1">
        {elastic ? (
          <ElasticResultView r={elastic.result} stale={elastic.stateVersion !== stateVersion} />
        ) : (
          <div className="grid h-full place-items-center text-center text-xs text-fg-4">
            {running ? (
              <span className="flex items-center gap-1.5 text-fg-2">
                <Loader2 size={12} className="animate-spin" /> fitting stress vs strain…
              </span>
            ) : (
              <p className="max-w-[360px]">Fit σ(ε) over 6 × {useSettings.getState().elastic.nStrains} deformations for C_ij, the Born stability criteria and VRH moduli. Relax the cell first: the constants are referred to the current stress state.</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
