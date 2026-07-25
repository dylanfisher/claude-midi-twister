"""A 4x4 bitmap font, because a bank of the Twister is a 16-pixel display.

Four rows of four characters each; ``#`` is a lit pixel and anything else is
dark. Four pixels is not enough for a legible alphabet in the typographic sense
-- several glyphs are frank approximations, and a couple of pairs (O/0, M/W) are
only barely distinguishable. It is enough to spell a word you are expecting,
which is the whole job: CLAUDE on boot, RATE when a turn dies on a rate limit, a
two-digit number when you want a count.

Ring brightness gives real grayscale per pixel, so a glyph can be dimmed
smoothly: boot strikes each letter at full and decays it to black, and the
darkness in between is what separates one letter from the next.
"""

from __future__ import annotations

from functools import lru_cache

#: Rows top to bottom, columns left to right.
GLYPHS: dict[str, tuple[str, str, str, str]] = {
    " ": ("....", "....", "....", "...."),
    "A": ("####", "#..#", "####", "#..#"),
    "B": ("###.", "#..#", "###.", "###."),
    "C": ("####", "#...", "#...", "####"),
    "D": ("###.", "#..#", "#..#", "###."),
    "E": ("####", "###.", "#...", "####"),
    "F": ("####", "#...", "###.", "#..."),
    "G": (".###", "#...", "#..#", ".###"),
    "H": ("#..#", "#..#", "####", "#..#"),
    "I": ("####", ".##.", ".##.", "####"),
    "J": ("..##", "...#", "#..#", ".##."),
    "K": ("#..#", "##..", "##..", "#..#"),
    "L": ("#...", "#...", "#...", "####"),
    "M": ("#..#", "####", "#..#", "#..#"),
    "N": ("#..#", "##.#", "#.##", "#..#"),
    "O": (".##.", "#..#", "#..#", ".##."),
    "P": ("###.", "#..#", "###.", "#..."),
    "Q": (".##.", "#..#", "#.##", ".###"),
    "R": ("###.", "#..#", "###.", "#..#"),
    "S": (".###", "##..", "..##", "###."),
    "T": ("####", ".##.", ".##.", ".##."),
    "U": ("#..#", "#..#", "#..#", "####"),
    "V": ("#..#", "#..#", "#..#", ".##."),
    "W": ("#..#", "#..#", "####", ".##."),
    "X": ("#..#", ".##.", ".##.", "#..#"),
    "Y": ("#..#", ".##.", ".##.", ".##."),
    "Z": ("####", "..#.", ".#..", "####"),
    "0": (".##.", "#..#", "#..#", ".##."),
    "1": (".#..", "##..", ".#..", "###."),
    "2": ("###.", "..#.", ".#..", "####"),
    "3": ("###.", "..#.", "..#.", "###."),
    "4": ("#..#", "#..#", "####", "...#"),
    "5": ("####", "##..", "..##", "###."),
    "6": (".##.", "#...", "###.", ".##."),
    "7": ("####", "...#", "..#.", ".#.."),
    "8": (".##.", "#..#", ".##.", ".##."),
    "9": (".##.", "#..#", ".###", "..#."),
}

@lru_cache(maxsize=None)
def pixels(char: str) -> tuple[float, ...]:
    """One glyph as one bank's worth of intensities, in slot order (row-major).

    Unknown characters render blank rather than raising, so a caller can pass
    arbitrary text through without sanitising it first.

    Cached and immutable: there are 37 glyphs and the boot animation asks for
    the same one on every frame it is on screen.
    """
    rows = GLYPHS.get(char.upper(), GLYPHS[" "])
    return tuple(1.0 if cell == "#" else 0.0 for row in rows for cell in row)
