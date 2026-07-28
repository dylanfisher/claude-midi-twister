/* The scripts. A scenario is a list of `[delay, fn]` steps run against the
 * simulator's own clock, so a scenario paused (tab hidden, reduced motion) is
 * paused rather than fast-forwarded. Each step does two things at once: prints
 * a line in the console pane and fires the hook event that would really have
 * accompanied it. Keeping those together is the whole point -- it is what makes
 * the board legible instead of decorative.
 */

const FILES = [
  "src/parser.rs", "src/lexer.rs", "tests/round_trip.rs", "README.md",
  "mft/board.py", "mft/render.py", "api/routes.ts", "lib/query.ts",
  "Cargo.toml", "docs/design.md", "hooks/notify.sh", "src/wire.go",
];
const pick = (a, i) => a[Math.abs(i) % a.length];

export class Director {
  constructor(sim, con) {
    this.sim = sim;
    this.con = con;
    this.queue = [];       // {at, fn}
    this.seq = 0;
    this.quiet = false;    // suppress the focus + console grab (see `spotlight`)
  }

  /* Bringing a session forward is two things -- focusing its encoder and
   * pointing the console pane at it -- and every scenario starts by doing both.
   * The demo runs several scenarios at once, though, and only one of them can
   * hold the console, so the pair lives behind one seam a caller can close. A
   * scenario run `quietly` still fires every hook event; it just doesn't grab
   * the page while it does. */
  spotlight(s) {
    if (this.quiet) return;
    this.sim.focus(s.slot);
    this.con.show(s);
  }

  /** Run `fn` without letting the scenario inside it take focus or the console. */
  quietly(fn) {
    const was = this.quiet;
    this.quiet = true;
    try { return fn(); } finally { this.quiet = was; }
  }

  /** Schedule `fn` `delay` seconds from now, on the simulator's clock. */
  at(delay, fn) { this.queue.push({ at: this.sim.now + delay, fn }); }

  /** Run a scenario: an array of [delaySinceStart, fn]. */
  run(steps) {
    let t = 0;
    for (const [delay, fn] of steps) { t += delay; this.at(t, fn); }
    return t;
  }

  cancelAll() { this.queue = []; }

  tick() {
    if (!this.queue.length) return;
    const due = this.queue.filter((s) => s.at <= this.sim.now);
    if (!due.length) return;
    this.queue = this.queue.filter((s) => s.at > this.sim.now);
    due.sort((a, b) => a.at - b.at);
    for (const s of due) s.fn();
  }

  // --- building blocks ----------------------------------------------------
  say(s, kind, text) { this.con.push(s, kind, text); }

  /** A run of tool calls: the shimmer under `working` is this, and nothing else. */
  toolRun(s, count, startDelay = 0.4) {
    const steps = [];
    for (let i = 0; i < count; i++) {
      const tool = pick(["Read", "Grep", "Edit", "Bash", "Read", "Glob"], i + this.seq);
      const target = pick(FILES, i + this.seq * 3);
      steps.push([i === 0 ? startDelay : 0.7 + (i % 3) * 0.25, () => {
        this.sim.event(s.slot, "PreToolUse", { tool, target });
        this.say(s, "tool", `${tool}(${target})`);
      }]);
    }
    this.seq++;
    return steps;
  }

  // --- the scenarios ------------------------------------------------------

  /** A plain turn: think, work, finish. The baseline everything else deviates from. */
  ordinaryTurn(s, prompt = "make the parser handle trailing commas") {
    this.spotlight(s);
    return this.run([
      [0, () => { this.sim.event(s.slot, "UserPromptSubmit"); this.say(s, "user", prompt); }],
      [0.5, () => { this.sim.event(s.slot, "Thinking"); this.say(s, "thought", "Thinking about the grammar…"); }],
      ...this.toolRun(s, 5, 1.4),
      [0.9, () => { this.sim.event(s.slot, "Streaming"); this.say(s, "assistant", "Trailing commas now parse in lists, tuples and call args."); }],
      [1.6, () => { this.sim.event(s.slot, "Stop"); this.say(s, "system", "done · 4 files changed"); }],
      [12, () => this.sim.event(s.slot, "Idle")],
    ]);
  }

  /** The one state the whole device exists for. */
  permissionGate(s) {
    this.spotlight(s);
    return this.run([
      [0, () => { this.sim.event(s.slot, "UserPromptSubmit"); this.say(s, "user", "clean up the build artifacts"); }],
      ...this.toolRun(s, 2, 0.7),
      [1.0, () => {
        this.sim.event(s.slot, "Notification", { question: "rm -rf ./target" });
        this.say(s, "gate", "Bash(rm -rf ./target) — allow? [y/N]");
        this.say(s, "system", "blocked until you answer · the encoder flashes red");
      }],
      // And then nothing. Debt ramps on its own; that is the point.
    ]);
  }

  /** A plan waiting for a yes: the same kind of block, its own hue, a slower flash. */
  planGate(s) {
    this.spotlight(s);
    return this.run([
      [0, () => { this.sim.event(s.slot, "UserPromptSubmit"); this.say(s, "user", "migrate the config loader off yaml"); }],
      [0.6, () => { this.sim.event(s.slot, "Thinking"); this.say(s, "thought", "Reading every call site first…"); }],
      ...this.toolRun(s, 4, 1.2),
      [1.2, () => {
        this.sim.event(s.slot, "Notification", { plan: true, question: "3-step migration" });
        this.say(s, "gate", "Plan ready — 3 steps, 11 files. Approve? [y/N]");
      }],
    ]);
  }

  /** Subagents, stacked from the far corner, ring = how long each has been out. */
  subagents(s, n = 3) {
    this.spotlight(s);
    const steps = [
      [0, () => { this.sim.event(s.slot, "UserPromptSubmit"); this.say(s, "user", "audit the whole repo for unhandled errors"); }],
      [0.6, () => { this.sim.event(s.slot, "Thinking"); this.say(s, "thought", "Fanning out — one agent per subsystem."); }],
    ];
    const names = ["explore:parser", "explore:runtime", "explore:api", "explore:tests"];
    for (let i = 0; i < n; i++) {
      steps.push([0.55, () => {
        this.sim.event(s.slot, "SubagentStart", { label: names[i % names.length] });
        this.say(s, "sub", `Task(${names[i % names.length]}) started`);
      }]);
    }
    steps.push(...this.toolRun(s, 3, 1.0));
    for (let i = 0; i < n; i++) {
      steps.push([1.6, () => {
        this.sim.event(s.slot, "SubagentStop");
        this.say(s, "sub", `Task(${names[i % names.length]}) returned`);
      }]);
    }
    steps.push([0.9, () => { this.sim.event(s.slot, "Stop"); this.say(s, "assistant", "17 unhandled error paths, ranked."); }]);
    steps.push([12, () => this.sim.event(s.slot, "Idle")]);
    return this.run(steps);
  }

  /** The encoder survives, because encoders belong to terminals, not sessions. */
  clear(s) {
    this.spotlight(s);
    return this.run([
      [0, () => { this.say(s, "user", "/clear"); }],
      [0.35, () => {
        this.sim.event(s.slot, "Clear");
        this.con.clear(s);
        this.con.push(s, "system", "context cleared — same terminal, same encoder", { instant: true });
      }],
    ]);
  }

  /** Drain, sit, refill — because the agent keeps what mattered. */
  compact(s) {
    this.spotlight(s);
    return this.run([
      [0, () => { this.say(s, "user", "/compact"); }],
      [0.4, () => { this.sim.event(s.slot, "Compact"); this.say(s, "system", "compacting conversation…"); }],
      [1.9, () => this.say(s, "system", "compacted · the ring now shows the new context size")],
    ]);
  }

  /** Solid red and a four-letter word across the bank. */
  rateLimit(s) {
    this.spotlight(s);
    return this.run([
      [0, () => { this.sim.event(s.slot, "UserPromptSubmit"); this.say(s, "user", "keep going"); }],
      ...this.toolRun(s, 2, 0.6),
      [0.9, () => {
        this.sim.event(s.slot, "Error", { question: "rate limit" });
        this.say(s, "error", "API error: rate_limit_exceeded — retry after 41s");
        this.sim.banner("RATE");
      }],
    ]);
  }

  /** Failure heat: the same working shimmer, the hue warming toward red. */
  failing(s) {
    this.spotlight(s);
    const steps = [
      [0, () => { this.sim.event(s.slot, "UserPromptSubmit"); this.say(s, "user", "fix the failing round-trip test"); }],
    ];
    for (let i = 0; i < 5; i++) {
      steps.push([1.1, () => {
        this.sim.event(s.slot, "PreToolUse", { tool: "Edit", target: "src/parser.rs" });
        this.sim.event(s.slot, "ToolFailed");
        this.say(s, "tool", "Edit(src/parser.rs)");
        this.say(s, "error", "String to replace not found in file.");
      }]);
    }
    steps.push([1.0, () => this.say(s, "system", "still working · the hue warms toward red as tool calls fail")]);
    return this.run(steps);
  }

  /** Fill the board, so arbitration has something to arbitrate. */
  fill(sim, con, n = 9) {
    const names = ["~/parser", "~/api", "~/infra", "~/web", "~/mft", "~/notes",
                   "~/docs", "~/wire", "~/bench", "~/cli", "~/proto"];
    const steps = [];
    for (let i = 0; i < n; i++) {
      steps.push([0.28, () => {
        const s = sim.add({ name: names[i % names.length] });
        if (!s) return;
        con.push(s, "system", `session started in ${s.name}`, { instant: true });
        const roll = i % 5;
        if (roll === 0) this.ordinaryTurn(s);
        else if (roll === 1) this.subagents(s, 2);
        else if (roll === 2) this.at(0.4, () => { sim.event(s.slot, "UserPromptSubmit"); con.push(s, "user", "refactor the client"); this.run(this.toolRun(s, 6, 0.5)); });
        else if (roll === 3) { sim.event(s.slot, "Waiting"); con.push(s, "system", "waiting for input", { instant: true }); }
        else { sim.event(s.slot, "Idle"); s.contextFraction = 0.2 + (i % 6) * 0.13; }
      }]);
    }
    // …and one permission gate, last, so it wins the one fast animation.
    steps.push([1.2, () => {
      const s = sim.list().find((x) => x.state === "working") || sim.list()[0];
      if (!s) return;
      sim.event(s.slot, "Notification", { question: "git push --force" });
      con.push(s, "gate", "Bash(git push --force origin main) — allow? [y/N]");
    }]);
    return this.run(steps);
  }
}

/* Free play. A small vocabulary, because the states worth showing are a small
 * set — anything unrecognised is just a prompt, which starts an ordinary turn.
 * The point is that you can type at it and the board answers. */
export function freePlay(director, sim, con, session, text) {
  const cmd = text.toLowerCase().trim();
  if (cmd === "/clear") return director.clear(session);
  if (cmd === "/compact") return director.compact(session);
  if (cmd === "/plan") return director.planGate(session);
  if (cmd === "/error" || cmd === "/rate") return director.rateLimit(session);
  if (cmd === "/permission" || cmd === "/allow") return director.permissionGate(session);
  if (cmd === "/agents" || cmd === "/subagents") return director.subagents(session, 3);
  if (cmd === "/fail") return director.failing(session);
  if (cmd === "/help") {
    con.push(session, "system", "/clear /compact /plan /error /permission /agents /fail, or type a prompt", { instant: true });
    return 0;
  }
  if (cmd === "/exit" || cmd === "/quit") {
    con.push(session, "system", "session ended · the encoder goes dark", { instant: true });
    director.at(0.6, () => sim.remove(session.slot));
    return 0.6;
  }
  return director.ordinaryTurn(session, text);
}
