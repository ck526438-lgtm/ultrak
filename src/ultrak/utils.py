import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""
APP_DIR = Path(os.environ.get("ULTRAK_HOME", Path.home() / ".ultrak"))
CACHE_FILE = APP_DIR / "cache" / "hardware.json"
DOWNLOAD_URLS = [
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]


class UltraKError(RuntimeError):
    """Raised for any user-facing failure; messages are safe to print."""


def ensure_app_dir() -> Path:
    (APP_DIR / "cache").mkdir(parents=True, exist_ok=True)
    return APP_DIR


def now() -> float:
    return time.time()


def run(cmd, timeout=None, cwd=None):
    """Run a command, returning (code, stdout, stderr) without raising."""
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"executable not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def which(name: str):
    return shutil.which(name)


def human_bytes(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def human_duration(seconds) -> str:
    if seconds is None:
        return "?"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    h, rem = divmod(s, 3600)
    return f"{h}h{rem // 60:02d}m"


def even(n) -> int:
    """libx264/x265 need even luma dimensions for yuv420p."""
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


def parse_fraction(text) -> float:
    if not text:
        return 0.0
    if "/" in text:
        a, _, b = text.partition("/")
        try:
            num, den = float(a), float(b)
            return num / den if den else 0.0
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def json_load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def json_dump(path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def dataclass_to_json(obj) -> dict:
    return asdict(obj)


def die(message: str) -> None:
    raise UltraKError(message)


def require_python() -> None:
    if sys.version_info < (3, 9):
        die(f"Python 3.9+ required, found {sys.version.split()[0]}")
