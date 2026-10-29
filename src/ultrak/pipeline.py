"""Build the ffmpeg filtergraph and the encode command from a plan."""

from dataclasses import dataclass, field

from .utils import even as _even

SCALE_UP = "lanczos+accurate_rnd+full_chroma_int+full_chroma_inp"
DEBAND = "deband=1thr=0.03:2thr=0.03:3thr=0.03:4thr=0.03:range=16"

DENOISE = {
    "none": [],
    "light": ["hqdn3d=1.2:1.2:3:3"],
    "medium": [DEBAND, "atadenoise=0a=0.02:0b=0.04:1a=0.02:1b=0.04", "hqdn3d=1.5:1.5:5:5"],
    "strong": [DEBAND, "nlmeans=s=3.0:p=7:r=15"],
}

SHARPEN = {
    "none": [],
    "soft": ["cas=0.3"],
    "pro": ["unsharp=5:5:0.6:3:3:0.25", "cas=0.35"],
    "strong": ["unsharp=5:5:1.0:5:5:0.4", "cas=0.5"],
}

GRADE = {
    "none": [],
    "natural": ["eq=contrast=1.03:saturation=1.05:gamma=1.01"],
    "punch": ["eq=contrast=1.06:saturation=1.14:gamma=0.99",
              "colortemperature=temperature=6800:mix=0.06"],
}

HEAVY_FILTERS = {"nlmeans", "guided", "minterpolate", "nnedenoise", "deconvolve"}


@dataclass
class PipelinePlan:
    """Everything needed to turn one source into one output file."""
    target_width: int
    target_height: int
    fps: float = 0.0
    smooth: bool = False
    denoise: str = "medium"
    sharpen: str = "pro"
    grade: str = "natural"
    codec: str = "libx264"
    preset: str = "medium"
    crf: int = 18
    max_bpp: float = 0.06
    audio: str = "copy"
    two_step: bool = True
    src_width: int = 0
    src_height: int = 0
    extra_vf: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def upscale_factor(self, src_height: float) -> float:
        return self.target_height / src_height if src_height else 1.0

    def used_filters(self) -> set:
        return {part.split("=")[0].split(":")[0] for part in self.vf().split(",") if part}

    def vf(self) -> str:
        """Order matters: temporal work happens at source resolution, sharpening last."""
        chain = []
        if self.fps:
            if self.smooth:
                chain.append(f"minterpolate=fps={self.fps:g}:mi_mode=mci:mc_mode=aobmc:"
                             f"me_mode=bidir:vsbmc=1:scd=fdiff:scd_threshold=8")
            else:
                chain.append(f"fps={self.fps:g}")
        chain += DENOISE.get(self.denoise, [])
        chain += self._scale_chain()
        chain += SHARPEN.get(self.sharpen, [])
        chain += GRADE.get(self.grade, [])
        chain += self.extra_vf
        chain.append("format=yuv420p")
        return ",".join(chain)

    def _scale_chain(self):
        w, h = self.target_width, self.target_height
        single = [f"scale={w}:{h}:flags={SCALE_UP}"]
        if not self.two_step or not self.src_height:
            return single
        # A 2x intermediate step only pays off on a big jump; below that it just blurs.
        mid = _even(self.src_height * 2)
        if mid >= h or h / self.src_height < 2.5:
            return single
        return [f"scale=-2:{mid}:flags={SCALE_UP}", f"scale={w}:{h}:flags={SCALE_UP}"]

    def quality_ceiling(self) -> int:
        return int(self.max_bpp * self.target_width * self.target_height * (self.fps or 30))

    def encoder_args(self) -> list:
        args = ["-c:v", self.codec, "-pix_fmt", "yuv420p"]
        if self.codec in ("libx264", "libx265"):
            args += ["-preset", self.preset, "-crf", str(self.crf)]
        if self.codec in ("libx264", "libx265", "hevc_nvenc", "h264_nvenc"):
            args += ["-profile:v", "main10" if self.codec == "libx265" else "high"]
        if self.codec in ("hevc_nvenc", "h264_nvenc"):
            args += ["-preset", self.preset, "-rc", "vbr", "-cq", str(self.crf), "-b:v", "0"]
        if self.codec in ("hevc_qsv", "h264_qsv", "h264_amf", "hevc_amf"):
            args += ["-preset", self.preset, "-global_quality", str(self.crf)]
        ceiling = self.quality_ceiling()
        if ceiling:
            args += ["-maxrate", str(int(ceiling)), "-bufsize", str(int(ceiling) * 2)]
        fps = self.fps or 30
        args += ["-g", str(max(2, int(round(fps * 4))))]
        return args

    def audio_args(self) -> list:
        if self.audio == "copy":
            return ["-c:a", "copy"]
        if self.audio == "none":
            return ["-an"]
        return ["-c:a", self.audio, "-b:a", "192k"]

    def build(self, src: str, dst: str, start: float = 0.0, duration: float = 0.0) -> list:
        """Job-specific args only; global flags and progress belong to the engine."""
        cmd = ["-y"]
        if start:
            cmd += ["-ss", f"{start:.3f}"]
        if duration:
            cmd += ["-t", f"{duration:.3f}"]
        cmd += ["-i", src, "-map", "0:v:0", "-vf", self.vf()]
        cmd += self.encoder_args() + self.audio_args()
        cmd += ["-map_metadata", "0", "-movflags", "+faststart", dst]
        return cmd
