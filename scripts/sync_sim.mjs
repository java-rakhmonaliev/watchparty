// Sync-engine simulator: runs the REAL inline JS from party/room.html in Node
// (vm sandbox + fake <video> + real WebSockets against a running dev server)
// and drives host/viewer scenarios end-to-end. This is the regression suite for
// the playback-sync logic — if it's green here, the math and message flow work;
// remaining real-world issues are browser/media-pipeline, not protocol.
//
// Usage:  python manage.py runserver   # in another terminal
//         node scripts/sync_sim.mjs
import vm from "node:vm";

const BASE = process.env.WP_BASE || "http://127.0.0.1:8000";
const HOST = new URL(BASE).host;
const ROOM = "sim-" + Math.random().toString(36).slice(2, 8);
const SEEK_MS = 120; // simulated decoder seek latency

const results = [];
function check(name, ok, detail = "") {
  results.push([name, ok]);
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 4000, what = "condition") {
  const t0 = Date.now();
  while (!fn()) {
    if (Date.now() - t0 > ms) throw new Error("timeout waiting for " + what);
    await wait(25);
  }
}

// ---- fake media element: position advances with wall clock while playing ----
class FakeVideo {
  constructor() {
    this._l = {}; this._pos = 0; this._at = Date.now(); this._rate = 1;
    this.paused = true; this.src = ""; this.duration = 3600;
    this.volume = 1; this.muted = false; this.style = {};
    this._stalled = false; this._frozenFrames = 0;
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  }
  addEventListener(t, f) { (this._l[t] ??= []).push(f); }
  removeEventListener(t, f) { this._l[t] = (this._l[t] || []).filter((x) => x !== f); }
  emit(t) { for (const f of this._l[t] || []) f(); }
  _sync() { this._pos = this.currentTime; this._at = Date.now(); }
  get playbackRate() { return this._rate; }
  set playbackRate(v) { this._sync(); this._rate = v; }
  get currentTime() {
    return this.paused ? this._pos
      : this._pos + ((Date.now() - this._at) / 1000) * this._rate;
  }
  set currentTime(v) {
    this._pos = Math.max(0, v); this._at = Date.now();
    if (!this._sticky) this._stalled = false;  // a plain seek reinitialises the "decoder"
    this.emit("seeking");
    clearTimeout(this._seekT);
    this._seekT = setTimeout(() => { this.emit("seeked"); this.emit("timeupdate"); }, SEEK_MS);
  }
  // Decoder model: ~30fps of decoded frames tracking the playhead, unless the
  // video track has stalled (clock/audio continue, frames freeze). A "sticky"
  // stall survives seeks and pause/play — only load() clears it (a truly
  // wedged decoder, as seen in Firefox-based browsers).
  _framesNow() {
    return this._stalled ? this._frozenFrames : Math.floor(this.currentTime * 30);
  }
  stallDecoder(sticky = false) {
    this._frozenFrames = this._framesNow(); this._stalled = true; this._sticky = sticky;
  }
  getVideoPlaybackQuality() { return { totalVideoFrames: this._framesNow() }; }
  load() {
    this._stalled = false; this._sticky = false;
    this._sync(); this.paused = true;
    clearTimeout(this._loadT);
    this._loadT = setTimeout(() => { this.emit("loadedmetadata"); this.emit("loadeddata"); }, 80);
  }
  play() {
    if (this.paused) { this.paused = false; this._at = Date.now(); setTimeout(() => this.emit("play"), 0); }
    return Promise.resolve();
  }
  pause() { if (!this.paused) { this._sync(); this.paused = true; this.emit("pause"); } }
}

// ---- minimal DOM stubs (enough for room.html's script) ----
function makeEl(id) {
  return {
    id, textContent: "", innerHTML: "", value: "0", max: "0", min: "0",
    style: {}, disabled: false, files: [], _l: {}, title: "",
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener(t, f) { (this._l[t] ??= []).push(f); },
    appendChild() {}, append() {}, remove() {},
    querySelector: () => ({ textContent: "" }),
    setAttribute() {}, getAttribute: () => null,
  };
}

function makeClient(label, source, iceJson) {
  const els = new Map();
  const video = new FakeVideo();
  els.set("video", video);
  const ice = makeEl("ice-config"); ice.textContent = iceJson; els.set("ice-config", ice);
  const documentStub = {
    getElementById(id) { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
    createElement: () => makeEl("dyn"),
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, title: "",
    documentElement: { setAttribute() {}, getAttribute: () => null },
    hidden: false,
  };
  const sandbox = {
    document: documentStub, WebSocket, console,
    setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
    Date, Math, JSON, Promise, String, Number, Object, Array, Map, Set, Uint8Array,
    isFinite, parseFloat, parseInt, crypto: globalThis.crypto,
    location: { protocol: "http:", host: HOST, href: BASE + "/room/" + ROOM + "/" },
    localStorage: { getItem: () => label, setItem() {}, removeItem() {} },
    matchMedia: () => ({ matches: true }),
    prompt: () => label, alert: (m) => console.log(`  [${label} alert] ${m}`), confirm: () => true,
    navigator: { clipboard: { writeText() {} } },
    URL: { createObjectURL: () => "blob:sim", revokeObjectURL() {} },
    requestAnimationFrame: (f) => setTimeout(f, 16),
    screen: {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  // The driver is appended to the same script source, so it closes over the
  // template's own let/const bindings — we test the real code, not a copy.
  const driver = `
;globalThis.__sim = {
  video,
  get selfId() { return selfId; },
  get isHost() { return isHost; },
  get state() { return currentState; },
  begin() { started = true; },
  setSrc() { video.src = "blob:sim"; },
  skip(d) { skip(d); },
  toggle() { togglePlay(); },
};`;
  vm.runInContext(source + driver, sandbox, { filename: `room-inline-${label}.js` });
  return sandbox;
}

// ---- scenarios ----
async function main() {
  const page = await (await fetch(`${BASE}/room/${ROOM}/`)).text();
  const ice = page.match(/<script id="ice-config"[^>]*>(.*?)<\/script>/s)?.[1] ?? "[]";
  const source = page.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/)[1];

  const H = makeClient("host", source, ice);
  await until(() => H.__sim.selfId, 4000, "host join");
  const V = makeClient("view", source, ice);
  await until(() => V.__sim.selfId, 4000, "viewer join");
  check("roles assigned", H.__sim.isHost && !V.__sim.isHost);

  for (const c of [H, V]) { c.__sim.setSrc(); c.__sim.begin(); }
  const hv = H.__sim.video, vv = V.__sim.video;
  const delta = () => Math.abs(hv.currentTime - vv.currentTime);
  const fmt = () => `host=${hv.currentTime.toFixed(2)} viewer=${vv.currentTime.toFixed(2)}`;

  // 1. host presses play
  H.__sim.toggle();
  await wait(1200);
  check("play propagates", !vv.paused, fmt());
  check("playing in sync (<0.35s)", delta() < 0.35, fmt());

  // 2. host skips +10 (the «10/10» button path: seek -> seeked event)
  H.__sim.skip(10);
  await wait(1500);
  check("+10 skip follows (<0.5s)", !vv.paused && delta() < 0.5, fmt());

  // 3. host skips -10
  H.__sim.skip(-10);
  await wait(1500);
  check("-10 skip follows (<0.5s)", delta() < 0.5, fmt());

  // 4. rapid triple-skip (three clicks inside 300ms)
  H.__sim.skip(10); await wait(120); H.__sim.skip(10); await wait(120); H.__sim.skip(10);
  await wait(2000);
  check("rapid 3x skip converges (<0.5s)", delta() < 0.5, fmt());

  // 5. host pauses — paused frames must match closely (the "paused pic differs" bug)
  H.__sim.toggle();
  await wait(1500);
  check("pause propagates", vv.paused, fmt());
  check("paused positions match (<0.12s)", vv.paused && delta() < 0.12, fmt());

  // 6. host seeks WHILE PAUSED (paused drift never self-corrected before)
  H.__sim.skip(10);
  await wait(1500);
  check("seek-while-paused follows (<0.12s)", delta() < 0.12, fmt());

  // 7. resume
  H.__sim.toggle();
  await wait(1200);
  check("resume propagates + sync (<0.35s)", !vv.paused && delta() < 0.35, fmt());

  // 8. shared control: the VIEWER pauses, host must follow
  V.__sim.toggle();
  await wait(1500);
  check("viewer pause propagates to host", hv.paused && vv.paused, fmt());
  check("viewer-pause positions match (<0.12s)", delta() < 0.12, fmt());

  // 9. shared control: viewer resumes + skips, host must FOLLOW the viewer
  V.__sim.toggle();
  await wait(600);
  const hostBefore = hv.currentTime;
  V.__sim.skip(10);
  await wait(1500);
  check("viewer +10 skip drives the ROOM forward",
        !hv.paused && hv.currentTime > hostBefore + 8, fmt());
  check("viewer +10 skip in sync (<0.5s)", delta() < 0.5, fmt());

  // 10. host heartbeat must not stomp a fresh viewer action (the old race)
  V.__sim.toggle();                    // viewer pauses right between heartbeats
  await wait(3500);                    // a heartbeat window passes
  check("heartbeat doesn't undo viewer pause", hv.paused && vv.paused, fmt());

  // 11. paused self-heal: knock the viewer's playhead WITHOUT events (as a
  // decoder stall / throttled tab would) — heartbeat + paused-position
  // enforcement must pull it back to the room's frame.
  vv._pos += 3; vv._at = Date.now();
  await wait(4500);
  check("paused desync self-heals (<0.12s)", vv.paused && delta() < 0.12, fmt());

  // 12. decoder-stall watchdog: the viewer's video track freezes (clock and
  // audio keep going, so drift stays ~0) — the watchdog must notice that no
  // frames are being decoded and kick the decoder with an in-place seek.
  H.__sim.toggle();                    // resume playing
  await wait(800);
  vv.stallDecoder();
  const framesAtStall = vv.getVideoPlaybackQuality().totalVideoFrames;
  await wait(5500);
  const framesAfter = vv.getVideoPlaybackQuality().totalVideoFrames;
  check("stalled decoder gets kicked (frames advance again)",
        framesAfter > framesAtStall, `frames ${framesAtStall} -> ${framesAfter}`);
  check("still in sync after decoder kick (<0.5s)", !vv.paused && delta() < 0.5, fmt());

  // 13. sticky stall: micro-seeks and pause/play don't help (a truly wedged
  // Gecko decoder) — the watchdog must escalate to a full element reload and
  // rejoin the room's timeline afterwards.
  vv.stallDecoder(true);
  const framesStuck = vv.getVideoPlaybackQuality().totalVideoFrames;
  await wait(12000);
  const framesHealed = vv.getVideoPlaybackQuality().totalVideoFrames;
  check("wedged decoder recovers via element reload",
        framesHealed > framesStuck, `frames ${framesStuck} -> ${framesHealed}`);
  check("in sync after reload (<0.5s)", !vv.paused && delta() < 0.5, fmt());

  const failed = results.filter(([, ok]) => !ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(2); });
