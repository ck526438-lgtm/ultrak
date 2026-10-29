"""UltraK command line."""

import argparse
import glob
import json
import os
import signal
import sys
import threading

from . import __version__
from .batch import analyze as analyze_source, prepare, run_batch, summarize
from .bench import run_bench
from .config import load as load_config
from .hardware import Hardware, detect
from .installer import install as install_ffmpeg
from .planner import MODES, total_frames
from .report import fidelity, gif, poster, zoom
from .utils import UltraKError, human_bytes, human_duration

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv", ".ts", ".flv")


def _expand(paths):
    files = []
    for item in paths:
        if os.path.isdir(item):
            files += [os.path.join(item, name) for name in sorted(os.listdir(item))
                      if name.lower().endswith(VIDEO_EXT)]
        elif any(ch in item for ch in "*?["):
            files += [f for f in sorted(glob.glob(item)) if f.lower().endswith(VIDEO_EXT)]
        else:
            files.append(item)
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        raise UltraKError("fichier(s) introuvable(s): " + ", ".join(os.path.basename(m) for m in missing))
    return files


def _bar(progress, label=""):
    pct = progress.percent
    filled = int(pct / 100 * 28)
    line = (f"\r  [{'#' * filled}{'-' * (28 - filled)}] {pct:5.1f}%  "
            f"{progress.speed:5.2f} fps  ETA {human_duration(progress.eta_seconds):>7s}  "
            f"{progress.frames} img")
    sys.stderr.write(line if label else line + "   ")
    sys.stderr.flush()
    if pct >= 99.5:
        sys.stderr.write("\n")


def _hw(args, refresh=False) -> Hardware:
    explicit = getattr(args, "ffmpeg", None) or os.environ.get("ULTRAK_FFMPEG")
    if explicit:
        return detect(explicit, refresh=refresh)
    try:
        return detect(refresh=refresh)
    except UltraKError:
        return detect(install_ffmpeg())


def cmd_doctor(args):
    hw = _hw(args, refresh=True)
    print(hw.summary())
    print(f"ffmpeg: {hw.ffmpeg}")
    print(f"ffprobe: {hw.ffprobe or 'MANQUANT'}")
    needed = ["nlmeans", "atadenoise", "hqdn3d", "unsharp", "cas", "deband", "guided", "minterpolate"]
    have = hw.filter(*needed)
    print("filtres: " + ", ".join(f"{n}{'' if n in have else ' (absent)'}" for n in needed))
    print("encode GPU verifies: " + (", ".join(hw.gpu_encoders) or
                                     "aucun -> rendu 100% CPU (libx264)"))
    if hw.rejected_encoders:
        print(f"encodeurs refuses au test reel: {', '.join(hw.rejected_encoders)} "
              f"(compiles dans ffmpeg mais non supportes par ton materiel)")
    print(f"libx264: {'ok' if hw.has_encoder('libx264') else 'ABSENT'} | "
          f"libx265: {'ok' if hw.has_encoder('libx265') else 'absent'} | "
          f"libvmaf: {'ok' if hw.has_encoder('libvmaf') or 'libvmaf' in hw.filters else 'absent'}")
    if hw.ram_free_gb < 3:
        print("attention: moins de 3 Go de RAM libre -> le denoise lourd (nlmeans) est "
              "automatiquement ecarte pour les grosses sources")
    return 0


def cmd_install(args):
    path = install_ffmpeg(force=args.force)
    print(f"ffmpeg pret: {path}")
    detect(path)
    return 0


def cmd_analyze(args):
    hw = _hw(args)
    for path in _expand(args.files):
        src = analyze_source(path, hw.ffmpeg, hw, content=not args.no_content)
        if args.json:
            print(json.dumps({k: v for k, v in vars(src).items()}, default=str))
            continue
        print(f"\n{os.path.basename(path)}")
        print(f"  {src.label} | {human_duration(src.duration)} | {human_bytes(src.file_bytes)} | "
              f"{src.codec} {src.pix_fmt}")
        print(f"  bitrate {src.effective_bit_rate // 1000} kbps = {src.bpp:.3f} bpp | "
              f"audio {'oui (' + src.audio_codec + ')' if src.has_audio else 'non'}")
        if src.detail_score >= 0:
            print(f"  contenu: structure {src.detail_score:.2f}/1, hf non conservees "
                  f"{src.noise_score:.2f}/1, "
                  f"mouvement {src.motion_score:.2f}/1 (hf {src.hf_native:.2f} -> "
                  f"{src.hf_struct:.2f} apres round-trip)")
        state = prepare(path, hw.ffmpeg, hw, mode=args.mode, use_preview=False,
                        out_dir=args.out or os.path.dirname(path), budget_minutes=args.budget)
        job = state.job
        print(f"  plan UltraK: {job.geometry} ({job.upscale_factor():.2f}x) "
              f"cap honnete {job.honest_cap}p, codec {job.plan.codec}, preset {job.plan.preset}")
        for decision in job.decisions:
            print(f"    - {decision.reason}" + (f" -- {decision.detail}" if decision.detail else ""))
        print(f"  video: {job.plan.vf()}")
        print(f"  sortie: {job.dst}")
        print(f"  ETA estime: {human_duration(job.eta_for(total_frames(job.src, job.plan)))}"
              f" (preview -- pour confirmer)")
    return 0


def cmd_upscale(args):
    hw = _hw(args)
    files = _expand(args.files)
    cfg = load_config()
    out_dir = args.out or cfg.get("last_out_dir") or ""
    states = []
    for index, path in enumerate(files, 1):
        if len(files) > 1:
            print(f"[{index}/{len(files)}] {os.path.basename(path)}")
        state = prepare(path, hw.ffmpeg, hw, mode=args.mode or cfg["mode"],
                        codec=args.codec, preset=args.preset or cfg["preset"],
                        crf=args.crf if args.crf is not None else cfg["crf"],
                        fps=args.fps, smooth=args.smooth, denoise=args.denoise,
                        sharpen=args.sharpen, grade=args.grade or cfg["grade"],
                        audio=args.audio or cfg["audio"], two_step=not args.no_two_step,
                        max_bpp=cfg["max_bpp"], budget_minutes=args.budget,
                        use_preview=not args.no_preview and not args.dry_run,
                        preview_seconds=args.preview_seconds,
                        out_dir=out_dir or os.path.dirname(os.path.abspath(path)),
                        dry_run=args.dry_run, log=print)
        states.append(state)
        if args.dry_run:
            print(f"  (dry-run) commande: {hw.ffmpeg} "
                  + " ".join(state.job.plan.build(state.job.src.path, state.job.dst)))

    if args.dry_run:
        return 0
    if not args.yes and not _confirm(states, hw):
        print("annule")
        return 1

    cancel = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        run_batch(states, hw.ffmpeg, hw, workers=args.jobs, force=args.force,
                  on_progress=(lambda s, p: _bar(p)) if len(states) == 1 else None,
                  cancel=cancel, log=print)
    finally:
        signal.signal(signal.SIGINT, previous)
    if cancel.is_set():
        print("\ninterrompu - les fichiers deja termines restent valides, relance pour "
              "la suite (ils seront ignores)")
    print(summarize(states))
    if args.poster:
        for state in states:
            if state.status == "done":
                _make_poster(hw, state.job.src.path, state.job.dst, args.poster_at)
    return 0 if all(s.status in ("done", "skipped") for s in states) else 1


def _confirm(states, hw) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        return True
    total = sum(state.job.eta_for(total_frames(state.job.src, state.job.plan)) for state in states)
    if total < 120:
        return True
    print(f"\nTemps total estime: {human_duration(total)} ({hw.summary()})")
    try:
        answer = input("Lancer maintenant ? [O/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("", "o", "oui", "y")


def cmd_compare(args):
    hw = _hw(args)
    target = args.out or os.path.splitext(args.dist)[0] + "_compare"
    made = []
    if args.png != "no":
        made.append(poster(hw, args.src, args.dist, target + ".png", at=args.at))
    if args.zoom != "no":
        made.append(zoom(hw, args.src, args.dist, target + "_zoom.png", at=args.at,
                         panel=args.width, frac=args.frac))
    if args.gif:
        made.append(gif(hw, args.src, args.dist, target + ".gif", at=args.at,
                        seconds=args.seconds, width=args.width, frac=args.frac))
    for path in made:
        print(f"{path}  ({human_bytes(os.path.getsize(path))})")
    if args.metrics:
        print("fidelite vs source:", fidelity(hw, args.src, args.dist, at=args.at))
    return 0


def cmd_bench(args):
    hw = _hw(args)
    run_bench(hw, seconds=args.seconds, target_height=args.height, quick=args.quick, log=print)
    return 0


def cmd_gui(args):
    from .gui import launch
    explicit = getattr(args, "ffmpeg", None) or os.environ.get("ULTRAK_FFMPEG")
    files = [f for f in getattr(args, "files", []) if os.path.isfile(f)]
    launch(detect(explicit) if explicit else None, files)
    return 0


def _make_poster(hw, src, dist, at):
    try:
        path = poster(hw, src, dist, os.path.splitext(dist)[0] + "_compare.png", at=at or 0)
        print(f"poster: {path}")
    except UltraKError as exc:
        print(f"poster impossible: {exc}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ultrak",
        description=f"UltraK {__version__} - transforme des videos moyennes en 4K "
                    "exploitable, localement, sans promettre des pixels qui n'existent pas")
    parser.add_argument("--ffmpeg", help="chemin de ffmpeg.exe")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="etat de la machine et des capacites ffmpeg")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("install-ffmpeg", help="telecharger ffmpeg (~100 Mo, une fois)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("analyze", help="mesurer une source et proposer un plan sans rien encoder")
    p.add_argument("files", nargs="+")
    p.add_argument("--mode", choices=MODES, default="auto")
    p.add_argument("--out")
    p.add_argument("--budget", type=float, default=0.0, help="limite de temps en minutes")
    p.add_argument("--no-content", action="store_true", help="sans mesure de detail/mouvement")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("upscale", help="rendre la ou les videos")
    p.add_argument("files", nargs="+")
    p.add_argument("--mode", choices=MODES, default=None)
    p.add_argument("--out", help="dossier de sortie")
    p.add_argument("--codec", default="auto",
                   help="auto|libx264|libx265|hevc_nvenc|hevc_qsv|hevc_amf|h264_nvenc")
    p.add_argument("--preset", default=None)
    p.add_argument("--crf", type=int, default=None)
    p.add_argument("--fps", type=float, default=0.0, help="ex: 50 ou 60 (doublage par copie d'images)")
    p.add_argument("--smooth", action="store_true",
                   help="interpolation de mouvement (tres couteux sur CPU)")
    p.add_argument("--denoise", default="auto", choices=["auto", "none", "light", "medium", "strong"])
    p.add_argument("--sharpen", default="auto", choices=["auto", "none", "soft", "pro", "strong"])
    p.add_argument("--grade", default=None, choices=["none", "natural", "punch"])
    p.add_argument("--audio", default=None, choices=["copy", "aac", "none"])
    p.add_argument("--no-two-step", action="store_true")
    p.add_argument("--budget", type=float, default=0.0, help="temps max en minutes, le plan s'y adapte")
    p.add_argument("--no-preview", action="store_true", help="sans test de 4 s (ETA estime seulement)")
    p.add_argument("--preview-seconds", type=float, default=4.0)
    p.add_argument("--jobs", type=int, default=0, help="rendus en parallele (0 = auto)")
    p.add_argument("--force", action="store_true", help="re-encoder meme si la sortie existe")
    p.add_argument("--yes", "-y", action="store_true", help="sans demander confirmation")
    p.add_argument("--dry-run", action="store_true", help="afficher le plan, ne rien encoder")
    p.add_argument("--poster", action="store_true", help="generer l'image avant/apres apres le rendu")
    p.add_argument("--poster-at", type=float, default=0.0)
    p.set_defaults(func=cmd_upscale)

    p = sub.add_parser("compare", help="images/GIF avant-apres + fidelite")
    p.add_argument("src")
    p.add_argument("dist")
    p.add_argument("--at", type=float, default=0.0)
    p.add_argument("--out")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--frac", type=float, default=0.22, help="taille de la zone zoomee (0-1)")
    p.add_argument("--seconds", type=float, default=4.0)
    p.add_argument("--gif", action="store_true")
    p.add_argument("--png", default="yes")
    p.add_argument("--zoom", default="yes")
    p.add_argument("--metrics", action="store_true")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("bench", help="etalonner cette machine (met a jour l'estimation d'ETA)")
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--height", type=int, default=2160)
    p.add_argument("--quick", action="store_true", help="2 pipelines seulement")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("gui", help="ouvrir la fenetre UltraK")
    p.add_argument("files", nargs="*", help="videos deja preloades (glisser-deposer sur Launch.bat)")
    p.set_defaults(func=cmd_gui)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\ninterrompu", file=sys.stderr)
        return 130
    except UltraKError as exc:
        print(f"\nerreur: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"\nerreur inattendue: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("reessaie avec ULTRAK_DEBUG=1 pour voir la trace complete", file=sys.stderr)
        if os.environ.get("ULTRAK_DEBUG"):
            raise
        return 3


if __name__ == "__main__":
    sys.exit(main())
