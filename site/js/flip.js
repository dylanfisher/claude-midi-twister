/* The page turning over.
 *
 * There are two pages in this site and they are the same page twice: the front
 * is paper with lit things on it, the bench is the negative of that — dark
 * stock, paper-colored panels. So the navigation between them is not a link,
 * it is a sheet being turned face-down, and the palette flip you land in is the
 * back of the sheet you just turned.
 *
 * The whole thing is two halves of one rotation. The outgoing page turns from
 * 0 to -90 degrees and the incoming one arrives at +90 and finishes the turn,
 * which is why the halves have different easings: the first is ease-in (a sheet
 * accelerating as it goes past vertical) and the second ease-out (the same
 * sheet slowing as it lands). Matching easings read as two separate animations.
 *
 * A View Transition would have been fewer lines and was tried first. It cannot
 * do this: the two documents are captured as flat screenshots and cross-faded
 * in the same plane, so a rotation past 90 degrees shows the *front* of the new
 * page edge-on rather than the back of the old one, and the sheet never reads
 * as having two sides. Two ordinary CSS animations either side of a real
 * navigation do, and they work in every browser.
 *
 * The handoff is one sessionStorage key. It is set immediately before the
 * navigation and consumed on the very next load, so a page opened any other way
 * — bookmark, refresh, a link from outside — has nothing to consume and simply
 * appears, which is correct: nothing was turned over.
 */

const KEY = "mft-flip";
const OUT_MS = 260;

const root = document.documentElement;

function reducedMotion() {
  // The same two-way rule the board uses: the corner toggle on the front page
  // wins over the OS in both directions, and it writes `data-motion` on <html>.
  const attr = root.dataset.motion;
  if (attr === "full") return false;
  if (attr === "reduced") return true;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/* The rotation happens about the middle of what you are looking at, not the
 * middle of the document — on a page four screens tall those are nowhere near
 * each other, and spinning about a point two screens above the viewport looks
 * like the page being flung, not turned. */
function setOrigin() {
  const y = window.scrollY + window.innerHeight / 2;
  root.style.setProperty("--flip-origin", `${y}px`);
}

// ── the second half: we arrived here by being turned over ──────────────────
function playIn() {
  if (!sessionStorage.getItem(KEY)) return;
  sessionStorage.removeItem(KEY);
  if (reducedMotion()) return;
  setOrigin();
  root.classList.add("flip-in");
  window.addEventListener(
    "animationend",
    () => root.classList.remove("flip-in"),
    { once: true },
  );
}

// ── the first half: turn this page away, then leave ────────────────────────
function flipTo(href) {
  if (reducedMotion()) { location.href = href; return; }
  setOrigin();
  root.classList.add("flip-out");
  sessionStorage.setItem(KEY, "1");
  // A timer rather than animationend: if the animation is dropped for any
  // reason the navigation still has to happen, and a link that sometimes does
  // nothing is far worse than a link that sometimes doesn't spin.
  setTimeout(() => { location.href = href; }, OUT_MS);
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-flip]");
  if (!a) return;
  // Anything that isn't a plain left-click is the browser's to handle: a new
  // tab must not be preceded by this tab turning over.
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  flipTo(a.href);
});

// Coming back through history restores the document mid-turn, class and all.
window.addEventListener("pageshow", (e) => {
  if (e.persisted) root.classList.remove("flip-out", "flip-in");
});

playIn();
