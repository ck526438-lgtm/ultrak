"""Build the demo source: a still photo turned into a believable 'average quality' clip.

Slow push-in + handheld drift + sensor noise + aggressive phone-like compression.
Re-runnable so the README always matches a real file anyone can regenerate.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from ultrak.hardware import find_ffmpeg  # noqa: E402
from ultrak.utils import run  # noqa: E402


def build(ffmpeg, image, out, seconds=8.0, size="1280x720", fps=30, quality="medium-bad"):
    width, height = size.split("x")
    frames = int(seconds * fps)
    grade = {
        "phone": "noise=alls=13:allf=t+u,gblur=sigma=0.6,eq=brightness=-0.01:saturation=0.92",
        "medium-bad": "noise=alls=9:allf=t+u,gblur=sigma=0.4",
        "clean": "format=yuv420p",
    }[quality]
    crf = {"phone": "36", "medium-bad": "33", "clean": "23"}[quality]
    vf = (f"zoompan=z='min(1.12+0.0009*on,1.34)':"
          f"x='iw/2-(iw/zoom/2)+iw*0.006*sin(on/30)':"
          f"y='ih/2-(ih/zoom/2)-ih*0.004*cos(on/37)':"
          f"d=1:s={size}:fps={fps},{grade},scale={width}:{height}:flags=bicubic,format=yuv420p")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-loop", "1", "-framerate", str(fps), "-t", f"{seconds}", "-i", image,
           "-vf", vf, "-frames:v", str(frames), "-r", str(fps),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
           "-pix_fmt", "yuv420p", out]
    code, _, err = run(cmd, timeout=600)
    if code != 0:
        raise SystemExit(f"ffmpeg a echoue: {err.strip()[-400:]}")
    return out


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?",
                        default=os.path.join(here, "..", "docs", "demo", "source-scene.jpg"))
    parser.add_argument("out", nargs="?", default=os.path.join(here, "..", "demo", "source_720.mp4"))
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--size", default="1280x720")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--quality", default="medium-bad",
                        choices=["phone", "medium-bad", "clean"])
    parser.add_argument("--ffmpeg")
    args = parser.parse_args()
    ff = args.ffmpeg or find_ffmpeg(probe=lambda p: True)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    path = build(ff, args.image, args.out, args.seconds, args.size, args.fps, args.quality)
    print(f"{path}  ({os.path.getsize(path) / 1e6:.2f} Mo)")
