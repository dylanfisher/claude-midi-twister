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

import * as C from "./config.js?v=58";
import { hex } from "./config.js?v=58";

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

/** Build one encoder's DOM. Returns a handle with `write(cell)`. */
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
    write(cell) {
      // De-duplicated the same way mft/twister.py de-duplicates its writes:
      // sixteen encoders at 30fps is a lot of DOM if you don't.
      const key = `${cell.color}|${cell.anim}|${cell.ring}|${cell.brightness.toFixed(3)}|${cell.ringLight.toFixed(3)}`;
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
      // they are two custom properties and the stylesheet does the rest --
      // which is also what lets the animation bands multiply them instead of
      // overwriting an opacity a dim encoder needs to keep.
      const dark = color === null || cell.brightness <= 0.001;
      root.style.setProperty("--glow", dark ? "transparent" : color);
      root.style.setProperty("--glow-a", dark ? "0" : cell.brightness.toFixed(3));

      // The hardware's own animation bands, as CSS. On the device these are
      // rate values 1-16 driven off the MIDI clock the daemon supplies; here
      // they are a keyframe duration at the same beat divisions.
      if (cell.anim) {
        const period = C.animPeriod(cell.anim);
        root.dataset.anim = C.animIsGate(cell.anim) ? "gate" : "pulse";
        root.style.setProperty("--anim-period", `${period}s`);
      } else {
        delete root.dataset.anim;
      }
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
