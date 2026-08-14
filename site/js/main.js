/* Wiring. One rAF loop drives two independent simulators: the legend board and
 * the interactive replica. They share no state — each is a Sim, and a Sim is the
 * daemon minus its outside world, so two of them cost nothing but two clocks.
 */

import * as C from "./config.js?v=74";
import { hex } from "./config.js?v=74";
import { Sim } from "./sim.js?v=74";
import { makeBoard } from "./twister.js?v=74";
import { Console } from "./console.js?v=74";
import { Director, freePlay } from "./scenarios.js?v=74";
import { Cell } from "./board.js?v=74";

// ─── motion ───────────────────────────────────────────────────────────────
// The OS preference is the starting position, not the last word: the control in
// the corner sets `data-motion` on <html>, and the stylesheet is written so that
// attribute wins in both directions. Somebody who asked the OS for less motion
// can still ask this page for all of it, and somebody who never asked at all can
// still turn it off here — which is the only way to see the board hold still.
const motionMQ = window.matchMedia("(prefers-reduced-motion: reduce)");
let reduced = motionMQ.matches;

function applyMotion() {
  document.documentElement.dataset.motion = reduced ? "reduced" : "full";
}
applyMotion();

// ─── the replica ──────────────────────────────────────────────────────────
const sim = new Sim();
const con = new Console(document.getElementById("console"));
const director = new Director(sim, con);
const { encoders } = makeBoard(document.getElementById("device"));

// The board is a square div sized off its own width (the grid of square
// encoders forces that), so its height isn't expressible as a CSS percentage
// of the row it sits in — percentages resolve against the *containing*
// block's height, not a sibling's rendered width. A ResizeObserver is the
// only thing that actually knows that number, and it re-fires on every width
// change the responsive layout makes, so the terminal tracks the board
// through a window resize instead of just matching it once at load.
const deviceEl = document.getElementById("device");
new ResizeObserver(([entry]) => {
  document.documentElement.style.setProperty("--device-h", `${entry.contentRect.height}px`);
}).observe(deviceEl);

const NAMES = ["~/parser", "~/api", "~/mft", "~/web", "~/infra", "~/docs",
               "~/proto", "~/bench", "~/cli", "~/notes", "~/wire", "~/schema"];

function seed() {
  sim.boot();
  // The first session's spawn strike paints red over whichever knob it lands
  // on -- wait for the unwrap to finish so it doesn't flash mid-boot.
  const bootLength = C.BOOT_RISE + C.BOOT_HOLD + C.BOOT_SPIRAL + C.BOOT_FALL;
  director.at(bootLength, () => {
    const first = sim.add({ name: NAMES[0] });
    lastSpawnAt = sim.now;
    con.push(first, "system", `session started in ${first.name}`, { instant: true });
    con.show(first);
    sim.focus(first.slot);
    director.at(1.4, () => {
      playFeatured(first, SCENARIOS.find((sc) => sc.key === "turn"));
      // The board should look inhabited well before the featured lane has had
      // eight turns to fill it, so the opening few land on their own clock --
      // spawn strikes spaced AUTO_SPAWN_COOLDOWN apart, same as every later
      // arrival, so the ramp-up reads as the same rhythm the steady state runs.
      for (let i = 1; i <= 4; i++) director.at(1.2 + i * AUTO_SPAWN_COOLDOWN, grow);
      director.at(randIn(AUTO_BACKGROUND_EVERY), backgroundBeat);
      director.at(randIn(AUTO_SHRINK_EVERY), shrinkBeat);
    });
  });
}

// --- the demo mode. Nobody lands on an empty board: it plays through the
// --- states on its own until the first real click, keypress or prompt, at
// --- which point it gets out of the way for good. Every step it takes is the
// --- same director call a scenario button makes, so "autoplay" is never a
// --- second code path to keep honest -- it's this one, on a timer.
//
// There are two lanes, and the split is the whole design. The *featured* lane
// runs one scenario at a time: the console follows it and its encoder is
// focused. Neither lane lights a scenario button -- that ping is reserved for
// a button someone actually pressed, so it keeps meaning "you did that"
// instead of also meaning "the demo did that".
//
// The first version ran everything as one lane at a beat and a half, which put
// six knobs in motion and strobed the button row past anything you could follow.
// One thing at a time, named, with a little life behind it.
let autoplay = true;
let autoStep = 0;
const AUTO_TARGET = 8;               //: sessions the demo grows the board to. Lit, mostly idle -- population, not motion.
const AUTO_GAP = [2.4, 3.6];         //: pause between featured scenarios, so a finished one reads as finished
const AUTO_BACKGROUND_EVERY = [8, 14]; //: seconds between quiet background scenarios
const AUTO_BACKGROUND_MAX = 1;       //: how many sessions may be busy besides the featured one
const AUTO_SPAWN_COOLDOWN = 5;       //: minimum seconds between new sessions, so arrivals never stack up
const AUTO_SHRINK_EVERY = [10, 18];  //: seconds between auto-mode retirements, so the board isn't only ever filling up
const AUTO_SHRINK_MIN = 3;           //: never shrink the board below this many sessions
// A gate scenario (permission/plan/error) never resolves on its own -- that's
// the point of it -- so this is how long it sits lit before the demo answers it
// in the terminal and hands the encoder back, not how long the scenario runs.
const AUTO_GATE_HOLD = 6.5;
const AUTO_PROMPTS = [
  "make the parser handle trailing commas", "wire up the retry queue",
  "why is the round-trip test flaky?", "add a --dry-run flag",
  "port the config loader to toml", "cache the schema lookups",
  "write tests for the wire format", "find every unhandled error path",
];

// A scenario holds its encoder until it is retired -- its gate answered, its
// turn ended -- so the knob comes back into the pool instead of sitting on one
// color forever.
const busy = new Set();              // slots a scenario is currently running on
const randIn = ([lo, hi]) => lo + Math.random() * (hi - lo);
const sample = (a) => a[Math.floor(Math.random() * a.length)];
let lastAutoKey = null;
let lastSpawnAt = -Infinity;

function stopAutoplay() {
  if (!autoplay) return;
  autoplay = false;
  busy.clear();
  paintDemoStatus();
}

/** A session nothing is currently telling a story about. */
function freeSession() {
  const free = sim.list().filter((s) => !busy.has(s.slot));
  return free.length ? sample(free) : null;
}

// --- the featured lane: one scenario at a time, then a pause.
function featuredBeat() {
  if (!autoplay) return;
  autoStep++;
  grow();
  const s = freeSession();
  if (!s) { director.at(1.5, featuredBeat); return; }
  // Don't play the same card twice running.
  let sc = sample(AUTO_POOL);
  if (sc.key === lastAutoKey) sc = sample(AUTO_POOL);
  playFeatured(s, sc);
}

function playFeatured(s, sc) {
  lastAutoKey = sc.key;
  busy.add(s.slot);
  const dur = sc.run(s) || 0;
  const hold = sc.auto.gate ? AUTO_GATE_HOLD : Math.min(Math.max(dur, 3), 11);
  director.at(hold, () => {
    retire(s.slot);
    director.at(randIn(AUTO_GAP), featuredBeat);
  });
}

// --- the background lane: one other session, quietly. No button, no console.
function backgroundBeat() {
  if (!autoplay) return;
  director.at(randIn(AUTO_BACKGROUND_EVERY), backgroundBeat);
  grow();
  if (busy.size > AUTO_BACKGROUND_MAX) return;      // the featured one is in there
  const s = freeSession();
  if (!s) return;
  const sc = sample(AUTO_POOL.filter((x) => !x.auto.gate));   // a gate nobody is looking at is just a red knob
  busy.add(s.slot);
  const dur = director.quietly(() => sc.run(s)) || 0;
  director.at(Math.min(Math.max(dur, 3), 11), () => retire(s.slot));
}

/** Hand an encoder back: answer whatever is blocking, end whatever is running. */
function retire(slot) {
  busy.delete(slot);
  const s = sim.get(slot);
  if (!s) return;
  if (["permission", "plan", "error"].includes(s.state)) {
    con.push(s, "system", s.state === "error" ? "retried — back to work" : "answered in the terminal — back to work");
    sim.event(slot, "PermissionResolved");
    director.at(1.4, () => sim.event(slot, "Stop"));
    director.at(3.6, () => sim.event(slot, "Idle"));
  } else if (["working", "thinking", "streaming"].includes(s.state)) {
    sim.event(slot, "Stop");
    director.at(2.4, () => sim.event(slot, "Idle"));
  }
}

/** Grow the board toward a populated one, one session at a time, no faster
 * than AUTO_SPAWN_COOLDOWN -- two lanes both call this, and without a shared
 * cooldown a featured and a background beat landing close together spawn two
 * sessions in the same breath, which reads as a burst instead of arrivals. */
function grow() {
  if (sim.list().length >= AUTO_TARGET) return;
  if (sim.now - lastSpawnAt < AUTO_SPAWN_COOLDOWN) return;
  const fresh = sim.add({ name: NAMES[autoStep % NAMES.length] });
  if (!fresh) return;
  lastSpawnAt = sim.now;
  con.push(fresh, "system", `session started in ${fresh.name}`, { instant: true });
}

// --- the shrink lane: auto mode otherwise only ever fills up toward
// --- AUTO_TARGET and sits there, which stops reading as a live board. An idle,
// --- unbusy session ends itself every so often, the same way /exit does, so
// --- the population breathes instead of monotonically climbing.
function shrinkBeat() {
  if (!autoplay) return;
  director.at(randIn(AUTO_SHRINK_EVERY), shrinkBeat);
  if (sim.list().length <= AUTO_SHRINK_MIN) return;
  const idle = sim.list().filter((s) => !busy.has(s.slot) && s.state === "idle");
  if (!idle.length) return;
  const s = sample(idle);
  con.push(s, "system", "session ended · the encoder goes dark", { instant: true });
  director.at(0.6, () => sim.remove(s.slot));
}

const demoStatus = document.getElementById("demoStatus");
const demoStatusText = document.getElementById("demoStatusText");
function paintDemoStatus() {
  if (!demoStatus) return;
  demoStatus.dataset.live = autoplay ? "auto" : "paused";
  demoStatusText.textContent = autoplay
    ? "running scenarios automatically — interact to take over"
    : "manual control — reload to restart the automatic run";
}

// --- encoder interaction. Press focuses; hold clears the encoder off the
// --- board. Invariant 1 in the one place the page could have broken it:
// --- nothing here answers anything, and a hold takes a knob away, never an
// --- agent -- in the daemon the session claims a fresh encoder with its next
// --- hook event.
let holdSlot = null, held = false;

function pressStart(slot) {
  stopAutoplay();
  holdSlot = slot; held = false;
  sim.startHold(slot);
}
function pressEnd() {
  // The fuse fires from the frame loop, not from here; by the time a release
  // arrives late the encoder is already gone and `held` says so.
  sim.cancelHold();
  if (held) { held = false; holdSlot = null; return; }
  if (holdSlot === null) return;
  const s = sim.get(holdSlot);
  if (s) { sim.focus(s.slot); con.show(s); }
  else {
    // An empty encoder is not a control — but starting a new fake session from
    // one is the friendliest possible way to say "this is where they land".
    const fresh = sim.add({ slot: holdSlot, name: NAMES[sim.list().length % NAMES.length] });
    if (fresh) {
      con.push(fresh, "system", `session started in ${fresh.name}`, { instant: true });
      con.show(fresh); sim.focus(fresh.slot);
    }
  }
  holdSlot = null;
}

encoders.forEach((enc, slot) => {
  enc.el.addEventListener("pointerdown", (e) => { e.preventDefault(); pressStart(slot); });
  enc.el.addEventListener("pointerup", pressEnd);
  enc.el.addEventListener("pointerleave", () => { if (holdSlot === slot && !held) { sim.cancelHold(); holdSlot = null; } });
  enc.el.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    if (!e.repeat) pressStart(slot);
  });
  enc.el.addEventListener("keyup", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    pressEnd();
  });
});

con.onSubmit = (text) => {
  stopAutoplay();
  let s = sim.focused === null ? null : sim.get(sim.focused);
  if (!s) s = sim.list()[0];
  if (!s) {
    s = sim.add({ name: NAMES[0] });
    if (!s) return;
    con.push(s, "system", `session started in ${s.name}`, { instant: true });
    con.show(s);
  }
  director.cancelAll();
  freePlay(director, sim, con, s, text);
};

// --- scenarios. One table, two consumers: the buttons under the heading, and
// --- the demo, which picks from the entries that carry an `auto` block.
const SCENARIOS = [
  { key: "permission", label: "permission gate", auto: { gate: true, spotlight: true }, run: (s) => director.permissionGate(s) },
  { key: "plan", label: "plan approval", auto: { gate: true, spotlight: true }, run: (s) => director.planGate(s) },
  { key: "subagents", label: "subagents", auto: {}, run: (s) => director.subagents(s, 1 + Math.floor(Math.random() * 3)) },
  { key: "turn", label: "ordinary turn", auto: {}, run: (s) => director.ordinaryTurn(s, sample(AUTO_PROMPTS)) },
  { key: "failing", label: "failing tools", auto: {}, run: (s) => director.failing(s) },
  { key: "clear", label: "/clear", auto: {}, run: (s) => director.clear(s) },
  { key: "compact", label: "/compact", auto: {}, run: (s) => { s.contextFraction = 0.82; return director.compact(s); } },
  { key: "rate", label: "rate limit", auto: { gate: true, spotlight: true }, run: (s) => director.rateLimit(s) },
  { key: "fill", label: "fill the board", click: () => director.fill(sim, con, 9) },
  { key: "boot", label: "boot sequence", click: () => sim.boot() },
  { key: "shutdown", label: "shutdown spiral", click: () => sim.shutdown() },
  { key: "sleep", label: "sleep / wake", click: () => sim.sleep(!sim.asleep) },
  { key: "reset", label: "reset", ghost: true, click: () => reset() },
];
const AUTO_POOL = SCENARIOS.filter((sc) => sc.auto);

function withSession(fn) {
  director.cancelAll();
  let s = sim.focused === null ? null : sim.get(sim.focused);
  if (!s) s = sim.list()[0];
  if (!s) {
    s = sim.add({ name: NAMES[0] });
    if (!s) return;
    con.push(s, "system", `session started in ${s.name}`, { instant: true });
  }
  fn(s);
}

function reset() {
  director.cancelAll();
  for (const s of sim.list()) sim.remove(s.slot);
  sim.overlays = [];
  sim.showBank(0);
  con.buffers.clear();
  con.show(null);
  autoStep = 0;
  busy.clear();
  lastAutoKey = null;
  lastSpawnAt = -Infinity;
  autoplay = true;             // reset is "start over", so the demo re-arms
  paintDemoStatus();
  seed();
}

const scenarioHost = document.getElementById("scenarios");
const scenarioBtns = new Map();               // key -> the button, for the flash
for (const sc of SCENARIOS) {
  const btn = document.createElement("button");
  btn.className = "btn" + (sc.ghost ? " ghost" : "");
  btn.type = "button";
  btn.textContent = sc.label;
  btn.addEventListener("click", () => {
    stopAutoplay();
    if (sc.click) sc.click();
    else { pingButton(btn); withSession((s) => sc.run(s)); }
  });
  scenarioHost.appendChild(btn);
  scenarioBtns.set(sc.key, btn);
}

/* A scenario button pings when it's clicked: the ring and fill jump to full
 * strength (`.ping`, transitions off) and then fade back to resting over 1s.
 * Both ends of that fade live in the stylesheet -- see the `.btn.ping` rules --
 * because a transition never has to name the resting value, it interpolates
 * out of whatever the cascade resolved for this button, which is the one thing
 * this page can't hardcode: the dark console/readout subtree carries its own
 * --accent alongside the light body. All that's left here is the handoff -- add
 * the class, force a reflow so the full-strength paint is what the transition
 * starts from, then drop it. */
function pingButton(btn) {
  clearTimeout(btn.__pingTimer);
  btn.classList.remove("ping", "ping-fade");
  void btn.offsetWidth;
  btn.classList.add("ping");
  if (reduced) {
    // No fade, but the ring still has to say "this one" -- held at the strength
    // the animated version starts from, then cleared on a plain timer.
    btn.__pingTimer = setTimeout(() => unpingButton(btn), 1000);
    return;
  }
  void btn.offsetWidth;
  btn.classList.add("ping-fade");
  btn.classList.remove("ping");
  // A hair past the 1s transition, so the class comes off after it has landed
  // rather than snapping the last frame.
  btn.__pingTimer = setTimeout(() => unpingButton(btn), 1100);
}
function unpingButton(btn) {
  clearTimeout(btn.__pingTimer);
  btn.classList.remove("ping", "ping-fade");
}

// --- the readout under the device: what the daemon's --status would say
const readout = document.getElementById("readout");
function paintReadout() {
  const s = sim.focused === null ? null : sim.get(sim.focused);
  const counts = {};
  for (const x of sim.list()) counts[x.state] = (counts[x.state] || 0) + 1;
  const tally = Object.entries(counts).map(([k, v]) => `${k}×${v}`).join("  ") || "—";
  readout.innerHTML =
    `<div class="readout-row">` +
    `<span><span class="k">focused</span> <b>${s ? esc(s.name) : "none"}</b></span>` +
    `<span><span class="k">state</span> <b style="color:${s ? hex(C.STATE_COLORS[s.state] || "white") : "var(--faint)"}">${s ? s.state : "—"}</b></span>` +
    `<span><span class="k">context</span> <b>${s && s.contextFraction ? Math.round(s.contextFraction * 100) + "%" : "—"}</b></span>` +
    `<span><span class="k">subagents</span> <b>${s ? s.subagents.length : 0}</b></span>` +
    `</div>` +
    `<div class="readout-row"><span><span class="k">board</span> <b>${tally}</b></span></div>`;
}
sim.on(() => paintReadout());

// ─── the legend board ─────────────────────────────────────────────────────
const legendSim = new Sim();
const legendUI = makeBoard(document.getElementById("legend-board"));
const LEGEND = ["permission", "plan", "error", "waiting", "working", "thinking",
                "streaming", "done", "idle"];
let solo = null;

LEGEND.forEach((state, i) => {
  const s = legendSim.add({ slot: i, name: state });
  s.state = state;
  s.stateSince = 0;
  s.turnStartedAt = -220;                        // a turn that has been running
  s.lastToolAt = -0.3;
  s.contextFraction = 0.15 + i * 0.09;
  s.attentionSince = state === "permission" ? -90 : null;
});
legendSim.overlays = [];                          // no spawn strike in a legend

// The two things that are not states: an agent running with permissions off,
// and a pile of subagents in the far corner, which is where they live.
const unsup = legendSim.add({ slot: 9, name: "unsupervised" });
unsup.state = "working"; unsup.stateSince = 0; unsup.turnStartedAt = -400;
unsup.lastToolAt = -0.4; unsup.unsupervised = true;
legendSim.get(4).subagents = [{ startedAt: -300 }, { startedAt: -1200 }, { startedAt: -60 }];

// The pile lands wherever spawnOrder leaves free, not just on the one slot
// EXTRA.subagent names for its swatch -- soloing "subagent" needs every dot.
const subagentSlots = [...legendSim.frame()]
  .filter(([, cell]) => cell.color === C.SUBAGENT_COLOR)
  .map(([slot]) => slot);

const EXTRA = {
  subagent: { slot: 15, color: C.SUBAGENT_COLOR,
    blurb: "subagents, stacked from the corner — ring is how long each has run" },
  unsupervised: { slot: 9, color: C.UNSUPERVISED_COLOR,
    blurb: "running unsupervised — a reserved color, used for nothing else" },
};

const legendList = document.getElementById("legend");
const legendEntryBySlot = new Map();     // slot -> { li, slots }
for (const state of [...LEGEND, "subagent", "unsupervised"]) {
  const extra = EXTRA[state];
  const color = extra ? extra.color : C.STATE_COLORS[state];
  const blurb = extra ? extra.blurb : C.STATE_BLURB[state];
  const slots = state === "subagent" ? subagentSlots : [extra ? extra.slot : LEGEND.indexOf(state)];
  const li = document.createElement("li");
  li.style.setProperty("--sw", hex(color));
  li.innerHTML = `<span class="swatch"></span><span class="name">${state}</span><span class="desc">${blurb}</span>`;
  li.addEventListener("pointerenter", () => { solo = slots; li.classList.add("solo"); });
  li.addEventListener("pointerleave", () => { solo = null; li.classList.remove("solo"); });
  legendList.appendChild(li);
  for (const slot of slots) legendEntryBySlot.set(slot, { li, slots });
}

// The inverse: hovering an encoder on the legend board solos the same state
// (every slot it occupies, not just the one under the cursor) and highlights
// its row, exactly as hovering the row solos the encoder.
for (const [slot, { li, slots }] of legendEntryBySlot) {
  const enc = legendUI.encoders[slot].el;
  enc.addEventListener("pointerenter", () => { solo = slots; li.classList.add("solo"); });
  enc.addEventListener("pointerleave", () => { solo = null; li.classList.remove("solo"); });
}

// ─── the status bar ───────────────────────────────────────────────────────
// Two halves, and the split matters: the left half is repainted every frame, so
// nothing interactive may live in it. The motion toggle is built once and never
// touched again, which is what lets it keep focus and hover across 30 repaints
// a second.
const statusbar = document.getElementById("statusbar");
const statusFields = document.createElement("span");
statusFields.className = "fields";
statusFields.style.display = "contents";

const statusSpacer = document.createElement("span");
statusSpacer.className = "spacer";

const motionBtn = document.createElement("button");
motionBtn.type = "button";
motionBtn.className = "motion-btn";
statusbar.append(statusFields, statusSpacer, motionBtn);

function paintMotionBtn() {
  motionBtn.innerHTML = `<span class="led"></span>motion: ${reduced ? "reduced" : "full"}`;
  motionBtn.setAttribute("aria-pressed", String(reduced));
  motionBtn.title = reduced
    ? "animations off — click to enable them"
    : "animations on — click to hold the board still";
}

// Follow the OS while nobody has expressed an opinion here; once they have,
// this page's setting is theirs and the OS stops speaking for them.
let motionTouched = false;
motionBtn.addEventListener("click", () => {
  motionTouched = true;
  reduced = !reduced;
  applyMotion();
  paintMotionBtn();
});
motionMQ.addEventListener("change", (e) => {
  if (motionTouched) return;
  reduced = e.matches;
  applyMotion();
  paintMotionBtn();
});
paintMotionBtn();

let fps = 30;
function paintStatus() {
  const blocked = sim.list().filter((s) => ["permission", "plan", "error", "waiting"].includes(s.state)).length;
  statusFields.innerHTML =
    `<span><span class="live" aria-hidden="true"></span> <span class="k">daemon</span> localhost:7654</span>` +
    `<span><span class="k">sessions</span> ${sim.list().length}/64</span>` +
    `<span><span class="k">bank</span> ${sim.bank + 1}/4</span>` +
    `<span><span class="k">blocking</span> ${blocked}</span>` +
    `<span class="hide-xs"><span class="k">uptime</span> ${fmtDur(sim.now)}</span>` +
    `<span class="hide-xs"><span class="k">render</span> ${Math.round(fps)}fps</span>` +
    `<span class="hide-xs"><span class="k">overlays</span> ${sim.overlays.length}</span>` +
    `<span class="hide-xs">simulated · no hardware attached</span>`;
}

// ─── the loop ─────────────────────────────────────────────────────────────
const STEP = 1 / C.FPS;
let acc = 0, last = null, fpsAcc = 0, fpsN = 0;
let visible = true;
document.addEventListener("visibilitychange", () => {
  visible = !document.hidden;
  last = null;   // don't fast-forward a scenario across a hidden tab
});

/* Nobody should arrive to a board that has already been running. The replica's
 * clock only advances while at least half of it is on screen -- so the boot
 * unwrap, the first session and the first prompt happen while you are looking
 * at them, and scrolling away pauses the whole thing rather than fast-forwarding
 * it. Same reasoning as the hidden-tab guard below, and the same mechanism: the
 * simulator has no wall clock of its own, so not ticking it *is* the pause.
 *
 * The ratio test needs its second half for a narrow window, where the replica is
 * taller than the viewport and so can never reach 50% of *itself*: covering half
 * the screen counts as being looked at. */
const stage = document.querySelector(".replica");
let onScreen = !stage, seeded = false;
if (stage) {
  new IntersectionObserver((entries) => {
    for (const e of entries) {
      onScreen = e.intersectionRatio >= 0.5 ||
                 e.intersectionRect.height >= window.innerHeight * 0.5;
    }
    if (onScreen && !seeded) { seeded = true; seed(); }
  }, { threshold: [0, 0.25, 0.5, 0.75, 1] }).observe(stage);
} else {
  seeded = true;
  seed();
}

/* The board's clock goes to the encoders along with the cells: the animation
 * bands are computed from it (js/twister.js), so all sixteen breathe in step the
 * way they do on the device. `reduced` rides along for the same reason -- the
 * renderer owns that preference now, rather than the stylesheet quietly
 * dropping half the board's motion behind it. */
function drawSim(s, ui, { soloIndex = null } = {}) {
  const cells = s.frame();
  for (let i = 0; i < C.PER_BANK; i++) {
    let cell = cells.get(i) || Cell({});
    if (soloIndex !== null && !soloIndex.includes(i)) cell = Cell({});
    ui.encoders[i].write(cell, s.now, reduced);
  }
}

function frame(t) {
  requestAnimationFrame(frame);
  if (!visible) return;
  if (last === null) { last = t; return; }
  let dt = (t - last) / 1000;
  last = t;
  if (dt > 0.25) dt = STEP;         // a long stall is one late frame, not a jump

  acc += dt;
  fpsAcc += dt;
  if (acc < STEP) return;
  const step = acc;
  acc = 0;
  // Board renders per second, not rAF ticks: the daemon paints at 30Hz and so
  // does this, however fast the display happens to be.
  fpsN++;
  if (fpsAcc > 0.5) { fps = fpsN / fpsAcc; fpsAcc = 0; fpsN = 0; }

  if (onScreen) {
    sim.tick(step);
    // The fuse under a held knob, checked where the daemon checks it: in the
    // frame loop, so the encoder empties and goes out in the same frame.
    const cleared = sim.checkHold();
    if (cleared) { held = true; con.push(cleared, "system", "cleared off the board", { instant: true }); }
    director.tick();
    con.tick(step);
  }
  legendSim.tick(step);

  drawSim(sim, { encoders });
  drawSim(legendSim, legendUI, { soloIndex: solo });

  // Nothing is printed under the knobs any more, so the only per-encoder text
  // left is the one a screen reader needs. It is cheap, and it only changes
  // when it changes.
  for (let i = 0; i < C.PER_BANK; i++) {
    const s = sim.get(i);
    const on = s && s.bank === sim.bank;
    encoders[i].describe(on ? `${s.name}, ${s.state}` : "empty");
  }

  paintStatus();
  if (Math.floor(sim.now * 4) % 2 === 0) paintReadout();
}

paintReadout();
paintStatus();
paintDemoStatus();
requestAnimationFrame(frame);        // `seed()` waits for the observer above

// ─── helpers ──────────────────────────────────────────────────────────────
function fmtDur(sec) {
  const s = Math.floor(sec % 60), m = Math.floor(sec / 60) % 60, h = Math.floor(sec / 3600);
  return `${h ? h + "h" : ""}${String(m).padStart(h ? 2 : 1, "0")}m${String(s).padStart(2, "0")}s`;
}
function esc(s) { return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
