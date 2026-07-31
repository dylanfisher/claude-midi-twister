/* The device, as pixels. This is the seam that mft/twister.py sits on: the
 * daemon writes a Cell to a MIDI port, and this writes the same Cell to the DOM.
 * Everything upstream of here -- config, board, overlays, font -- is the real
 * logic; only this file is a lie, and it is a lie about the wire and nothing
 * else.
 *
 * The drawing is the real encoder, part for part. Eleven round white indicator
 * LEDs sit on a black collar over 288 degrees with the gap at the *bottom*, and
 * the RGB LED is not the knob at all -- it is the fat curved lens filling that
 * gap, which is the only colored thing on the hardware. The cap over them is
 * knurled black plastic and stays black however loud the session is.
 *
 * An earlier version of this file lit the whole cap in the session's hue and
 * put the ring's gap on the right. It read well and it was wrong twice, and the
 * second one mattered: half of what this page teaches is where to look on a
 * device you are holding, and the color is at six o'clock.
 *
 * Ring position 0..127 fills the arc from the lower left; brightness lights
 * the lens. The color spills onto the face under the knob, kept to a minimal,
 * near-uniform glow rather than tracking brightness one-for-one -- and it
 * never spills onto the ring or collar, which stay on channel 1's neutral
 * white the same as the real hardware.
 */

import * as C from "./config.js?v=73";
import { hex } from "./config.js?v=73";

const SEGMENTS = 11;
const ARC_START = 216;    // degrees, clockwise from 12 o'clock; the lower left
const ARC_SWEEP = 288;    // ... round to the lower right: 72 degrees left at six
const CX = 50, CY = 47;   // the knob rides a little high in its square
const R_RING = 34.5;      // indicator LEDs and the RGB lens share one radius
const NS = "http://www.w3.org/2000/svg";

function polar(cx, cy, r, deg) {
  const rad = ((deg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
}

function arcPath(cx, cy, r, a0, a1) {
  const [x0, y0] = polar(cx, cy, r, a0);
  const [x1, y1] = polar(cx, cy, r, a1);
  const large = Math.abs(a1 - a0) > 180 ? 1 : 0;
  return `M ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1}`;
}

/* One frame of the hardware's two animation bands, off the board's own clock.
 *
 * These were CSS keyframes until they turned out to be the one thing on the
 * page a user preference could switch off behind the renderer's back: under
 * `prefers-reduced-motion` the stylesheet dropped `animation` and every
 * strobing state went dead flat, while everything modulated in here -- the
 * subagent shimmer, the ring sweeps, the ambient breath -- carried on. Half the
 * board honouring a preference is worse than either answer, and from in here
 * the lens looked lit and correct.
 *
 * Driving them from the clock is also the more faithful drawing, which is the
 * argument that would have won on its own. The device's animations run off the
 * MIDI clock the daemon supplies, so all sixteen encoders breathe *in unison* --
 * that is why `config.DARK_VALUE` is 18 and not 17, the slowest pulse. A CSS
 * animation starts when the class lands on its element, so sixteen encoders each
 * breathed on whatever phase they happened to be claimed at: the page was
 * contradicting the one hardware fact it exists to teach.
 *
 * The gate is the keyframe it replaces. The breath is not: see BAND_FLOOR. */

//: The bottom of both bands. The gate has always been here; the breath used to
//: bottom out at 0.3, which cost it the deepest third of the swing it had
//: available and left a slow pulse looking like a lit encoder wavering slightly.
//: Both bands now travel the same distance, which is also the honest reading --
//: on the device they are two shapes over one range, not two ranges.
const BAND_FLOOR = 0.12;
//: How the breath spends its cycle, as an exponent on a raised cosine. A plain
//: cosine is symmetric, so a breath spends as long dark as lit and its peak is
//: an instant you can miss between glances; under 1 it climbs early and dwells
//: near the top, which is what makes the swing read as a pulse rather than as a
//: slow wobble. Toward 0 it approaches a gate. 1.0 restores the old curve.
const BAND_DWELL = 0.6;

function bandLevel(anim, now) {
  const period = C.animPeriod(anim);
  if (period <= 0) return 1;
  const phase = ((now % period) + period) % period / period;
  if (C.animIsGate(anim)) return phase < 0.5 ? 1 : BAND_FLOOR;   // hard on/off
  const swell = (1 - Math.cos(phase * 2 * Math.PI)) / 2;         // a soft breath
  return BAND_FLOOR + (1 - BAND_FLOOR) * Math.pow(swell, BAND_DWELL);
}

function el(name, attrs) {
  const node = document.createElementNS(NS, name);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  return node;
}

/* The gradients that make flat circles read as moulded plastic: light from the
 * top left. Document-wide and built once -- three boards on this page would
 * otherwise define the same ids three times, and the last one would win for
 * everybody. */
function ensureDefs() {
  if (document.getElementById("mft-defs")) return;
  const svg = el("svg", { id: "mft-defs", width: "0", height: "0", "aria-hidden": "true" });
  svg.style.position = "absolute";
  svg.innerHTML = `
    <defs>
      <radialGradient id="mft-collar" cx="34%" cy="20%" r="88%">
        <stop offset="0%" stop-color="#2e2e2e"/>
        <stop offset="55%" stop-color="#161616"/>
        <stop offset="100%" stop-color="#070707"/>
      </radialGradient>
      <linearGradient id="mft-cap" x1="20%" y1="0%" x2="80%" y2="100%">
        <stop offset="0%" stop-color="#1a1a1a"/>
        <stop offset="45%" stop-color="#080808"/>
        <stop offset="100%" stop-color="#000"/>
      </linearGradient>
      <linearGradient id="mft-cap-top" x1="25%" y1="0%" x2="75%" y2="100%">
        <stop offset="0%" stop-color="#343434"/>
        <stop offset="60%" stop-color="#1c1c1c"/>
        <stop offset="100%" stop-color="#101010"/>
      </linearGradient>
    </defs>`;
  document.body.appendChild(svg);
}

/** Build one encoder's DOM. Returns a handle with `write(cell, now, reduced)`. */
export function makeEncoder(index) {
  ensureDefs();

  const root = document.createElement("div");
  root.className = "enc";
  root.dataset.slot = String(index);
  root.tabIndex = 0;
  root.setAttribute("role", "button");
  root.setAttribute("aria-label", `encoder ${index + 1}, empty`);

  const svg = el("svg", { viewBox: "0 0 100 100", "aria-hidden": "true" });

  // The collar the LEDs are set into, and the shadow it drops on the face.
  svg.appendChild(el("ellipse", { cx: CX, cy: CY + 2.5, rx: 43, ry: 42, class: "shade" }));
  svg.appendChild(el("circle", { cx: CX, cy: CY, r: 42.5, class: "collar" }));
  svg.appendChild(el("circle", { cx: CX, cy: CY, r: 41.9, class: "collar-rim" }));

  const step = ARC_SWEEP / SEGMENTS;
  const angle = (i) => ARC_START + (i + 0.5) * step;

  // Unlit lenses first: eleven white plastic dots, which is why an encoder with
  // nothing on it still reads as an encoder.
  for (let i = 0; i < SEGMENTS; i++) {
    const [x, y] = polar(CX, CY, R_RING, angle(i));
    svg.appendChild(el("circle", { cx: x, cy: y, r: 4.3, class: "dot-off" }));
  }
  const dots = [];
  for (let i = 0; i < SEGMENTS; i++) {
    const [x, y] = polar(CX, CY, R_RING, angle(i));
    const dot = el("circle", { cx: x, cy: y, r: 4.3, class: "dot" });
    svg.appendChild(dot);
    dots.push(dot);
  }

  // The RGB lens in the gap, dark body first, then the lit lens and its bloom.
  // Three paths on one arc: the dark one is the moulding, which is there at
  // every brightness including none.
  const gapMid = ARC_START + ARC_SWEEP + (360 - ARC_SWEEP) / 2;
  const gapArc = arcPath(CX, CY, R_RING, gapMid - 28, gapMid + 28);
  svg.appendChild(el("path", { d: gapArc, class: "lens-off" }));
  svg.appendChild(el("path", { d: gapArc, class: "lens-bloom" }));
  svg.appendChild(el("path", { d: gapArc, class: "lens" }));

  // The cap: a knurled skirt under a smooth top, and it stays black.
  svg.appendChild(el("circle", { cx: CX, cy: CY, r: 27, class: "cap" }));
  const teeth = 44;
  for (let i = 0; i < teeth; i++) {
    const a = (i * 360) / teeth;
    const [x0, y0] = polar(CX, CY, 22.2, a);
    const [x1, y1] = polar(CX, CY, 26.4, a);
    svg.appendChild(el("line", { x1: x0, y1: y0, x2: x1, y2: y1, class: "knurl" }));
  }
  svg.appendChild(el("circle", { cx: CX, cy: CY, r: 21.8, class: "cap-top" }));

  root.appendChild(svg);

  // Nothing is printed under the knob. The real face is blank there, and a name
  // silkscreened on it was the one thing on this drawing competing with the
  // lens for attention -- which is the opposite of what the page is teaching.
  // The session's name lives in the readout and in the aria-label instead.

  let last = "";
  const handle = {
    el: root,
    /** `now` is the board's clock; `reduced` is the page's motion preference,
     *  and it is the whole board's answer rather than the stylesheet's, so an
     *  encoder that has been asked to hold still holds still on every surface
     *  at once. A still encoder gives its lens back to brightness -- the same
     *  swap the wire makes, in the other direction. */
    write(cell, now = 0, reduced = false) {
      const animating = Boolean(cell.anim) && !reduced;
      const pulse = animating ? bandLevel(cell.anim, now) : 1;

      // De-duplicated the same way mft/twister.py de-duplicates its writes:
      // sixteen encoders at 30fps is a lot of DOM if you don't. An animating
      // encoder moves every frame by definition and writes every frame; the
      // other fifteen still cost nothing.
      // `animating` is in the key on its own account and not as a proxy for the
      // pulse: an encoder asked to hold still lands on a pulse of 1, which is a
      // value the band passes through anyway, so the frame the preference
      // changes on can otherwise key identically to the one before it and be
      // skipped -- leaving the lens at the animating level with nothing moving.
      const key = `${cell.color}|${cell.anim}|${animating}|${cell.ring}` +
                  `|${cell.brightness.toFixed(3)}|${cell.ringLight.toFixed(3)}|${pulse.toFixed(3)}`;
      if (key === last) return;
      last = key;

      const color = cell.color === null ? null : hex(cell.color);
      const fill = (cell.ring / 127) * SEGMENTS;

      for (let i = 0; i < SEGMENTS; i++) {
        // Partial on the leading segment: the ring has real grayscale per LED,
        // which is what makes a slow stopwatch look like it is moving at all.
        const amount = Math.max(0, Math.min(1, fill - i));
        dots[i].style.opacity = (amount * cell.ringLight).toFixed(3);
      }

      // The lens, its bloom and the spill are all one hue at one strength, so
      // they are three custom properties and the stylesheet does the rest --
      // which is what lets the animation multiply them instead of overwriting
      // an opacity a dim encoder needs to keep.
      //
      // An animating cell's lens sits at full and lets the band do the
      // modulating, which is what `mft.twister.Twister.write` does on the wire:
      // channel 3 carries *either* an animation or a brightness, so a strobing
      // encoder's brightness is never sent and never reaches its lens. This
      // page used to send both and multiply them, and two of the states that
      // animate also breathe their brightness in the renderer (`waiting`, at a
      // 2.4s period, against a 2s or 4s band). Two breaths at periods that
      // don't divide beat against each other: the lens swelled on some cycles
      // and sat almost still on others, which read as the pulse not working.
      const dark = color === null || cell.brightness <= 0.001;
      const level = animating ? 1 : cell.brightness;
      root.style.setProperty("--glow", dark ? "transparent" : color);
      root.style.setProperty("--glow-a", dark ? "0" : level.toFixed(3));
      root.style.setProperty("--pulse", pulse.toFixed(3));
    },
    describe(text) {
      root.setAttribute("aria-label", `encoder ${index + 1}, ${text}`);
    },
  };
  return handle;
}

/** A whole 4x4 bank plus the device's chassis. */
export function makeBoard(host) {
  host.classList.add("twister");
  host.innerHTML = "";

  // Nothing is printed on the real top face -- the brand is on the front edge,
  // below the knobs -- so nothing is printed on this one either.
  const chassis = document.createElement("div");
  chassis.className = "chassis";

  // The aluminium chassis has its own bordered plastic bezel set into the
  // face, a shade darker and flatter than the metal around it, and the grid
  // of encoders sits inside that.
  const panel = document.createElement("div");
  panel.className = "panel";

  const grid = document.createElement("div");
  grid.className = "grid";
  const encoders = [];
  for (let i = 0; i < C.PER_BANK; i++) {
    const enc = makeEncoder(i);
    encoders.push(enc);
    grid.appendChild(enc.el);
  }
  panel.appendChild(grid);
  chassis.appendChild(panel);
  host.appendChild(chassis);

  // No bank row. On the hardware the bank switch is a side button on an edge a
  // top-down drawing does not have, and drawing four numbered pads under the
  // board invented a control that isn't there. The board follows a block onto
  // its own bank by itself (`Sim._followAlerts`, the daemon's `_follow_alerts`),
  // which is the behaviour worth showing anyway; the status bar says which bank
  // you are looking at.

  return { encoders, chassis };
}
