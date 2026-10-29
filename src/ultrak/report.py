"""Before/after evidence: comparison stills, shareable GIFs, fidelity metrics."""

import os
import re

from .hardware import Hardware
from .utils import UltraKError, run

FONTS = ("arial.ttf", "verdana.ttf", "tahoma.ttf", "segoeuib.ttf")


def _font() -> str:
    root = os.environ.get("SystemRoot", "C:/Windows").replace("\\", "/")
    for name in FONTS:
        path = f"{root}/Fonts/{name}"
        if os.path.isfile(path):
            # drawtext needs the value quoted AND the drive colon escaped.
            quoted = path.replace(":", "\\:")
            return f"'{quoted}'"
    return ""


def _label(text: str, size_div=16) -> str:
    font = _font()
    if not font:
        return ""
    # expansion=none keeps a literal % in labels like "100%".
    return (f"drawtext=fontfile={font}:text='{text}':x=14:y=10:expansion=none:"
            f"fontsize=h/{size_div}:fontcolor=white@0.95:shadowcolor=black@0.7:"
            f"shadowx=2:shadowy=2")


def _ffmpeg_ok(hw: Hardware) -> None:
    if not hw.ffmpeg:
        raise UltraKError("ffmpeg manquant")


def _call(cmd, timeout=300):
    code, out, err = run(cmd, timeout=timeout)
    if code != 0:
        raise UltraKError(f"ffmpeg a refuse ce montage: {(err or out).strip()[-320:]}")
    return out, err


def pair(hw: Hardware, src: str, dist: str, dst: str, *, at=0.0, width=1280, layout="side",
         labels=True, frame=1) -> str:
    """Same moment from two files, displayed at the same size: layout side|stack."""
    _ffmpeg_ok(hw)
    half = int(width / 2 / 2) * 2
    h = int(half * 9 / 16 / 2) * 2
    tag_a = f",{_label('AVANT  source')}" if labels else ""
    tag_b = f",{_label('APRES  UltraK')}" if labels else ""
    # Same resampler on both sides: a fair comparison must not flatter either file.
    chain = (f"[0:v]scale={'-1' if layout == 'stack' else half}:{h if layout != 'stack' else -1}"
             f":flags=area{tag_a}[a];"
             f"[1:v]scale={'-1' if layout == 'stack' else half}:{h if layout != 'stack' else -1}"
             f":flags=area{tag_b}[b];"
             f"[a][b]{'hstack' if layout == 'side' else 'vstack'}[out]")
    cmd = [hw.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{at:.3f}", "-i", src, "-ss", f"{at:.3f}", "-i", dist,
           "-filter_complex", chain, "-map", "[out]", "-frames:v", str(frame),
           "-update", "1", dst]
    _call(cmd)
    return dst


def zoom(hw: Hardware, src: str, dist: str, dst: str, *, at=0.0, panel=640,
         center=(0.5, 0.45), frac=0.22, labels=True) -> str:
    """Magnified crop pair: the only honest way to judge an upscale."""
    _ffmpeg_ok(hw)
    box = int(panel / 2 / 2) * 2
    box_h = int(box * 9 / 16 / 2) * 2
    tag_a = f",{_label('AVANT  detail')}" if labels else ""
    tag_b = f",{_label('APRES  detail')}" if labels else ""
    def crop(stream):
        return (f"[{stream}:v]crop=iw*{frac}:ih*{frac}:(iw-iw*{frac})*{center[0]}:"
                f"(ih-ih*{frac})*{center[1]}")
    chain = (f"{crop(0)},scale={box}:{box_h}:flags=neighbor{tag_a}[a];"
             f"{crop(1)},scale={box}:{box_h}:flags=neighbor{tag_b}[b];"
             f"[a][b]hstack=inputs=2[out]")
    cmd = [hw.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{at:.3f}", "-i", src, "-ss", f"{at:.3f}", "-i", dist,
           "-filter_complex", chain, "-map", "[out]", "-frames:v", "1", "-update", "1", dst]
    _call(cmd)
    return dst


def poster(hw: Hardware, src: str, dist: str, dst: str, *, at=0.0, width=1280,
           center=(0.5, 0.45), frac=0.22) -> str:
    """One README-ready image: full pair above, magnified pair below."""
    _ffmpeg_ok(hw)
    tmp = dst + ".tmp"
    pair(hw, src, dist, tmp + "a.png", at=at, width=width, layout="side")
    zoom(hw, src, dist, tmp + "b.png", at=at, panel=width, center=center, frac=frac)
    chain = (f"[0:v]scale={width}:-1[a];[1:v]scale={width}:-1[b];[a][b]vstack=inputs=2[out]")
    cmd = [hw.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", tmp + "a.png", "-i", tmp + "b.png",
           "-filter_complex", chain, "-map", "[out]", "-update", "1", dst]
    _call(cmd)
    for suffix in ("a.png", "b.png"):
        try:
            os.remove(tmp + suffix)
        except OSError:
            pass
    return dst


def gif(hw: Hardware, src: str, dist: str, dst: str, *, at=0.0, seconds=4.0, width=860,
        fps=6, center=(0.5, 0.45), frac=0.30, labels=True, max_mb=8.0) -> str:
    """Animated AVANT/APRES loop, auto-shrunk to stay README-friendly."""
    _ffmpeg_ok(hw)
    half = int(width / 2 / 2) * 2
    h = int(half * 9 / 16 / 2) * 2
    tag_a = f",{_label('AVANT', 12)}" if labels else ""
    tag_b = f",{_label('APRES', 12)}" if labels else ""
    crop = (f"crop=iw*{frac}:ih*{frac}:(iw-iw*{frac})*{center[0]}:"
            f"(ih-ih*{frac})*{center[1]}")
    chain = (f"[0:v]{crop},fps={fps},scale={half}:{h}:flags=neighbor{tag_a}[a];"
             f"[1:v]{crop},fps={fps},scale={half}:{h}:flags=neighbor{tag_b}[b];"
             f"[a][b]hstack,split[s][o];[s]palettegen=max_colors=64[p];[o][p]paletteuse=dither=bayer"
             f"[g]")
    cmd = [hw.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{at:.3f}", "-t", f"{seconds}", "-i", src,
           "-ss", f"{at:.3f}", "-t", f"{seconds}", "-i", dist,
           "-filter_complex", chain, "-map", "[g]", "-loop", "0", dst]
    _call(cmd, timeout=900)
    size = os.path.getsize(dst)
    if size > max_mb * 1024 * 1024 and width > 420:
        os.remove(dst)
        return gif(hw, src, dist, dst, at=at, seconds=seconds * 0.75, width=int(width * 0.8),
                   fps=max(4, fps - 1), center=center, frac=frac, labels=labels, max_mb=max_mb)
    return dst


def fidelity(hw: Hardware, src: str, dist: str, seconds=20.0, at=0.0) -> dict:
    """PSNR/SSIM of the output against the source at source resolution.

    High PSNR = the upscale stayed faithful to the original picture instead of
    inventing content. Pairs with the detail/sharpen numbers, not against them.
    """
    _ffmpeg_ok(hw)
    from .analyze import probe
    info = probe(src, hw.ffprobe)
    trim_r = f"[0:v]trim=start={at}:duration={seconds},setpts=PTS-STARTPTS,format=yuv420p,split=2[r1][r2]"
    trim_d = (f"[1:v]trim=start={at}:duration={seconds},setpts=PTS-STARTPTS,"
              f"scale={info.width}:{info.height}:flags=area,format=yuv420p,split=2[d1][d2]")
    chain = f"{trim_r};{trim_d};[r1][d1]psnr[p];[r2][d2]ssim[s]"
    cmd = [hw.ffmpeg, "-hide_banner", "-loglevel", "info", "-y",
           "-i", src, "-i", dist, "-filter_complex", chain,
           "-map", "[p]", "-map", "[s]", "-t", f"{min(seconds, info.duration or seconds)}",
           "-f", "null", "-"]
    code, out, err = run(cmd, timeout=1200)
    text = out + err
    result = {}
    m = re.search(r"PSNR y:([\d.]+) u:[\d.]+ v:[\d.]+ average:([\d.inf]+)", text)
    if m:
        result["psnr_y_db"] = round(float(m.group(1)), 2)
        result["psnr_avg_db"] = float("inf") if "inf" in m.group(2) else float(m.group(2))
    m = re.search(r"SSIM Y:([\d.]+)", text)
    if m:
        result["ssim_y"] = round(float(m.group(1)), 4)
    m = re.search(r"All:([\d.]+)", text)
    if m:
        result["ssim"] = round(float(m.group(1)), 4)
    if code != 0 and not result:
        raise UltraKError(f"mesure de fidelite impossible: {text.strip()[-200:]}")
    return result


def detail_at(hw: Hardware, video: str, at=0.0) -> float:
    """Native-resolution high-frequency energy; compare two files at the same size."""
    from .analyze import detail_of_video
    return detail_of_video(hw.ffmpeg, video, at=at)
