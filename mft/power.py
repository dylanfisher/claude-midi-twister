"""System sleep and wake, so the board is dark while nobody is there.

macOS keeps the USB bus powered through sleep, so a Twister left lit stays lit:
close the lid and sixteen encoders go on glowing at an empty desk all night,
describing sessions frozen mid-turn. Nothing in the render loop can notice on
its own, either -- `time.monotonic()` on this platform *is* `mach_absolute_time`,
which does not advance while the machine is asleep, so from the daemon's side a
nine-hour suspend is one frame that took slightly longer than usual.

That frozen clock is the right behaviour and worth being explicit about: no
session ages while you are away, no TTL expires, no `working` encoder decays
into a stall it never had, and the board you come back to is the board you left.
It is also precisely why a suspend has to be *reported* from outside rather than
measured from inside.

Three independent detectors, because a missed transition means a board that
glows at an empty desk all night -- and that is not hypothetical. On a machine
with `standby` and `powernap` on, the log looked like this: one
`_SYSTEM_WILL_SLEEP` at 12:51, and then nine wakes over the next two and a half
hours, every one of them reported by the clock and *none* by IOKit. The Mac was
dark-waking every fifteen minutes for maintenance, the fallback was dutifully
relighting the board for each one, and the notification that would have put it
back never came again. So:

*   **IOKit power notifications** (`IORegisterForSystemPower`), which are the
    only thing that fires *before* the machine suspends -- and being early is
    the entire point of the sleep half; there is no darkening a board after the
    fact. Reached through `ctypes` rather than pyobjc: this project has two
    dependencies and neither of them is going to be a framework bridge pulled in
    for one callback.
*   **A clock comparison** (:class:`WakeClock`), for when that fails to attach.
    `mach_continuous_time` counts time spent asleep and `mach_absolute_time`
    does not, so the gap between them grows only across a suspend. It can report
    a wake and never a sleep, and only after the fact -- but after the fact is
    still in time to repaint.
*   **The display's power state** (:class:`DisplayPower`), which is the one
    that actually carries the weight. `CGDisplayIsAsleep` is a poll rather than
    a notification, so nothing can fail to deliver it; it is true through a
    suspend *and* through a dark wake, which is exactly the distinction the
    other two cannot make; and it costs 28 microseconds, which buys the right
    to just ask every second and stop reasoning about it.

    It also answers a question the other two never asked: the screen going off
    on its own idle timer, with the machine still awake. The board follows the
    screen -- lit when you can see it, dark when you cannot -- and that is the
    rule, not a side effect of it.

All three fire for the same wake on a healthy machine. That is deliberate: the
handler is cheap and idempotent, and the daemon debounces it
(`config.WAKE_DEBOUNCE_SECONDS`), so the cost of the overlap is nothing and the
cost of trusting any one alone is a board that lies.

The sleep callback runs on a run loop with the whole machine waiting on it, so
it must be quick and it must never raise: `IOAllowPowerChange` goes in a
`finally`, and nothing here ever calls `IOCancelPowerChange`. Vetoing a sleep is
not a thing a display gets to do -- same argument as the invariant about never
answering a prompt, one layer down.

A *dark wake* -- Power Nap, a backup, a network arrival -- is indistinguishable
from you opening the lid as far as either of the first two detectors is
concerned, and the board used to light back up for every one of them. The
display is what tells them apart: a dark wake leaves the screen off, because
that is what makes it dark. So a wake is only a wake if you could see it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
import threading
from typing import Callable, Optional

log = logging.getLogger("mft.power")

#: From `IOKit/IOMessage.h`: `iokit_common_msg(x)` is `0xe0000000 | x`. The two
#: we act on, plus the one that has to be answered to get out of the way.
_CAN_SYSTEM_SLEEP = 0xE0000270
_SYSTEM_WILL_SLEEP = 0xE0000280
_SYSTEM_HAS_POWERED_ON = 0xE0000300

_IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
_CORE_FOUNDATION = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_CORE_GRAPHICS = (
    "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
)

#: How long a run loop that returned on its own is left alone before being
#: re-entered. Only ever reached on a failure, and its job is to keep that
#: failure from becoming a spin: a run loop with nothing left in it returns
#: instantly, every time, forever.
_RELOOP_SECONDS = 1.0

#: How long :meth:`PowerWatcher.start` waits for the notification thread to say
#: whether it managed to register. Only a couple of framework calls, so this is
#: an upper bound on a failure, not on the normal path.
_START_TIMEOUT_SECONDS = 2.0


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


def _libc():
    """libc with the two mach clocks bound, or None where they don't exist.

    Cached on the function, because this is called once a frame in the fallback
    path and `find_library` shells out to `dyld` the first time.
    """
    if _libc.cached is not None:
        return _libc.cached or None
    _libc.cached = False
    if sys.platform != "darwin":
        return None
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        lib.mach_absolute_time.restype = ctypes.c_uint64
        lib.mach_absolute_time.argtypes = []
        lib.mach_continuous_time.restype = ctypes.c_uint64
        lib.mach_continuous_time.argtypes = []
        lib.mach_timebase_info.argtypes = [ctypes.POINTER(_Timebase)]
        timebase = _Timebase()
        lib.mach_timebase_info(ctypes.byref(timebase))
        if not timebase.denom:
            return None
    except Exception:
        log.debug("mach clocks unavailable", exc_info=True)
        return None
    _libc.cached = lib
    _libc.scale = timebase.numer / timebase.denom / 1e9
    return lib


_libc.cached = None
_libc.scale = 1e-9


def slept_since_boot() -> Optional[float]:
    """Seconds this machine has spent asleep since it booted, or None.

    The difference between the two mach clocks and nothing more: both count in
    the same ticks, so the timebase conversion applies to the difference just as
    it does to either one. Meaningless as an absolute number -- what it is for
    is that it only ever *changes* across a suspend.
    """
    lib = _libc()
    if lib is None:
        return None
    try:
        ticks = lib.mach_continuous_time() - lib.mach_absolute_time()
    except Exception:
        return None
    return ticks * _libc.scale


def _core_graphics():
    """CoreGraphics with the two display symbols bound, or None. Cached, for the
    same reason :func:`_libc` is."""
    if _core_graphics.cached is not None:
        return _core_graphics.cached or None
    _core_graphics.cached = False
    if sys.platform != "darwin":
        return None
    try:
        lib = ctypes.CDLL(_CORE_GRAPHICS)
        lib.CGMainDisplayID.restype = ctypes.c_uint32
        lib.CGMainDisplayID.argtypes = []
        lib.CGDisplayIsAsleep.restype = ctypes.c_uint32
        lib.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
    except Exception:
        log.debug("CoreGraphics display state unavailable", exc_info=True)
        return None
    _core_graphics.cached = lib
    return lib


_core_graphics.cached = None


def display_asleep() -> Optional[bool]:
    """Whether the main display is powered down, or None if we cannot ask.

    The main display and not all of them: on a clamshell Mac the main display
    *is* the external one, and a machine with two screens puts them to sleep
    together. Asking about one and being right is better than asking about a
    list and having to decide what a half-lit desk means.
    """
    lib = _core_graphics()
    if lib is None:
        return None
    try:
        return bool(lib.CGDisplayIsAsleep(lib.CGMainDisplayID()))
    except Exception:
        return None


class DisplayPower:
    """An edge detector over :func:`display_asleep`.

    Level rather than event, which is what makes it the reliable one: it cannot
    miss a transition it was not told about, only notice it a poll interval
    late. Reader injected for the same reason :class:`WakeClock`'s is -- the
    interesting cases are ones you cannot ask a real machine to perform.
    """

    def __init__(self, read: Callable[[], Optional[bool]] = display_asleep):
        self._read = read
        #: Awake until told otherwise, which makes the asymmetry come out right:
        #: a daemon that starts with the screen already off reports that on its
        #: first poll and darkens, and one that starts with the screen on
        #: reports nothing and leaves the boot animation alone.
        self._asleep = False

    @property
    def asleep(self) -> bool:
        """The last state reported. A reader that cannot answer never moves it,
        so a machine where this is unavailable is one that is always awake --
        the same degradation :class:`WakeClock` makes, for the same reason."""
        return self._asleep

    def poll(self) -> Optional[bool]:
        """The new state if it just changed, else None."""
        now = self._read()
        if now is None or now == self._asleep:
            return None
        self._asleep = now
        return now


class WakeClock:
    """The fallback detector: reports how long the last suspend was.

    Pure apart from its reader, which is injected so the arithmetic is testable
    without a machine that will actually go to sleep for you. A reader that
    returns None (not macOS, symbols missing) makes every poll report nothing,
    which is the correct degradation: no false wakes, just no fallback.
    """

    def __init__(self, read: Callable[[], Optional[float]] = slept_since_boot):
        self._read = read
        self._last = read()

    def poll(self, minimum: float = 0.0) -> float:
        """Seconds slept since the last call, or 0.0 for no suspend.

        `minimum` filters out the small stuff, which is not sleep: a laptop that
        thermally throttles or a process that loses its scheduler slice can move
        these two clocks apart by milliseconds.
        """
        now = self._read()
        if now is None or self._last is None:
            self._last = now
            return 0.0
        gap = now - self._last
        self._last = now
        return gap if gap >= minimum else 0.0


class PowerWatcher:
    """IOKit power notifications, delivered to two callbacks.

    ``on_sleep`` runs with the machine waiting on it and must return promptly;
    ``on_wake`` has no such constraint. Both run on this class's own thread, so
    both have to be safe to call while the render loop is mid-frame.

    Not started by construction: :meth:`start` reports whether it attached, and
    a daemon that gets `False` still has :class:`WakeClock`.
    """

    def __init__(
        self,
        on_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        self._on_sleep = on_sleep
        self._on_wake = on_wake
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        #: Set by :meth:`stop`, and the only reason the run loop is allowed to
        #: return. See :meth:`_run`.
        self._stopping = threading.Event()
        self._attached = False
        self._loop = None  # the CFRunLoop this is parked on, for stop()
        # Held for the life of the watcher: ctypes does not keep the trampoline
        # alive for the C side, and a collected callback is a callback the
        # kernel calls into freed memory.
        self._callback = None
        self._root_port = 0
        self._iokit = None
        self._core = None

    def start(self) -> bool:
        """Attach, and say whether it worked. Never raises."""
        if self._thread is not None:
            return self._attached
        self._thread = threading.Thread(
            target=self._run, name="mft-power", daemon=True
        )
        self._thread.start()
        self._ready.wait(_START_TIMEOUT_SECONDS)
        return self._attached

    def stop(self) -> None:
        """Let the notification thread out of its run loop. Best effort."""
        self._stopping.set()
        loop, core = self._loop, self._core
        if loop is None or core is None:
            return
        try:
            core.CFRunLoopStop.argtypes = [ctypes.c_void_p]
            core.CFRunLoopStop(loop)
        except Exception:
            log.debug("could not stop the power run loop", exc_info=True)

    # -- the notification thread --------------------------------------------

    def _run(self) -> None:
        try:
            self._attached = self._attach()
        except Exception:
            log.debug("power notifications unavailable", exc_info=True)
            self._attached = False
        finally:
            self._ready.set()
        if not self._attached:
            return
        # In a loop, because `CFRunLoopRun` returning is not the same as being
        # finished with: it returns the moment the loop has no sources left in
        # it, and a run loop that has quietly shed its source is exactly what a
        # daemon that stops hearing about sleep looks like from in here. It
        # returned once on this machine after delivering a single will-sleep,
        # the thread fell out of `_run`, and the board glowed for two and a half
        # hours. Re-entering costs nothing; not re-entering costs the feature.
        reloops = 0
        while not self._stopping.is_set():
            try:
                self._core.CFRunLoopRun()
            except Exception:
                log.exception("power run loop died; falling back to the clock")
                return
            if self._stopping.is_set():
                return
            reloops += 1
            # Loudly the first time and quietly after: whatever makes a run loop
            # return on its own tends to make it do so again immediately, and a
            # warning 30 times a second is its own outage.
            (log.warning if reloops == 1 else log.debug)(
                "power run loop returned on its own (%d); re-entering", reloops
            )
            if self._stopping.wait(_RELOOP_SECONDS):
                return

    def _attach(self) -> bool:
        if sys.platform != "darwin":
            return False
        iokit = ctypes.CDLL(_IOKIT)
        core = ctypes.CDLL(_CORE_FOUNDATION)
        self._iokit, self._core = iokit, core

        callback_type = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
        )
        iokit.IORegisterForSystemPower.restype = ctypes.c_uint32
        iokit.IORegisterForSystemPower.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            callback_type,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        iokit.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
        iokit.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
        iokit.IOAllowPowerChange.restype = ctypes.c_int
        iokit.IOAllowPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        core.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        core.CFRunLoopAddSource.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]

        self._callback = callback_type(self._notify)
        notify_port = ctypes.c_void_p()
        notifier = ctypes.c_uint32()
        self._root_port = iokit.IORegisterForSystemPower(
            None,
            ctypes.byref(notify_port),
            self._callback,
            ctypes.byref(notifier),
        )
        if not self._root_port:
            log.debug("IORegisterForSystemPower declined")
            return False

        source = iokit.IONotificationPortGetRunLoopSource(notify_port)
        if not source:
            return False
        self._loop = core.CFRunLoopGetCurrent()
        modes = ctypes.c_void_p.in_dll(core, "kCFRunLoopCommonModes")
        core.CFRunLoopAddSource(self._loop, source, modes)
        log.info("watching for system sleep")
        return True

    def _notify(self, _refcon, _service, message: int, argument) -> None:
        """The kernel's callback. Answers first, thinks second.

        Every path that can hold up a sleep releases it in a `finally`, and the
        handlers are wrapped separately: a display that raises in here would
        otherwise be a machine that will not go to sleep.
        """
        if message == _CAN_SYSTEM_SLEEP:
            # Consent, immediately and unconditionally. Not answering at all is
            # how you get a Mac that sits awake for 30 seconds and then sleeps
            # anyway, having blamed this process in the log.
            self._allow(argument)
        elif message == _SYSTEM_WILL_SLEEP:
            try:
                self._on_sleep()
            except Exception:
                log.exception("sleep handler failed")
            finally:
                self._allow(argument)
        elif message == _SYSTEM_HAS_POWERED_ON:
            try:
                self._on_wake()
            except Exception:
                log.exception("wake handler failed")

    def _allow(self, argument) -> None:
        try:
            self._iokit.IOAllowPowerChange(self._root_port, argument)
        except Exception:
            log.exception("IOAllowPowerChange failed")
