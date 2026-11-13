// Integration smoke test through the Vite dev-server proxy (no browser needed).
//
//   node scripts/proxy-smoke.mjs [baseUrl] [--record tests/fixtures/recorded-session.json]
//
// Requires `uvicorn app.main:app --port 8000` (backend/) and `npm run dev`.
// Uses Node's global fetch and WebSocket (Node >= 22).
import { writeFileSync } from "node:fs";

const args = process.argv.slice(2);
const recordIdx = args.indexOf("--record");
const recordPath = recordIdx >= 0 ? args[recordIdx + 1] : null;
const positional = args.filter((a, i) => !a.startsWith("--") && !(recordIdx >= 0 && i === recordIdx + 1));
const base = (positional[0] ?? "http://localhost:5173").replace(/\/+$/, "");
const wsBase = base.replace(/^http/, "ws");

const log = (...a) => console.log(...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForHealth(timeoutMs = 120_000) {
  const t0 = Date.now();
  for (;;) {
    try {
      const res = await fetch(`${base}/api/health`);
      if (res.ok) return { body: await res.json(), ms: Date.now() - t0 };
    } catch {
      /* not up yet */
    }
    if (Date.now() - t0 > timeoutMs) throw new Error("backend did not become healthy through the proxy");
    await sleep(1000);
  }
}

class Session {
  constructor(url) {
    this.url = url;
    this.queue = [];
    this.waiters = [];
    this.recorded = [];
  }
  open() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.url);
      this.ws.onopen = () => resolve();
      this.ws.onerror = (e) => reject(new Error(`websocket error: ${e.message ?? e.type}`));
      this.ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        msg._t = performance.now();
        this.recorded.push(msg);
        const w = this.waiters.shift();
        if (w) w(msg);
        else this.queue.push(msg);
      };
    });
  }
  next(timeoutMs = 60_000) {
    if (this.queue.length) return Promise.resolve(this.queue.shift());
    return new Promise((resolve, reject) => {
      const h = setTimeout(() => reject(new Error("timed out waiting for a server message")), timeoutMs);
      this.waiters.push((m) => {
        clearTimeout(h);
        resolve(m);
      });
    });
  }
  async until(pred, timeoutMs = 120_000) {
    const seen = [];
    for (;;) {
      const m = await this.next(timeoutMs);
      seen.push(m);
      if (pred(m)) return seen;
    }
  }
  send(type, payload) {
    this.sentAt = performance.now();
    this.recorded.push({ _sent: true, type, payload });
    this.ws.send(JSON.stringify({ type, payload }));
  }
  close() {
    this.ws.close();
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(`assertion failed: ${msg}`);
}

const CRYSTAL_KEYS = ["formula", "spacegroup_symbol", "spacegroup_number", "lattice", "atoms", "bonds", "energy_per_atom", "max_force", "stress_gpa", "bulk_modulus_gpa", "e_hull_ev", "rdf_r", "rdf_g"];
function checkCrystal(c, where) {
  for (const k of CRYSTAL_KEYS) assert(k in c, `${where}: crystal.${k} missing`);
  assert(c.lattice.matrix.length === 3, `${where}: lattice.matrix 3x3`);
  for (const a of c.atoms) {
    for (const k of ["index", "element", "atomic_number", "frac_coords", "cart_coords", "force", "force_norm", "is_unstable_actor"]) assert(k in a, `${where}: atom.${k}`);
  }
  for (const b of c.bonds) {
    for (const k of ["source_index", "target_index", "image_offset", "length", "strain_ratio", "is_unstable"]) assert(k in b, `${where}: bond.${k}`);
  }
}

async function main() {
  log(`proxy base: ${base}`);
  const health = await waitForHealth();
  log(`GET /api/health via proxy -> ${JSON.stringify(health.body)} (ready after ${health.ms} ms)`);

  const presetsRes = await fetch(`${base}/api/presets`);
  assert(presetsRes.ok, `/api/presets status ${presetsRes.status}`);
  const presets = await presetsRes.json();
  log(`GET /api/presets via proxy -> ${presetsRes.status}, ${presets.length} presets: ${presets.map((p) => `${p.id}(${p.formula}, ${p.spacegroup_symbol}, ${p.num_atoms} atoms, ${p.expected_stability})`).join(", ")}`);

  const s = new Session(`${wsBase}/ws/studio`);
  const t0 = performance.now();
  await s.open();
  log(`WS ${wsBase}/ws/studio via proxy: open in ${(performance.now() - t0).toFixed(0)} ms`);

  // 1. auto-loaded SrTiO3
  const first = await s.next();
  assert(first.type === "SINGLE_POINT_UPDATE", `first message ${first.type}`);
  checkCrystal(first.payload.crystal, "auto-load");
  const c0 = first.payload.crystal;
  log(`  auto-load: ${first.type} ${c0.formula} ${c0.spacegroup_symbol} (#${c0.spacegroup_number}), N=${c0.atoms.length}, E=${c0.energy_per_atom.toFixed(4)} eV/atom, max|F|=${c0.max_force.toFixed(4)}, bonds=${c0.bonds.length} (${(first._t - t0).toFixed(0)} ms after connect)`);
  const strains = c0.bonds.map((b) => b.strain_ratio);
  log(`  bond strain_ratio range: ${Math.min(...strains).toFixed(3)} .. ${Math.max(...strains).toFixed(3)}; image_offsets: ${[...new Set(c0.bonds.map((b) => b.image_offset.join(",")))].join(" | ")}`);

  // 2. drag frame: coordinate-only perturb -> SINGLE_POINT_UPDATE
  const ti = c0.atoms.find((a) => a.element === "Ti") ?? c0.atoms[0];
  const target = [ti.cart_coords[0] + 0.15, ti.cart_coords[1], ti.cart_coords[2]];
  s.send("PERTURB_OR_TRANSMUTE_ATOM", { atom_index: ti.index, new_cart_coords: target });
  const drag = await s.next();
  assert(drag.type === "SINGLE_POINT_UPDATE", `drag reply ${drag.type}`);
  const moved = drag.payload.crystal.atoms[ti.index];
  log(`  drag #${ti.index} ${ti.element} +0.15 Å x: ${drag.type} in ${(drag._t - s.sentAt).toFixed(1)} ms round trip (first); |F_Ti| ${ti.force_norm.toFixed(3)} -> ${moved.force_norm.toFixed(3)} eV/Å`);
  const rtts = [];
  for (let k = 1; k <= 5; k++) {
    s.send("PERTURB_OR_TRANSMUTE_ATOM", { atom_index: ti.index, new_cart_coords: [target[0] + 0.02 * k, target[1], target[2]] });
    const m = await s.next();
    assert(m.type === "SINGLE_POINT_UPDATE", `drag reply ${m.type}`);
    rtts.push(m._t - s.sentAt);
  }
  log(`  5 more drag frames: round trips ${rtts.map((x) => x.toFixed(1)).join(", ")} ms`);

  // 3. relaxation stream (few steps)
  s.send("START_RELAXATION", { fmax: 0.02, max_steps: 25, relax_cell: true });
  const relax = await s.until((m) => (m.type === "RELAXATION_FRAME" && m.payload.final) || m.type === "ERROR");
  const frames = relax.filter((m) => m.type === "RELAXATION_FRAME");
  const fin = frames.at(-1).payload;
  log(`  relaxation: ${frames.length} RELAXATION_FRAMEs, final step ${fin.step} converged=${fin.converged}, max|F| ${frames[0].payload.crystal.max_force.toFixed(3)} -> ${fin.crystal.max_force.toFixed(4)}`);

  // 4. phonon shake-test on SrTiO3 (re-load the preset first so it is the cubic cell)
  s.send("LOAD_STRUCTURE", { preset_id: "srtio3" });
  await s.until((m) => m.type === "SINGLE_POINT_UPDATE");
  const tp = performance.now();
  s.send("RUN_PHONON_SHAKE_TEST", { supercell_dim: [2, 2, 2], displacement_distance: 0.01 });
  const ph = await s.until((m) => m.type === "PHONON_ANALYSIS_COMPLETE" || m.type === "ERROR" || m.type === "STREAM_STOPPED", 300_000);
  const done = ph.at(-1);
  assert(done.type === "PHONON_ANALYSIS_COMPLETE", `phonon terminal ${done.type} ${JSON.stringify(done.payload).slice(0, 200)}`);
  const flags = await s.next();
  assert(flags.type === "SINGLE_POINT_UPDATE", `post-phonon message ${flags.type}`);
  const p = done.payload;
  const actors = flags.payload.crystal.atoms.filter((a) => a.is_unstable_actor).map((a) => `#${a.index}${a.element}`);
  log(`  phonons: ${((performance.now() - tp) / 1000).toFixed(1)} s, stable=${p.is_dynamically_stable}, min f=${p.min_frequency_thz} THz, imaginary=${p.imaginary_mode_count}, n_q=${p.kpath_distances.length}, labels=${p.kpath_labels.map((l) => l.label).join(" ")}, eigenmodes=${p.eigenmodes.length}; flagged SPU actors: ${actors.join(" ") || "none"}, unstable bonds: ${flags.payload.crystal.bonds.filter((b) => b.is_unstable).length}`);

  // 5. eigenmode request (no cancel)
  s.send("REQUEST_EIGENMODE", { q_index: Math.floor(p.kpath_distances.length / 3), band_index: 0 });
  const em = await s.next();
  log(`  REQUEST_EIGENMODE -> ${em.type} q_frac=${JSON.stringify(em.payload.eigenmode?.q_frac)} f=${em.payload.eigenmode?.frequency_thz}`);

  // 6. short flow + implicit cancel semantics
  s.send("START_FLOW_GENERATION", { elements: ["Sr", "Ti", "O"], num_atoms: 5, target_crystal_system: "cubic", steps: 20, physics_guidance_scale: 1.5, seed: 7 });
  const tf = performance.now();
  const flow = await s.until((m) => m.type === "CRYSTAL_STATE_FINAL" || m.type === "ERROR" || m.type === "STREAM_STOPPED", 300_000);
  const ff = flow.filter((m) => m.type === "FLOW_TRAJECTORY_FRAME");
  const span = ff.length > 1 ? (ff.at(-1)._t - ff[0]._t) / 1000 : 0;
  const last = flow.at(-1);
  log(`  flow: ${ff.length} FLOW_TRAJECTORY_FRAMEs at ${span > 0 ? ((ff.length - 1) / span).toFixed(1) : "?"} Hz, terminal ${last.type}${last.type === "CRYSTAL_STATE_FINAL" ? ` ${last.payload.crystal.formula} ${last.payload.crystal.spacegroup_symbol}, K=${last.payload.crystal.bulk_modulus_gpa}` : ""} (${((performance.now() - tf) / 1000).toFixed(1)} s)`);

  // 7. stop semantics
  s.send("START_RELAXATION", { max_steps: 200 });
  await s.until((m) => m.type === "RELAXATION_FRAME" || m.type === "ERROR");
  s.send("STOP_STREAM", {});
  const stop = await s.until((m) => m.type === "STREAM_STOPPED" || (m.type === "RELAXATION_FRAME" && m.payload.final));
  log(`  STOP_STREAM during relaxation -> ${stop.at(-1).type} ${JSON.stringify(stop.at(-1).payload).slice(0, 60)}`);
  s.send("STOP_STREAM", {});
  const idle = await s.next();
  log(`  STOP_STREAM idle -> ${idle.type} ${JSON.stringify(idle.payload)}`);

  // 8. error path
  s.send("DELETE_ATOM", { atom_index: 999 });
  const err = await s.next();
  log(`  DELETE_ATOM 999 -> ${err.type} ${err.payload.code}: ${err.payload.message}`);

  s.close();
  if (recordPath) {
    writeFileSync(recordPath, JSON.stringify(s.recorded.map(({ _t, ...m }) => m)));
    log(`recorded ${s.recorded.length} messages -> ${recordPath}`);
  }
  log("PROXY SMOKE OK");
}

main().catch((e) => {
  console.error("PROXY SMOKE FAILED:", e.message);
  process.exit(1);
});
