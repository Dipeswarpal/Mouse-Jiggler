import contextlib
import ctypes
import io
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jiggler  # noqa: E402


class FakeBackend:
    """In-memory mouse. Records every call so tests can check nothing else happens."""

    def __init__(self, pos=(500, 300), idle=None, accel=1, user_jump_on_move=None):
        self.x, self.y = pos
        self.idle = idle
        self.accel = accel                      # simulate pointer acceleration
        self.user_jump_on_move = user_jump_on_move
        self.calls = []
        self._moves = 0

    def get_pos(self):
        self.calls.append("get_pos")
        return self.x, self.y

    def move_rel(self, dx, dy):
        self.calls.append(("move_rel", dx, dy))
        self._moves += 1
        factor = self.accel if self._moves % 2 == 1 else 1
        self.x += dx * factor
        self.y += dy * factor
        if self.user_jump_on_move == self._moves:
            self.x += 400                       # user grabs the mouse mid-jiggle

    def set_pos(self, x, y):
        self.calls.append(("set_pos", x, y))
        self.x, self.y = x, y

    def idle_seconds(self):
        return self.idle

    def keep_awake(self):
        pass

    def close(self):
        pass


def no_sleep(_):
    pass


class StructLayoutTests(unittest.TestCase):
    """SendInput rejects events if sizeof(INPUT) is wrong, so pin the layout."""

    def test_input_struct_matches_windows_abi(self):
        is_64 = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(ctypes.sizeof(jiggler.MOUSEINPUT), 32 if is_64 else 24)
        self.assertEqual(ctypes.sizeof(jiggler.INPUT), 40 if is_64 else 28)
        self.assertEqual(ctypes.sizeof(jiggler.LASTINPUTINFO), 8)
        self.assertEqual(ctypes.sizeof(jiggler.POINT), 8)


class JiggleOnceTests(unittest.TestCase):
    def test_returns_to_exact_start(self):
        for seed in range(20):
            b = FakeBackend(pos=(100, 200))
            jiggler.jiggle_once(b, pixels=1, rng=random.Random(seed), sleep=no_sleep)
            self.assertEqual((b.x, b.y), (100, 200))

    def test_moves_are_tiny_and_opposite(self):
        b = FakeBackend()
        jiggler.jiggle_once(b, pixels=2, rng=random.Random(1), sleep=no_sleep)
        moves = [c for c in b.calls if isinstance(c, tuple) and c[0] == "move_rel"]
        self.assertEqual(len(moves), 2)
        (_, dx1, dy1), (_, dx2, dy2) = moves
        self.assertEqual(abs(dx1) + abs(dy1), 2)
        self.assertEqual((dx1 + dx2, dy1 + dy2), (0, 0))

    def test_corrects_acceleration_drift(self):
        b = FakeBackend(pos=(50, 50), accel=2)   # first move goes 2 px, back only 1
        jiggler.jiggle_once(b, pixels=1, rng=random.Random(0), sleep=no_sleep)
        self.assertEqual((b.x, b.y), (50, 50))
        self.assertIn(("set_pos", 50, 50), b.calls)

    def test_does_not_yank_cursor_back_if_user_moved_it(self):
        b = FakeBackend(pos=(50, 50), user_jump_on_move=1)
        jiggler.jiggle_once(b, pixels=1, rng=random.Random(0), sleep=no_sleep)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "set_pos" for c in b.calls))
        self.assertNotEqual((b.x, b.y), (50, 50))

    def test_only_safe_operations(self):
        b = FakeBackend()
        jiggler.Jiggler(b, interval=5, sleep=no_sleep, quiet=True).run(max_cycles=10)
        ops = {c if isinstance(c, str) else c[0] for c in b.calls}
        self.assertTrue(ops <= {"get_pos", "move_rel", "set_pos"})


class JigglerLoopTests(unittest.TestCase):
    def test_jiggles_when_idle(self):
        b = FakeBackend(idle=120)
        j = jiggler.Jiggler(b, interval=30, sleep=no_sleep, quiet=True)
        self.assertEqual(j.run(max_cycles=3), 3)

    def test_stands_by_while_user_is_active(self):
        b = FakeBackend(idle=5)
        j = jiggler.Jiggler(b, interval=30, sleep=no_sleep, quiet=True)
        self.assertEqual(j.run(max_cycles=3), 0)
        self.assertEqual(j.step(), 25)           # waits only until 30 s idle

    def test_always_mode_ignores_activity(self):
        b = FakeBackend(idle=0)
        j = jiggler.Jiggler(b, interval=30, idle_only=False, sleep=no_sleep, quiet=True)
        self.assertEqual(j.run(max_cycles=4), 4)

    def test_unknown_idle_time_jiggles_on_schedule(self):
        b = FakeBackend(idle=None)
        j = jiggler.Jiggler(b, interval=30, sleep=no_sleep, quiet=True)
        self.assertEqual(j.run(max_cycles=2), 2)

    def test_backend_error_is_survived(self):
        class Locked(FakeBackend):
            def move_rel(self, dx, dy):
                raise OSError("screen locked")

        out = io.StringIO()
        j = jiggler.Jiggler(Locked(idle=100), interval=30, sleep=no_sleep, out=out)
        self.assertEqual(j.run(max_cycles=2), 0)
        self.assertEqual(j.errors, 2)
        self.assertIn("screen locked", out.getvalue())

    def test_wait_includes_small_jitter(self):
        waits = []
        b = FakeBackend(idle=100)
        j = jiggler.Jiggler(b, interval=30, sleep=waits.append, quiet=True,
                            rng=random.Random(3))
        j.run(max_cycles=5)
        long_waits = [w for w in waits if w > 1]
        self.assertTrue(all(30 <= w <= 33 for w in long_waits), long_waits)


class _FakeFn:
    def __init__(self, impl):
        self.impl = impl
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.impl(*args)


class _FakeDLL:
    def __init__(self, **fns):
        for name, impl in fns.items():
            setattr(self, name, _FakeFn(impl))


class WindowsBackendTests(unittest.TestCase):
    """Exercise the Win32 code path against fake user32/kernel32 DLLs."""

    def setUp(self):
        self.cursor = [640, 360]
        self.sent = []
        self.exec_state = []

        def send_input(n, ref, size):
            inp = ref._obj
            self.assertEqual(n, 1)
            self.assertEqual(size, ctypes.sizeof(jiggler.INPUT))
            self.assertEqual(inp.type, jiggler.INPUT_MOUSE)
            self.assertEqual(inp.mi.dwFlags, jiggler.MOUSEEVENTF_MOVE)  # move only, no click
            self.sent.append((inp.mi.dx, inp.mi.dy))
            self.cursor[0] += inp.mi.dx
            self.cursor[1] += inp.mi.dy
            return 1

        def get_cursor_pos(ref):
            ref._obj.x, ref._obj.y = self.cursor
            return 1

        def set_cursor_pos(x, y):
            self.cursor[:] = [x, y]
            return 1

        def get_last_input(ref):
            ref._obj.dwTime = 0xFFFFFF00            # just before tick-count wrap
            return 1

        user32 = _FakeDLL(SendInput=send_input, GetCursorPos=get_cursor_pos,
                          SetCursorPos=set_cursor_pos, GetLastInputInfo=get_last_input,
                          SetProcessDPIAware=lambda: 1)
        kernel32 = _FakeDLL(GetTickCount=lambda: 0x00002000,
                            SetThreadExecutionState=lambda s: self.exec_state.append(s) or 1)
        dlls = {"user32": user32, "kernel32": kernel32}

        patcher = mock.patch.object(ctypes, "WinDLL", create=True,
                                    side_effect=lambda name, **kw: dlls[name])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = jiggler.WindowsBackend()

    def test_jiggle_round_trip(self):
        jiggler.jiggle_once(self.backend, pixels=1, rng=random.Random(2), sleep=no_sleep)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.cursor, [640, 360])

    def test_idle_seconds_handles_wrap(self):
        # 0x2000 - 0xFFFFFF00 (mod 2^32) = 0x2100 ms
        self.assertAlmostEqual(self.backend.idle_seconds(), 0x2100 / 1000.0)

    def test_keep_awake_and_release(self):
        self.backend.keep_awake()
        self.backend.close()
        self.assertEqual(self.exec_state[0],
                         jiggler.ES_CONTINUOUS | jiggler.ES_SYSTEM_REQUIRED
                         | jiggler.ES_DISPLAY_REQUIRED)
        self.assertEqual(self.exec_state[-1], jiggler.ES_CONTINUOUS)


class ArgTests(unittest.TestCase):
    def test_defaults(self):
        a = jiggler.parse_args([])
        self.assertEqual((a.interval, a.pixels, a.always), (15.0, 5, False))

    def test_rejects_bad_values(self):
        for bad in (["-i", "0"], ["-p", "0"], ["-p", "500"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                jiggler.parse_args(bad)


if __name__ == "__main__":
    unittest.main()
