# site/

- **Bump `?v=` on every change under `site/`.** The project page ships from
  `site/` to GitHub Pages with no build step, so nothing fingerprints the
  filenames for us and browsers hold on to ES modules and stylesheets hard.
  Every asset reference carries a version query — the `<link>` and `<script>` in
  `site/index.html`, the two `<link>`s and the `<script>` in `site/bench.html`,
  and every relative `import ... from "./x.js?v=N"` in `site/js/`. They share **one** number: edit any file under `site/`, raise it
  everywhere in the same commit. A partial bump is worse than none, because a
  module imported under two specifiers is loaded and instantiated twice.

  ```sh
  cd site && grep -rn '?v=' index.html bench.html js/*.js   # the current number
  # raise N -> N+1 across all of them, then hard-reload and check the console
  ```
