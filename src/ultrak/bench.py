"""Calibrate the estimator on the machine that will actually do the renders."""

import os
import platform
import time

from .config import save
from .hardware import Hardware
from .pipeline import PipelinePlan
from .utils import UltraKError, even, human_bytes, run

REFERENCE_LADDER = [
    ("lanczos seul", {"denoise": "none", "sharpen": "none", "two_step": False}),
    ("medium + pro", {"denoise": "medium", "sharpen": "pro", "two_step": False}),
    ("two-step medium + pro", {"denoise": "medium", "sharpen": "pro", "two_step": True}),
    ("nlmeans strong + pro", {"denoise": "strong", "sharpen": "pro", "two_step": True}),
]
SOURCE_FILTERS = ["noise=alls=18:allf=t+u"]


def make_source(ffmpeg: str, dst: str, height=480, seconds=5.0, fps=25) -> str:
    width = int(height * 16 / 9 / 2) * 2
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=s={width}x{height}:d={seconds}:r={fps}",
           "-vf", SOURCE_FILTERS[0], "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
           "-pix_fmt", "yuv420p", dst]
    code, out, err = run(cmd, timeout=180)
    if code != 0:
        raise UltraKError(f"source de test impossible: {err.strip()[:200]}")
    return dst


def measure(hw: Hardware, src: str, target_height=2160, seconds=5.0, fps_in=25,
            variants=None, log=print) -> list:
    variants = variants or REFERENCE_LADDER
    frames = int(round(seconds * fps_in))
    width = even(target_height * 16 / 9)
    rows = []
    for name, kwargs in variants:
        plan = PipelinePlan(target_width=width, target_height=target_height, **kwargs)
        out = os.path.join(os.path.dirname(src), f"bench_{abs(hash(name)) % 999}.mp4")
        cmd = [hw.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
               "-vf", plan.vf()]
        cmd += plan.encoder_args() + ["-an", "-frames:v", str(frames), out]
        t0 = time.perf_counter()
        code, _, err = run(cmd, timeout=3600)
        dt = time.perf_counter() - t0
        if code != 0:
            rows.append((name, 0.0, 0.0, err.strip()[:80]))
            log(f"  {name:24s} echec: {err.strip()[:120]}")
            continue
        fps = frames / dt if dt else 0.0
        size = human_bytes(os.path.getsize(out))
        rows.append((name, round(fps, 2), dt, size))
        log(f"  {name:24s} {fps:6.2f} fps | {dt:6.1f}s / {frames} images | {size}")
    return rows


def run_bench(hw: Hardware, seconds=5.0, target_height=2160, quick=False,
              log=print, write_to=None) -> dict:
    """Render the reference ladder and derive this machine's throughput constant."""
    workdir = write_to or os.path.join(os.getcwd(), "bench")
    os.makedirs(workdir, exist_ok=True)
    src = make_source(hw.ffmpeg, os.path.join(workdir, "bench_source.mp4"), seconds=seconds)
    variants = REFERENCE_LADDER[:2] if quick else REFERENCE_LADDER
    log(f"etalonnage sur {platform.machine()} / {hw.cpu_name or 'CPU'} "
        f"({hw.logical_cpus} threads, {hw.ram_free_gb:.1f} Go libres)")
    log(f"source {seconds:g}s 480p -> {3840}x{target_height}, libx264 medium crf 18")
    rows = measure(hw, src, target_height=target_height, seconds=seconds,
                   variants=variants, log=log)

    reference = next((r for r in rows if r[0] == "medium + pro"), rows[0])
    fps = reference[1]
    pixels = even(target_height * 16 / 9) * target_height
    pps = round(fps * pixels, 1) if fps else 0.0
    result = {"rows": rows, "pixels_per_second": pps, "reference_score": hw.cpu_score(),
              "machine": hw.cpu_name, "target_height": target_height}
    if pps:
        save({"calibration": {"pixels_per_second": pps, "reference_score": hw.cpu_score()}})
        log(f"etalonnage enregistre: {pps/1e6:.2f} Mpx/s de rendu 4K "
            f"({fps:.2f} fps), score machine {hw.cpu_score()}")
    markdown = render_markdown(hw, rows, seconds, target_height)
    report = os.path.join(workdir, f"bench-{platform.node()[:24]}.md")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    log(f"rapport: {report}")
    result["report"] = report
    return result


def render_markdown(hw: Hardware, rows, seconds, target_height) -> str:
    width = even(target_height * 16 / 9)
    lines = ["# Bench UltraK",
             f"",
             f"- Machine: {hw.cpu_name or 'inconnue'} ({hw.logical_cpus} threads, "
             f"{hw.ram_total_gb:.1f} Go RAM, {hw.ram_free_gb:.1f} Go libres)",
             f"- ffmpeg {hw.version}, GPU encode: {', '.join(hw.gpu_brands) or 'aucun'}",
             f"- Source: {seconds:g}s 854x480 bruitee, sortie {width}x{target_height}, "
             f"libx264 medium crf 18",
             f"",
             f"| pipeline | fps | temps pour {int(seconds*25)} images | taille |",
             f"|---|---|---|---|"]
    for name, fps, dt, size in rows:
        lines.append(f"| {name} | {fps:.2f} | {dt:.1f}s | {size} |")
    lines.append("")
    lines.append("Ratio temps reel (25 i/s): multiplier par 25 pour obtenir le nombre "
                 "d'images traitees par seconde, puis diviser la duree de la video par ce "
                 "ratio pour estimer le temps de rendu.")
    return "\n".join(lines)
