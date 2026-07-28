# site/

The project page, plus the MIDI bench. Hand-written static files — no build
step, no dependencies, no bundler. `.github/workflows/pages.yml` uploads this
directory to GitHub Pages as-is on every push to `main`, so whatever you see
locally is exactly what ships.

```
index.html      the page: a browser replica of the board, driven by js/
bench.html      the Web MIDI bench — its JS is all inline, its CSS is not
css/            site.css, and bench.css: the same page with its palette inverted
js/             ES modules — the daemon's render logic, ported, plus flip.js
```

The bench is the front page turned face-down. It loads `site.css` and then
`bench.css`, which does one thing: swap the color tokens end for end. No
component is restated, which is why every rule in `site.css` below `:root` names
a custom property and never a hex. The links between the two pages are marked
`data-flip`, and `js/flip.js` turns the sheet over — half a rotation out of this
page, half a rotation into the next, either side of an ordinary navigation.

There are no images, fonts or videos: the page is text, one stylesheet, and a
board drawn as SVG. Nothing is downloaded that isn't in this directory, which is
why it renders complete on the first paint.

## Running it locally

You need a web server. Opening `index.html` from the Finder does not work:
`js/main.js` is an ES module, and browsers refuse `import` over `file://` for
cross-origin reasons. You get a blank page and a CORS error in the console.

Python's stdlib is enough — nothing here needs the venv, because nothing here is
the daemon:

```sh
python3 -m http.server 8000 --directory site
```

Then open <http://localhost:8000/>. The bench is at
<http://localhost:8000/bench.html>.

From inside `site/` the `--directory` flag is redundant:

```sh
cd site && python3 -m http.server 8000
```

Pick another port if 8000 is taken; the daemon's own HTTP port is 7654, so
avoid that one and you will never have to wonder which process answered.

### Reloading

`http.server` sends no-cache headers for most things but browsers still hold on
to JS modules hard. If an edit to `js/` doesn't show up, hard-reload
(<kbd>⌘⇧R</kbd>), or keep DevTools open with "Disable cache" ticked while you
work.

## Versioning the assets

Every asset reference carries a `?v=` query, and **you raise it in the same
commit as any change under `site/`**:

```sh
grep -rn '?v=' index.html bench.html js/*.js
```

That is the stylesheet `<link>` and the module `<script>` in `index.html`, the
two `<link>`s and the module `<script>` in `bench.html`, plus every relative
`import ... from "./x.js?v=N"` in `js/`. There is no build step
here, so nothing puts a hash in a filename for us; the query is the only thing
telling a returning visitor's browser — and GitHub Pages' own CDN — that a file
it already has is stale. Shipping a change without bumping it means people who
saw the page yesterday get yesterday's JS against today's HTML.

They all share **one** number, and it has to move together. Bumping half of
them is worse than bumping none: `./board.js?v=2` and `./board.js?v=3` are two
different specifiers to a browser, so the module gets fetched, executed and
instantiated twice, and the second copy's module-level state is not the one the
first copy's importers are holding.

The number is a counter, not a date or a hash — nothing reads it, it only has to
differ from the last one.

### The bench and Web MIDI

`bench.html` asks for Web MIDI, which browsers only grant in a **secure
context**. `localhost` counts as one — that is the whole reason to serve the
page rather than open the file — but a LAN address like `192.168.1.x:8000` does
not, and the permission prompt will simply never appear. Test on `localhost`.

Chrome and Edge implement Web MIDI; Safari and Firefox do not, or do not by
default. And the bench talks to the same hardware port the daemon holds open, so
**stop the daemon first**:

```sh
.venv/bin/python -m mft.daemon --stop
```

Otherwise the bench either fails to open the port or fights the daemon for it,
and the board flickers between two writers.

The one exception is the usage-milestone panel (09/10). It plays the
announcement — the word `USE`, then the reading as rows filling from the bottom
of the bank — to the device like everything else here, but it also draws it into
a 4×4 preview on the page, so the letter envelope, the bar and the watermark
rule can all be judged in a browser with no Twister attached and no MIDI
permission granted. Its tables are a mirror of `mft/font.py` and the `USAGE_*`
block of `mft/config.py`, same as every other value on this page.

## Checking a change before it ships

There is no test suite for the site. The loop is: serve, look, hard-reload. Two
things worth checking by hand because CI cannot:

- **Reduced motion.** The OS preference sets the starting position and the
  control at the bottom right of the page overrides it, in both directions —
  `main.js` writes `data-motion="reduced"|"full"` on `<html>` and the stylesheet
  lets that attribute beat the media query. Check all four combinations: the
  DevTools emulation (Rendering → Emulate CSS media feature) crossed with the
  button.
- **The page flip.** Click "midi bench" and then come back. Both halves have to
  read as one sheet turning: if the second half is missing you land flat, and if
  the `sessionStorage` handoff leaks you get a spin on a page nobody turned to.
  Check the back button too — history restores the document mid-turn.
- **Narrow widths.** Several nav links carry `hide-sm`, and the board replica
  reflows. Check a phone width before pushing.
