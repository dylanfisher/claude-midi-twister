/* A port of mft/font.py -- the 4x4 bitmap alphabet, copied glyph for glyph.
 *
 * A bank of the Twister is a 16-pixel display. Four pixels is not enough for a
 * legible alphabet in the typographic sense -- several glyphs are frank
 * approximations, and a couple of pairs (O/0, M/W) are only barely
 * distinguishable. It is enough to spell a word you are expecting, which is the
 * whole job: CLAUDE on boot, RATE when a turn dies on a rate limit.
 *
 * Rows top to bottom, columns left to right. `#` is a lit pixel.
 */

export const GLYPHS = {
  " ": ["....", "....", "....", "...."],
  "A": ["####", "#..#", "####", "#..#"],
  "B": ["###.", "#..#", "###.", "###."],
  "C": ["####", "#...", "#...", "####"],
  "D": ["###.", "#..#", "#..#", "###."],
  "E": ["####", "###.", "#...", "####"],
  "F": ["####", "#...", "###.", "#..."],
  "G": [".###", "#...", "#..#", ".###"],
  "H": ["#..#", "#..#", "####", "#..#"],
  "I": ["####", ".##.", ".##.", "####"],
  "J": ["..##", "...#", "#..#", ".##."],
  "K": ["#..#", "##..", "##..", "#..#"],
  "L": ["#...", "#...", "#...", "####"],
  "M": ["#..#", "####", "#..#", "#..#"],
  "N": ["#..#", "##.#", "#.##", "#..#"],
  "O": [".##.", "#..#", "#..#", ".##."],
  "P": ["###.", "#..#", "###.", "#..."],
  "Q": [".##.", "#..#", "#.##", ".###"],
  "R": ["###.", "#..#", "###.", "#..#"],
  "S": [".###", "##..", "..##", "###."],
  "T": ["####", ".##.", ".##.", ".##."],
  "U": ["#..#", "#..#", "#..#", "####"],
  "V": ["#..#", "#..#", "#..#", ".##."],
  "W": ["#..#", "#..#", "####", ".##."],
  "X": ["#..#", ".##.", ".##.", "#..#"],
  "Y": ["#..#", ".##.", ".##.", ".##."],
  "Z": ["####", "..#.", ".#..", "####"],
  "0": [".##.", "#..#", "#..#", ".##."],
  "1": [".#..", "##..", ".#..", "###."],
  "2": ["###.", "..#.", ".#..", "####"],
  "3": ["###.", "..#.", "..#.", "###."],
  "4": ["#..#", "#..#", "####", "...#"],
  "5": ["####", "##..", "..##", "###."],
  "6": [".##.", "#...", "###.", ".##."],
  "7": ["####", "...#", "..#.", ".#.."],
  "8": [".##.", "#..#", ".##.", ".##."],
  "9": [".##.", "#..#", ".###", "..#."],
};

const cache = new Map();

/** One glyph as one bank's worth of intensities, in slot order (row-major).
 *  Unknown characters render blank rather than raising. */
export function pixels(char) {
  const key = String(char).toUpperCase();
  if (cache.has(key)) return cache.get(key);
  const rows = GLYPHS[key] || GLYPHS[" "];
  const out = [];
  for (const row of rows) for (const cell of row) out.push(cell === "#" ? 1 : 0);
  cache.set(key, out);
  return out;
}
