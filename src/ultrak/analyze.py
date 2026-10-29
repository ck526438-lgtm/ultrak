"""Measure a source file: geometry, bitrate density and real content detail."""

import json
import os
import re
from dataclasses import dataclass, field

from .utils import UltraKError, parse_fraction, run


@dataclass
class SourceInfo:
    path: str
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    frames: int = 0
    bit_rate: int = 0
    pix_fmt: str = ""
    profile: str = ""
    has_audio: bool = False
    audio_codec: str = ""
    rotation: int = 0
    file_bytes: int = 0
    detail_score: float = -1.0
    noise_score: float = -1.0
    motion_score: float = -1.0
    hf_native: float = 0.0
    hf_struct: float = 0.0
    samples: list = field(default_factory=list)

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 16 / 9

    @property
    def effective_bit_rate(self) -> int:
        """Many containers omit a per-stream bitrate; the file size still tells the truth."""
        if self.bit_rate:
            return self.bit_rate
        if self.file_bytes and self.duration:
            return int(self.file_bytes * 8 / self.duration)
        return 0

    @property
    def bpp(self) -> float:
        """Bits per pixel per frame: how much real information the source carries."""
        rate = self.effective_bit_rate
        if not (rate and self.width and self.height and self.fps):
            return 0.0
        return rate / (self.width * self.height * self.fps)

    @property
    def short_res(self) -> str:
        return f"{self.width}x{self.height}@{self.fps:g}"

    @property
    def label(self) -> str:
        tier = "inconnue"
        h = self.height
        if h <= 480:
            tier = "SD"
        elif h <= 720:
            tier = "HD"
        elif h <= 1080:
            tier = "Full HD"
        elif h <= 1440:
            tier = "QHD"
        elif h <= 2160:
            tier = "4K"
        else:
            tier = "au-dela de la 4K"
        return f"{tier} ({self.short_res})"


def _rotation_of(stream) -> int:
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
        if sd.get("side_data_type") == "Display Matrix":
            m = re.search(r"rotation[\"']?\s*[:=]\s*(-?[\d.]+)", json.dumps(sd))
            if m:
                return int(round(float(m.group(1)))) % 360
    return 0


def probe(path: str, ffprobe: str) -> SourceInfo:
    if not os.path.isfile(path):
        raise UltraKError(f"fichier introuvable: {path}")
    if not ffprobe:
        raise UltraKError("ffprobe introuvable a cote de ffmpeg")
    cmd = [ffprobe, "-hide_banner", "-loglevel", "error", "-print_format", "json",
           "-show_streams", "-show_format", path]
    code, out, err = run(cmd, timeout=120)
    if code != 0:
        raise UltraKError(f"ffprobe a echoue sur {os.path.basename(path)}: {err.strip()[:200]}")
    data = json.loads(out or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise UltraKError(f"pas de piste video dans {path}")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    fmt = data.get("format", {})
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rotation = _rotation_of(video)
    if rotation in (90, 270):
        width, height = height, width

    duration = _to_float(video.get("duration"), _to_float(fmt.get("duration")))
    bit_rate = int(_to_float(video.get("bit_rate"), 0)) or int(_to_float(fmt.get("bit_rate"), 0))
    file_bytes = int(_to_float(fmt.get("size"), 0)) or _os_size(path)
    if not bit_rate and duration and file_bytes:
        bit_rate = int(file_bytes * 8 / duration)

    fps = parse_fraction(video.get("r_frame_rate")) or parse_fraction(video.get("avg_frame_rate"))
    frames = int(_to_float(video.get("nb_frames"), 0))
    if not frames and duration and fps:
        frames = int(round(duration * fps))

    return SourceInfo(
        path=os.path.abspath(path), codec=video.get("codec_name", ""),
        width=width, height=height, fps=round(fps, 3), duration=round(duration, 3),
        frames=frames, bit_rate=bit_rate, pix_fmt=video.get("pix_fmt", ""),
        profile=video.get("profile", ""), has_audio=audio is not None,
        audio_codec=(audio or {}).get("codec_name", ""), rotation=rotation,
        file_bytes=file_bytes,
    )


def _to_float(*vals):
    for v in vals:
        if v not in (None, "", "N/A"):
            try:
                f = float(v)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                continue
    return 0.0


def _os_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def run_metric(ffmpeg: str, src: str, start: float, vf: str, key: str, seconds=0.5):
    """Sample one second of video through a filter chain and collect metadata values."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info", "-ss", f"{start:.3f}",
           "-t", f"{seconds}", "-i", src, "-an", "-vf", vf, "-f", "null", "-"]
    code, out, err = run(cmd, timeout=180)
    return [float(m) for m in re.findall(rf"{re.escape(key)}=(-?[\d.]+)", out + err)]


HF_KEY = "lavfi.signalstats.YAVG"
# Centre crop in native pixels: measuring after a resize would compare apples to oranges.
CROP = "crop=w='min(iw\\,512)':h='min(ih\\,288)':x='(iw-min(iw\\,512))/2':y='(ih-min(ih\\,288))/2'"


def _hf_graph(pre: str) -> str:
    """High-frequency energy = residual after subtracting a blurred copy of the frame."""
    return (f"[0:v]{CROP}{pre},format=gray,split[a][b];"
            f"[b]gblur=sigma=2[bl];[a][bl]blend=all_mode=difference,signalstats,"
            f"metadata=print:key={HF_KEY}[out]")


def _clip(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 3)


def _measure_hf(ffmpeg: str, src: str, start: float, graph: str, seconds=0.5) -> float:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info", "-ss", f"{start:.3f}",
           "-t", f"{seconds}", "-i", src, "-an", "-filter_complex", graph,
           "-map", "[out]", "-f", "null", "-"]
    code, out, err = run(cmd, timeout=180)
    vals = [float(m) for m in re.findall(rf"{HF_KEY}=(-?[\d.]+)", out + err)]
    return sum(vals) / len(vals) if vals else 0.0


def measure_content(src: str, info: SourceInfo, ffmpeg: str, passes=3) -> SourceInfo:
    """Estimate real structure, fine-detail loss and motion without decoding the whole file.

    Structure is what survives a 2x down/up round trip. What the round trip destroys is
    the finest detail -- grain, blocking and ringing live there -- so that gap sets how
    hard to denoise before scaling, and the retained level sets how far it is worth going up.
    """
    dur = info.duration or 0
    starts = [dur * f for f in (0.12, 0.45, 0.78)][:passes if dur > 3 else 1] or [0.0]
    roundtrip = ",scale=iw/2:ih/2:flags=area,scale=iw*2:ih*2:flags=bicubic"

    native, struct, motion = [], [], []
    for point in starts:
        native.append(_measure_hf(ffmpeg, src, point, _hf_graph("")))
        struct.append(_measure_hf(ffmpeg, src, point, _hf_graph(roundtrip)))
        motion += run_metric(ffmpeg, src, point,
                             "scale=320:180:flags=area,setsar=1,format=gray,scdet=t=0:s=1,"
                             f"metadata=print:key=lavfi.scd.score", "lavfi.scd.score")

    info.hf_native = round(sum(native) / len(native), 3)
    info.hf_struct = round(sum(struct) / len(struct), 3)
    # 8.0 residual gray units is a very sharp reference frame; half the native high
    # frequency failing a 2x round trip is a grain- or block-dominated picture.
    info.detail_score = _clip(info.hf_struct / 8.0)
    ratio = (info.hf_native - info.hf_struct) / info.hf_native if info.hf_native > 0 else 0.0
    info.noise_score = _clip(ratio / 0.5)
    if motion:
        info.motion_score = round(max(0.0, min(1.0, sum(motion) / len(motion) / 8.0)), 3)
    info.samples = [{"hf_native": info.hf_native, "hf_struct": info.hf_struct,
                     "points": len(starts)}]
    return info


def detail_of_video(ffmpeg: str, video: str, at=0.0, seconds=0.5) -> float:
    """Native-resolution high-frequency energy, for before/after comparisons."""
    return round(_measure_hf(ffmpeg, video, at, _hf_graph(""), seconds), 3)
