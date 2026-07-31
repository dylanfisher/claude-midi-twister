/* A port of mft/config.py, for the simulator on the showcase page.
 *
 * That file is the source of truth; this one exists because the browser can't
 * import it. Every number here is copied from it, and the MIDI value is kept
 * next to each color so the drift is visible if it ever happens.
 *
 * The one thing invented here is the hex column. The repo has no hex values
 * anywhere -- a color on this hardware is a channel-2 value 0..127 on the
 * device's own hue wheel, and what that wheel actually emits is a property of
 * the LEDs. These are eyeballed approximations of the wheel positions, good
 * enough to tell the ten states apart on a screen, which is the entire job.
 */

/** The device's hue wheel, as `name: {midi, hex}`. midi is the truth. */
export const COLORS = {
  white:   { midi: 127, hex: "#f2f4f8" },
  blue:    { midi: 1,   hex: "#2b5cff" },
  azure:   { midi: 12,  hex: "#2b9cff" },
  cyan:    { midi: 25,  hex: "#3fe0e0" },
  spring:  { midi: 37,  hex: "#3fe08a" },
  green:   { midi: 48,  hex: "#4ade4a" },
  lime:    { midi: 58,  hex: "#9ade3f" },
  yellow:  { midi: 62,  hex: "#e8e03f" },
  amber:   { midi: 68,  hex: "#f0a83f" },
  orange:  { midi: 72,  hex: "#f5762a" },
  red:     { midi: 78,  hex: "#f0322a" },
  magenta: { midi: 98,  hex: "#e63fd0" },
  purple:  { midi: 110, hex: "#a04ff0" },
  violet:  { midi: 118, hex: "#7a4ff5" },
};

/** A color name, a raw wheel value, or null, as a CSS color. */
export function hex(color) {
  if (color === null || color === undefined) return "#000000";
  if (typeof color === "string") return (COLORS[color] || COLORS.white).hex;
  return wheelHex(color);
}

/* A raw wheel value interpolated between the two named colors it falls
 * between. `working` warms its hue toward red as tool calls fail, and it does
 * that by returning a number rather than a name -- see working_color() in
 * mft/render.py. Without this the failure heat would snap instead of ramp. */
const WHEEL = Object.values(COLORS)
  .filter((c) => c.midi !== 127)
  .sort((a, b) => a.midi - b.midi);

function wheelHex(value) {
  const v = Math.max(0, Math.min(127, value));
  if (v >= 127) return COLORS.white.hex;
  let lo = WHEEL[0], hi = WHEEL[WHEEL.length - 1];
  for (let i = 0; i < WHEEL.length - 1; i++) {
    if (v >= WHEEL[i].midi && v <= WHEEL[i + 1].midi) {
      lo = WHEEL[i]; hi = WHEEL[i + 1]; break;
    }
  }
  if (hi.midi === lo.midi) return lo.hex;
  return mixHex(lo.hex, hi.hex, (v - lo.midi) / (hi.midi - lo.midi));
}

export function mixHex(a, b, t) {
  const pa = parseInt(a.slice(1), 16), pb = parseInt(b.slice(1), 16);
  const ch = (p, s) => (p >> s) & 255;
  const m = (s) => Math.round(ch(pa, s) + (ch(pb, s) - ch(pa, s)) * t);
  return `rgb(${m(16)}, ${m(8)}, ${m(0)})`;
}

// --- animation bands -------------------------------------------------------
// Channel 3, low band: 1-8 gate (hard on/off), 9-16 pulse (a soft breath).
// The label is the rate in beats. The daemon supplies the MIDI clock, so all
// sixteen encoders breathe in unison -- which is why 17, the slowest pulse, is
// not "off": sixteen encoders breathing together is a very loud kind of off.
const RATES = ["1/8", "1/4", "1/2", "1", "2", "4", "8", "16"];
export const ANIM_NONE = 0;
export const ANIM_GATE = {};
export const ANIM_PULSE = {};
RATES.forEach((r, i) => { ANIM_GATE[r] = 1 + i; ANIM_PULSE[r] = 9 + i; });

/** Beats per second for an animation value, for the CSS keyframe duration. */
export function animPeriod(anim) {
  if (!anim) return 0;
  const gate = anim >= 1 && anim <= 8;
  const beats = [8, 4, 2, 1, 0.5, 0.25, 0.125, 0.0625][(anim - (gate ? 1 : 9))];
  return (beats === undefined ? 1 : beats) * 1.0;  // one beat = one second here
}
export function animIsGate(anim) { return anim >= 1 && anim <= 8; }

// --- states ----------------------------------------------------------------
/** mft/config.py STATE_COLORS. `ended` is dark. */
export const STATE_COLORS = {
  permission: "red",     // a gate is open, waiting on you: fast red strobe
  plan: "yellow",        // a plan is written and wants a yes
  error: "red",          // rate limit, overload, billing: solid red, no motion
  waiting: "amber",      // agent is idling for input: slow amber breath
  streaming: "spring",
  working: "orange",     // the ring is the turn's length
  thinking: "cyan",
  done: "green",
  idle: "green",
  ended: null,
};

/** mft/config.py STATE_ANIM. Absent means no animation. */
export const STATE_ANIM = {
  permission: ANIM_GATE["4"],
  plan: ANIM_GATE["2"],
  waiting: ANIM_PULSE["1/2"],
  streaming: ANIM_PULSE["2"],
};
export const SLOW_ANIM = ANIM_PULSE["1/4"];

/** Loudest first. board.arbitrate_motion ranks by this before it ranks by debt. */
export const STATE_PRIORITY = [
  "permission", "plan", "error", "waiting", "streaming",
  "working", "thinking", "done", "idle", "ended",
];

/** One line of English per state, for the legend and the screen reader. */
export const STATE_BLURB = {
  permission: "asking permission — blocked until you answer",
  plan: "a plan is ready and needs approval",
  error: "errored (rate limit, overload, billing)",
  waiting: "waiting on you",
  streaming: "streaming a reply",
  working: "working — the ring is how long the turn has run",
  thinking: "thinking",
  done: "finished, fading out",
  idle: "idle — the ring is the context window",
  ended: "gone",
};

// --- brightness ------------------------------------------------------------
export const IDLE_BRIGHTNESS = 0.22;
export const ACTIVE_BRIGHTNESS = 1.0;
export const ACTIVE_FLOOR = 0.55;
/* mft/config.py's RING_CEILING is 0.5, and this page used to copy it verbatim
 * -- but that fraction lands differently on the two surfaces. On channel 6 it
 * is a MIDI value mid-way through a narrow 65-95 band, which still reads as lit;
 * here it is a literal CSS opacity on a white dot, which reads as flat grey.
 * Raised for legibility on this page specifically, same as ATTENTION_CEILING
 * below is lowered for the opposite reason. */
export const RING_CEILING = 0.75;
export const ATTENTION_RING_LEVEL = 1.0;
export const GAUGE_STALE_LEVEL = 0.08;
export const DONE_DEBT_CEILING = 0.5;
export const ATTENTION_FLOOR = 0.45;

/* Deliberately below mft/config.py's ATTENTION_CEILING (1.0), and the same
 * split applies to SWEEP_BRIGHTNESS and DONE_FLASH_BRIGHTNESS below. On the
 * real hardware `Twister.write` only ever sends brightness when a cell has no
 * RGB animation, so a permission gate's brightness never reaches its lens --
 * only the ring feels this constant there, and twister.js now does the same.
 * What is left for it here is `error`, the one insistent state that doesn't
 * animate: solid red, brightness straight onto the lens. The ceiling stays
 * under 1.0 because that is the one case where nothing else is modulating it. */
export const ATTENTION_CEILING = 0.8;
export const ATTENTION_RAMP_SECONDS = 300.0;
export const ATTENTION_ANIM_STEPS = 2;

/* `thinking` and `streaming` are narration, not a block, and used to borrow
 * `ACTIVE_BRIGHTNESS` wholesale -- full lens glow for the mere fact that a
 * session is talking. Under half, so it is legible as "something is moving"
 * without reading as urgent.
 *
 * In practice this is now `thinking`'s number alone: `streaming` animates, and
 * an animated cell's lens is driven by its keyframes rather than by brightness
 * (twister.js, mirroring the wire). 0.3 was tuned against the old, very
 * compressed opacity curve, where it came out at 0.59; against the steep one it
 * would have dropped to 0.6 of what a working encoder shows, which is dimmer
 * than a state this close to the active band should read. */
export const SWEEP_BRIGHTNESS = 0.42;

/* Peak glow on the instant a turn finishes, before the flash decays toward
 * IDLE_BRIGHTNESS. Under ACTIVE_BRIGHTNESS on purpose: the flash fires on
 * every turn, not just the ones worth the lens's harshest setting, and
 * DONE_DEBT_CEILING still climbs back toward it later if the result goes
 * unread. */
export const DONE_FLASH_BRIGHTNESS = 0.6;

// --- ring ------------------------------------------------------------------
export const PIP = 2;                    // render.PIP: "this encoder is claimed"
export const RING_MAX_VALUE = 127;
export const CONTEXT_RING_FLOOR = 4;
export const TURN_RING_FULL_SECONDS = 3600.0;
export const ARC_SEGMENTS = 16;

/* The stopwatch shape. Front-loaded: nearly every turn finishes inside a few
 * minutes and the ones you care about run to half an hour, and no linear ring
 * shows you both. Marks are (fraction of span, fraction of ring), and on the
 * hour span they read: one minute an eighth, five minutes a quarter, fifteen a
 * half, half an hour three quarters. */
export const STOPWATCH_CURVE = [
  [0.0, 0.0],
  [1 / 60, 0.125],
  [1 / 12, 0.25],
  [0.25, 0.5],
  [0.5, 0.75],
  [1.0, 1.0],
];

// --- timings, all from mft/config.py ---------------------------------------
export const FPS = 30;
export const DONE_FADE_SECONDS = 180.0;
export const TOOL_KICK_SECONDS = 1.2;
export const STALL_SECONDS = 45.0;
export const STALL_FADE_SECONDS = 120.0;

export const SPAWN_SECONDS = 1.8;
export const SPAWN_FLASHES = 3;
export const SPAWN_SETTLE = 0.55;
export const SPAWN_COLOR = "red";

export const CLEAR_SECONDS = 1.0;
export const CLEAR_SETTLE = 0.6;

export const COMPACT_DRAIN = 0.6;       // COMPACT_DRAIN_SECONDS
export const COMPACT_REFILL = 0.8;      // COMPACT_REFILL_SECONDS
export const COMPACT_HOLD = 0.35;       // the beat between them, this page only

export const HOLD_SECONDS = 0.8;
export const DISMISS_ARM = 0.2;         // DISMISS_ARM_SECONDS

export const FOCUS_SECONDS = 0.5;       // the swell, this page only

export const BOOT_RISE = 1.0;           // BOOT_UNWRAP_RISE_SECONDS
export const BOOT_HOLD = 0.5;           // BOOT_UNWRAP_HOLD_SECONDS
export const BOOT_SPIRAL = 1.4;         // BOOT_UNWRAP_SPIRAL_SECONDS
export const BOOT_FALL = 0.3;           // BOOT_UNWRAP_FALL_SECONDS
export const BOOT_UNWRAP_COLOR = "blue";
export const BOOT_WORD = "CLAUDE";
export const BOOT_FADE_SECONDS = 0.75;  // one letter's strike-and-decay
export const BOOT_LETTER_SECONDS = BOOT_FADE_SECONDS;

export const SHUTDOWN_RISE = 0.3;
export const SHUTDOWN_SPIRAL = 1.6;
export const SHUTDOWN_HOLD = 0.9;
export const SHUTDOWN_FADE = 1.4;
export const SHUTDOWN_DARK_LEVEL = 0.02;
export const SHUTDOWN_COLOR = "violet";

export const BANNER_COLOR = "red";
export const BANNER_SECONDS = 2.4;
export const BANNER_LETTER_SECONDS = BANNER_SECONDS / 4;   // RATE is four letters

export const AMBIENT_PERIOD = 12.0;     // AMBIENT_PERIOD_SECONDS
export const AMBIENT_BRIGHTNESS = 0.14;
export const WAITING_BRIGHTNESS = 0.3;
export const WAITING_PERIOD = 9.0;      // WAITING_PERIOD_SECONDS
export const WAITING_WIDTH = 0.55;
export const WAITING_SECONDS = 180.0;

export const SUBAGENT_COLOR = "violet";
export const SUBAGENT_RING_FULL_SECONDS = 3600.0;   // SUBAGENT_RING_SECONDS
export const SUBAGENT_RING_FLOOR = 4;
export const SUBAGENT_BRIGHTNESS = 0.45;
export const SUBAGENT_IDLE_BRIGHTNESS = 0.3;
export const SUBAGENT_KICK_BRIGHTNESS = 0.85;
export const SUBAGENT_KICK_SECONDS = 3.0;
export const UNSUPERVISED_COLOR = "magenta";
export const FAILURE_HEAT_COLOR = "red";
export const FAILURE_HEAT_CURVE = 2.0;

/* The real SLEEP_FADE_SECONDS is 20.0 -- twenty seconds is right for a machine
 * going to sleep and absurd for a button on a web page, so the demo uses the
 * wake time in both directions. The only knowingly-shortened number here. */
export const SLEEP_FADE_SECONDS = 2.0;
export const SLEEP_WAKE_SECONDS = 0.4;
export const SLEEP_DARK_LEVEL = 0.02;

// --- geometry --------------------------------------------------------------
export const PER_BANK = 16;
export const BANKS = 4;
export const SLOT_COUNT = PER_BANK * BANKS;
export const BANK_COOLDOWN = 20.0;
