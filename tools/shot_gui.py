"""Capture the real UltraK window for the README (no mockup, no retouch).

    python tools/shot_gui.py docs/assets/gui.png demo/source_720.mp4

ULTRAK_DEMO=1 makes the window run its own analysis on startup, so the shot shows
the numbers a first-time user would actually see.
"""

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

from PIL import ImageGrab

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from ultrak.gui import window_title  # noqa: E402

user32 = ctypes.windll.user32
WM_CLOSE = 0x0010
SW_RESTORE = 9
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001


def wait_window(title, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            time.sleep(1.0)  # let Tk finish painting
            return hwnd
        time.sleep(0.2)
    raise SystemExit(f"fenetre '{title}' introuvable au bout de {timeout}s")


def work_area():
    rect = wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect


def grab(hwnd, out):
    user32.SetProcessDPIAware()
    user32.ShowWindow(hwnd, SW_RESTORE)
    # Decale la fenetre vers le haut : sinon la barre des taches mange le bas de la capture.
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 40, 20, 0, 0, SWP_NOSIZE)
    user32.LockWindowUpdate(hwnd)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    work = work_area()
    box = (max(rect.left, work.left), max(rect.top, work.top),
           min(rect.right, work.right), min(rect.bottom, work.bottom - 4))
    image = ImageGrab.grab(bbox=box, all_screens=True)
    user32.LockWindowUpdate(None)
    image.save(out)
    return image.size


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join("docs", "assets", "gui.png")
    files = sys.argv[2:]
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    env = {**os.environ, "PYTHONPATH": os.path.join(root, "src"), "ULTRAK_DEMO": "1"}
    proc = subprocess.Popen([sys.executable, os.path.join(root, "ultrak.py"), "gui"] + files, env=env)
    hwnd = 0
    try:
        hwnd = wait_window(window_title())
        time.sleep(float(os.environ.get("ULTRAK_SHOT_WAIT", "3")))
        width, height = grab(hwnd, out)
        print(f"{out}  {width}x{height}  ({os.path.getsize(out) / 1024:.0f} Ko)")
    finally:
        if hwnd:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
