"""Locate ffmpeg and describe what this machine can actually do."""

import os
import platform
import re
from dataclasses import dataclass, field

from .utils import (APP_DIR, CACHE_FILE, EXE, IS_WINDOWS, UltraKError, ensure_app_dir,
                    json_dump, json_load, now, run, which)

GPU_ENCODERS = {
    "h264_nvenc": "nvidia", "hevc_nvenc": "nvidia", "av1_nvenc": "nvidia",
    "h264_qsv": "intel", "hevc_qsv": "intel", "av1_qsv": "intel",
    "h264_amf": "amd", "hevc_amf": "amd", "av1_amf": "amd",
}
CACHE_SCHEMA = 2


@dataclass
class Hardware:
    ffmpeg: str = ""
    ffprobe: str = ""
    version: str = ""
    cpu_name: str = ""
    logical_cpus: int = 1
    ram_total_gb: float = 0.0
    ram_free_gb: float = 0.0
    encoders: frozenset = field(default_factory=frozenset)
    filters: frozenset = field(default_factory=frozenset)
    gpu_brands: tuple = ()
    gpu_encoders: tuple = ()
    rejected_encoders: tuple = ()

    @property
    def has(self, name):
        return name in self.gpu_brands

    def filter(self, *names):
        return [n for n in names if n in self.filters]

    def has_encoder(self, name):
        return name in self.encoders

    def has_gpu_encoder(self, name):
        return name in self.gpu_encoders

    def cpu_score(self) -> int:
        """Rough 0-100 capability index used to pick presets and guard filters."""
        return int(min(100, self.logical_cpus * 9 + self.ram_free_gb * 4))

    def summary(self) -> str:
        gpu = ", ".join(self.gpu_encoders) if self.gpu_encoders else "aucun (CPU only)"
        return (f"ffmpeg {self.version or '?'} | {self.cpu_name or 'CPU inconnu'} "
                f"({self.logical_cpus} threads) | RAM {self.ram_total_gb:.1f} Go "
                f"({self.ram_free_gb:.1f} Go libres) | encode GPU utilisables: {gpu} "
                f"| score {self.cpu_score()}")


def _candidate_paths():
    for env in ("ULTRAK_FFMPEG", "FFMPEG_PATH"):
        p = os.environ.get(env)
        if p:
            yield p
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for base in (root, os.getcwd(), APP_DIR):
        for sub in ("tools", "bin", "."):
            yield os.path.join(str(base), sub, "ffmpeg" + EXE)
    yield which("ffmpeg")


def find_ffmpeg(ffmpeg_arg=None, probe=None) -> str:
    """Return a working ffmpeg path, or raise with the fix command."""
    seen = set()
    candidates = []
    if ffmpeg_arg:
        candidates.append(ffmpeg_arg)
    for c in _candidate_paths():
        if c:
            candidates.append(c)
    for cand in candidates:
        cand = os.path.abspath(os.path.expanduser(str(cand)))
        if cand in seen:
            continue
        seen.add(cand)
        if not os.path.isfile(cand):
            continue
        if probe is None:
            return cand
        code, out, _ = run([cand, "-hide_banner", "-version"], timeout=25)
        if code == 0 and "ffmpeg" in out.lower():
            return cand
    raise UltraKError(
        "ffmpeg introuvable. Installe-le avec:  python -m ultrak install-ffmpeg"
    )


def _read_free_ram_gb():
    if IS_WINDOWS:
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                            ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                            ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                            ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                            ("ullAvailExtendedVirtual", ctypes.c_uint64)]

            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.ullTotalPhys / 2**30, m.ullAvailPhys / 2**30
        except Exception:
            return 0.0, 0.0
    try:
        total = avail = 0
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
        return total / 1024 / 1024, avail / 1024 / 1024
    except OSError:
        return 0.0, 0.0


def _cpu_name():
    if IS_WINDOWS:
        code, out, _ = run(["wmic", "cpu", "get", "name", "/value"], timeout=25)
        if code == 0 and "=" in out:
            return out.split("=", 1)[1].strip().splitlines()[0].strip()
        code, out, _ = run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_Processor).Name"], timeout=25)
        if code == 0 and out.strip():
            return out.strip().splitlines()[0].strip()
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or ""


def _list_from(ffmpeg, args, pattern):
    code, out, _ = run([ffmpeg, "-hide_banner"] + args, timeout=90)
    if code != 0:
        return frozenset()
    names = set()
    for line in out.splitlines():
        m = re.match(pattern, line.strip())
        if m:
            names.add(m.group(1))
    return frozenset(names)


def _encoder_usable(ffmpeg: str, name: str) -> bool:
    """Compile-time presence proves nothing; render 3 frames to check the driver really works."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "color=c=black:s=320x240:r=25:d=0.12", "-frames:v", "3",
           "-c:v", name, "-f", "null", "-"]
    code, out, err = run(cmd, timeout=60)
    return code == 0 and "not supported" not in (out + err).lower()


def detect(ffmpeg_path=None, use_cache=True, refresh=False) -> Hardware:
    """Probe ffmpeg + CPU + RAM + GPU encoders. Cached on disk, keyed by binary."""
    ffmpeg_path = find_ffmpeg(ffmpeg_path, probe=lambda p: os.path.isfile(p))
    if use_cache and not refresh:
        ensure_app_dir()
        stamp = os.stat(ffmpeg_path).st_mtime_ns
        cached = json_load(CACHE_FILE)
        if (cached and cached.get("ffmpeg") == ffmpeg_path and cached.get("stamp") == stamp
                and cached.get("schema") == CACHE_SCHEMA):
            data = dict(cached["hw"])
            hw = Hardware(**data)
            hw.encoders = frozenset(data["encoders"])
            hw.filters = frozenset(data["filters"])
            return hw

    code, out, _ = run([ffmpeg_path, "-hide_banner", "-version"], timeout=30)
    version = ""
    if code == 0:
        m = re.search(r"ffmpeg version\s+\S*?(\d+\.\d+(?:\.\d+)?)", out)
        version = m.group(1) if m else out.splitlines()[0].split()[-1]

    base = os.path.dirname(ffmpeg_path)
    ffprobe = os.path.join(base, "ffprobe" + EXE)
    if not os.path.isfile(ffprobe):
        ffprobe = which("ffprobe") or ""

    encoders = _list_from(ffmpeg_path, ["-encoders"], r"^V[.A-Z]{5}\s+(\S+)")
    filters = _list_from(ffmpeg_path, ["-filters"], r"^\S+\s+(\S+)\s+V->V")
    filters |= _list_from(ffmpeg_path, ["-filters"], r"^\.\.\s+(\S+)\s+VV->V")

    total_gb, free_gb = _read_free_ram_gb()
    candidates = sorted(e for e in encoders if e in GPU_ENCODERS)
    working = [e for e in candidates if _encoder_usable(ffmpeg_path, e)]
    gpu_brands = tuple(sorted({GPU_ENCODERS[e] for e in working}))

    hw = Hardware(ffmpeg=ffmpeg_path, ffprobe=ffprobe, version=version,
                  cpu_name=_cpu_name(), logical_cpus=os.cpu_count() or 1,
                  ram_total_gb=round(total_gb, 2), ram_free_gb=round(free_gb, 2),
                  encoders=encoders, filters=filters, gpu_brands=gpu_brands,
                  gpu_encoders=tuple(working),
                  rejected_encoders=tuple(e for e in candidates if e not in working))

    json_dump(CACHE_FILE, {"ffmpeg": ffmpeg_path, "stamp": os.stat(ffmpeg_path).st_mtime_ns,
                           "schema": CACHE_SCHEMA, "checked_at": now(),
                           "hw": {**{k: v for k, v in vars(hw).items()},
                                  "encoders": sorted(hw.encoders), "filters": sorted(hw.filters),
                                  "gpu_brands": list(hw.gpu_brands),
                                  "gpu_encoders": list(hw.gpu_encoders),
                                  "rejected_encoders": list(hw.rejected_encoders)}})
    return hw
