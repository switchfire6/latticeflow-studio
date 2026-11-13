/**
 * "Local structure" dock pane (§5.7): octahedral tilts with the Glazer symbol
 * (a⁰a⁰c⁻) and tilt angles, the global instability index, then tabs for the
 * partial g_AB(r) (pair toggles), the site table (CN, oxidation state, BVS
 * with its deviation coloured) and the coordination polyhedra (shape, mean
 * length, distortion, quadratic elongation). Refreshes automatically for
 * committed structures (services/localActions.ts); stale while the structure
 * moves (drag / streams).
 */
import { SERIES_COLORS } from "../../utils/chartData";
import { Loader2, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { commands } from "../../services/wsClient";
import { useCrystalStore, type LocalAnalysisState } from "../../store/useCrystalStore";
import { bvsTone, giiVerdict, glazerDisplay, partialPairs } from "../../utils/localStructure";
import { LineChart, type LineSeries } from "../charts/LineChart";
import { Badge, Button, Segmented, cx } from "./ui";

export { SERIES_COLORS };

type Tab = "rdf" | "sites" | "polyhedra";
const f = (v: number | null | undefined, d = 2) => (v == null || !Number.isFinite(v) ? "—" : v.toFixed(d));
const signed = (v: number | null | undefined, d = 2) => (v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}`);

function TiltReadout({ la }: { la: LocalAnalysisState }) {
  const t = la.data.octahedral_tilts;
  const gii = la.data.global_instability_index;
  const gv = giiVerdict(gii);
  return (
    <div className="flex min-w-0 items-center gap-3">
      {t ? (
        <>
          <span className="font-serif text-md leading-none text-fg italic" title={`Glazer tilt system ${t.glazer ?? "(undetermined)"} · ${t.method}`} data-glazer>
            {glazerDisplay(t.glazer)}
          </span>
          <span className="font-mono text-xs text-fg-2 tabular-nums" title="Rotation of the BX₆ octahedra about the pseudo-cubic a, b, c axes (phase: + in-phase, − anti-phase)">
            {["a", "b", "c"].map((ax, i) => (
              <span key={ax} className="mr-2">
                φ<sub>{ax}</sub> {f(t.tilt_angles_deg[i], 1)}°<span className="text-fg-4">{t.phases[i] === "0" ? "" : t.phases[i]}</span>
              </span>
            ))}
          </span>
        </>
      ) : (
        <span className="text-xs text-fg-4" title="Tilts are analysed for corner-sharing BX₆ (perovskite-like) networks only">no octahedral tilt network</span>
      )}
      <span className="ml-auto flex shrink-0 items-center gap-1 font-mono text-xs" title="Global instability index √⟨(BVS − |V|)²⟩ in valence units: < 0.1 unstrained, ~0.2 typical, > 0.2 strained / possibly unstable">
        <span className="text-fg-4">GII</span>
        <span className="text-fg tabular-nums">{f(gii, 3)} v.u.</span>
        <Badge tone={gv.tone}>{gv.text}</Badge>
      </span>
    </div>
  );
}

function PartialRdf({ la }: { la: LocalAnalysisState }) {
  const pairs = partialPairs(la.data.partial_rdf);
  const [off, setOff] = useState<string[]>([]);
  const r = la.data.rdf_r;
  const series: LineSeries[] = pairs
    .map((p, i) => ({ p, i }))
    .filter(({ p }) => !off.includes(p))
    .map(({ p, i }) => ({ label: `g ${p}`, xs: r, ys: la.data.partial_rdf![p]!, color: SERIES_COLORS[i % SERIES_COLORS.length]!, width: 1.6 }));
  if (!pairs.length) return <div className="grid h-full w-full place-items-center text-xs text-fg-4">No partial g(r) in this analysis.</div>;
  return (
    <div className="flex h-full w-full min-w-0 gap-2">
      <div className="flex w-[74px] shrink-0 flex-col gap-0.5 overflow-y-auto" data-pairs>
        {pairs.map((p, i) => (
          <button
            key={p}
            type="button"
            onClick={() => setOff(off.includes(p) ? off.filter((x) => x !== p) : [...off, p])}
            className={cx("flex items-center gap-1 rounded-ctl px-1 py-0.5 text-left font-mono text-2xs", off.includes(p) ? "text-fg-5" : "bg-raised text-fg")}
            aria-pressed={!off.includes(p)}
          >
            <span className="h-[2px] w-2.5 shrink-0 rounded-ctl" style={{ background: off.includes(p) ? "#3e444d" : SERIES_COLORS[i % SERIES_COLORS.length] }} />
            {p}
          </button>
        ))}
      </div>
      <div className="relative min-w-0 flex-1">
        <LineChart series={series} xLabel="r (Å)" yLabel="g_AB(r)" xDomain={[r[0] ?? 0.5, r[r.length - 1] ?? 8]} includeZeroY empty="All pairs hidden" />
      </div>
    </div>
  );
}

function SiteTable({ la }: { la: LocalAnalysisState }) {
  return (
    <div className="h-full w-full overflow-auto">
      <table className="w-full font-mono text-xs tabular-nums" data-sites>
        <thead className="sticky top-0 bg-surface text-fg-4">
          <tr>
            <th className="px-1 text-left font-normal">#</th>
            <th className="px-1 text-left font-normal">el</th>
            <th className="px-1 text-right font-normal">CN</th>
            <th className="px-1 text-right font-normal" title="Assigned oxidation state">ox</th>
            <th className="px-1 text-right font-normal" title="Bond-valence sum (v.u.)">BVS</th>
            <th className="px-1 text-right font-normal" title="|BVS| − |ox|: positive = over-bonded (compressed), negative = under-bonded">Δ</th>
            <th className="px-1 text-left font-normal">neighbours (Å)</th>
          </tr>
        </thead>
        <tbody>
          {la.data.sites.map((s) => {
            const tone = bvsTone(s.bond_valence_sum, s.oxidation_state);
            const dev = s.bond_valence_sum != null && s.oxidation_state != null ? Math.abs(s.bond_valence_sum) - Math.abs(s.oxidation_state) : null;
            const nb = [...s.neighbors].sort((a, b) => a.distance - b.distance);
            return (
              <tr key={s.index} className="border-t border-line-soft hover:bg-hover" onMouseEnter={() => useCrystalStore.getState().setHoveredAtom(s.index)} onMouseLeave={() => useCrystalStore.getState().setHoveredAtom(null)}>
                <td className="px-1 text-fg-4">{s.index}</td>
                <td className="px-1 text-fg">{s.element}</td>
                <td className="px-1 text-right text-fg">{s.coordination_number}</td>
                <td className="px-1 text-right text-fg-2">{s.oxidation_state == null ? "—" : signed(s.oxidation_state, 0)}</td>
                <td className="px-1 text-right text-fg">{f(s.bond_valence_sum)}</td>
                <td className={cx("px-1 text-right", tone === "ok" ? "text-ok" : tone === "warn" ? "text-warn" : tone === "bad" ? "text-bad" : "text-fg-5")}>{signed(dev)}</td>
                <td className="max-w-[260px] truncate px-1 text-fg-3" title={nb.map((n) => `${n.element}${n.index} ${n.distance.toFixed(3)}`).join(", ")}>
                  {nb
                    .slice(0, 6)
                    .map((n) => `${n.element} ${n.distance.toFixed(2)}`)
                    .join(" · ")}
                  {nb.length > 6 ? " …" : ""}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PolyhedraTable({ la }: { la: LocalAnalysisState }) {
  const polyhedra = la.data.polyhedra;
  if (!polyhedra.length) return <div className="grid h-full w-full place-items-center text-xs text-fg-4">No coordination polyhedra found.</div>;
  return (
    <div className="h-full w-full overflow-auto">
      <table className="w-full font-mono text-xs tabular-nums" data-polyhedra>
        <thead className="sticky top-0 bg-surface text-fg-4">
          <tr>
            <th className="px-1 text-left font-normal">centre</th>
            <th className="px-1 text-left font-normal">shape</th>
            <th className="px-1 text-right font-normal">CN</th>
            <th className="px-1 text-right font-normal" title="Mean centre–ligand distance">⟨d⟩ Å</th>
            <th className="px-1 text-right font-normal" title="Bond-length distortion (1/n) Σ |d_i − ⟨d⟩| / ⟨d⟩">Δd</th>
            <th className="px-1 text-right font-normal" title="Quadratic elongation ⟨λ⟩ (Robinson et al.; 1 = regular)">⟨λ⟩</th>
          </tr>
        </thead>
        <tbody>
          {polyhedra.map((p) => (
            <tr key={p.center_index} className="border-t border-line-soft hover:bg-hover" onMouseEnter={() => useCrystalStore.getState().setHoveredAtom(p.center_index)} onMouseLeave={() => useCrystalStore.getState().setHoveredAtom(null)}>
              <td className="px-1 text-fg">
                {p.center_element}
                <span className="text-fg-4">{p.center_index}</span>
              </td>
              <td className="px-1 text-fg-2">{p.shape}</td>
              <td className="px-1 text-right text-fg">{p.ligand_indices.length}</td>
              <td className="px-1 text-right text-fg">{f(p.mean_bond_length, 3)}</td>
              <td className={cx("px-1 text-right", p.bond_length_distortion > 0.02 ? "text-warn" : "text-fg")}>{f(p.bond_length_distortion, 4)}</td>
              <td className={cx("px-1 text-right", p.quadratic_elongation != null && p.quadratic_elongation > 1.01 ? "text-warn" : "text-fg")}>{f(p.quadratic_elongation, 4)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function LocalStructureView({ la, stale, pending, canRefresh, onRefresh }: { la: LocalAnalysisState | null; stale: boolean; pending: boolean; canRefresh: boolean; onRefresh(): void }) {
  const [tab, setTab] = useState<Tab>("rdf");
  const body = useMemo(() => {
    if (!la) return null;
    return tab === "rdf" ? <PartialRdf la={la} /> : tab === "sites" ? <SiteTable la={la} /> : <PolyhedraTable la={la} />;
  }, [la, tab]);
  return (
    <div className="flex h-full w-full min-w-0 flex-col gap-1.5" data-local-pane>
      <div className="flex shrink-0 items-center gap-2">
        {la ? <TiltReadout la={la} /> : <span className="flex-1 text-xs text-fg-4">{pending ? "analysing…" : "Partial g(r), bond valence, polyhedra and octahedral tilts of the current structure."}</span>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Segmented<Tab>
          value={tab}
          options={[
            { value: "rdf", label: "g_AB(r)" },
            { value: "sites", label: `sites${la ? ` (${la.data.sites.length})` : ""}` },
            { value: "polyhedra", label: `polyhedra${la ? ` (${la.data.polyhedra.length})` : ""}` },
          ]}
          onChange={setTab}
        />
        {stale && la && <span className="text-2xs text-warn/80">structure changed · refreshes when it settles</span>}
        <span className="ml-auto flex items-center gap-1.5">
          {la && <span className="max-w-[220px] truncate text-2xs text-fg-5" title={la.data.method}>{la.data.method}</span>}
          <Button className="px-1.5 py-0.5 text-xs" disabled={!canRefresh || pending} onClick={onRefresh} title="Re-run REQUEST_LOCAL_ANALYSIS now">
            {pending ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />} {la ? "Refresh" : "Analyse"}
          </Button>
        </span>
      </div>
      <div className="min-h-0 flex-1">{body}</div>
    </div>
  );
}

export function LocalStructurePane() {
  const la = useCrystalStore((s) => s.localAnalysis);
  const pending = useCrystalStore((s) => s.localPending);
  const stateVersion = useCrystalStore((s) => s.stateVersion);
  const canRefresh = useCrystalStore((s) => s.connection === "open" && s.crystal !== null);
  return <LocalStructureView la={la} stale={!!la && la.stateVersion !== stateVersion} pending={pending} canRefresh={canRefresh} onRefresh={() => commands.requestLocalAnalysis({ include_partial_rdf: true })} />;
}
