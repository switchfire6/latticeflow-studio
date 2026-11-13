// Records the README demo: cubic SrTiO3 -> relax -> phonons -> follow the R-point soft mode.
//
//   node scripts/record-demo.mjs [out.gif]          (default ../docs/media/demo.gif)
//
// Needs the backend on :8000 (MACE), `npm run dev` on :5173 and ffmpeg on PATH.
// Drives the real UI in a headless Edge/Chrome over the DevTools Protocol (no extra deps):
// a fake cursor clicks the real buttons, captions are overlaid, and the screencast
// frames are assembled into a GIF with their real timestamps.
// Env: BROWSER=<msedge/chrome>, URL, WIDTH, HEIGHT, GIF_WIDTH, GIF_FPS, KEEP_FRAMES=1, STILLS=<dir>.
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(process.argv[2] ?? join(here, "../../docs/media/demo.gif"));
const url = process.env.URL ?? "http://localhost:5173/";
const width = Number(process.env.WIDTH ?? 1280);
const CAPTION_H = 48;
const height = Number(process.env.HEIGHT ?? 800) + CAPTION_H;
const gifWidth = Number(process.env.GIF_WIDTH ?? 960);
const gifFps = Number(process.env.GIF_FPS ?? 10);
const stills = process.env.STILLS ? resolve(process.env.STILLS) : null;

const candidates = [
  process.env.BROWSER,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean);
const browser = candidates.find((p) => existsSync(p));
if (!browser) {
  console.error("no Edge/Chrome found (set BROWSER)");
  process.exit(2);
}
const port = 9300 + Math.floor(Math.random() * 500);
const work = mkdtempSync(join(tmpdir(), "lf-demo-"));
const proc = spawn(
  browser,
  [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${join(work, "profile")}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu-sandbox",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--hide-scrollbars",
    `--window-size=${width},${height}`,
    "about:blank",
  ],
  { stdio: "ignore" },
);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws;
let seq = 0;
const pending = new Map();
const frames = []; // { file, t }
let recording = false;

function cdp(method, params = {}) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => pending.set(id, { res, rej, method }));
}
async function js(expression) {
  const r = await cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
  return r.result?.value;
}
async function until(expression, timeoutMs, what) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await js(expression)) return;
    await sleep(150);
  }
  throw new Error(`timed out waiting for ${what}`);
}

// ---- overlay: fake cursor, click ripple, caption ------------------------- //
const OVERLAY = `(() => {
  const css = document.createElement('style');
  css.textContent = \`
    #demo-cursor { position: fixed; left: 0; top: 0; z-index: 99999; pointer-events: none;
      transform: translate(640px, 300px); transition: transform 0.7s cubic-bezier(.4,0,.2,1); }
    #demo-ripple { position: fixed; z-index: 99998; pointer-events: none; width: 34px; height: 34px;
      margin: -17px 0 0 -17px; border-radius: 50%; border: 2px solid #67e8f9; opacity: 0; }
    #demo-ripple.go { animation: demo-ripple 0.5s ease-out; }
    @keyframes demo-ripple { from { opacity: .9; transform: scale(.3); } to { opacity: 0; transform: scale(1.4); } }
    #root { height: calc(100% - ${CAPTION_H}px) !important; }
    #demo-caption { position: fixed; z-index: 99997; pointer-events: none; left: 0; right: 0; bottom: 0;
      height: ${CAPTION_H}px; display: flex; align-items: center; justify-content: center;
      background: #060b16; border-top: 1px solid rgba(103, 232, 249, .35); color: #e2f9ff;
      font: 600 19px/1 system-ui, sans-serif; letter-spacing: .01em; }
    #demo-caption span { transition: opacity .3s; }
  \`;
  document.head.appendChild(css);
  const cur = document.createElement('div');
  cur.id = 'demo-cursor';
  cur.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M4 2l16 9.5-7 1.6-3.4 6.9z" fill="#fff" stroke="#0b1220" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  const rip = document.createElement('div'); rip.id = 'demo-ripple';
  const cap = document.createElement('div'); cap.id = 'demo-caption'; cap.appendChild(document.createElement('span'));
  document.body.append(cur, rip, cap);
  window.__demo = {
    move(x, y) { cur.style.transform = 'translate(' + x + 'px,' + y + 'px)'; },
    ripple(x, y) { rip.style.left = x + 'px'; rip.style.top = y + 'px'; rip.classList.remove('go'); void rip.offsetWidth; rip.classList.add('go'); },
    caption(t) { cap.firstChild.style.opacity = 0; setTimeout(() => { cap.firstChild.textContent = t ?? ''; cap.firstChild.style.opacity = 1; }, 180); },
    center(el) { const r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; },
    button(text) { return [...document.querySelectorAll('button')].find((b) => b.textContent.trim().includes(text) && !b.disabled); },
  };
})()`;

const caption = (t) => js(`__demo.caption(${JSON.stringify(t)})`);
async function moveTo(x, y) {
  await js(`__demo.move(${x}, ${y})`);
  await cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await sleep(850);
}
async function clickAt(x, y) {
  await moveTo(x, y);
  await js(`__demo.ripple(${x}, ${y})`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await cdp("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons: type === "mousePressed" ? 1 : 0, clickCount: 1 });
  }
  await sleep(250);
}
async function clickButton(text) {
  await until(`!!__demo.button(${JSON.stringify(text)})`, 20000, `button "${text}"`);
  const [x, y] = await js(`(() => { const b = __demo.button(${JSON.stringify(text)}); b.scrollIntoView({ block: 'nearest' }); return __demo.center(b); })()`);
  await clickAt(Math.round(x), Math.round(y));
}
let stillNo = 0;
async function still(name) {
  if (!stills) return;
  mkdirSync(stills, { recursive: true });
  const r = await cdp("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(stills, `${String(++stillNo).padStart(2, "0")}-${name}.png`), Buffer.from(r.data, "base64"));
}

/** Smoothly orbit the camera to a direction (keeps the distance; dev-only r3f handle). */
async function orbitTo(dir, ms) {
  await js(`new Promise((done) => { const r = __latticeflow.r3f; if (!r) return done();
    const cam = r.camera, from = cam.position.clone(), d = from.length();
    const to = from.clone().set(${dir[0]}, ${dir[1]}, ${dir[2]}).normalize().multiplyScalar(d); const t0 = performance.now();
    const step = (now) => { const k = Math.min(1, (now - t0) / ${ms}), e = k * k * (3 - 2 * k);
      cam.position.copy(from).lerp(to, e).setLength(d); r.controls?.update(); r.invalidate?.();
      if (k < 1) requestAnimationFrame(step); else done(); };
    requestAnimationFrame(step); })`);
}

const S = "__latticeflow.useCrystalStore.getState()";

async function storyboard() {
  await until(`!!window.__latticeflow && !!${S}.frame.crystal`, 30000, "the studio session");
  await js(OVERLAY);
  await sleep(1200);
  await cdp("Page.startScreencast", { format: "jpeg", quality: 95, maxWidth: width, maxHeight: height, everyNthFrame: 1 });
  recording = true;

  await caption("Cubic SrTiO₃ with live forces and stress from MACE");
  await sleep(2600);
  await still("loaded");

  await caption("1 · Relax the cell and the atoms");
  await clickButton("Relax (FIRE");
  await sleep(600);
  await until(`${S}.streams.length === 0`, 60000, "the relaxation");
  await sleep(1800);
  await still("relaxed");

  await caption("2 · Phonons: is it dynamically stable?");
  await clickButton("Run phonons");
  await until(`!!${S}.phonon`, 180000, "the phonons");
  await sleep(600);
  await caption("Imaginary modes: cubic SrTiO₃ is unstable at 0 K");
  await sleep(3000);
  await still("phonons");

  // 3 · select the softest mode at R (click its row in the mode list when it is shown)
  const mode = await js(`(() => { const p = ${S}.phonon; const ms = p.eigenmodes.filter((m) => m.is_imaginary);
    const pick = (ms.filter((m) => (m.q_label ?? '').toUpperCase() === 'R')[0]) ?? ms.sort((a, b) => a.frequency_thz - b.frequency_thz)[0];
    return pick ? { q: pick.q_index, band: pick.band_index, nq: p.frequencies_thz.length, label: pick.q_label } : null; })()`);
  if (!mode) throw new Error("no imaginary mode found (is the backend running MACE?)");
  await caption("3 · Pick the soft mode at R: the octahedra want to tilt");
  const chart = await js(`(() => { const c = [...document.querySelectorAll('canvas')].filter((c) => !c.closest('[data-engine]') && c.getBoundingClientRect().top > 480)
      .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width)[0];
    if (!c) return null; const r = c.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()`);
  if (chart) await moveTo(Math.round(chart[0] + 48 + (mode.q / (mode.nq - 1)) * (chart[2] - 64)), Math.round(chart[1] + chart[3] - 34));
  await js(`__demo.ripple(...(() => { const m = document.getElementById('demo-cursor').style.transform.match(/-?[\\d.]+/g); return [Number(m[0]), Number(m[1])]; })())`);
  await js(`import('/src/services/phononActions.ts').then((m) => m.selectModeAt(${mode.q}, ${mode.band}))`);
  await until(`!!${S}.selectedEigenmode && ${S}.selectedEigenmode.q_index === ${mode.q}`, 30000, "the eigenvector");
  await sleep(4500);
  await still("mode");

  await caption("4 · Follow the instability into the distorted phase");
  await clickButton("Follow instability");
  await sleep(1000);
  await until(`${S}.streams.length === 0 && ${S}.followResult?.status !== 'running'`, 240000, "follow instability");
  await sleep(1200);
  await caption("Pm-3m → I4/mcm: the tilted ground state, lower in energy");
  await sleep(2200);
  await clickButton("Polyhedra");
  await until(`!!${S}.localAnalysis`, 30000, "the local analysis").catch(() => {});
  await caption("TiO₆ octahedra rotate about c: Glazer a⁰a⁰c⁻");
  await orbitTo([0.1, 0.12, 1], 2200); // look down the tilt axis
  await sleep(3500);
  await still("followed");

  recording = false;
  await cdp("Page.stopScreencast");
}

async function main() {
  let target;
  for (let i = 0; i < 100 && !target; i++) {
    try {
      target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === "page");
    } catch {
      /* not up yet */
    }
    if (!target) await sleep(200);
  }
  if (!target) throw new Error("browser did not expose a page target");
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r, j) => {
    ws.onopen = r;
    ws.onerror = j;
  });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id);
      pending.delete(m.id);
      if (m.error) p.rej(new Error(`${p.method}: ${m.error.message}`));
      else p.res(m.result);
    } else if (m.method === "Page.screencastFrame") {
      ws.send(JSON.stringify({ id: ++seq, method: "Page.screencastFrameAck", params: { sessionId: m.params.sessionId } }));
      if (!recording) return;
      const file = join(work, `f${String(frames.length).padStart(5, "0")}.jpg`);
      writeFileSync(file, Buffer.from(m.params.data, "base64"));
      frames.push({ file, t: m.params.metadata.timestamp });
    }
  };
  await cdp("Runtime.enable");
  await cdp("Page.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
  await cdp("Page.navigate", { url });
  await storyboard();

  if (frames.length < 2) throw new Error("no frames captured");
  const span = frames[frames.length - 1].t - frames[0].t;
  console.log(`${frames.length} frames over ${span.toFixed(1)} s (${(frames.length / span).toFixed(1)} fps captured)`);
  const lines = ["ffconcat version 1.0"];
  frames.forEach((f, i) => {
    const d = i + 1 < frames.length ? Math.max(0.01, frames[i + 1].t - f.t) : 2.5; // hold the last frame
    lines.push(`file '${f.file.replace(/\\/g, "/")}'`, `duration ${d.toFixed(4)}`);
  });
  lines.push(`file '${frames[frames.length - 1].file.replace(/\\/g, "/")}'`);
  const list = join(work, "frames.txt");
  writeFileSync(list, lines.join("\n"));
  mkdirSync(dirname(out), { recursive: true });
  const vf = `fps=${gifFps},scale=${gifWidth}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle`;
  const ff = spawnSync("ffmpeg", ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list, "-vf", vf, out], { stdio: "inherit" });
  if (ff.status !== 0) throw new Error("ffmpeg failed");
  console.log(`wrote ${out}`);
}

main()
  .catch((e) => {
    console.error("record-demo failed:", e.message);
    process.exitCode = 1;
  })
  .finally(() => {
    try {
      ws?.close();
    } catch {
      /* ignore */
    }
    proc.kill();
    if (!process.env.KEEP_FRAMES) setTimeout(() => rmSync(work, { recursive: true, force: true }), 500);
    else console.log(`frames kept in ${work}`);
  });
