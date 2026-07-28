/* The daemon, minus everything that touches the outside world.
 *
 * mft/state.py owns which encoder a session is on; mft/events.py folds one hook
 * payload into one session; mft/daemon.py runs the loop. This is all three,
 * shrunk to what a page needs. It is deliberately event-driven in the same
 * shape as the real thing -- the console pane emits hook events, this folds
 * them, the board renders the result -- because a simulator that skipped the
 * events would be showing you a different program.
 *
 * Note what is *not* here: no HTTP, no process table, no focus adapters, no
 * MIDI. That split is not a convenience for the demo, it is the codebase's own
 * seam. The pure half ports; the impure half has nothing to port to.
 */

import * as C from "./config.js?v=59";
import { render, Cell, arbitrateMotion, subagentCell, ambient, markFocus, capRings, spawnOrder } from "./board.js?v=59";
import {
  SpawnOverlay, ClearOverlay, CompactOverlay, DismissOverlay, FocusOverlay,
  TextOverlay, UnwrapOverlay, ShutdownOverlay, WaitingOverlay, SleepOverlay,
} from "./overlays.js?v=59";

let nextId = 1;

export function makeSession(slot, { name = "~/project", bank = 0 } = {}) {
  return {
    id: nextId++,
    slot,
    bank,
    name,
    state: "idle",
    stateSince: 0,
    turnStartedAt: null,
    lastToolAt: null,
    contextFraction: null,
    failureFraction: 0,
    attentionSince: null,
    unsupervised: false,
    subagents: [],
    history: [],       // recent tool calls, for the console pane
    transcript: [],    // what the console pane shows
    pending: null,     // the question a permission gate is asking
  };
}

/** Which states owe you something. Only these accrue attention debt. */
const BLOCKING = new Set(["permission", "plan", "error", "waiting"]);

export class Sim {
  constructor() {
    this.now = 0;
    this.sessions = new Map();     // slot -> session
    this.overlays = [];
    this.bank = 0;
    this.focused = null;
    this.hold = null;
    this.asleep = false;
    this.bankChangedAt = -Infinity;
    this.listeners = new Set();
    this.waiting = new WaitingOverlay(0);
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(kind, payload) { for (const fn of this.listeners) fn(kind, payload); }

  // --- slots -------------------------------------------------------------
  /* Invariant 3, in the one form the simulator can honour: a terminal owns a
   * slot, so a session that clears keeps its encoder. Sessions fill top-left
   * forwards; there is no reconcile pass here because there are no real tabs to
   * mistake for each other. */
  freeSlot() {
    for (const s of spawnOrder(false)) if (!this.sessions.has(s)) return s;
    return null;
  }

  add(opts = {}) {
    const slot = opts.slot ?? this.freeSlot();
    if (slot === null) return null;
    const session = makeSession(slot, opts);
    session.stateSince = this.now;
    this.sessions.set(slot, session);
    this.overlays.push(new SpawnOverlay(this.now, slot));
    this.emit("added", session);
    return session;
  }

  remove(slot) {
    const s = this.sessions.get(slot);
    if (!s) return;
    this.sessions.delete(slot);
    if (this.focused === slot) this.focused = null;
    this.emit("removed", s);
  }

  get(slot) { return this.sessions.get(slot); }
  list() { return [...this.sessions.values()]; }

  // --- events (mft/events.py) --------------------------------------------
  /** Fold one hook event into one session. The names are the real hook names. */
  event(slot, kind, payload = {}) {
    const s = this.sessions.get(slot);
    if (!s) return;
    const was = s.state;

    switch (kind) {
      case "UserPromptSubmit":
        this.setState(s, "working");
        s.turnStartedAt = this.now;
        s.failureFraction = 0;
        s.attentionSince = null;
        break;
      case "Thinking":
        this.setState(s, "thinking");
        break;
      case "Streaming":
        this.setState(s, "streaming");
        break;
      case "PreToolUse":
        this.setState(s, "working");
        s.lastToolAt = this.now;
        s.history.push({ tool: payload.tool || "Bash", target: payload.target || "" });
        if (s.history.length > 40) s.history.shift();
        s.contextFraction = Math.min(0.98, (s.contextFraction || 0.05) + 0.012);
        break;
      case "ToolFailed":
        // Failure heat: hue only. A failing agent is still working and must not
        // be able to promote itself into an alert.
        s.failureFraction = Math.min(1, s.failureFraction + 0.25);
        s.lastToolAt = this.now;
        break;
      case "Notification":
        this.setState(s, payload.plan ? "plan" : "permission");
        s.pending = payload.question || null;
        break;
      case "PermissionResolved":
        s.pending = null;
        this.setState(s, "working");
        s.lastToolAt = this.now;
        break;
      case "SubagentStart":
        s.subagents.push({ startedAt: this.now, label: payload.label || "explore" });
        break;
      case "SubagentStop":
        s.subagents.shift();
        break;
      case "Stop":
        this.setState(s, "done");
        s.turnStartedAt = null;
        s.failureFraction = 0;
        break;
      case "Idle":
        this.setState(s, "idle");
        break;
      case "Waiting":
        this.setState(s, "waiting");
        break;
      case "Error":
        this.setState(s, "error");
        s.pending = payload.question || "rate limit";
        break;
      case "Clear":
        s.contextFraction = null;
        s.history = [];
        s.subagents = [];
        this.setState(s, "idle");
        this.overlays.push(new ClearOverlay(this.now, slot));
        break;
      case "Compact": {
        const to = Math.max(0.12, (s.contextFraction || 0.6) * 0.35);
        this.overlays.push(new CompactOverlay(this.now, slot, to));
        s.contextFraction = to;
        break;
      }
      case "SessionEnd":
        this.setState(s, "ended");
        break;
      default:
        break;
    }
    if (s.state !== was) this.emit("state", s);
  }

  setState(s, state) {
    if (s.state === state) return;
    s.state = state;
    s.stateSince = this.now;
    // Debt starts the moment a session begins owing you something, and is
    // forgiven the instant you focus the tab.
    if (BLOCKING.has(state)) {
      if (s.attentionSince === null) s.attentionSince = this.now;
    } else if (state !== "done") {
      s.attentionSince = null;
    } else if (s.attentionSince === null) {
      s.attentionSince = this.now;
    }
    if (this.focused === s.slot) s.attentionSince = null;
  }

  // --- gestures ----------------------------------------------------------
  focus(slot) {
    const s = this.sessions.get(slot);
    if (!s) return;
    this.focused = slot;
    s.attentionSince = null;
    this.overlays.push(new FocusOverlay(this.now, slot));
    this.emit("focus", s);
  }

  /* Hold to clear. The fuse is armed on the way down and only fires from the
   * frame loop, so the encoder goes out under your finger at the moment the
   * ring empties rather than whenever you happen to let go. Nothing here
   * reaches the session: an encoder cleared from a live agent is claimed again
   * by its very next event. */
  startHold(slot) {
    if (!this.sessions.has(slot) || this.hold) return;
    this.hold = new DismissOverlay(this.now, slot);
    this.overlays.push(this.hold);
  }

  cancelHold() {
    if (!this.hold) return;
    this.overlays = this.overlays.filter((o) => o !== this.hold);
    this.hold = null;
  }

  /** Called once a frame: has anything burned all the way down? */
  checkHold() {
    if (!this.hold || !this.hold.matured(this.now)) return null;
    const slot = this.hold.slot;
    this.cancelHold();
    const s = this.sessions.get(slot);
    if (s) this.remove(slot);
    return s;
  }

  banner(word, color = C.BANNER_COLOR) {
    this.overlays.push(new TextOverlay(this.now, word, { color, letterSeconds: C.BANNER_LETTER_SECONDS }));
  }

  /* The daemon spells CLAUDE across the bank once the unwrap lands. The page
   * doesn't: the demo boots on page load, before anyone has read a word of the
   * copy, and five seconds of a word you already know is five seconds of the
   * board not doing the thing the page is here to show. The unwrap stays --
   * it's short, and it's what clears the board to black. */
  boot() {
    this.overlays.push(new UnwrapOverlay(this.now));
  }

  shutdown() { this.overlays.push(new ShutdownOverlay(this.now)); }

  sleep(asleep) {
    this.asleep = asleep;
    this.overlays.push(new SleepOverlay(this.now, !asleep));
  }

  /* Banks and the board following a prompt. A permission gate three banks away
   * is invisible, and an empty bank looks exactly like a dead daemon -- so a
   * block on another bank pulls the view onto itself. On a cooldown, because a
   * panel that moves under your hand is worse than one that is late. */
  followAlerts() {
    if (this.now - this.bankChangedAt < C.BANK_COOLDOWN) return;
    for (const s of this.sessions.values()) {
      if ((s.state === "permission" || s.state === "plan") && s.bank !== this.bank) {
        this.showBank(s.bank);
        return;
      }
    }
  }

  showBank(bank) {
    if (bank === this.bank) return;
    this.bank = bank;
    this.bankChangedAt = this.now;
    this.emit("bank", bank);
  }

  // --- the frame ---------------------------------------------------------
  /** The whole board for `this.now`, as a Map of slot -> Cell. */
  frame() {
    const cells = new Map();
    const onBank = new Map();
    for (let i = 0; i < C.PER_BANK; i++) cells.set(i, Cell({}));

    for (const s of this.sessions.values()) {
      if (s.bank !== this.bank) continue;
      onBank.set(s.slot, s);
      cells.set(s.slot, render(s, this.now));
    }

    // Nothing claimed: the slow breath that says the daemon is alive. Going
    // dark because nothing is happening looks exactly like going dark because
    // the daemon died.
    if (onBank.size === 0 && this.overlays.length === 0) {
      for (let i = 0; i < C.PER_BANK; i++) cells.set(i, ambient(this.now, i));
      this.waiting.paint(cells, this.now);
    }

    // Subagents pile up from the far corner, backwards, in the parent's bank.
    const subs = [];
    for (const s of onBank.values()) for (const sub of s.subagents) subs.push(sub);
    const back = spawnOrder(true);
    let k = 0;
    for (const sub of subs) {
      while (k < back.length && onBank.has(back[k])) k++;
      if (k >= back.length) break;
      cells.set(back[k], subagentCell(sub, this.now, k));
      k++;
    }

    // Invariant 4: one fast animation, on the encoder where a human is blocking.
    arbitrateMotion(cells, onBank, this.now);
    capRings(cells, onBank);
    if (this.focused !== null && onBank.has(this.focused)) markFocus(cells, this.focused);

    // Invariant 5: overlays are pure paint, applied last, over everything.
    this.overlays = this.overlays.filter((o) => !o.done(this.now));
    for (const o of this.overlays) {
      if (o.startedAt <= this.now) o.paint(cells, this.now);
    }

    return cells;
  }

  tick(dt) {
    this.now += dt;
    this.followAlerts();
  }
}
