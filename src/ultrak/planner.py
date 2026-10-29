"""Turn a source file + this machine into a defensible rendering plan."""

import os
from dataclasses import dataclass, field

from .analyze import SourceInfo
from .config import calibration
from .hardware import Hardware
from .pipeline import PipelinePlan

MODES = ("auto", "4k", "qhd", "1080")
CEILING_H = {"4k": 2160, "qhd": 1440, "1080": 1080}
LADDER = (720, 1080, 1440, 2160)
DENOISE_COST = {"none": 1.58, "light": 1.25, "medium": 1.0, "strong": 0.41}

# Measured by `python ultrak bench` on the reference machine: i3-2100 (2C/4T), 8 GB,
# no hardware encoder, 480p -> 3840x2160 (8.29 Mpx), libx264 medium crf18, 125 frames.
#   lanczos only 2.82 fps | medium denoise + pro sharpen 1.24 fps
#   two-step 1.39 fps     | nlmeans strong + pro sharpen 0.74 fps
CALIBRATION = {"pixels_per_second": 1.24 * 8.29e6, "reference_score": 44.0, "gpu_bonus": 2.6}

PRESET_SPEED = {"ultrafast": 4.5, "superfast": 3.4, "veryfast": 2.4, "faster": 1.7,
                "fast": 1.3, "medium": 1.0, "slow": 0.72, "slower": 0.5, "veryslow": 0.34}
PRESET_ORDER = ["veryslow", "slower", "slow", "medium", "fast", "faster", "veryfast",
                "superfast", "ultrafast"]


@dataclass
class Decision:
    """Why the planner picked what it picked. Surfaced to the user, never silent."""
    reason: str
    detail: str = ""


@dataclass
class Job:
    src: SourceInfo
    plan: PipelinePlan
    dst: str
    decisions: list = field(default_factory=list)
    estimated_fps: float = 0.0
    eta_seconds: float = 0.0
    honest_cap: int = 0
    measured_fps: float = 0.0

    @property
    def geometry(self) -> str:
        return f"{self.src.width}x{self.src.height} -> {self.plan.target_width}x{self.plan.target_height}"

    @property
    def throughput(self) -> float:
        return self.measured_fps or self.estimated_fps

    def eta_for(self, frames: int) -> float:
        rate = self.throughput
        return frames / rate if rate else 0.0

    def upscale_factor(self) -> float:
        return self.plan.target_height / self.src.height if self.src.height else 1.0


def useful_upscale_factor(src: SourceInfo) -> float:
    """How far this source can be pushed before 4K becomes an empty label."""
    factor = 2.0
    if src.bpp >= 0.12:
        factor += 1.2
    elif src.bpp >= 0.07:
        factor += 0.8
    elif src.bpp >= 0.04:
        factor += 0.4
    else:
        factor -= 0.5
    if src.detail_score >= 0:
        if src.detail_score > 0.55:
            factor += 0.9
        elif src.detail_score > 0.35:
            factor += 0.45
        elif src.detail_score < 0.18:
            factor -= 0.6
    if src.codec in ("mjpeg", "gif", "png"):
        factor += 0.5
    return max(1.5, min(4.5, round(factor, 2)))


def honest_height(src: SourceInfo, factor: float) -> int:
    """Largest ladder step the source can genuinely support, never below its own height."""
    ceiling = int(src.height * factor)
    choice = LADDER[0]
    for step in LADDER:
        if step <= ceiling:
            choice = step
    return max(choice, min(LADDER[-1], src.height))


def choose_height(src: SourceInfo, mode: str) -> (int, Decision):
    factor = useful_upscale_factor(src)
    cap = honest_height(src, factor)
    if mode in CEILING_H:
        height = CEILING_H[mode]
        if height > cap:
            return height, Decision(
                f"4K forcee en mode '{mode}'",
                f"la source ne justifie que {cap}p ({factor}x utile, {src.bpp:.3f} bpp, "
                f"detail {src.detail_score:.2f}). Les pixels en plus seront du lissage.")
        return height, Decision(f"sortie {height}p ({mode})", f"{factor}x d'agrandissement, soutenu")
    if cap >= 2160 or src.height >= 2160:
        return 2160, Decision("4K justifiee", f"source dense: {factor}x utile, {src.bpp:.3f} bpp")
    return cap, Decision(
        f"fausse 4K evitee: sortie plafonnee a {cap}p",
        f"{src.bpp:.3f} bpp et detail {src.detail_score:.2f} ne supportent pas "
        f"{2160 / src.height:.2f}x. Utilise --mode 4k pour forcer quand meme.")


def choose_codec(hw: Hardware, requested: str) -> (str, Decision):
    if requested != "auto":
        return requested, Decision(f"codec {requested} demande", "")
    for name, brand in (("hevc_nvenc", "nvidia"), ("hevc_qsv", "intel"), ("hevc_amf", "amd")):
        if hw.has_gpu_encoder(name):
            return name, Decision(f"encode GPU {brand} ({name})", "secours automatique sur libx264")
    return "libx264", Decision("encode CPU libx264", "aucun encodeur materiel detecte")


def choose_denoise(src: SourceInfo, hw: Hardware, requested: str) -> (str, Decision):
    if requested != "auto":
        return requested, Decision(f"denoise = {requested}", "choix utilisateur")
    grainy = src.noise_score > 0.35 or src.bpp < 0.05
    clean = src.height >= 1080 and src.bpp >= 0.16 and src.detail_score > 0.5
    if clean:
        return "light", Decision("denoise = light", "source propre: un denoise lourd "
                                                   "effacerait du detail reel")
    if grainy and src.height <= 1080 and hw.ram_free_gb >= 3.0:
        return "strong", Decision("denoise = strong",
                                  f"artefacts/hf fines mesures {src.noise_score:.2f} sur "
                                  f"{src.bpp:.3f} bpp, nlmeans possible a {src.height}p")
    if grainy:
        blockers = []
        if src.height > 1080:
            blockers.append(f"source en {src.height}p trop grande pour nlmeans")
        if hw.ram_free_gb < 3.0:
            blockers.append(f"RAM libre {hw.ram_free_gb:.1f} Go < 3 Go")
        return "medium", Decision("denoise = medium",
                                  f"hf non conservees {src.noise_score:.2f}; "
                                  + (" et ".join(blockers) if blockers else
                                     "nlmeans garde une charge inutile"))
    return "medium", Decision("denoise = medium",
                              f"hf non conservees {src.noise_score:.2f}, densite {src.bpp:.3f} bpp")


def choose_sharpen(src: SourceInfo, factor: float, requested: str) -> (str, Decision):
    if requested != "auto":
        return requested, Decision(f"sharpen = {requested}", "choix utilisateur")
    if factor <= 1.35:
        return "soft", Decision("sharpen = soft", "presque pas d'agrandissement; un sharpen "
                                                  "fort ajouterait du halo blanc")
    if src.detail_score >= 0 and src.detail_score < 0.2:
        return "pro", Decision("sharpen = pro", "source tres molle: unsharp + CAS pour "
                                                "restituer les micro-contrastes")
    return "pro", Decision("sharpen = pro", "reglage par defaut equilibre")


def estimate_fps(plan: PipelinePlan, hw: Hardware) -> float:
    cal = calibration(CALIBRATION)
    pixels = max(1, plan.target_width * plan.target_height)
    speed = PRESET_SPEED.get(plan.preset, 1.0)
    ratio = max(0.2, hw.cpu_score() / float(cal["reference_score"]))
    gpu = float(cal["gpu_bonus"]) if not plan.codec.startswith("libx") else 1.0
    value = float(cal["pixels_per_second"]) * speed * ratio * gpu * \
        DENOISE_COST.get(plan.denoise, 1.0) / pixels
    if plan.smooth:
        value *= 0.22
    return round(value, 3)


def total_frames(src: SourceInfo, plan: PipelinePlan) -> int:
    fps = plan.fps or src.fps or 30
    if src.frames and not plan.fps:
        return src.frames
    return int(round((src.duration or 0) * fps)) if src.duration else src.frames


def tune_to_budget(job: Job, hw: Hardware, budget_seconds: float, allow_res_drop: bool) -> Job:
    """Step presets down (then resolution, in auto mode) until the render fits the budget."""
    frames = total_frames(job.src, job.plan)
    job.estimated_fps = estimate_fps(job.plan, hw)
    job.eta_seconds = job.eta_for(frames)
    if not budget_seconds or not frames:
        return job

    for _ in range(16):
        fps = estimate_fps(job.plan, hw)
        eta = frames / fps if fps else float("inf")
        if eta <= budget_seconds:
            job.estimated_fps, job.eta_seconds = fps, eta
            return job
        if job.plan.preset != "ultrafast":
            job.plan.preset = _faster(job.plan.preset)
            job.decisions.append(Decision(
                f"preset = {job.plan.preset}",
                f"ETA estime {eta / 60:.0f} min > budget {budget_seconds / 60:.0f} min"))
            continue
        if allow_res_drop and job.plan.target_height > 720:
            new_h = _next_step_down(job.plan.target_height)
            job.plan.target_height = new_h
            job.plan.target_width = _even(new_h * job.src.aspect)
            job.decisions.append(Decision(f"resolution baisse a {new_h}p", "budget temps"))
            continue
        job.estimated_fps, job.eta_seconds = fps, eta
        job.decisions.append(Decision(
            "budget temps non tenable",
            f"au plus rapide possible ici: ETA {eta / 60:.0f} min > "
            f"{budget_seconds / 60:.0f} min"))
        return job
    return job


def _faster(preset: str) -> str:
    i = PRESET_ORDER.index(preset) if preset in PRESET_ORDER else 3
    return PRESET_ORDER[min(len(PRESET_ORDER) - 1, i + 1)]


def _next_step_down(height: int) -> int:
    for step in reversed(LADDER):
        if step < height:
            return step
    return LADDER[0]


def _even(value: float) -> int:
    n = int(round(value))
    return n if n % 2 == 0 else n + 1


def default_output(src: SourceInfo, plan: PipelinePlan) -> str:
    stem = os.path.splitext(os.path.basename(src.path))[0]
    safe = "".join(c if c.isalnum() or c in " ._-" else "_" for c in stem).strip() or "video"
    tag = f"{plan.target_width}x{plan.target_height}"
    if plan.fps:
        tag += f"_{plan.fps:g}fps"
    return os.path.join(os.path.dirname(src.path), f"{safe}__ultrak_{tag}.mp4")


def make_plan(src: SourceInfo, hw: Hardware, mode="auto", *, codec="auto", preset="medium",
              crf=18, fps=0.0, smooth=False, denoise="auto", sharpen="auto", grade="natural",
              audio="copy", two_step=True, max_bpp=0.06) -> Job:
    height, d_height = choose_height(src, mode)
    width = _even(height * src.aspect)
    factor = height / src.height if src.height else 1.0
    chosen_codec, d_codec = choose_codec(hw, codec)
    level_denoise, d_denoise = choose_denoise(src, hw, denoise)
    level_sharpen, d_sharpen = choose_sharpen(src, factor, sharpen)

    plan = PipelinePlan(target_width=width, target_height=height, fps=fps or 0.0,
                        smooth=smooth, denoise=level_denoise, sharpen=level_sharpen,
                        grade=grade, codec=chosen_codec, preset=preset, crf=crf,
                        max_bpp=max_bpp, audio=audio, two_step=two_step,
                        src_width=src.width, src_height=src.height)
    job = Job(src=src, plan=plan, dst=default_output(src, plan),
              decisions=[d_height, d_codec, d_denoise, d_sharpen],
              honest_cap=honest_height(src, useful_upscale_factor(src)))
    return tune_to_budget(job, hw, 0.0, allow_res_drop=(mode == "auto"))
