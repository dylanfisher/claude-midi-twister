/* The right-hand pane: a fake Claude Code session, typing itself out.
 *
 * It is a prop, but a load-bearing one -- it is the only thing on the page that
 * explains *why* an encoder is orange. Every line it prints is paired with the
 * hook event the real Claude Code would have fired at that moment, and that
 * event is what moves the board. Nothing here reaches into the simulator's
 * state directly.
 */

/* The glyph column used to be literal characters -- U+23FA for a turn, U+273B
 * for a thought, U+21B3 for a subagent. None of those are in the monospace
 * faces this page asks for, so a phone would fall through the stack to the
 * emoji font and paint a grey placeholder button where a small mark belonged.
 * They are drawn instead, Phosphor-style: one 16x16 box, currentColor, so the
 * per-kind colors in the stylesheet still apply and nothing depends on which
 * fonts the device happens to ship.
 */
const svg = (body) =>
  `<svg class="ic" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"` +
  ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

const CARET = svg(`<path d="M6 3.5 10.5 8 6 12.5"/>`);

const KINDS = {
  user: CARET,
  assistant: svg(`<circle cx="8" cy="8" r="4" fill="currentColor" stroke="none"/>`),
  thought: svg(`<path d="M8 3.4V12.6M4.02 5.7l7.96 4.6M4.02 10.3l7.96-4.6"/>`),
  tool: svg(`<circle cx="8" cy="8" r="4" fill="currentColor" stroke="none"/>`),
  result: "",
  gate: svg(`<path d="M8 4.2v4.4"/><circle cx="8" cy="11.4" r="0.9" fill="currentColor" stroke="none"/>`),
  error: svg(`<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>`),
  system: svg(`<circle cx="8" cy="8" r="1.3" fill="currentColor" stroke="none"/>`),
  sub: svg(`<path d="M4 3.8V9h7.5"/><path d="M9.2 6.7 11.5 9l-2.3 2.3"/>`),
};

export class Console {
  constructor(host) {
    this.host = host;
    this.host.classList.add("console");
    this.host.innerHTML = `
      <div class="console-bar">
        <span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
        <span class="console-title">— zsh — claude</span>
      </div>
      <div class="console-body" role="log" aria-live="polite" aria-label="session transcript"></div>
      <form class="console-input" autocomplete="off">
        <span class="prompt">${CARET}</span>
        <input type="text" name="line" placeholder="type a prompt, or /clear, /compact, /plan, /error"
               aria-label="prompt input for the simulated session" />
      </form>`;
    this.body = this.host.querySelector(".console-body");
    this.form = this.host.querySelector("form");
    this.input = this.host.querySelector("input");
    this.titleEl = this.host.querySelector(".console-title");
    this.buffers = new Map();      // session id -> [{kind, text, shown}]
    this.current = null;
    this.typing = null;
    this.speed = 90;               // characters per second

    this.form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = this.input.value.trim();
      this.input.value = "";
      if (text && this.onSubmit) this.onSubmit(text);
    });
  }

  buffer(session) {
    if (!this.buffers.has(session.id)) this.buffers.set(session.id, []);
    return this.buffers.get(session.id);
  }

  /** Append a line to a session's transcript. `instant` skips the typewriter. */
  push(session, kind, text, { instant = false } = {}) {
    const line = { kind, text, shown: instant ? text.length : 0 };
    this.buffer(session).push(line);
    const buf = this.buffer(session);
    if (buf.length > 200) buf.shift();
    if (session.id === this.current?.id) this.repaint();
    return line;
  }

  /** Replace the last line's text in place — for a spinner or a live counter. */
  amend(session, text) {
    const buf = this.buffer(session);
    if (!buf.length) return;
    const line = buf[buf.length - 1];
    line.text = text;
    line.shown = text.length;
    if (session.id === this.current?.id) this.repaint();
  }

  show(session) {
    this.current = session;
    this.titleEl.textContent = session
      ? `— ${session.name} — claude (encoder ${session.slot + 1})`
      : "— no session —";
    this.host.dataset.state = session ? session.state : "none";
    this.repaint(true);
  }

  tick(dt) {
    if (!this.current) return;
    const buf = this.buffer(this.current);
    let typed = false;
    for (const line of buf) {
      if (line.shown < line.text.length) {
        line.shown = Math.min(line.text.length, line.shown + this.speed * dt);
        typed = true;
        break;    // one line at a time, in order
      }
    }
    if (typed) this.repaint();
  }

  repaint(jump = false) {
    if (!this.current) { this.body.innerHTML = `<div class="console-empty">no session on this encoder</div>`; return; }
    const buf = this.buffer(this.current);
    const atBottom = jump || this.body.scrollHeight - this.body.scrollTop - this.body.clientHeight < 60;
    const out = [];
    for (let i = 0; i < buf.length; i++) {
      const line = buf[i];
      const text = line.text.slice(0, Math.floor(line.shown));
      const partial = line.shown < line.text.length;
      const cursor = partial ? `<span class="cursor"></span>` : "";
      out.push(
        `<div class="line ${line.kind}"><span class="glyph">${KINDS[line.kind] ?? ""}</span>` +
        `<span class="text">${esc(text)}${cursor}</span></div>`
      );
    }
    // The live cursor sits on the last line only when nothing is being typed.
    const busy = buf.some((l) => l.shown < l.text.length);
    if (!busy) out.push(`<div class="line caret"><span class="glyph">${CARET}</span><span class="text"><span class="cursor blink"></span></span></div>`);
    this.body.innerHTML = out.join("");
    if (atBottom) this.body.scrollTop = this.body.scrollHeight;
  }

  clear(session) {
    this.buffers.set(session.id, []);
    if (session.id === this.current?.id) this.repaint(true);
  }
}

function esc(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
