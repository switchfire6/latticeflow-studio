// Phase 5 acceptance run through the Vite proxy (PROJECT_SPEC.md §8.4).
//
//   node scripts/acceptance-phase5.mjs [http://localhost:5173]
//
//  1. batio3 → shake-test → Γ imaginary band 0 → FOLLOW_INSTABILITY → P4mm, max|F| < 0.02
//  2. srtio3 → shake-test → R imaginary band 0 → FOLLOW_INSTABILITY → I4/mcm
//  3. export CIF → re-import (round trip)
//  4. hull → 503 HULL_UNAVAILABLE (no MP_API_KEY on this machine)
const base = (process.argv[2] ?? "http://localhost:5173").replace(/\/+$/, "");
const wsUrl = `${base.replace(/^http/, "ws")}/ws/studio`;
const log = (...a) => console.log(...a);
let failures = 0;
const check = (cond, msg) => {
  log(`  ${cond ? "PASS" : "FAIL"}  ${msg}`);
  if (!cond) failures++;
};

class Session {
  constructor(url) {
    this.url = url;
    this.queue = [];
    this.waiters = [];
  }
  open() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.url);
      this.ws.onopen = resolve;
      this.ws.onerror = () => reject(new Error("websocket error"));
      this.ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        const w = this.waiters.shift();
        if (w) w(m);
        else this.queue.push(m);
      };
    });
  }
  next(timeoutMs = 300_000) {
    if (this.queue.length) return Promise.resolve(this.queue.shift());
    return new Promise((resolve, reject) => {
      const h = setTimeout(() => reject(new Error("timeout waiting for the server")), timeoutMs);
      this.waiters.push((m) => {
        clearTimeout(h);
        resolve(m);
      });
    });
  }
  async until(pred) {
    const seen = [];
    for (;;) {
      const m = await this.next();
      seen.push(m);
      if (pred(m)) return seen;
    }
  }
  send(type, payload) {
    this.ws.send(JSON.stringify({ type, payload }));
  }
}

const isLabel = (m, name) => (m.q_label ?? "").replace(/\\/g, "").toUpperCase() === name.toUpperCase();

async function followCase(s, presetId, label, expectSg) {
  log(`\n[${presetId}] load → shake-test → ${label} band 0 → FOLLOW_INSTABILITY (expect ${expectSg})`);
  s.send("LOAD_STRUCTURE", { preset_id: presetId });
  const load = (await s.until((m) => m.type === "SINGLE_POINT_UPDATE" || m.type === "ERROR")).at(-1);
  const c0 = load.payload.crystal;
  log(`  loaded ${c0.formula} ${c0.spacegroup_symbol} (#${c0.spacegroup_number}), E = ${c0.energy_per_atom.toFixed(4)} eV/atom`);
  const t0 = performance.now();
  s.send("RUN_PHONON_SHAKE_TEST", { supercell_dim: [2, 2, 2], displacement_distance: 0.01 });
  const ph = (await s.until((m) => m.type === "PHONON_ANALYSIS_COMPLETE" || m.type === "ERROR")).at(-1);
  if (ph.type !== "PHONON_ANALYSIS_COMPLETE") return check(false, `shake-test: ${JSON.stringify(ph.payload)}`);
  await s.until((m) => m.type === "SINGLE_POINT_UPDATE"); // actor flags
  const p = ph.payload;
  log(`  shake-test ${((performance.now() - t0) / 1000).toFixed(1)} s: min f = ${p.min_frequency_thz} THz, ${p.imaginary_mode_count} imaginary, ${p.kpath_distances.length} q × ${p.frequencies_thz[0].length} bands`);
  const mode = p.eigenmodes.find((m) => isLabel(m, label) && m.band_index === 0);
  check(!!mode && mode.is_imaginary, `${label} band 0 is imaginary (f = ${mode?.frequency_thz} THz, q_index ${mode?.q_index})`);
  if (!mode) return;
  const t1 = performance.now();
  s.send("FOLLOW_INSTABILITY", { q_index: mode.q_index, band_index: 0, amplitude: 0.1, fmax: 0.02, max_steps: 200 });
  const frames = await s.until((m) => (m.type === "RELAXATION_FRAME" && m.payload.final) || m.type === "ERROR" || m.type === "STREAM_STOPPED");
  const fin = frames.at(-1);
  if (fin.type !== "RELAXATION_FRAME") return check(false, `follow ended with ${fin.type} ${JSON.stringify(fin.payload)}`);
  const c1 = fin.payload.crystal;
  const dE = (c1.energy_per_atom - c0.energy_per_atom) * 1000;
  log(`  follow: ${frames.filter((m) => m.type === "RELAXATION_FRAME").length} frames in ${((performance.now() - t1) / 1000).toFixed(1)} s, N ${c0.atoms.length} → ${c1.atoms.length}`);
  log(`  ${c0.spacegroup_symbol} (#${c0.spacegroup_number}) → ${c1.spacegroup_symbol} (#${c1.spacegroup_number}), ΔE = ${dE.toFixed(1)} meV/atom, max|F| = ${c1.max_force.toFixed(4)}, converged = ${fin.payload.converged}, steps = ${fin.payload.step}`);
  check(c1.spacegroup_symbol === expectSg, `space group ${c1.spacegroup_symbol} === ${expectSg}`);
  check(c1.max_force < 0.02, `max |F| ${c1.max_force.toFixed(4)} < 0.02 eV/Å`);
  check(dE < 0, `energy lowered (${dE.toFixed(1)} meV/atom)`);
  return c1;
}

async function main() {
  log(`proxy base ${base}`);
  const health = await (await fetch(`${base}/api/health`)).json();
  log(`health: ${health.umlip_backend} on ${health.device} (${health.dtype}), hull_available = ${health.hull_available}`);
  const s = new Session(wsUrl);
  await s.open();
  await s.until((m) => m.type === "SINGLE_POINT_UPDATE"); // auto-loaded srtio3

  const bto = await followCase(s, "batio3", "GAMMA", "P4mm");
  const sto = await followCase(s, "srtio3", "R", "I4/mcm");

  log("\n[export CIF → re-import]");
  const crystal = sto ?? bto;
  for (const fmt of ["cif", "poscar"]) {
    const res = await fetch(`${base}/api/export/cif`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ crystal, format: fmt }) });
    const text = await res.text();
    log(`  export ${fmt}: HTTP ${res.status}, ${res.headers.get("content-disposition")}, ${text.length} bytes`);
    check(res.ok && text.length > 100, `export ${fmt} ok`);
    if (fmt !== "cif") continue;
    const imp = await fetch(`${base}/api/import/cif`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cif: text }) });
    const back = await imp.json();
    log(`  re-import: HTTP ${imp.status}, ${back.formula} ${back.spacegroup_symbol}, N = ${back.atoms?.length}, E = ${back.energy_per_atom?.toFixed(5)} (was ${crystal.energy_per_atom.toFixed(5)})`);
    check(imp.ok && back.formula === crystal.formula && back.atoms.length === crystal.atoms.length, "same formula and atom count");
    check(back.spacegroup_symbol === crystal.spacegroup_symbol, `same space group (${back.spacegroup_symbol})`);
    check(Math.abs(back.energy_per_atom - crystal.energy_per_atom) < 1e-4, "same energy (< 0.1 meV/atom)");
    // and load it into the WebSocket session like the UI's CIF upload does
    s.send("LOAD_STRUCTURE", { cif: text });
    const ld = (await s.until((m) => m.type === "SINGLE_POINT_UPDATE" || m.type === "ERROR")).at(-1);
    check(ld.type === "SINGLE_POINT_UPDATE" && ld.payload.crystal.spacegroup_symbol === crystal.spacegroup_symbol, `LOAD_STRUCTURE {cif} → ${ld.payload.crystal?.spacegroup_symbol}`);
  }

  log("\n[hull]");
  const hres = await fetch(`${base}/api/hull/evaluate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ crystal }) });
  const hbody = await hres.json();
  log(`  POST /api/hull/evaluate → HTTP ${hres.status} ${JSON.stringify(hbody)}`);
  check(hres.status === 503 && hbody.code === "HULL_UNAVAILABLE", "503 HULL_UNAVAILABLE without MP_API_KEY");

  s.ws.close();
  log(`\n${failures ? `ACCEPTANCE FAILED (${failures})` : "ACCEPTANCE OK"}`);
  process.exitCode = failures ? 1 : 0;
}

main().catch((e) => {
  console.error("acceptance error:", e.message);
  process.exitCode = 2;
});
