"""Run ffmpeg, watch it, and verify what came out."""

import os
import subprocess
import threading
import time
from dataclasses import dataclass, replace

from .analyze import SourceInfo, probe
from .pipeline import PipelinePlan
from .planner import Job
from .utils import UltraKError, human_bytes


@dataclass
class Progress:
    frames: int = 0
    percent: float = 0.0
    speed: float = 0.0
    elapsed: float = 0.0
    eta_seconds: float = 0.0
    out_time: str = ""

    @property
    def rate(self) -> float:
        return self.frames / self.elapsed if self.elapsed > 0 else 0.0


@dataclass
class Result:
    ok: bool = False
    dst: str = ""
    seconds: float = 0.0
    frames: int = 0
    size_bytes: int = 0
    fps: float = 0.0
    error: str = ""
    fallback_used: str = ""
    out_bit_rate: int = 0

    @property
    def speed(self) -> str:
        return f"{self.fps:.2f} fps" if self.fps else "n/a"

    @property
    def realtime_ratio(self) -> float:
        return self.fps / self.frames if self.frames and self.fps else 0.0


def _thread_args(hw, plan: PipelinePlan) -> list:
    cpus = max(1, hw.logical_cpus)
    heavy = bool({"nlmeans", "guided", "minterpolate"} & plan.used_filters())
    workers = max(1, cpus // (2 if heavy else 1))
    return ["-filter_threads", str(workers), "-filter_complex_threads", str(min(2, workers))]


def render(ffmpeg: str, plan: PipelinePlan, src: str, dst: str, hw,
           *, expect_frames: int = 0, on_progress=None, cancel: threading.Event = None,
           start: float = 0.0, duration: float = 0.0, quiet: bool = False) -> Result:
    """Encode src -> dst with live progress, atomic rename and GPU/audio fallbacks."""
    tmp = dst + ".ultrak-part.mp4"
    queue = [plan]
    last_error = ""
    used_fallback = ""
    started = time.perf_counter()

    while queue:
        active_plan = queue.pop(0)
        if cancel is not None and cancel.is_set():
            _cleanup(tmp)
            return Result(ok=False, dst=dst, error="annule", fallback_used=used_fallback)
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats",
               "-progress", "pipe:1"] + _thread_args(hw, active_plan)
        cmd += active_plan.build(src, tmp, start, duration)

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.PIPE if cancel is not None else subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        state = {"err": ""}

        def drain():
            state["err"] = proc.stderr.read() or ""

        err_thread = threading.Thread(target=drain, daemon=True)
        err_thread.start()
        tracker = _Tracker(expect_frames)
        cancelled = False
        for line in proc.stdout:
            line = line.strip()
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            if on_progress and line.startswith("frame="):
                if on_progress(tracker.update(_int_of(line, "frame") or 0)) is False:
                    cancelled = True
                    break
            if line == "progress=end":
                break
        if cancelled:
            _stop(proc)
            _cleanup(tmp)
            return Result(ok=False, dst=dst, error="annule", fallback_used=used_fallback)

        proc.wait()
        err_thread.join(timeout=5)
        err = state["err"].strip()
        if proc.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 1024:
            os.replace(tmp, dst)
            elapsed = time.perf_counter() - started
            info = probe(dst, hw.ffprobe) if hw.ffprobe else None
            frames = info.frames if info else 0
            return _finish(dst, elapsed, frames, info, used_fallback)

        last_error = err or f"ffmpeg code {proc.returncode}"
        _cleanup(tmp)
        if _needs_audio_fallback(last_error) and active_plan.audio == "copy":
            queue.insert(0, _clone(active_plan, audio="aac"))
            used_fallback = "audio re-encodee en aac"
            continue
        if _needs_cpu_fallback(last_error) and not active_plan.codec.startswith("libx"):
            queue.insert(0, _clone(active_plan, codec="libx264", preset=_safe_preset(hw)))
            used_fallback = f"{active_plan.codec} indisponible, repli CPU libx264"
            continue
        if not quiet:
            raise UltraKError(f"echec du rendu: {last_error[:400]}")
    return Result(ok=False, dst=dst, error=last_error[:400], fallback_used=used_fallback)


def _finish(dst, elapsed, frames, info, fallback) -> Result:
    size = os.path.getsize(dst)
    return Result(ok=True, dst=dst, seconds=round(elapsed, 2), frames=frames, size_bytes=size,
                  fps=round(frames / elapsed, 3) if elapsed and frames else 0.0,
                  fallback_used=fallback, out_bit_rate=(info.bit_rate if info else 0))


class _Tracker:
    """Wall-clock throughput tracker: ETA comes from measured fps, not a guess."""

    def __init__(self, expect_frames: int):
        self.expect = max(0, expect_frames)
        self.t0 = time.perf_counter()

    def update(self, frame: int) -> Progress:
        elapsed = time.perf_counter() - self.t0
        rate = frame / elapsed if elapsed > 0 and frame else 0.0
        remaining = max(0, self.expect - frame)
        return Progress(frames=frame,
                        percent=min(99.9, frame * 100.0 / self.expect) if self.expect else 0.0,
                        speed=round(rate, 3), elapsed=round(elapsed, 2),
                        eta_seconds=round(remaining / rate, 2) if rate else 0.0)


def _int_of(line, key):
    head, _, value = line.partition("=")
    if head.strip() != key:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _stop(proc):
    try:
        if proc.stdin:
            proc.stdin.write("q\n")
            proc.stdin.flush()
    except (OSError, ValueError):
        pass
    for _ in range(20):
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def _cleanup(path, retries=12):
    """A killed ffmpeg can still hold the Windows file handle for a moment."""
    for attempt in range(retries):
        if not os.path.isfile(path):
            return True
        try:
            os.remove(path)
            return True
        except OSError:
            time.sleep(0.1 * (attempt + 1))
    return False


def _needs_audio_fallback(err: str) -> bool:
    return ("codec" in err.lower() and "mp4" in err.lower()) or "copy for stream" in err.lower() \
        or "could not find codec" in err.lower()


def _needs_cpu_fallback(err: str) -> bool:
    low = err.lower()
    return any(k in low for k in ("not supported", "unknown encoder", "initialise",
                                  "hardware", "nvenc", "qsv", "amf", "cannot open"))


def _safe_preset(hw) -> str:
    return "fast" if hw.cpu_score() >= 60 else "veryfast"


def _clone(plan: PipelinePlan, **changes) -> PipelinePlan:
    return replace(plan, **changes)


def preview(job: Job, ffmpeg: str, hw, seconds=4.0, offset=0.0) -> float:
    """Render a short slice with the real pipeline and return measured fps."""
    frames = int(round(seconds * (job.plan.fps or job.src.fps or 30)))
    out = os.path.join(os.path.dirname(job.dst) or ".", ".ultrak-preview.mp4")
    result = render(ffmpeg, job.plan, job.src.path, out, hw, expect_frames=frames,
                    start=max(0.0, min(offset, max(0.0, (job.src.duration or seconds) - seconds))),
                    duration=seconds)
    try:
        os.remove(out)
    except OSError:
        pass
    if not result.ok or not result.fps:
        raise UltraKError(f"preview impossible: {result.error or 'aucune frame rendue'}")
    return result.fps


def verify(dst: str, hw, plan: PipelinePlan, src: SourceInfo) -> str:
    """Post-render integrity report: geometry, duration, bitrate honesty."""
    if not os.path.isfile(dst):
        raise UltraKError("fichier de sortie absent")
    out = probe(dst, hw.ffprobe)
    problems = []
    if out.height != plan.target_height:
        problems.append(f"hauteur {out.height} != {plan.target_height} annoncee")
    if out.duration and src.duration and abs(out.duration - src.duration) > max(1.0, 0.02 * src.duration):
        problems.append(f"duree {out.duration:.1f}s != source {src.duration:.1f}s")
    bpp = out.bpp
    if bpp and bpp > plan.max_bpp * 3:
        problems.append(f"bitrate anormal ({bpp:.3f} bpp)")
    if src.has_audio and not out.has_audio and plan.audio != "none":
        problems.append("piste audio perdue")
    report = (f"verifie: {out.width}x{out.height}@{out.fps:g}fps, {out.duration:.1f}s, "
              f"{human_bytes(out.file_bytes)}, {bpp:.3f} bpp")
    return report + (" | " + "; ".join(problems) if problems else " | OK")
