# Mouse Jiggler

A tiny Python script that keeps your screen from locking or sleeping, and keeps
your chat status (Teams, Slack, etc.) from going "Away", by nudging the mouse
**5 pixels and straight back**.

- **Never clicks**, scrolls or presses keys. It only moves the pointer 5 px and returns it.
- **Snaps back to the exact pixel** it started from, so the cursor never wanders.
- **Stays out of your way.** It only jiggles after you've been idle for the
  interval (15 s by default). While you're using the mouse it just waits.
- **No install on Windows.** It uses the Win32 API directly through `ctypes`,
  so there's no `pip install` and no admin rights needed.
- Runs until you press **Ctrl+C** or close the terminal.

## Quick start (Windows)

```powershell
git clone https://github.com/saurabh-1907/Mouse-jiggler.git
cd Mouse-jiggler
python jiggler.py
```

Or open the folder in VS Code, open a terminal (`` Ctrl+` ``), and run
`python jiggler.py`. Leave that terminal open; it will keep running in the
background while you use other windows.

You can also double-click **`run.bat`**.

Output:

```
Mouse Jiggler 1.0.0 - Windows (native Win32)
Nudging 5 px and back after 15s of inactivity. Never clicks.
Press Ctrl+C (or close this terminal) to stop.

[21:44:05] Jiggled 12 x - next in ~15s | Ctrl+C to stop
```

## macOS / Linux

```bash
pip install -r requirements.txt     # installs pyautogui
python3 jiggler.py
```

- **macOS:** give your terminal (or VS Code) permission under
  *System Settings → Privacy & Security → Accessibility*. The script also runs
  `caffeinate` to keep the display awake, and it stops when the script exits.
- **Linux:** needs an X11 session and `python3-tk`. If `xprintidle` is installed, it is used for idle detection.

## Options

| Flag | Default | What it does |
|---|---|---|
| `-i`, `--interval SECONDS` | `15` | Idle time before each jiggle |
| `-p`, `--pixels N` | `5` | How far to nudge (1–20 px) |
| `--always` | off | Jiggle every interval, even while you're using the mouse |
| `--no-keep-awake` | off | Don't also ask the OS to keep the display/system awake |
| `-q`, `--quiet` | off | No status output |

Examples:

```bash
python jiggler.py -i 30        # jiggle after 30 s idle
python jiggler.py -p 2         # smaller 2 px nudge, less visible
python jiggler.py --always -q  # fixed schedule, silent
```

## How it works

Every cycle the script checks how long since your last real input
(`GetLastInputInfo` on Windows, `HIDIdleTime` on macOS). If it has been at
least `--interval` seconds, it:

1. reads the cursor position,
2. sends a relative mouse move of `+N` px in a random direction (`SendInput`),
3. sends the opposite move of `−N` px,
4. if pointer acceleration left it a pixel off, snaps it back to the exact
   start with `SetCursorPos`. If you grabbed the mouse mid-jiggle, it leaves
   your cursor alone.

`SendInput` generates a genuine input event, so the OS idle timer resets. That
idle timer is what drives screen lock, sleep and "Away" status. On Windows it
also calls `SetThreadExecutionState(ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)`
as a backup, and Windows releases that automatically when the process exits.

If the screen gets locked anyway, the script just skips that round and keeps
going. It can't unlock the screen.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests use a fake mouse, so they don't touch your real cursor. They check
the return-to-origin logic, the acceleration-drift correction, that the cursor
is never yanked back from you, that no click calls exist, the idle-only
behaviour, and that the Win32 `INPUT` struct layout matches the Windows ABI.
They also run the Windows code path against fake `user32`/`kernel32` DLLs.

## Stopping

Press **Ctrl+C** in the terminal, or close the terminal or VS Code window.
