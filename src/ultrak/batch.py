"""Prepare and run batches: analysis, measured preview, budget tuning, resume."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .analyze import SourceInfo, measure_content, probe
from .engine import Result, preview, render, verify
from .hardware import Hardware
from .planner import Job, estimate_fps, make_plan, total_frames, tune_to_budget
from .utils import UltraKError, human_bytes, human_duration, json_dump, json_load, now

MANIFEST = ".ultrak-jobs.json"


@dataclass
class JobState:
    job: Job
    status: str = "pending"
    result: Result = None
    error: str = ""
    log: list = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.status in ("done", "failed", "skipped")


def analyze(path: str, ffmpeg: str, hw: Hardware, content=True) -> SourceInfo:
    src = probe(path, hw.ffprobe)
    if content and src.duration and src.duration > 0.5:
        try:
            measure_content(path, src, ffmpeg)
        except UltraKError:
            src.detail_score = -1.0
    return src


def prepare(path: str, ffmpeg: str, hw: Hardware, *, mode="auto", codec="auto",
            preset="medium", crf=18, fps=0.0, smooth=False, denoise="auto",
            sharpen="auto", grade="natural", audio="copy", two_step=True,
            max_bpp=0.06, budget_minutes=0.0, use_preview=True, preview_seconds=4.0,
            out_dir="", dry_run=False, log=None) -> JobState:
    """Full decision chain for one file, with the ETA corrected by a measured preview."""
    emit = log or (lambda *a: None)
    src = analyze(path, ffmpeg, hw)
    emit(f"  source {src.label}, {human_bytes(src.file_bytes)}, {human_duration(src.duration)}, "
         f"bitrate {src.effective_bit_rate // 1000} kbps ({src.bpp:.3f} bpp)")
    if src.detail_score >= 0:
        emit(f"  contenu: structure {src.detail_score:.2f}, hf non conservees {src.noise_score:.2f}, "
             f"mouvement {src.motion_score:.2f}")

    job = make_plan(src, hw, mode=mode, codec=codec, preset=preset, crf=crf, fps=fps,
                    smooth=smooth, denoise=denoise, sharpen=sharpen, grade=grade,
                    audio=audio, two_step=two_step, max_bpp=max_bpp)
    for decision in job.decisions:
        emit(f"  -> {decision.reason}" + (f"  [{decision.detail}]" if decision.detail else ""))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        job.dst = os.path.join(out_dir, os.path.basename(job.dst))

    frames = total_frames(src, job.plan)
    job.estimated_fps = estimate_fps(job.plan, hw)
    job.eta_seconds = job.eta_for(frames)
    emit(f"  plan: {job.geometry}, preset {job.plan.preset}, crf {job.plan.crf}, "
         f"denoise {job.plan.denoise}, ETA estime {human_duration(job.eta_seconds)}")

    if use_preview and not dry_run and frames > 60:
        t0 = time.perf_counter()
        emit(f"  preview {preview_seconds:g}s en cours...", )
        try:
            job.measured_fps = preview(job, ffmpeg, hw, seconds=preview_seconds,
                                       offset=(src.duration or 0) * 0.3)
            emit(f"  preview: {job.measured_fps:.2f} fps mesures sur cette machine "
                 f"({human_duration(job.eta_for(frames))} pour la video complete)")
        except UltraKError as exc:
            emit(f"  preview echouee ({exc}), estimation conservee")
        emit(f"  preview en {time.perf_counter() - t0:.1f}s")

    if budget_minutes:
        tune_to_budget(job, hw, budget_minutes * 60.0, allow_res_drop=(mode == "auto"))
        for decision in job.decisions[-3:]:
            if "budget" in decision.reason or "ETA" in decision.detail:
                emit(f"  -> {decision.reason}  [{decision.detail}]")
        emit(f"  apres budget: preset {job.plan.preset}, {job.geometry}, "
             f"ETA {human_duration(job.eta_for(frames))}")

    return JobState(job=job)


def _load_manifest(directory: str) -> dict:
    return json_load(os.path.join(directory, MANIFEST)) or {"done": {}}


def _save_manifest(directory: str, data: dict) -> None:
    json_dump(os.path.join(directory, MANIFEST), data)


def auto_workers(hw: Hardware, jobs) -> int:
    """CPU encodes already saturate the machine; GPU encodes leave room for a second job."""
    if not jobs:
        return 1
    gpu = all(not j.job.plan.codec.startswith("libx") for j in jobs)
    if gpu:
        return max(1, min(4, hw.logical_cpus // 2))
    return 1 if hw.cpu_score() < 70 else max(1, min(2, hw.logical_cpus // 4))


def run_batch(states, ffmpeg, hw, *, workers=0, on_progress=None, cancel=None,
              force=False, log=None, verify_output=True):
    """Render a queue. Each job is resumable: finished outputs are skipped, not redone."""
    emit = log or (lambda *a: None)
    workers = workers or auto_workers(hw, states)
    manifest_dir = os.path.dirname(states[0].job.dst) if states else "."
    manifest = _load_manifest(manifest_dir)

    pending = []
    for state in states:
        dst = state.job.dst
        if not force and (os.path.isfile(dst) or dst in manifest.get("done", {})):
            state.status = "skipped"
            state.log.append("deja rendu, ignore (--force pour relancer)")
            emit(f"  ignore (existe deja): {os.path.basename(dst)}")
            continue
        pending.append(state)

    emit(f"file d'attente: {len(pending)} rendu(s) a lancer, {workers} en parallele")
    if not pending:
        return states

    lock = threading.Lock()

    def worker(state: JobState):
        if cancel is not None and cancel.is_set():
            state.status = "failed"
            state.error = "annule avant demarrage"
            return state
        job = state.job
        state.status = "running"
        emit(f"  >> {os.path.basename(job.src.path)} -> {job.geometry} "
             f"({job.plan.codec}/{job.plan.preset}, {human_duration(job.eta_for(total_frames(job.src, job.plan)))} estime)")

        def cb(progress):
            if cancel is not None and cancel.is_set():
                return False
            if on_progress:
                with lock:
                    on_progress(state, progress)
            return True

        try:
            result = render(ffmpeg, job.plan, job.src.path, job.dst, hw,
                            expect_frames=total_frames(job.src, job.plan),
                            on_progress=cb, cancel=cancel, quiet=True)
            state.result = result
            if result.ok:
                state.status = "done"
                if verify_output:
                    report = verify(job.dst, hw, job.plan, job.src)
                    state.log.append(report)
                    emit(f"     {report}")
                with lock:
                    manifest.setdefault("done", {})[job.dst] = {
                        "src": job.src.path, "at": now(), "seconds": result.seconds,
                        "size": result.size_bytes, "fps": result.fps}
                    _save_manifest(manifest_dir, manifest)
                emit(f"     termine en {human_duration(result.seconds)}, "
                     f"{human_bytes(result.size_bytes)}, {result.speed}")
                if result.fallback_used:
                    emit(f"     repli: {result.fallback_used}")
            else:
                state.status = "failed"
                state.error = result.error or "echec inconnu"
                emit(f"     ECHEC: {state.error[:160]}")
        except Exception as exc:
            state.status = "failed"
            state.error = str(exc)[:300]
            emit(f"     ECHEC: {state.error}")
        return state

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, pending))
    return states


def summarize(states) -> str:
    done = [s for s in states if s.status == "done"]
    failed = [s for s in states if s.status == "failed"]
    skipped = [s for s in states if s.status == "skipped"]
    size = sum(s.result.size_bytes for s in done)
    seconds = sum(s.result.seconds for s in done)
    lines = [f"bilan: {len(done)} rendu(s), {len(skipped)} ignore(s), {len(failed)} echec(s)"]
    if done:
        lines.append(f"sorties: {human_bytes(size)} en {human_duration(seconds)} "
                     f"(moyenne {size / len(done) / 1e6:.1f} Mo par fichier)")
    for state in failed:
        lines.append(f"  echec: {os.path.basename(state.job.src.path)} - {state.error[:120]}")
    return "\n".join(lines)
