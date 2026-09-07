// Dev-only: capture the README screenshots from the bundled demo with headless Chrome over CDP.
// Headless Chrome has no H.264 decoder, so the demo is served from a temp copy whose media is a VP9 .webm
// (built by scripts/build_demo.py into .demo_build/source.webm). Usage: node scripts/screenshots.js
const { spawn } = require("child_process");
const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");

const REPO = path.resolve(__dirname, "..");
const CHROME = process.env.CHROME || (os.platform() === "win32" ? "C:/Program Files/Google/Chrome/Application/chrome.exe" : "google-chrome");
const PORT = 8897, CDP = 9447;
const OUT = path.join(REPO, "docs", "screenshots");
const sleep = ms => new Promise(r => setTimeout(r, ms));

function tempProject() {
  const src = path.join(REPO, "notecut", "examples", "demo");
  const dst = path.join(REPO, ".demo_build", "shots");
  fs.rmSync(dst, { recursive: true, force: true });
  fs.cpSync(src, dst, { recursive: true });
  fs.copyFileSync(path.join(REPO, ".demo_build", "source.webm"), path.join(dst, "assets", "demo", "source.webm"));
  const cfgp = path.join(dst, "notecut.json");
  const cfg = JSON.parse(fs.readFileSync(cfgp, "utf8"));
  cfg.videos.demo.media = "assets/demo/source.webm";
  fs.writeFileSync(cfgp, JSON.stringify(cfg, null, 1));
  return dst;
}

const getJson = () => new Promise((r, j) => http.get(`http://127.0.0.1:${CDP}/json`, res => { let b = ""; res.on("data", d => b += d); res.on("end", () => r(JSON.parse(b))); }).on("error", j));

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const root = tempProject();
  const server = spawn(process.platform === "win32" ? "python" : "python3", ["-m", "notecut", "serve", "--root", root, "--host", "127.0.0.1", "--port", String(PORT)], { cwd: REPO, stdio: "inherit" });
  await sleep(1500);
  const profile = path.join(REPO, ".demo_build", "chrome-profile");
  const ch = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--autoplay-policy=no-user-gesture-required",
    `--user-data-dir=${profile}`, `--remote-debugging-port=${CDP}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
  const cleanup = () => { try { ch.kill(); } catch (e) { } try { server.kill(); } catch (e) { } };
  process.on("exit", cleanup);
  let list = null; for (let i = 0; i < 30 && !list; i++) { try { list = await getJson(); } catch (e) { await sleep(500); } }
  const pg = list.find(t => t.type === "page");
  const ws = new WebSocket(pg.webSocketDebuggerUrl);
  let id = 0; const pend = {};
  const send = (m, p = {}) => new Promise(r => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method: m, params: p })); });
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend[m.id]) { pend[m.id](m.result || m.error); delete pend[m.id]; } };
  await new Promise(r => ws.onopen = r);
  await send("Runtime.enable"); await send("Page.enable");
  const evaluate = async expr => (await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result?.value;

  async function shot(name, { url, w, h, mobile = false, setup = "", wait = 1200 }) {
    await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: mobile ? 3 : 2, mobile });
    await send("Emulation.setTouchEmulationEnabled", { enabled: mobile });
    await send("Page.navigate", { url: "about:blank" }); await sleep(200);   // hash-only navigations would not reload
    await send("Page.navigate", { url: `http://127.0.0.1:${PORT}${url}` });
    for (let i = 0; i < 80; i++) { const v = await evaluate("document.readyState==='complete' && (typeof D==='undefined' || (D.words&&D.words.length>0))"); if (v) break; await sleep(200); }
    if (setup) { await evaluate(setup); }
    await sleep(wait);
    const data = (await send("Page.captureScreenshot", { format: "png" })).data;
    const file = path.join(OUT, name); fs.writeFileSync(file, Buffer.from(data, "base64"));
    console.log("wrote", file, fs.statSync(file).size, "bytes");
  }

  // seek + wait for the frame, then set the view up
  const seek = t => `new Promise(res=>{v.pause();const done=()=>{v.removeEventListener('seeked',done);res(1)};v.addEventListener('seeked',done);v.currentTime=${t};setTimeout(()=>res(0),4000)})`;
  // the moment: "I'm having nightmares ... claw" (bridge scene, both faces in frame); indices found by text so a rebuild cannot drift
  const HERO = "(()=>{const i=D.words.findIndex(w=>/^nightmares/i.test(w.w));return {i, t:D.words[i].cut_s-0.35}})()";
  await shot("home.png", { url: "/", w: 1440, h: 900 });
  await shot("desktop.png", { url: "/v/demo#t=16", w: 1440, h: 900, wait: 1500,
    setup: `(async()=>{setWave(false);const h=${HERO};await ${seek("h.t")};const a=posOfI(h.i-2),b=posOfI(h.i+9);if(a!=null&&b!=null){paintSel(a,b);setSelection(a,b);}ta.blur();})()` });
  await shot("waveform.png", { url: "/v/demo#t=20.6&wave=1", w: 1440, h: 900, wait: 1500, setup: `${seek(20.6)}` });
  await shot("mobile-transcript.png", { url: "/v/demo#t=16", w: 390, h: 844, mobile: true, wait: 1500, setup: `(async()=>{setWave(false);const h=${HERO};await ${seek("h.t")};})()` });
  await shot("mobile-comments.png", { url: "/v/demo#t=16", w: 390, h: 844, mobile: true, wait: 1500, setup: `(async()=>{const h=${HERO};await ${seek("h.t")};showTab('c')})()` });
  ws.close(); cleanup(); process.exit(0);
}
main().catch(e => { console.error(e); process.exit(1); });
