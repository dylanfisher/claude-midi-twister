#!/usr/bin/env python3
"""Build a Spotlight-searchable .app that toggles the daemon on and off.

    python3 app/make_app.py                 # -> ~/Applications/Claude Twister.app

Double-click (or Spotlight -> "Claude Twister") to start it; invoke it again to
stop it. It's a background app -- no dock icon, no window -- so it reports what
happened with a notification.

An .app is just a folder with a specific layout, so this needs no compiler and
no dependencies: an Info.plist describing the bundle, a shell script as the
executable, and an icon drawn below with zlib and arithmetic.
"""

from __future__ import annotations

import argparse
import math
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUNDLE_ID = "com.dylanfisher.claude-twister"
APP_NAME = "Claude Twister"

#: What the daemon calls itself in Activity Monitor and `ps`. Keep it under 16
#: characters: the kernel's copy of the name (`p_comm`) is truncated there, so
#: "claude-midi-twister" would show up as "claude-midi-twis".
PROC_NAME = "claude-twister"


# --- icon -------------------------------------------------------------------
# One encoder, lit. Distance fields with smooth edges, so it's antialiased
# without the cost of supersampling.

BG = (0x16, 0x16, 0x1A)
RING_ON = (0x3A, 0xD6, 0xC8)
RING_WARN = (0xE8, 0xC0, 0x4A)
RING_OFF = (0x2E, 0x2E, 0x38)
KNOB = (0x26, 0x26, 0x2E)
KNOB_EDGE = (0x34, 0x34, 0x40)

#: Fraction of the ring that reads as "in progress".
LIT_FRACTION = 0.62
SEGMENTS = 28


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge0 == edge1:
        return 0.0 if x < edge0 else 1.0
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3 - 2 * t)


def _blend(dst, src, alpha: float):
    return tuple(int(d + (s - d) * alpha) for d, s in zip(dst, src))


def draw_icon(size: int = 1024) -> bytes:
    half = size / 2
    # Radii as fractions of the icon, matched to the Twister's proportions.
    r_outer = half * 0.86
    r_inner = half * 0.68
    r_knob = half * 0.56
    corner = size * 0.22

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter byte: none
        for x in range(size):
            px = x + 0.5 - half
            py = y + 0.5 - half

            # Rounded-square background, as a signed distance.
            qx = abs(px) - (half - corner)
            qy = abs(py) - (half - corner)
            dist = (
                math.hypot(max(qx, 0.0), max(qy, 0.0))
                + min(max(qx, qy), 0.0)
                - corner
            )
            bg_alpha = 1.0 - _smoothstep(-1.0, 1.0, dist)
            if bg_alpha <= 0.0:
                rows.extend((0, 0, 0, 0))
                continue

            color = BG
            radius = math.hypot(px, py)

            # LED ring: a band of discrete segments, filled clockwise from top.
            if r_inner - 2 < radius < r_outer + 2:
                angle = (math.degrees(math.atan2(px, -py)) + 360) % 360
                seg = int(angle / 360 * SEGMENTS)
                gap = (angle / 360 * SEGMENTS) - seg
                # Leave a dark gap between segments.
                if 0.12 < gap < 0.88:
                    frac = seg / SEGMENTS
                    if frac <= LIT_FRACTION:
                        led = RING_WARN if frac > LIT_FRACTION - 0.08 else RING_ON
                    else:
                        led = RING_OFF
                    band = _smoothstep(r_inner - 1.5, r_inner + 1.5, radius) * (
                        1.0 - _smoothstep(r_outer - 1.5, r_outer + 1.5, radius)
                    )
                    color = _blend(color, led, band)

            # The knob itself, with a lighter rim.
            knob_alpha = 1.0 - _smoothstep(r_knob - 1.5, r_knob + 1.5, radius)
            if knob_alpha > 0:
                rim = _smoothstep(r_knob * 0.86, r_knob, radius)
                color = _blend(color, _blend(KNOB, KNOB_EDGE, rim), knob_alpha)

            rows.extend((*color, int(255 * bg_alpha)))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">2I5B", size, size, 8, 6, 0, 0, 0)  # RGBA, 8-bit
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def build_icns(png: bytes, out: Path) -> bool:
    """Turn the PNG into an .icns via the system tools, if they're present."""
    if not (shutil.which("sips") and shutil.which("iconutil")):
        print("sips/iconutil missing; app will use the generic icon")
        return False
    iconset = out.parent / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    source = iconset / "source.png"
    source.write_bytes(png)

    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            subprocess.run(
                ["sips", "-z", str(pixels), str(pixels), str(source), "--out", str(iconset / name)],
                capture_output=True,
                check=False,
            )
    source.unlink()
    result = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    shutil.rmtree(iconset)
    if result.returncode != 0:
        print(f"iconutil failed: {result.stderr.strip()}")
        return False
    return True


# --- a name worth reading ---------------------------------------------------
# Left alone, the daemon appears in Activity Monitor as "Python", indis-
# tinguishable from every other script you have running. macOS names a process
# after the executable *file* it exec'd, so the only way to change it is to
# give the interpreter another name on disk.
#
# Symlinks don't do it -- tried both a link to `bin/python3.14` and one
# straight at the framework binary, and the kernel resolves through either and
# reports "Python" anyway. Neither does rewriting argv[0] (`exec -a`,
# setproctitle): that changes the Command column, not the name. What works is a
# plain copy of the interpreter under the name we want, parked in the venv's
# bin/ so that sys.prefix still finds the venv beside it.
#
# The copy is pinned to the interpreter it was made from, which matters only
# across a Python minor upgrade -- the same moment the venv itself has to be
# rebuilt, so it costs no failure mode that wasn't already there.


def _real_executable(python: Path) -> Path | None:
    """Ask an interpreter which file the kernel actually exec'd.

    Not the same question as `sys.executable`, and not answerable by resolving
    symlinks: Homebrew's `bin/python3.14` is a stub that re-execs the binary
    inside `Python.app`, and that re-exec is what names the process. Only the
    process itself can say where it ended up.
    """
    probe = (
        "import ctypes;"
        "b=ctypes.create_string_buffer(4096);"
        "n=ctypes.c_uint32(4096);"
        "ctypes.CDLL(None)._NSGetExecutablePath(b, ctypes.byref(n));"
        "print(b.value.decode())"
    )
    result = subprocess.run(
        [str(python), "-c", probe], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    path = Path(result.stdout.strip())
    return path if path.is_file() else None


def named_interpreter(python: Path) -> Path:
    """Return an interpreter named PROC_NAME, or `python` if that fell over.

    Falling back is deliberate: an app that starts under a dull name beats one
    that doesn't start.
    """
    real = _real_executable(python)
    if real is None:
        print(f"couldn't locate the real interpreter behind {python}")
        return python

    copy = python.parent / PROC_NAME
    try:
        shutil.copy2(real, copy)
        copy.chmod(0o755)
    except OSError as exc:
        print(f"couldn't write {copy}: {exc}")
        return python

    # The copy carries the original's ad-hoc signature, which covers contents
    # rather than filename -- but verify rather than assume.
    check = subprocess.run(
        [str(copy), "-c", "import mido"], capture_output=True, text=True, check=False
    )
    if check.returncode != 0:
        print(f"{copy} doesn't run; keeping {python.name}")
        copy.unlink(missing_ok=True)
        return python

    print(f"interpreter copy: {copy}")
    return copy


# --- the executable ---------------------------------------------------------

LAUNCHER = """#!/bin/bash
# Toggle the Claude Twister daemon. Generated by app/make_app.py -- rebuild
# rather than editing, or your changes vanish on the next build.
#
# Liveness comes from the daemon's own pid file rather than `pgrep -f`, which
# matches any process that merely mentions the daemon on its command line --
# including the shell you typed it into.
#
# PYTHON is a copy of the venv's interpreter renamed so Activity Monitor has
# something to call it; see named_interpreter() in app/make_app.py.
PYTHON={python}
REPO={repo}
LOG="$HOME/Library/Logs/claude-twister.log"

notify() {{
    /usr/bin/osascript -e "display notification \\"$1\\" with title \\"{name}\\"" \\
        >/dev/null 2>&1 || true
}}

if [ ! -x "$PYTHON" ]; then
    notify "Python missing at $PYTHON — rebuild the venv, then app/make_app.py"
    exit 1
fi

cd "$REPO" || exit 1

if "$PYTHON" -m mft.daemon --status >/dev/null 2>&1; then
    if "$PYTHON" -m mft.daemon --stop >>"$LOG" 2>&1; then
        notify "Stopped — encoders cleared"
    else
        notify "Could not stop the daemon — see log"
    fi
    exit 0
fi

mkdir -p "$(dirname "$LOG")"
echo "--- starting $(date) ---" >> "$LOG"
/usr/bin/nohup "$PYTHON" -m mft.daemon >> "$LOG" 2>&1 &

# Wait for the pid file rather than guessing at a sleep duration.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.3
    if "$PYTHON" -m mft.daemon --status >/dev/null 2>&1; then
        if /usr/bin/tail -n 20 "$LOG" | /usr/bin/grep -q "connected to"; then
            notify "Running — Twister connected"
        else
            notify "Running — no Twister found, check it's plugged in"
        fi
        exit 0
    fi
done

notify "Failed to start — see ~/Library/Logs/claude-twister.log"
exit 1
"""


def build(dest_dir: Path, python: Path) -> Path:
    app = dest_dir / f"{APP_NAME}.app"
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    if app.exists():
        shutil.rmtree(app)
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "claude-twister",
        "CFBundleIconFile": "icon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "11.0",
        # Background app: no dock icon, no menu bar, just does the thing.
        "LSUIElement": True,
        "NSHumanReadableCopyright": "",
    }
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))

    launcher = macos / "claude-twister"
    launcher.write_text(
        LAUNCHER.format(
            python=f'"{named_interpreter(python)}"', repo=f'"{REPO}"', name=APP_NAME
        )
    )
    launcher.chmod(0o755)

    print("drawing icon...")
    build_icns(draw_icon(), resources / "icon.icns")

    # Nudge Spotlight/LaunchServices so it shows up without a logout.
    subprocess.run(["touch", str(app)], check=False)
    subprocess.run(["mdimport", str(app)], capture_output=True, check=False)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        default=os.path.expanduser("~/Applications"),
        help="where to install (default: ~/Applications, no sudo needed)",
    )
    parser.add_argument(
        "--python",
        default=str(REPO / ".venv" / "bin" / "python"),
        help="interpreter to run the daemon with",
    )
    args = parser.parse_args()

    # Deliberately absolute-but-not-resolved: .venv/bin/python is a symlink to
    # the system interpreter, and resolving it would point the app at a Python
    # that has none of the venv's packages.
    python = Path(os.path.abspath(os.path.expanduser(args.python)))
    if not python.exists():
        print(f"no interpreter at {python} — create the venv first")
        return 1

    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    app = build(dest, python)
    print(f"built {app}")
    print(f'spotlight: press cmd-space and type "{APP_NAME}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
