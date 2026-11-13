// Headless browser driver for visual checks (Chrome DevTools Protocol, no extra deps).
//
//   node scripts/cdp-shot.mjs <url> [step ...]
//   steps:  wait:<ms>   eval:<js expression>   click:<button text>   shot:<file.png>
//           dragatom:<instance>,<dx px>,<dy px>,<moves>   (real mouse drag via Input.dispatchMouseEvent;
//           needs the dev-only window.__latticeflow.r3f handle)
//
// Launches its own headless Edge/Chrome (SwiftShader WebGL, throwaway profile),
// runs the steps in order and always kills the browser on exit.
// Env: BROWSER=<path to msedge/chrome>, CDP_PROFILE=<profile dir>, WIDTH, HEIGHT.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [url = "http://localhost:5173/", ...steps] = process.argv.slice(2);
const width = Number(process.env.WIDTH ?? 1600);
const height = Number(process.env.HEIGHT ?? 1000);
const port = 9300 + Math.floor(Math.random() * 500);
const candidates = [
  process.env.BROWSER,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean);
const browser = candidates.find((p) => existsSync(p));
if (!browser) {
  console.error("no Edge/Chrome found (set BROWSER)");
  process.exit(2);
}
const profile = process.env.CDP_PROFILE ?? mkdtempSync(join(tmpdir(), "lf-cdp-"));
const proc = spawn(
  browser,
  [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
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
const consoleLines = [];

function cdp(method, params = {}) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject, method }));
}

async function main() {
  let target;
  for (let i = 0; i < 100 && !target; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      target = list.find((t) => t.type === "page");
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
      if (m.error) p.reject(new Error(`${p.method}: ${m.error.message}`));
      else p.resolve(m.result);
    } else if (m.method === "Runtime.consoleAPICalled") {
      consoleLines.push(`[console.${m.params.type}] ${m.params.args.map((a) => a.value ?? a.description ?? "").join(" ")}`);
    } else if (m.method === "Runtime.exceptionThrown") {
      consoleLines.push(`[exception] ${m.params.exceptionDetails.exception?.description ?? m.params.exceptionDetails.text}`);
    }
  };
  await cdp("Runtime.enable");
  await cdp("Page.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
  await cdp("Page.navigate", { url });

  for (const step of steps) {
    const i = step.indexOf(":");
    const kind = step.slice(0, i);
    const arg = step.slice(i + 1);
    if (kind === "wait") {
      await sleep(Number(arg));
    } else if (kind === "eval") {
      const r = await cdp("Runtime.evaluate", { expression: arg, returnByValue: true, awaitPromise: true });
      console.log(`eval ${arg.slice(0, 60)} -> ${JSON.stringify(r.result?.value ?? r.exceptionDetails?.text)}`);
    } else if (kind === "click") {
      const expr = `(() => { const b = [...document.querySelectorAll('button')].find(b => b.textContent.trim().includes(${JSON.stringify(arg)}) && !b.disabled); if (!b) return 'not found'; b.click(); return 'clicked'; })()`;
      const r = await cdp("Runtime.evaluate", { expression: expr, returnByValue: true });
      console.log(`click "${arg}" -> ${r.result?.value}`);
    } else if (kind === "clickxy") {
      const [x, y] = arg.split(",").map(Number);
      for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
        await cdp("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons: type === "mousePressed" ? 1 : 0, clickCount: type === "mouseMoved" ? 0 : 1 });
      }
      console.log(`clicked (${x}, ${y})`);
    } else if (kind === "dragatom") {
      const [inst, dx, dy, moves = 20] = arg.split(",").map(Number);
      const expr = `(() => { const r = __latticeflow.r3f; const mesh = r.scene.getObjectByName('lf-atoms');
        const m = mesh.matrixWorld.clone(); mesh.getMatrixAt(${inst}, m);
        const v = mesh.position.clone().setFromMatrixPosition(m).applyMatrix4(mesh.matrixWorld).project(r.camera);
        const rect = r.gl.domElement.getBoundingClientRect();
        return [rect.left + (v.x + 1) / 2 * rect.width, rect.top + (1 - v.y) / 2 * rect.height]; })()`;
      const pos = (await cdp("Runtime.evaluate", { expression: expr, returnByValue: true })).result.value;
      const [x0, y0] = pos;
      const mouse = (type, x, y, buttons) => cdp("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons, clickCount: type === "mouseMoved" ? 0 : 1, pointerType: "mouse" });
      await mouse("mouseMoved", x0, y0, 0);
      await mouse("mousePressed", x0, y0, 1);
      for (let k = 1; k <= moves; k++) {
        await mouse("mouseMoved", x0 + (dx * k) / moves, y0 + (dy * k) / moves, 1);
        await sleep(16);
      }
      await mouse("mouseReleased", x0 + dx, y0 + dy, 0);
      console.log(`dragged instance ${inst} from (${x0.toFixed(0)}, ${y0.toFixed(0)}) by (${dx}, ${dy}) px in ${moves} moves`);
    } else if (kind === "shot") {
      const r = await cdp("Page.captureScreenshot", { format: "png" });
      writeFileSync(arg, Buffer.from(r.data, "base64"));
      console.log(`screenshot -> ${arg}`);
    } else {
      throw new Error(`unknown step ${step}`);
    }
  }
  if (consoleLines.length) console.log(consoleLines.slice(-30).join("\n"));
}

main()
  .catch((e) => {
    console.error("cdp-shot failed:", e.message);
    if (consoleLines.length) console.error(consoleLines.slice(-30).join("\n"));
    process.exitCode = 1;
  })
  .finally(() => {
    try {
      ws?.close();
    } catch {
      /* ignore */
    }
    proc.kill();
  });
