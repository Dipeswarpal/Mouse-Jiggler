#!/usr/bin/env python3
"""
Mouse Jiggler
=============

Keeps your screen awake and your status "Active" by nudging the mouse a tiny
amount (5 px by default) and moving it straight back to where it was.

* It NEVER clicks, scrolls or presses keys.
* It only jiggles when you've been idle for a while, so it never fights you
  while you're actually using the mouse.
* Runs until you press Ctrl+C or close the terminal.

Windows : works out of the box, no pip install (uses the Win32 API via ctypes).
macOS   : `pip install pyautogui` and allow your terminal under
          System Settings > Privacy & Security > Accessibility.
Linux   : `pip install pyautogui` (needs an X11 session).

Usage:
    python jiggler.py                # jiggle after 15 s of idle time, 5 px
    python jiggler.py -i 30 -p 3     # after 30 s idle, 3 px
    python jiggler.py --always       # jiggle on schedule even while you're active
"""

import argparse
import ctypes
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

__version__ = "1.0.0"

SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == "Windows"

MIN_INTERVAL = 1.0
MAX_PIXELS = 20
DEFAULT_INTERVAL = 15.0
DEFAULT_PIXELS = 5

# ---------------------------------------------------------------------------
# Win32 structures. Fixed-width types are used on purpose so the layout matches
# Windows exactly (LONG/DWORD are always 32-bit on Windows, ULONG_PTR is
# pointer-sized). sizeof(INPUT) must be 40 on 64-bit and 28 on 32-bit Windows,
# otherwise SendInput silently rejects the event.
# ---------------------------------------------------------------------------
ULONG_PTR = ctypes.c_size_t

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


# ---------------------------------------------------------------------------
# Backends: each one exposes get_pos / move_rel / set_pos / idle_seconds /
# keep_awake / close. None of them has any way to click.
# ---------------------------------------------------------------------------
class WindowsBackend:
    """Native Win32 backend. No third-party packages required."""

    name = "Windows (native Win32)"

    def __init__(self):
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u, k = self._user32, self._kernel32

        u.SendInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(INPUT), ctypes.c_int]
        u.SendInput.restype = ctypes.c_uint32
        u.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        u.GetCursorPos.restype = ctypes.c_int
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.SetCursorPos.restype = ctypes.c_int
        u.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
        u.GetLastInputInfo.restype = ctypes.c_int
        k.GetTickCount.argtypes = []
        k.GetTickCount.restype = ctypes.c_uint32
        k.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        k.SetThreadExecutionState.restype = ctypes.c_uint32

        # Work in physical pixels so GetCursorPos/SetCursorPos agree exactly on
        # scaled (125%/150%) and mixed-DPI multi-monitor setups.
        try:
            u.SetProcessDPIAware()
        except Exception:
            pass

    def get_pos(self):
        pt = POINT()
        if not self._user32.GetCursorPos(ctypes.byref(pt)):
            raise OSError("GetCursorPos failed (is the screen locked?)")
        return pt.x, pt.y

    def move_rel(self, dx, dy):
        # SendInput produces a real input event, so Windows' idle timer resets.
        # That is what keeps the screen saver / lock and Teams "Away" at bay.
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, 0)
        if self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
            raise OSError("SendInput was blocked (is the screen locked?)")

    def set_pos(self, x, y):
        self._user32.SetCursorPos(int(x), int(y))

    def idle_seconds(self):
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not self._user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = self._kernel32.GetTickCount()
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0  # handles 49-day wrap

    def keep_awake(self):
        # Belt and braces: also tell Windows not to sleep or turn the display off.
        self._kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )

    def close(self):
        self._kernel32.SetThreadExecutionState(ES_CONTINUOUS)


class PyAutoGuiBackend:
    """macOS / Linux backend built on pyautogui."""

    def __init__(self):
        try:
            import pyautogui
        except ImportError:
            raise SystemExit(
                "pyautogui is required on %s. Install it with:\n"
                "    pip install pyautogui" % SYSTEM
            )
        except (Exception, SystemExit) as exc:  # no X display, missing tkinter...
            raise SystemExit("pyautogui could not start: %s" % exc)

        # Our moves are 1-2 px and never click, so the corner fail-safe would
        # only crash the script if you park the cursor in a corner.
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0
        self._pg = pyautogui
        self._caffeinate = None
        self.name = "%s (pyautogui)" % SYSTEM

    def get_pos(self):
        pos = self._pg.position()
        return int(pos[0]), int(pos[1])

    def move_rel(self, dx, dy):
        self._pg.moveRel(dx, dy, duration=0)

    def set_pos(self, x, y):
        self._pg.moveTo(x, y, duration=0)

    def idle_seconds(self):
        try:
            if SYSTEM == "Darwin":
                out = subprocess.run(
                    ["ioreg", "-c", "IOHIDSystem"],
                    capture_output=True, text=True, timeout=3,
                ).stdout
                match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
                return int(match.group(1)) / 1e9 if match else None
            if shutil.which("xprintidle"):
                out = subprocess.run(
                    ["xprintidle"], capture_output=True, text=True, timeout=3
                ).stdout
                return int(out.strip()) / 1000.0
        except Exception:
            pass
        return None  # unknown -> jiggle on schedule

    def keep_awake(self):
        if SYSTEM == "Darwin" and shutil.which("caffeinate"):
            # -w ties caffeinate to this process, so it exits when we do.
            self._caffeinate = subprocess.Popen(
                ["caffeinate", "-d", "-i", "-w", str(os.getpid())]
            )

    def close(self):
        if self._caffeinate and self._caffeinate.poll() is None:
            self._caffeinate.terminate()


def make_backend():
    return WindowsBackend() if IS_WINDOWS else PyAutoGuiBackend()


# ---------------------------------------------------------------------------
# Core logic (backend-agnostic, unit tested)
# ---------------------------------------------------------------------------
DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def jiggle_once(backend, pixels=DEFAULT_PIXELS, rng=random, settle=0.1, sleep=time.sleep):
    """Nudge the cursor `pixels` px in one direction and bring it back.

    After the round trip the cursor is snapped back to its exact starting point
    if pointer acceleration left it a pixel or two off. If the cursor ended up
    far away (you grabbed the mouse mid-jiggle) it is left alone.
    """
    x0, y0 = backend.get_pos()
    ux, uy = rng.choice(DIRECTIONS)
    dx, dy = ux * pixels, uy * pixels

    backend.move_rel(dx, dy)
    sleep(settle)
    backend.move_rel(-dx, -dy)
    sleep(settle)

    x1, y1 = backend.get_pos()
    drift = max(abs(x1 - x0), abs(y1 - y0))
    if 0 < drift <= pixels * 4:
        backend.set_pos(x0, y0)
    return x0, y0


class Jiggler:
    def __init__(self, backend, interval=DEFAULT_INTERVAL, pixels=DEFAULT_PIXELS, idle_only=True,
                 quiet=False, sleep=time.sleep, rng=random, out=sys.stdout):
        self.backend = backend
        self.interval = float(interval)
        self.pixels = int(pixels)
        self.idle_only = idle_only
        self.quiet = quiet
        self.sleep = sleep
        self.rng = rng
        self.out = out
        self.jiggles = 0
        self.errors = 0

    def _status(self, msg):
        if self.quiet:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.out.write("\r[%s] %s" % (stamp, msg.ljust(68)))
        self.out.flush()

    def _jitter(self):
        # A little randomness so the timing isn't perfectly periodic.
        return self.rng.uniform(0, min(5.0, self.interval * 0.1))

    def step(self):
        """Run one cycle. Returns how long to wait before the next one."""
        idle = self.backend.idle_seconds() if self.idle_only else None

        if idle is not None and idle < self.interval:
            self._status("You're active - standing by (%d jiggles) | Ctrl+C to stop"
                         % self.jiggles)
            return max(self.interval - idle, MIN_INTERVAL)

        try:
            jiggle_once(self.backend, self.pixels, rng=self.rng, sleep=self.sleep)
            self.jiggles += 1
            self._status("Jiggled %d x - next in ~%ds | Ctrl+C to stop"
                         % (self.jiggles, int(self.interval)))
        except Exception as exc:  # locked screen, permission issue, etc.
            self.errors += 1
            self._status("Skipped this round: %s" % exc)
        return self.interval

    def run(self, max_cycles=None):
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            wait = self.step()
            self.sleep(wait + self._jitter())
        return self.jiggles


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Keep the screen awake by nudging the mouse a few px and back. "
                    "Never clicks. Stop with Ctrl+C."
    )
    parser.add_argument("-i", "--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds of inactivity before each jiggle (default: %g)" % DEFAULT_INTERVAL)
    parser.add_argument("-p", "--pixels", type=int, default=DEFAULT_PIXELS,
                        help="how far to nudge, in pixels (default: %d, max: %d)"
                             % (DEFAULT_PIXELS, MAX_PIXELS))
    parser.add_argument("--always", action="store_true",
                        help="jiggle every interval even while you are using the mouse")
    parser.add_argument("--no-keep-awake", action="store_true",
                        help="don't also ask the OS to keep the display/system awake")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="no status output")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = parser.parse_args(argv)

    if args.interval < MIN_INTERVAL:
        parser.error("--interval must be at least %g second" % MIN_INTERVAL)
    if not 1 <= args.pixels <= MAX_PIXELS:
        parser.error("--pixels must be between 1 and %d" % MAX_PIXELS)
    return args


def main(argv=None):
    args = parse_args(argv)
    backend = make_backend()
    jiggler = Jiggler(backend, interval=args.interval, pixels=args.pixels,
                      idle_only=not args.always, quiet=args.quiet)

    if not args.quiet:
        mode = "every %gs" % args.interval if args.always else \
               "after %gs of inactivity" % args.interval
        print("Mouse Jiggler %s - %s" % (__version__, backend.name))
        print("Nudging %d px and back %s. Never clicks." % (args.pixels, mode))
        print("Press Ctrl+C (or close this terminal) to stop.\n")

    if not args.no_keep_awake:
        backend.keep_awake()
    try:
        jiggler.run()
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
        if not args.quiet:
            print("\nStopped. Jiggled %d times." % jiggler.jiggles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
